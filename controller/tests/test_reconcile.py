from __future__ import annotations

import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json

import yaml

from contour.alert import send as alert_send
from contour.config import Config, load_config
from contour.db import get_meta, init_db, set_meta
from contour.lock import MemoryBackend, acquire
from contour.reconcile import (
    RuntimePaths,
    WriterStateUnknown,
    cleanup_jobs,
    reap_jobs,
    reconcile,
    reopen_reads,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"
LIVE_REPO = Path(r"C:\MCP\ubd_config")
LIVE_DAEMON = Path(r"C:\MCP\code_index\deploy\daemon.toml")
VLAD = ZoneInfo("Asia/Vladivostok")


def _now(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 20, hour, minute, tzinfo=VLAD)


def _make_config(
    tmp_path: Path, drop_adapters: bool = False, **schedule_overrides: object
) -> Config:
    """Build schema_version=1 UBD config for legacy reconcile contract tests.

    Multi-db v2 coverage lives in test_reconcile_multidb.py. Task 8 routes v2
    through a separate loop; these tests must stay on the historical path.
    """
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    ubd = raw["databases"]["ubd"]
    toml_path = tmp_path / "daemon.toml"
    toml_path.write_text('[[paths]]\npath = "/repos/SRC"\n', encoding="utf-8")
    assert toml_path.resolve() != LIVE_DAEMON.resolve()
    services = dict(ubd["services"])
    services["bsl_daemon_toml"] = str(toml_path)
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text(
        "services:\n  metadata-indexer:\n    image: alpine\n",
        encoding="utf-8",
    )
    services["graph_compose_dir"] = str(compose_dir)
    repo = tmp_path / "ubd"
    repo.mkdir()
    assert repo.resolve() != LIVE_REPO.resolve()
    schedule = dict(raw["schedule"])
    schedule.update(schedule_overrides)
    adapters = None if drop_adapters else ubd.get("adapters")
    legacy = {
        "schema_version": 1,
        "repo": {
            "path": str(repo),
            "remote": ubd["source"]["remote"],
            "branch": ubd["source"]["branch"],
            "workspace_subdir": ubd["source"].get("workspace_subdir") or "src",
        },
        "schedule": schedule,
        "alert": raw["alert"],
        "services": services,
        "storage": ubd["storage"],
        "features": {"graph": True, "code_index": True, "mdd": False},
    }
    if adapters is not None:
        legacy["adapters"] = adapters
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(legacy, sort_keys=False), encoding="utf-8")
    return load_config(str(cfg_path))


@dataclass
class FakeGit:
    """Injectable git: records fetch vs write (ff-only). Never touches live UBD."""

    head: str = "aaa000"
    target: str = "bbb111"
    dirty: bool = False
    fail_ff: bool = False
    name_only: list[str] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    diffs: list[tuple[str, str]] = field(default_factory=list)

    def fetch(self, repo) -> None:
        self.calls.append("fetch")

    def is_dirty(self, repo) -> bool:
        self.calls.append("is_dirty")
        return self.dirty

    def ff_only(self, repo, branch: str) -> str:
        self.calls.append("ff_only")
        if self.fail_ff:
            raise RuntimeError("ff failed")
        self.writes.append("ff_only")
        self.head = self.target
        return self.head

    def rev_parse(self, repo, ref: str) -> str:
        self.calls.append(f"rev_parse:{ref}")
        if ref in ("HEAD", "head"):
            return self.head
        return self.target

    def diff_name_only(self, repo, old_sha: str, new_sha: str) -> list[str]:
        self.diffs.append((old_sha, new_sha))
        return list(self.name_only)

    def diff_deletions(self, repo, old_sha: str, new_sha: str) -> list[str]:
        return list(self.deletions)


@dataclass
class FakeDocker:
    """Injectable engine: ping, reads close/open, idle_probe. Never talks to live Docker."""

    db_path: str | None = None
    ping_hang: bool = False
    probe_idle: bool = True
    graph_status: str = "running"
    connected_networks: set[str] = field(default_factory=lambda: {"ai-stack_default"})
    firewall_blocked_ports: set[int] = field(default_factory=set)
    calls: list[list[str]] = field(default_factory=list)
    meta_at_first_mutation: str | None = None
    removed: list[str] = field(default_factory=list)
    exec_ids: list[str] = field(default_factory=list)
    exec_exit: dict[str, int] = field(default_factory=dict)
    container_states: dict[str, str] = field(default_factory=dict)
    _exec_i: int = 0

    def idle_probe(self) -> bool:
        return self.probe_idle

    def create_exec(
        self, container: str, cmd: list[str], env: list[str] | None = None
    ) -> str:
        self.calls.append(["create_exec", container, *(env or []), *cmd])
        self._exec_i += 1
        exec_id = f"exec-{self._exec_i}"
        self.exec_exit[exec_id] = 0
        return exec_id

    def start_exec(self, exec_id: str, detach: bool = True) -> None:
        self.calls.append(["start_exec", exec_id, str(detach)])
        self.exec_ids.append(exec_id)

    def wait_exec(self, container: str, exec_id: str) -> int:
        if exec_id in self.exec_ids:
            self.exec_ids.remove(exec_id)
        return self.exec_exit.get(exec_id, 0)

    def inspect_exec(self, exec_id: str) -> dict:
        return {"Running": exec_id in self.exec_ids, "ExitCode": self.exec_exit.get(exec_id, 0)}

    def _record_meta_if_mutation(self, cmd: list[str]) -> None:
        if self.db_path is None or self.meta_at_first_mutation is not None:
            return
        mutating = (
            cmd[:2] == ["docker", "stop"]
            or cmd[:3] == ["docker", "network", "disconnect"]
            or cmd[:3] == ["docker", "network", "connect"]
            or cmd[:2] == ["docker", "start"]
            or (cmd and cmd[0] == "iptables")
        )
        if mutating:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = ?",
                    ("reads_close_requested",),
                ).fetchone()
                self.meta_at_first_mutation = "" if row is None else str(row["value"])
            finally:
                conn.close()

    def run(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if cmd[:2] == ["docker", "info"]:
            if self.ping_hang:
                raise subprocess.TimeoutExpired(cmd, timeout or 0)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        self._record_meta_if_mutation(cmd)

        if cmd[:2] == ["docker", "stop"] and len(cmd) >= 3:
            if cmd[2] == "bsl-indexer":
                raise AssertionError("must not docker stop the BSL container")
            self.graph_status = "exited"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "start"] and len(cmd) >= 3:
            self.graph_status = "running"
            return subprocess.CompletedProcess(cmd, 0, stdout=cmd[2], stderr="")
        if cmd[:3] == ["docker", "network", "disconnect"] and len(cmd) >= 5:
            self.connected_networks.discard(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["docker", "network", "connect"] and len(cmd) >= 5:
            self.connected_networks.add(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "volume"] and "inspect" in cmd:
            volume = cmd[-1]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{volume}|2026-01-01T00:00:00Z\n", stderr=""
            )
        if cmd[:2] == ["docker", "inspect"]:
            fmt = ""
            if "-f" in cmd:
                fmt = cmd[cmd.index("-f") + 1]
            name = cmd[-1]
            if "ExecIDs" in fmt:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(self.exec_ids) + "\n", stderr=""
                )
            if "Network" in fmt:
                nets = "\n".join(sorted(self.connected_networks))
                return subprocess.CompletedProcess(cmd, 0, stdout=nets + "\n", stderr="")
            if "Image" in fmt:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="sha256:testimg\n", stderr=""
                )
            if "Name" in fmt:
                status = self.container_states.get(name, "exited")
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"/{name} {status}\n", stderr=""
                )
            if "Status" in fmt or name == "mcp-metadata-graph":
                if name in self.container_states:
                    status = self.container_states[name]
                elif name == "mcp-metadata-graph":
                    status = self.graph_status
                else:
                    status = "exited"
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=status + "\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=f"cid-{name}\n", stderr="")
        if cmd[:2] == ["docker", "exec"]:
            if "-d" in cmd:
                self._exec_i += 1
                exec_id = f"exec-{self._exec_i}"
                self.exec_ids.append(exec_id)
                self.exec_exit[exec_id] = 0
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd and cmd[0] == "iptables":
            port = None
            if "--dport" in cmd:
                port = int(cmd[cmd.index("--dport") + 1])
            action = None
            if "-I" in cmd or "-A" in cmd:
                action = "add"
            elif "-D" in cmd:
                action = "delete"
            elif "-C" in cmd:
                action = "check"
            if port is not None and action == "add":
                self.firewall_blocked_ports.add(port)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if port is not None and action == "delete":
                self.firewall_blocked_ports.discard(port)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if port is not None and action == "check":
                code = 0 if port in self.firewall_blocked_ports else 1
                return subprocess.CompletedProcess(cmd, code, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "rm"]:
            self.removed.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "wait"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0", stderr="")
        if cmd[:2] == ["docker", "compose"] and "create" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "compose"] and "ps" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="cid-job\n", stderr="")
        if cmd[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "daemon" in cmd and "stop" in cmd:
            raise AssertionError("must not daemon stop")
        raise AssertionError(f"unexpected command: {cmd}")


@dataclass
class FakeProbe:
    status: str = "ready"
    fail_with: Exception | None = None
    bsl_checked: list[str] = field(default_factory=list)
    xml_checked: list[str] = field(default_factory=list)
    deletions_checked: list[str] = field(default_factory=list)

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def check_bsl(self, path: str) -> None:
        self.bsl_checked.append(path)
        self._maybe_fail()

    def check_xml(self, path: str) -> None:
        self.xml_checked.append(path)
        self._maybe_fail()

    def check_deletion(self, path: str) -> None:
        self.deletions_checked.append(path)
        self._maybe_fail()


@dataclass
class FakeProbes:
    code_index: FakeProbe = field(default_factory=FakeProbe)
    graph: FakeProbe = field(default_factory=FakeProbe)


def _seed_components(db_path: str, applied: str, cfg: Config | None = None) -> None:
    conn = init_db(db_path)
    try:
        volumes = {
            "code_index": (
                cfg.storage.code_index_volume
                if cfg is not None
                else "bsl-indexer_bsl-indexer-home"
            ),
            "graph": (
                cfg.storage.graph_volume if cfg is not None else "mcp_for_1c_neo4j-data"
            ),
        }
        for name in ("code_index", "graph"):
            conn.execute(
                """
                INSERT INTO components (
                  name, status, applied_commit, storage_id, indexer_version,
                  container_id, exec_id, detail, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    "ready",
                    applied,
                    f"{volumes[name]}|2026-01-01T00:00:00Z",
                    "sha256:testimg",
                    None,
                    None,
                    None,
                    "2026-09-20T00:00:00Z",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _opts(tmp_path: Path, cfg: Config, git: FakeGit, docker: FakeDocker, **extra):
    db_path = str(tmp_path / "state.db")
    lock_path = str(tmp_path / "sync.lock")
    docker.db_path = db_path
    alerts: list[str] = extra.pop("alerts", [])
    backend = extra.pop("lock_backend", MemoryBackend())
    applied = extra.pop("applied_commit", git.head)
    _seed_components(db_path, applied, cfg)
    return {
        "paths": RuntimePaths(db_path=db_path, lock_path=lock_path),
        "lock_backend": backend,
        "docker": docker,
        "git": git,
        "alert_send": lambda _cfg, message: alerts.append(message),
        "watch_reload": lambda: None,
        "probes": FakeProbes(),
        **extra,
    }, alerts, backend, db_path


def test_outside_window_deferred_no_git_write(tmp_path: Path):
    cfg = _make_config(tmp_path)
    git = FakeGit()
    docker = FakeDocker()
    opts, alerts, _, _ = _opts(tmp_path, cfg, git, docker)
    result = reconcile(
        cfg, timer=False, measure=False, now=_now(12, 0), **opts
    )
    assert result == "deferred"
    assert "ff_only" not in git.writes
    assert git.writes == []
    assert "fetch" in git.calls


def test_timer_null_warn_in_window_writes(tmp_path: Path):
    cfg = _make_config(
        tmp_path, duration_warn_seconds=None, build_deadline_seconds=None
    )
    git = FakeGit()
    docker = FakeDocker()
    opts, alerts, _, _ = _opts(tmp_path, cfg, git, docker)
    result = reconcile(
        cfg, timer=True, measure=False, now=_now(2, 0), **opts
    )
    assert result in {"success", "failed"}
    assert git.writes == ["ff_only"]
    assert not any("deadline" in msg.lower() and "blocked" in msg.lower() for msg in alerts)


def test_measure_null_deadline_in_window_git_write(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    git = FakeGit()
    docker = FakeDocker()
    opts, _, _, _ = _opts(tmp_path, cfg, git, docker)
    result = reconcile(
        cfg, timer=False, measure=True, now=_now(2, 0), **opts
    )
    assert result in {"success", "failed", "interrupted"}
    assert git.writes == ["ff_only"]


def test_second_lock_no_write(tmp_path: Path):
    cfg = _make_config(tmp_path)
    git = FakeGit()
    docker = FakeDocker()
    opts, _, backend, _ = _opts(tmp_path, cfg, git, docker)
    held = acquire(opts["paths"].lock_path, backend=backend)
    assert held is not None
    try:
        result = reconcile(
            cfg, timer=False, measure=True, now=_now(2, 0), **opts
        )
        assert result != "success"
        assert git.writes == []
        assert "fetch" not in git.calls
        assert not any(c[:2] == ["docker", "stop"] for c in docker.calls)
    finally:
        held.__exit__(None, None, None)


def test_close_intent_before_apply(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    git = FakeGit()
    docker = FakeDocker()
    opts, _, _, db_path = _opts(tmp_path, cfg, git, docker)
    reconcile(cfg, timer=False, measure=True, now=_now(2, 0), **opts)
    assert docker.meta_at_first_mutation == "1"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert get_meta(conn, "reads_close_requested") in {"1", ""}
    finally:
        conn.close()


def test_dirty_tree_failed_does_not_close(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    git = FakeGit(dirty=True)
    docker = FakeDocker()
    opts, alerts, _, db_path = _opts(tmp_path, cfg, git, docker)
    result = reconcile(
        cfg, timer=False, measure=True, now=_now(2, 0), **opts
    )
    assert result == "failed"
    assert git.writes == []
    assert not any(c[:2] == ["docker", "stop"] for c in docker.calls)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert get_meta(conn, "reads_close_requested") == ""
        assert get_meta(conn, "reads_closed") == ""
    finally:
        conn.close()
    assert alerts


def test_hung_ping_failed_alerts(tmp_path: Path):
    cfg = _make_config(tmp_path)
    git = FakeGit()
    docker = FakeDocker(ping_hang=True)
    opts, alerts, _, _ = _opts(tmp_path, cfg, git, docker)
    result = reconcile(
        cfg, timer=False, measure=True, now=_now(2, 0), **opts
    )
    assert result == "failed"
    assert git.writes == []
    assert alerts


def _watch_backup(cfg: Config) -> Path:
    return Path(cfg.services.bsl_daemon_toml + ".bak")


def _assert_reads_still_closed(cfg: Config, docker: FakeDocker) -> None:
    graph_mcp = cfg.services.graph_mcp
    assert cfg.services.webui_network not in docker.connected_networks
    assert cfg.services.bsl_publish_port in docker.firewall_blocked_ports
    assert not any(
        c[:2] == ["docker", "start"] and graph_mcp in c for c in docker.calls
    )
    assert not any(c[:3] == ["docker", "network", "connect"] for c in docker.calls)
    assert not any(c and c[0] == "iptables" and "-D" in c for c in docker.calls)


def test_retry_after_ff_failure_does_not_clobber_watch_backup(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    toml = Path(cfg.services.bsl_daemon_toml)
    assert "/repos/SRC" in toml.read_text(encoding="utf-8")
    git = FakeGit(fail_ff=True)
    docker = FakeDocker()
    opts, _, _, _ = _opts(tmp_path, cfg, git, docker)
    bak = _watch_backup(cfg)

    result1 = reconcile(cfg, timer=False, measure=True, now=_now(2, 0), **opts)
    assert result1 == "failed"
    assert "/repos/SRC" not in toml.read_text(encoding="utf-8")
    assert bak.is_file()
    assert "/repos/SRC" in bak.read_text(encoding="utf-8")

    git.fail_ff = False
    result2 = reconcile(cfg, timer=False, measure=True, now=_now(2, 0), **opts)
    assert result2 == "success"
    assert "/repos/SRC" in bak.read_text(encoding="utf-8")
    assert "/repos/SRC" in toml.read_text(encoding="utf-8")


def test_skip_backup_overwrite_when_bak_already_holds_prepause(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    bak = _watch_backup(cfg)
    bak.write_text(
        '[[paths]]\npath = "/repos/SRC"\n# pre-pause-marker\n',
        encoding="utf-8",
    )
    git = FakeGit(fail_ff=True)
    docker = FakeDocker()
    opts, _, _, _ = _opts(tmp_path, cfg, git, docker)

    result = reconcile(cfg, timer=False, measure=True, now=_now(2, 0), **opts)
    assert result == "failed"
    assert "# pre-pause-marker" in bak.read_text(encoding="utf-8")
    assert "/repos/SRC" in bak.read_text(encoding="utf-8")


def test_empty_applied_commit_uses_tree_samples_not_forced_empty(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    repo = Path(cfg.repo.path)
    (repo / "src").mkdir()
    (repo / "src" / "Foo.bsl").write_text("// foo", encoding="utf-8")
    (repo / "src" / "Catalog.xml").write_text("<Meta/>", encoding="utf-8")
    git = FakeGit()
    docker = FakeDocker()
    probes = FakeProbes()
    opts, _, _, _ = _opts(
        tmp_path, cfg, git, docker, probes=probes, applied_commit=""
    )

    result = reconcile(cfg, timer=False, measure=True, now=_now(2, 0), **opts)

    assert result == "success"
    assert git.diffs == []
    assert any(path.endswith("Foo.bsl") for path in probes.code_index.bsl_checked)
    assert any(path.endswith("Catalog.xml") for path in probes.graph.xml_checked)


def test_missing_adapters_failed_does_not_close(tmp_path: Path):
    cfg = _make_config(tmp_path, drop_adapters=True, build_deadline_seconds=None)
    git = FakeGit()
    docker = FakeDocker()
    opts, alerts, _, db_path = _opts(tmp_path, cfg, git, docker, probes=None)

    result = reconcile(cfg, timer=False, measure=True, now=_now(2, 0), **opts)

    assert result == "failed"
    assert git.writes == []
    assert not any(c[:2] == ["docker", "stop"] for c in docker.calls)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert get_meta(conn, "reads_close_requested") == ""
        assert get_meta(conn, "reads_closed") == ""
    finally:
        conn.close()
    assert alerts


def test_injected_probes_that_raise_failed_no_open(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    git = FakeGit(name_only=["src/Foo.bsl"])
    docker = FakeDocker()
    probes = FakeProbes(
        code_index=FakeProbe(fail_with=RuntimeError("bsl probe boom")),
        graph=FakeProbe(),
    )
    opts, _, _, db_path = _opts(tmp_path, cfg, git, docker, probes=probes)

    result = reconcile(cfg, timer=False, measure=True, now=_now(2, 0), **opts)

    assert result == "failed"
    _assert_reads_still_closed(cfg, docker)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert get_meta(conn, "reads_closed") == "1"
        assert get_meta(conn, "reads_open_requested") in {"", None}
    finally:
        conn.close()


def test_alert_send_passes_message_on_stdin(tmp_path: Path):
    out = tmp_path / "alert.txt"
    script = (
        "import sys; "
        f"open({str(out)!r}, 'w', encoding='utf-8').write(sys.stdin.read())"
    )
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["alert"]["command"] = [sys.executable, "-c", script]
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(str(cfg_path))
    alert_send(cfg, "hello-stdin")
    assert out.read_text(encoding="utf-8") == "hello-stdin"
    assert "hello-stdin" not in cfg.alert.command


def test_alert_send_missing_binary_does_not_raise(tmp_path: Path):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["alert"]["command"] = [str(tmp_path / "no-such-contour-alert-bin")]
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(str(cfg_path))
    alert_send(cfg, "must-not-raise")


def test_reap_persists_exit_code(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = init_db(str(db_path))
    try:
        conn.execute(
            "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
            ("abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id)
            VALUES (1, 'graph', 'created', 'cid-reap-3')
            """
        )
        conn.commit()

        class WaitDocker:
            def run(self, cmd: list[str], *, timeout: float | None = None):
                if cmd[:2] == ["docker", "inspect"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="exited\n", stderr=""
                    )
                if cmd[:2] == ["docker", "wait"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="3\n", stderr="")
                if cmd[:2] == ["docker", "logs"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="log", stderr="")
                raise AssertionError(cmd)

        reap_jobs(conn, WaitDocker())
        row = conn.execute(
            "SELECT exit_code, status, finished_at, log_path FROM jobs WHERE id = 1"
        ).fetchone()
        assert row["exit_code"] == 3
        assert row["status"] == "failed"
        assert row["finished_at"]
        assert get_meta(conn, "graph_full_needed") == "1"
    finally:
        conn.close()


def test_reopen_clears_flags_only_after_verify(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    docker = FakeDocker()
    docker.graph_status = "exited"
    docker.connected_networks = set()
    docker.firewall_blocked_ports.add(cfg.services.bsl_publish_port)
    db_path = str(tmp_path / "state.db")
    lock_path = str(tmp_path / "sync.lock")
    _seed_components(db_path, "aaa000", cfg)
    conn = init_db(db_path)
    try:
        set_meta(conn, "reads_closed", "1")
        set_meta(conn, "reads_close_requested", "1")
    finally:
        conn.close()

    result = reopen_reads(
        cfg,
        paths=RuntimePaths(db_path=db_path, lock_path=lock_path),
        lock_backend=MemoryBackend(),
        docker=docker,
        watch_reload=lambda: None,
        alert_send=lambda _c, _m: None,
        git=FakeGit(head="aaa000"),
        probes=FakeProbes(),
    )
    assert result == "success"
    assert docker.graph_status == "running"
    assert cfg.services.webui_network in docker.connected_networks
    conn = init_db(db_path)
    try:
        assert get_meta(conn, "reads_closed") == ""
        assert get_meta(conn, "reads_close_requested") == ""
    finally:
        conn.close()


def test_reopen_keeps_flags_if_verify_fails(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    docker = FakeDocker()
    docker.graph_status = "exited"

    def broken_start(cmd: list[str], *, timeout: float | None = None):
        docker.calls.append(list(cmd))
        if cmd[:2] == ["docker", "start"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return FakeDocker.run(docker, cmd, timeout=timeout)

    docker.run = broken_start  # type: ignore[method-assign]
    db_path = str(tmp_path / "state.db")
    lock_path = str(tmp_path / "sync.lock")
    _seed_components(db_path, "aaa000", cfg)
    conn = init_db(db_path)
    try:
        set_meta(conn, "reads_closed", "1")
    finally:
        conn.close()

    result = reopen_reads(
        cfg,
        paths=RuntimePaths(db_path=db_path, lock_path=lock_path),
        lock_backend=MemoryBackend(),
        docker=docker,
        watch_reload=lambda: None,
        alert_send=lambda _c, _m: None,
        git=FakeGit(head="aaa000"),
        probes=FakeProbes(),
    )
    assert result == "failed"
    conn = init_db(db_path)
    try:
        assert get_meta(conn, "reads_closed") == "1"
    finally:
        conn.close()


def test_deferred_while_closed_alerts(tmp_path: Path):
    cfg = _make_config(tmp_path)
    git = FakeGit()
    docker = FakeDocker()
    opts, alerts, _, db_path = _opts(tmp_path, cfg, git, docker)
    conn = init_db(db_path)
    try:
        set_meta(conn, "reads_closed", "1")
    finally:
        conn.close()
    result = reconcile(cfg, timer=False, measure=False, now=_now(12, 0), **opts)
    assert result == "deferred"
    assert any("reads_closed" in msg for msg in alerts)


def test_busy_writer_does_not_start_new(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    git = FakeGit()
    docker = FakeDocker()
    docker.container_states["cid-busy"] = "running"
    opts, alerts, _, db_path = _opts(tmp_path, cfg, git, docker)
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
            ("abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id)
            VALUES (1, 'graph', 'running', 'cid-busy')
            """
        )
        conn.commit()
    finally:
        conn.close()
    result = reconcile(cfg, timer=False, measure=True, now=_now(2, 0), **opts)
    assert result == "deferred"
    assert git.writes == []
    assert any("still running" in msg for msg in alerts)


def test_unknown_writer_blocks_new_writer(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    git = FakeGit()
    docker = FakeDocker()
    opts, alerts, _, db_path = _opts(tmp_path, cfg, git, docker)

    class EmptyWait(FakeDocker):
        def run(self, cmd: list[str], *, timeout: float | None = None):
            if cmd[:2] == ["docker", "inspect"] and "Status" in (
                cmd[cmd.index("-f") + 1] if "-f" in cmd else ""
            ):
                return subprocess.CompletedProcess(cmd, 0, stdout="exited\n", stderr="")
            if cmd[:2] == ["docker", "wait"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return super().run(cmd, timeout=timeout)

    empty = EmptyWait()
    empty.db_path = docker.db_path
    opts["docker"] = empty
    conn = init_db(db_path)
    try:
        conn.execute(
            "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
            ("abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id)
            VALUES (1, 'graph', 'created', 'cid-unknown')
            """
        )
        conn.commit()
    finally:
        conn.close()
    result = reconcile(cfg, timer=False, measure=True, now=_now(2, 0), **opts)
    assert result == "failed"
    assert git.writes == []
    assert any("unknown" in msg.lower() for msg in alerts)


def test_needs_index_when_storage_stale_same_sha(tmp_path: Path):
    """F01: storage_id mismatch must require reindex even if applied_commit == target."""
    from contour.reconcile import _needs_index

    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    docker = FakeDocker()
    db_path = str(tmp_path / "state.db")
    conn = init_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO components (
              name, status, applied_commit, storage_id, indexer_version,
              container_id, exec_id, detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "code_index",
                "ready",
                "aaa000",
                f"{cfg.storage.code_index_volume}|OLD-CREATED",
                "sha256:testimg",
                None,
                None,
                None,
                "2026-09-20T00:00:00Z",
            ),
        )
        conn.commit()
        assert _needs_index(
            conn, "code_index", "aaa000", ["code_index"], cfg=cfg, docker=docker
        )
    finally:
        conn.close()


def test_reap_cancels_created_never_started(tmp_path: Path):
    """R04 edge: job status created + exec not running → cancel, not busy forever."""
    docker = FakeDocker()
    db_path = str(tmp_path / "state.db")
    conn = init_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO runs (id, target_commit, started_at, result, message)
            VALUES (1, 'aaa', '2026-09-20T00:00:00Z', NULL, NULL)
            """
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id, exec_id)
            VALUES (1, 'bsl', 'created', 'bsl-indexer', 'exec-never-started')
            """
        )
        conn.commit()
        assert reap_jobs(conn, docker) == "idle"
        row = conn.execute("SELECT status, exit_code FROM jobs WHERE id = 1").fetchone()
        assert row["status"] == "failed"
        assert row["exit_code"] == -1
    finally:
        conn.close()
    """R05: non-empty storage_id that does not match live volume is not ready."""
    from contour.reconcile import _all_ready

    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    docker = FakeDocker()
    db_path = str(tmp_path / "state.db")
    conn = init_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO components (
              name, status, applied_commit, storage_id, indexer_version,
              container_id, exec_id, detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "code_index",
                "ready",
                "aaa000",
                f"{cfg.storage.code_index_volume}|OLD-CREATED-AT",
                "sha256:testimg",
                None,
                None,
                None,
                "2026-09-20T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO components (
              name, status, applied_commit, storage_id, indexer_version,
              container_id, exec_id, detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "graph",
                "ready",
                "aaa000",
                f"{cfg.storage.graph_volume}|2026-01-01T00:00:00Z",
                "sha256:testimg",
                None,
                None,
                None,
                "2026-09-20T00:00:00Z",
            ),
        )
        conn.commit()
        assert (
            _all_ready(conn, cfg, docker, ["code_index", "graph"], "aaa000", FakeProbes())
            is False
        )
    finally:
        conn.close()


def test_reopen_refuses_mixed_indexes(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=None)
    docker = FakeDocker()
    db_path = str(tmp_path / "state.db")
    lock_path = str(tmp_path / "sync.lock")
    conn = init_db(db_path)
    try:
        set_meta(conn, "reads_closed", "1")
        vol_ci = cfg.storage.code_index_volume
        vol_g = cfg.storage.graph_volume
        conn.execute(
            """
            INSERT INTO components (
              name, status, applied_commit, storage_id, indexer_version,
              container_id, exec_id, detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "code_index",
                "ready",
                "aaa000",
                f"{vol_ci}|2026-01-01T00:00:00Z",
                "sha256:testimg",
                None,
                None,
                None,
                "2026-09-20T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO components (
              name, status, applied_commit, storage_id, indexer_version,
              container_id, exec_id, detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "graph",
                "ready",
                "bbb111",
                f"{vol_g}|2026-01-01T00:00:00Z",
                "sha256:testimg",
                None,
                None,
                None,
                "2026-09-20T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    result = reopen_reads(
        cfg,
        paths=RuntimePaths(db_path=db_path, lock_path=lock_path),
        lock_backend=MemoryBackend(),
        docker=docker,
        watch_reload=lambda: None,
        alert_send=lambda _c, _m: None,
        git=FakeGit(head="aaa000"),
        probes=FakeProbes(),
    )
    assert result == "failed"
    assert not any(c[:2] == ["docker", "start"] for c in docker.calls)
    conn = init_db(db_path)
    try:
        assert get_meta(conn, "reads_closed") == "1"
    finally:
        conn.close()


def test_cleanup_finished_only_without_force(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = init_db(str(db_path))
    docker = FakeDocker()
    try:
        conn.execute(
            "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
            ("abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id)
            VALUES (1, 'graph', 'ok', 'cid-done')
            """
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id)
            VALUES (1, 'graph', 'running', 'cid-live')
            """
        )
        conn.commit()
        removed = cleanup_jobs(conn, docker, dry_run=False)
        assert "cid-done" in removed
        assert "cid-live" not in removed
        rm_cmds = [c for c in docker.calls if c[:2] == ["docker", "rm"]]
        assert rm_cmds
        for cmd in rm_cmds:
            assert "-f" not in cmd
            assert "--force" not in cmd
    finally:
        conn.close()
