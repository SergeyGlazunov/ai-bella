from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
import json

import yaml

from contour.alert import send as alert_send
from contour.config import load_config
from contour.db import get_meta, init_db
from contour.dockerutil import ExecRecordGone
from contour.lock import MemoryBackend, acquire
from contour.reconcile import (
    RuntimePaths,
    cleanup_jobs,
    reap_jobs,
    reconcile,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"


def _make_config(
    tmp_path: Path, drop_adapters: bool = False, **schedule_overrides: object
) -> Config:
    """Minimal schema_version=1 fixture for helpers that only need schedule/services.

    Reconcile itself rejects schema_version < 2; remaining tests that call
    reconcile use lock contention (before the schema gate) or duration helpers.
    """
    del drop_adapters
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    ubd = raw["databases"]["ubd"]
    toml_path = tmp_path / "daemon.toml"
    toml_path.write_text('[[paths]]\npath = "/repos/SRC"\n', encoding="utf-8")
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
    schedule = dict(raw["schedule"])
    schedule.update(schedule_overrides)
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
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(legacy, sort_keys=False), encoding="utf-8")
    return load_config(str(cfg_path))


@dataclass
class FakeDocker:
    """Injectable engine for reap/cleanup. Never talks to live Docker."""

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
        return {
            "Running": exec_id in self.exec_ids,
            "ExitCode": self.exec_exit.get(exec_id, 0),
        }

    def run(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if cmd[:2] == ["docker", "info"]:
            if self.ping_hang:
                raise subprocess.TimeoutExpired(cmd, timeout or 0)
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
        if cmd[:2] == ["docker", "rm"]:
            self.removed.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "wait"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0", stderr="")
        if cmd[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")


def test_schema_v1_reconcile_rejected(tmp_path: Path):
    cfg = _make_config(tmp_path)
    assert cfg.schema_version == 1
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    try:
        reconcile(
            cfg,
            timer=False,
            measure=False,
            paths=paths,
            lock_backend=MemoryBackend(),
            docker=FakeDocker(),
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "only schema_version 2 is supported" in str(exc)


def test_second_lock_no_write(tmp_path: Path):
    cfg = _make_config(tmp_path)
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    backend = MemoryBackend()
    held = acquire(paths.lock_path, backend=backend)
    assert held is not None
    try:
        result = reconcile(
            cfg,
            timer=False,
            measure=True,
            paths=paths,
            lock_backend=backend,
            docker=FakeDocker(),
        )
        assert result == "deferred"
    finally:
        held.__exit__(None, None, None)


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


def test_alert_delivery_failure_visible_in_stderr_and_meta(tmp_path: Path, capsys):
    """Review 1.4: a broken alert channel must not be silent."""
    from contour.alert import _note_delivery
    from contour.reconcile import _persist_alert_delivery_note

    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["alert"]["command"] = [str(tmp_path / "no-such-contour-alert-bin")]
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(str(cfg_path))

    alert_send(cfg, "nightly summary")
    captured = capsys.readouterr()
    assert "alert delivery failed" in captured.err

    db_path = str(tmp_path / "state.db")
    conn = init_db(db_path)
    try:
        _note_delivery(cfg, "alert.command exit 3")
        _persist_alert_delivery_note(cfg, conn)
        note = get_meta(conn, "alert_delivery_last_error")
        assert "alert.command exit 3" in note
    finally:
        conn.close()


def test_duration_warn_uses_scoped_key_per_database(tmp_path: Path):
    """Review 1.5: v2 duration warn is scoped per database and warns once."""
    from contour.reconcile import _maybe_duration_warn

    cfg = _make_config(tmp_path, duration_warn_seconds=0)
    db_path = str(tmp_path / "state.db")
    conn = init_db(db_path)
    alerts: list[str] = []
    try:
        _maybe_duration_warn(
            cfg,
            conn,
            time.monotonic() - 5,
            lambda _c, m: alerts.append(m),
            warn_key="duration_warned:kb",
            database="kb",
        )
        _maybe_duration_warn(
            cfg,
            conn,
            time.monotonic() - 5,
            lambda _c, m: alerts.append(m),
            warn_key="duration_warned:kb",
            database="kb",
        )
        assert len(alerts) == 1
        assert alerts[0].startswith("kb: duration_warn_seconds exceeded")
        assert get_meta(conn, "duration_warned:kb") == "1"
    finally:
        conn.close()


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


def test_reap_marks_job_failed_when_exec_record_gone(tmp_path: Path):
    """A2/GC: vanished exec record resolves as failed, never 'unknown' forever."""
    db_path = tmp_path / "state.db"
    conn = init_db(str(db_path))
    try:
        conn.execute(
            "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
            ("abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id, exec_id)
            VALUES (1, 'bsl_index', 'running', 'bsl-indexer', 'exec-gone')
            """
        )
        conn.commit()

        class GoneDocker(FakeDocker):
            def wait_exec(self, container: str, exec_id: str) -> int:
                raise ExecRecordGone(f"exec record gone: {exec_id}")

        assert reap_jobs(conn, GoneDocker()) == "idle"
        row = conn.execute(
            "SELECT status, exit_code FROM jobs WHERE id = 1"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["exit_code"] is None
        note = get_meta(conn, "last_exec_record_gone")
        assert "exec-gone" in note
        assert "job=1" in note
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


def test_protected_container_names_cover_config_profiles(tmp_path: Path):
    """A6: v2 profile service containers must be protected regardless of name."""
    from contour.reconcile import protected_container_names

    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["runtime"] = {
        "root": str(tmp_path),
        "db_path": str(tmp_path / "state.db"),
        "lock_path": str(tmp_path / "sync.lock"),
        "logs_path": str(tmp_path / "logs"),
        "backups_path": str(tmp_path / "backups"),
    }
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(str(cfg_path))

    protected = protected_container_names(cfg)
    assert {"bsl-indexer", "bsl-kb", "bsl-klon-opl"} <= protected
    assert {"mcp-metadata-graph", "mcp-graph-kb"} <= protected
    assert {"open-webui", "omniroute"} <= protected


def test_cleanup_spares_config_protected_container(tmp_path: Path):
    """A stopped v2 BSL service container referenced by a job must stay."""
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
            VALUES (1, 'bsl_index', 'ok', 'bsl-kb')
            """
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id)
            VALUES (1, 'bsl_index', 'ok', 'cid-job-ok')
            """
        )
        conn.commit()
        removed = cleanup_jobs(
            conn,
            docker,
            dry_run=False,
            protected=frozenset({"bsl-kb", "bsl-indexer", "mcp-metadata-graph"}),
        )
        assert "cid-job-ok" in removed
        assert "bsl-kb" not in removed
        assert "bsl-kb" not in docker.removed
    finally:
        conn.close()
