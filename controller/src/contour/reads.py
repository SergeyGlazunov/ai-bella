from __future__ import annotations

import subprocess
import time
from typing import Protocol, runtime_checkable

from contour.db import set_meta

# Host DROP on bsl_publish_port is optional (ADM-SAPHX-TECH: no host/LAN MCP
# clients; updates off-hours). Required close = graph stop + WebUI network cut.
_FW_ERRORS = (subprocess.CalledProcessError, FileNotFoundError, OSError)


@runtime_checkable
class ReadsDocker(Protocol):
    def run(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]: ...

    def idle_probe(self) -> bool: ...


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
) -> None:
    """Poll graph inspect + IdleProbe until idle, or raise. Not sleep-N-as-drain."""
    if attempts < 1:
        attempts = 1
    last_status = ""
    last_idle = False
    for i in range(attempts):
        last_status = _inspect_status(cfg, docker)
        last_idle = bool(docker.idle_probe())
        if last_status != "running" and last_idle:
            return
        if i + 1 < attempts and interval > 0:
            time.sleep(interval)
    if last_status == "running":
        raise RuntimeError(f"graph mcp still running ({cfg.services.graph_mcp})")
    if not last_idle:
        raise RuntimeError("bsl idle probe busy")
    raise RuntimeError("wait_server_idle exceeded attempts")
