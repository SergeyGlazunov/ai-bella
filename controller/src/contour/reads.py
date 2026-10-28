from __future__ import annotations

import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from contour.config import DatabaseConfig
from contour.db import get_meta, set_meta
from contour.remount_bsl import (
    DEFAULT_MIN_BSL_FILES,
    DEFAULT_MIN_INDEX_FILES,
    assert_bsl_mount_healthy,
    assert_code_index_nonempty,
    published_workspace,
)
from contour.sources import published_path_for
from contour.watch_bsl import pause_watch, restore_watch, watch_is_paused

# Host DROP on bsl_publish_port is optional (ADM-SAPHX-TECH: no host/LAN MCP
# clients; updates off-hours). Required close = graph stop + WebUI network cut.
_FW_ERRORS = (subprocess.CalledProcessError, FileNotFoundError, OSError)

_LAYER_ORDER = ("code_index", "graph")


@runtime_checkable
class ReadsDocker(Protocol):
    def run(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]: ...

    def idle_probe(self) -> bool: ...


def scoped_read_key(flag: str, database: str, layer: str) -> str:
    """Per-database / per-layer read-state meta key (v2)."""
    return f"{flag}:{database}:{layer}"


def request_close(db) -> None:
    set_meta(db, "reads_close_requested", "1")


def apply_close(cfg, docker) -> None:
    if _inspect_status(cfg, docker) == "running":
        docker.run(["docker", "stop", cfg.services.graph_mcp])
    _try_firewall_drop(cfg, docker)
    if cfg.services.webui_network in _network_names(cfg, docker):
        docker.run(
            [
                "docker",
                "network",
                "disconnect",
                cfg.services.webui_network,
                cfg.services.bsl_container,
            ]
        )


def open_reads(cfg, docker) -> None:
    if _inspect_status(cfg, docker) != "running":
        docker.run(["docker", "start", cfg.services.graph_mcp])
    if cfg.services.webui_network not in _network_names(cfg, docker):
        docker.run(
            [
                "docker",
                "network",
                "connect",
                cfg.services.webui_network,
                cfg.services.bsl_container,
            ]
        )
    _try_firewall_undrop(cfg, docker)


def _inspect_status(cfg, docker) -> str:
    result = docker.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Status}}",
            cfg.services.graph_mcp,
        ]
    )
    return result.stdout.strip()


def _network_names(cfg, docker) -> str:
    result = docker.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}\n{{end}}",
            cfg.services.bsl_container,
        ]
    )
    return result.stdout


def _firewall_blocked(cfg, docker) -> bool:
    port = str(cfg.services.bsl_publish_port)
    try:
        result = docker.run(
            ["iptables", "-C", "INPUT", "-p", "tcp", "--dport", port, "-j", "DROP"]
        )
        return result.returncode == 0
    except _FW_ERRORS:
        return False


def _try_firewall_drop(cfg, docker) -> None:
    """Best-effort host DROP; not required for verify_closed."""
    if _firewall_blocked(cfg, docker):
        return
    port = str(cfg.services.bsl_publish_port)
    try:
        docker.run(
            ["iptables", "-I", "INPUT", "-p", "tcp", "--dport", port, "-j", "DROP"]
        )
    except _FW_ERRORS:
        return


def _try_firewall_undrop(cfg, docker) -> None:
    """Best-effort remove DROP if present (cleanup after optional apply)."""
    port = str(cfg.services.bsl_publish_port)
    while _firewall_blocked(cfg, docker):
        try:
            docker.run(
                ["iptables", "-D", "INPUT", "-p", "tcp", "--dport", port, "-j", "DROP"]
            )
        except _FW_ERRORS:
            return


def verify_closed(cfg, docker) -> bool:
    """Closed iff graph MCP down and BSL off WebUI network. Firewall optional."""
    if _inspect_status(cfg, docker) == "running":
        return False
    if cfg.services.webui_network in _network_names(cfg, docker):
        return False
    return True


def wait_server_idle(
    cfg,
    docker,
    *,
    attempts: int = 30,
    interval: float = 1.0,
    required_layers: Sequence[str] | None = None,
) -> None:
    """Poll graph inspect + IdleProbe until idle, or raise. Not sleep-N-as-drain."""
    if attempts < 1:
        attempts = 1
    layers = (
        frozenset(required_layers)
        if required_layers is not None
        else frozenset({"code_index", "graph"})
    )
    need_graph = "graph" in layers
    need_code = "code_index" in layers
    last_status = ""
    last_idle = False
    for i in range(attempts):
        last_status = _inspect_status(cfg, docker) if need_graph else "exited"
        last_idle = bool(docker.idle_probe()) if need_code else True
        graph_idle = (not need_graph) or last_status != "running"
        code_idle = (not need_code) or last_idle
        if graph_idle and code_idle:
            return
        if i + 1 < attempts and interval > 0:
            time.sleep(interval)
    if need_graph and last_status == "running":
        raise RuntimeError(f"graph mcp still running ({cfg.services.graph_mcp})")
    if need_code and not last_idle:
        raise RuntimeError("bsl idle probe busy")
    raise RuntimeError("wait_server_idle exceeded attempts")


class DatabaseReadController:
    """Per-database ReadController for ``run_database_pipeline``.

    Layer-scoped meta keys: ``reads_close_requested:<db>:<layer>`` and
    ``reads_closed:<db>:<layer>``. Legacy global flags are not written for
    non-UBD profiles. OPL (no graph in required_layers) never stops Graph MCP.
    """

    def __init__(
        self,
        database: DatabaseConfig,
        conn: sqlite3.Connection,
        docker: ReadsDocker,
        *,
        watch_reload: Callable[[], None] | None = None,
        active_writer: bool = False,
        index_mutated: bool = False,
        open_gate_src: Path | None = None,
        min_bsl_files: int = DEFAULT_MIN_BSL_FILES,
        min_index_files: int = DEFAULT_MIN_INDEX_FILES,
    ) -> None:
        self.database = database
        self.conn = conn
        self.docker = docker
        self.watch_reload = watch_reload or (lambda: None)
        self.active_writer = active_writer
        self.index_mutated = index_mutated
        self.open_gate_src = open_gate_src
        self.min_bsl_files = min_bsl_files
        self.min_index_files = min_index_files
        self.required_layers = tuple(
            layer for layer in _LAYER_ORDER if layer in database.required_layers
        )
        self.close_attempted = False
        self._watch_backup: str | None = None
        # Migrate legacy flags before snapshotting entry state so a prior
        # global reads_closed=1 is not mistaken for "was open".
        self._migrate_legacy_ubd_flags()
        self._closed_on_entry = {
            layer: self._layer_flag_closed(layer) for layer in self.required_layers
        }

    def _code_open_src(self) -> Path:
        if self.open_gate_src is not None:
            return Path(self.open_gate_src)
        return published_workspace(
            self.database, published_path_for(self.database)
        )

    def _assert_code_open_gates(self) -> None:
        """Refuse to restore watcher / expose BSL MCP on a wiped or stale index."""
        src = self._code_open_src()
        assert_bsl_mount_healthy(
            self.docker,
            self.database.services.bsl_container,
            published_src=src,
            min_bsl_files=self.min_bsl_files,
        )
        assert_code_index_nonempty(src, min_files=self.min_index_files)

    def _migrate_legacy_ubd_flags(self) -> None:
        """Copy legacy UBD global closed flag into per-layer keys once."""
        if self.database.name != "ubd":
            return
        if get_meta(self.conn, "reads_closed") != "1":
            return
        for layer in self.required_layers:
            key = scoped_read_key("reads_closed", "ubd", layer)
            if get_meta(self.conn, key) != "1":
                set_meta(self.conn, key, "1")

    def _layer_flag_closed(self, layer: str) -> bool:
        key = scoped_read_key("reads_closed", self.database.name, layer)
        return get_meta(self.conn, key) == "1"

    def _clear_legacy_ubd_if_open(self) -> None:
        if self.database.name != "ubd":
            return
        if any(self._layer_flag_closed(layer) for layer in self.required_layers):
            return
        set_meta(self.conn, "reads_closed", "")
        set_meta(self.conn, "reads_close_requested", "")

    def is_closed(self, layer: str | None = None) -> bool:
        layers = (layer,) if layer is not None else self.required_layers
        if not layers:
            return True
        return all(self._layer_flag_closed(item) for item in layers)

    def close(self, layer: str | None = None) -> None:
        layers = (layer,) if layer is not None else self.required_layers
        self.close_attempted = True
        errors: list[str] = []
        for item in layers:
            try:
                self.close_layer(item)
            except Exception as exc:
                errors.append(f"{item}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def open(self, layer: str | None = None) -> None:
        layers = (layer,) if layer is not None else self.required_layers
        errors: list[str] = []
        for item in layers:
            try:
                self.open_layer(item)
            except Exception as exc:
                errors.append(f"{item}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def close_layer(self, layer: str) -> None:
        if layer not in self.required_layers:
            raise ValueError(
                f"layer {layer!r} is not required for {self.database.name}"
            )
        name = self.database.name
        services = self.database.services
        set_meta(self.conn, scoped_read_key("reads_close_requested", name, layer), "1")
        if layer == "graph":
            status = self._graph_status()
            if status == "running":
                self.docker.run(["docker", "stop", services.graph_mcp])
        elif layer == "code_index":
            if not bool(self.docker.idle_probe()):
                raise RuntimeError(
                    f"bsl idle probe busy for {name}; refusing close/write"
                )
            self._pause_watcher()
            self._try_firewall_drop()
            if services.webui_network in self._network_names():
                self.docker.run(
                    [
                        "docker",
                        "network",
                        "disconnect",
                        services.webui_network,
                        services.bsl_container,
                    ]
                )
        else:
            raise ValueError(f"unknown layer: {layer}")
        if not self._layer_verify_closed(layer):
            raise RuntimeError(f"verify closed failed for {name}/{layer}")
        set_meta(self.conn, scoped_read_key("reads_closed", name, layer), "1")

    def open_layer(self, layer: str) -> None:
        if layer not in self.required_layers:
            raise ValueError(
                f"layer {layer!r} is not required for {self.database.name}"
            )
        name = self.database.name
        services = self.database.services
        if layer == "graph":
            if self._graph_status() != "running":
                self.docker.run(["docker", "start", services.graph_mcp])
        elif layer == "code_index":
            if self.active_writer:
                raise RuntimeError(
                    f"refusing to restore watcher for {name}: active writer"
                )
            self._assert_code_open_gates()
            if services.webui_network not in self._network_names():
                self.docker.run(
                    [
                        "docker",
                        "network",
                        "connect",
                        services.webui_network,
                        services.bsl_container,
                    ]
                )
            self._try_firewall_undrop()
            self._restore_watcher()
        else:
            raise ValueError(f"unknown layer: {layer}")
        set_meta(self.conn, scoped_read_key("reads_closed", name, layer), "")
        set_meta(self.conn, scoped_read_key("reads_close_requested", name, layer), "")
        self._clear_legacy_ubd_if_open()

    def reopen_if_safe_after_pre_activation_failure(self) -> None:
        """Restore reads this attempt may have partially closed (pre-activation)."""
        if self.index_mutated:
            return
        if not self.close_attempted:
            return
        for layer in self.required_layers:
            if self._closed_on_entry.get(layer):
                continue
            self.open_layer(layer)

    def _layer_verify_closed(self, layer: str) -> bool:
        services = self.database.services
        if layer == "graph":
            return self._graph_status() != "running"
        if layer == "code_index":
            return services.webui_network not in self._network_names()
        return False

    def _graph_status(self) -> str:
        result = self.docker.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}",
                self.database.services.graph_mcp,
            ]
        )
        return (result.stdout or "").strip()

    def _network_names(self) -> str:
        result = self.docker.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}\n{{end}}",
                self.database.services.bsl_container,
            ]
        )
        return result.stdout or ""

    def _try_firewall_drop(self) -> None:
        port = str(self.database.services.bsl_publish_port)
        try:
            check = self.docker.run(
                ["iptables", "-C", "INPUT", "-p", "tcp", "--dport", port, "-j", "DROP"]
            )
            if check.returncode == 0:
                return
            self.docker.run(
                ["iptables", "-I", "INPUT", "-p", "tcp", "--dport", port, "-j", "DROP"]
            )
        except _FW_ERRORS:
            return

    def _try_firewall_undrop(self) -> None:
        port = str(self.database.services.bsl_publish_port)
        while True:
            try:
                check = self.docker.run(
                    [
                        "iptables",
                        "-C",
                        "INPUT",
                        "-p",
                        "tcp",
                        "--dport",
                        port,
                        "-j",
                        "DROP",
                    ]
                )
                if check.returncode != 0:
                    return
                self.docker.run(
                    [
                        "iptables",
                        "-D",
                        "INPUT",
                        "-p",
                        "tcp",
                        "--dport",
                        port,
                        "-j",
                        "DROP",
                    ]
                )
            except _FW_ERRORS:
                return

    def _pause_watcher(self) -> None:
        toml_path = self.database.services.bsl_daemon_toml
        path = Path(toml_path)
        if not path.is_file():
            raise RuntimeError(
                f"daemon.toml missing for {self.database.name}: {toml_path}"
            )
        backup = toml_path + ".bak"
        if not Path(backup).is_file():
            shutil.copy2(path, backup)
        self._watch_backup = backup
        if not watch_is_paused(lambda: path.read_text(encoding="utf-8")):
            pause_watch(toml_path, self.watch_reload)

    def _restore_watcher(self) -> None:
        if self.active_writer:
            raise RuntimeError(
                f"refusing watcher restore for {self.database.name}: active writer"
            )
        toml_path = self.database.services.bsl_daemon_toml
        backup = self._watch_backup or (toml_path + ".bak")
        path = Path(toml_path)
        if not path.is_file() or not Path(backup).is_file():
            return
        if watch_is_paused(lambda: path.read_text(encoding="utf-8")):
            restore_watch(toml_path, backup, self.watch_reload)
