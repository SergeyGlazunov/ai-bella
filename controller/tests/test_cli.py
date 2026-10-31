from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from contour.cli import main
from contour.db import init_db, set_meta
from contour.lock import Lock, MemoryBackend

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"


def _write_cfg(tmp_path: Path, *, remote: str | None = None, **schedule) -> Path:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    if remote is not None:
        raw["databases"]["ubd"]["source"]["remote"] = remote
    raw["schedule"].update(schedule)
    raw["alert"]["command"] = [sys.executable]
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    raw["runtime"]["root"] = str(root)
    raw["runtime"]["db_path"] = str(tmp_path / "state.db")
    raw["runtime"]["lock_path"] = str(tmp_path / "sync.lock")
    raw["runtime"]["logs_path"] = str(tmp_path / "logs")
    raw["runtime"]["backups_path"] = str(tmp_path / "backups")
    path = tmp_path / "contour.yaml"
    path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _argv(cfg_path: Path, db_path: Path, lock_path: Path, *command: str) -> list[str]:
    return [
        "--config",
        str(cfg_path),
        "--db",
        str(db_path),
        "--lock",
        str(lock_path),
        *command,
    ]


def test_validate_ok(tmp_path: Path, capsys):
    cfg = _write_cfg(tmp_path)
    db = tmp_path / "state.db"
    lock = tmp_path / "sync.lock"
    rc = main(_argv(cfg, db, lock, "validate"))
    assert rc == 0


def test_validate_accepts_config_after_subcommand(tmp_path: Path):
    """R01: bootstrap/INSTALL used `contour validate --config ...`."""
    cfg = _write_cfg(tmp_path)
    rc = main(
        [
            "validate",
            "--config",
            str(cfg),
            "--db",
            str(tmp_path / "db"),
            "--lock",
            str(tmp_path / "lock"),
        ]
    )
    assert rc == 0


def test_validate_rejects_kb(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "features: {graph: true, code_index: true, kb: true}\n"
        "alert: {command: [/bin/true]}\n",
        encoding="utf-8",
    )
    rc = main(
        ["--config", str(p), "--db", str(tmp_path / "db"), "--lock", str(tmp_path / "lock"), "validate"]
    )
    assert rc != 0


def test_validate_fails_if_alert_command_missing(tmp_path: Path):
    cfg = _write_cfg(tmp_path)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["alert"]["command"] = [str(tmp_path / "missing-alert-bin")]
    cfg.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    rc = main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "validate"))
    assert rc != 0


def test_cli_open_invokes_reopen(tmp_path: Path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_reopen(cfg, **kwargs):
        seen["called"] = True
        seen["force"] = kwargs.get("force")
        return "success"

    monkeypatch.setattr("contour.cli.reopen_reads", fake_reopen)
    cfg = _write_cfg(tmp_path)
    rc = main(
        _argv(cfg, tmp_path / "db", tmp_path / "lock", "open", "--database", "ubd")
    )
    assert rc == 0
    assert seen["called"] is True
    assert seen["force"] is False


def test_cli_open_force_flag(tmp_path: Path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_reopen(cfg, **kwargs):
        seen["force"] = kwargs.get("force")
        return "success"

    monkeypatch.setattr("contour.cli.reopen_reads", fake_reopen)
    cfg = _write_cfg(tmp_path)
    rc = main(
        _argv(
            cfg,
            tmp_path / "db",
            tmp_path / "lock",
            "open",
            "--database",
            "ubd",
            "--force",
        )
    )
    assert rc == 0
    assert seen["force"] is True


def test_update_without_measure_is_not_measure(tmp_path: Path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_reconcile(cfg, *, timer, measure, **kwargs):
        seen["timer"] = timer
        seen["measure"] = measure
        seen["force_window"] = kwargs.get("force_window")
        return "deferred"

    monkeypatch.setattr("contour.cli.reconcile", fake_reconcile)
    cfg = _write_cfg(tmp_path, build_deadline_seconds=None)
    rc = main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "update"))
    assert rc == 0
    assert seen["timer"] is False
    assert seen["measure"] is False
    assert seen["force_window"] is False


def test_update_measure_flag(tmp_path: Path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_reconcile(cfg, *, timer, measure, **kwargs):
        seen["measure"] = measure
        seen["timer"] = timer
        return "success"

    monkeypatch.setattr("contour.cli.reconcile", fake_reconcile)
    cfg = _write_cfg(tmp_path, build_deadline_seconds=None)
    rc = main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "update", "--measure"))
    assert rc == 0
    assert seen["measure"] is True
    assert seen["timer"] is False


def test_update_force_window_flag(tmp_path: Path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_reconcile(cfg, *, timer, measure, **kwargs):
        seen["force_window"] = kwargs.get("force_window")
        seen["timer"] = timer
        return "success"

    monkeypatch.setattr("contour.cli.reconcile", fake_reconcile)
    cfg = _write_cfg(tmp_path, build_deadline_seconds=None)
    rc = main(
        _argv(cfg, tmp_path / "db", tmp_path / "lock", "update", "--force-window")
    )
    assert rc == 0
    assert seen["force_window"] is True
    assert seen["timer"] is False


def test_measure_only_on_update(tmp_path: Path):
    cfg = _write_cfg(tmp_path)
    with pytest.raises(SystemExit):
        main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "validate", "--measure"))
    with pytest.raises(SystemExit):
        main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "status", "--measure"))
    with pytest.raises(SystemExit):
        main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "cleanup", "--measure"))
    with pytest.raises(SystemExit):
        main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "open", "--measure"))


def test_status_strips_remote_userinfo(tmp_path: Path, capsys):
    token_remote = "https://oauth2:supersecret@host.example/r"
    cfg = _write_cfg(tmp_path, remote=token_remote)
    db = tmp_path / "state.db"
    init_db(str(db))
    rc = main(_argv(cfg, db, tmp_path / "lock", "status"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "supersecret" not in out
    assert "oauth2" not in out
    assert "host.example/r" in out or "host.example" in out


def test_cleanup_dry_run_lists_no_rm(tmp_path: Path, capsys, monkeypatch):
    db = tmp_path / "state.db"
    conn = init_db(str(db))
    try:
        conn.execute(
            "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
            ("abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id)
            VALUES (1, 'graph', 'ok', 'cid-keep-me')
            """
        )
        conn.commit()
    finally:
        conn.close()

    calls: list[list[str]] = []

    @dataclass
    class SpyDocker:
        def run(self, cmd: list[str], *, timeout: float | None = None):
            calls.append(list(cmd))
            if cmd[:2] == ["docker", "inspect"]:
                name = cmd[-1]
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"/{name} exited\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_acquire(path, backend=None):
        return Lock(path, MemoryBackend())

    monkeypatch.setattr("contour.cli._docker", lambda: SpyDocker())
    monkeypatch.setattr("contour.cli.acquire", fake_acquire)
    cfg = _write_cfg(tmp_path)
    rc = main(_argv(cfg, db, tmp_path / "lock", "cleanup", "--dry-run"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "cid-keep-me" in out
    assert not any(c[:2] == ["docker", "rm"] for c in calls)
    assert not any("--rm" in c for c in calls)


def test_cleanup_removes_recorded_containers(tmp_path: Path, monkeypatch):
    db = tmp_path / "state.db"
    conn = init_db(str(db))
    try:
        conn.execute(
            "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
            ("abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id)
            VALUES (1, 'graph', 'ok', 'cid-job-1')
            """
        )
        conn.commit()
    finally:
        conn.close()

    calls: list[list[str]] = []

    @dataclass
    class SpyDocker:
        def run(self, cmd: list[str], *, timeout: float | None = None):
            calls.append(list(cmd))
            if cmd[:2] == ["docker", "inspect"]:
                name = cmd[-1]
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"/{name} exited\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_acquire(path, backend=None):
        return Lock(path, MemoryBackend())

    monkeypatch.setattr("contour.cli._docker", lambda: SpyDocker())
    monkeypatch.setattr("contour.cli.acquire", fake_acquire)
    cfg = _write_cfg(tmp_path)
    rc = main(_argv(cfg, db, tmp_path / "lock", "cleanup"))
    assert rc == 0
    assert calls
    assert any(c[:2] == ["docker", "rm"] and "cid-job-1" in c for c in calls)
    for cmd in calls:
        assert "--rm" not in cmd
        if cmd[:2] == ["docker", "rm"]:
            assert "-f" not in cmd
            assert "--force" not in cmd
        assert cmd[:3] != ["docker", "compose", "run"]


def test_update_does_not_inject_probes(tmp_path: Path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_reconcile(cfg, *, timer, measure, **kwargs):
        seen["probes"] = kwargs.get("probes", None)
        seen["has_probes_kw"] = "probes" in kwargs
        return "failed"

    monkeypatch.setattr("contour.cli.reconcile", fake_reconcile)
    cfg = _write_cfg(tmp_path, build_deadline_seconds=None)
    rc = main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "update", "--measure"))
    assert rc != 0
    assert seen["probes"] is None
    assert seen["has_probes_kw"] is False


def test_validate_reports_all_database_profiles(tmp_path: Path, capsys):
    cfg = _write_cfg(tmp_path)
    rc = main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "validate"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "database: ubd" in out
    assert "database: kb" in out
    assert "database: klon_opl" in out


def test_status_database_filter_shows_layers(tmp_path: Path, capsys):
    cfg = _write_cfg(
        tmp_path,
        remote="https://user:supersecret@example.invalid/ubd.git",
    )
    db = tmp_path / "state.db"
    conn = init_db(str(db))
    try:
        from contour.state import StateStore

        state = StateStore(conn)
        state.register_database("kb", "package")
        state.mark_layer("kb", "code_index", "r1", "ready", "vol|1", "img")
        state.mark_layer("kb", "graph", "r1", "ready", "vol|g", "img")
        state.mark_published("kb", "r1")
    finally:
        conn.close()
    rc = main(_argv(cfg, db, tmp_path / "lock", "status", "--database", "kb"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "database: kb" in out
    assert "code_index:" in out
    assert "graph:" in out
    assert "published=r1" in out
    assert "supersecret" not in out

    rc_ubd = main(_argv(cfg, db, tmp_path / "lock", "status", "--database", "ubd"))
    assert rc_ubd == 0
    out_ubd = capsys.readouterr().out
    assert "git_remote: https://example.invalid/ubd.git" in out_ubd
    assert "supersecret" not in out_ubd
    assert "user:supersecret" not in out_ubd


def test_status_unknown_database_fails(tmp_path: Path):
    cfg = _write_cfg(tmp_path)
    rc = main(
        _argv(cfg, tmp_path / "db", tmp_path / "lock", "status", "--database", "nope")
    )
    assert rc != 0


def test_update_unknown_database_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("contour.cli.reconcile", lambda *a, **k: "success")
    cfg = _write_cfg(tmp_path)
    rc = main(
        _argv(cfg, tmp_path / "db", tmp_path / "lock", "update", "--database", "nope")
    )
    assert rc != 0


def test_update_database_filter_is_passed(tmp_path: Path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_reconcile(cfg, **kwargs):
        seen["database"] = kwargs.get("database")
        return "success"

    monkeypatch.setattr("contour.cli.reconcile", fake_reconcile)
    cfg = _write_cfg(tmp_path)
    rc = main(
        _argv(cfg, tmp_path / "db", tmp_path / "lock", "update", "--database", "kb")
    )
    assert rc == 0
    assert seen["database"] == "kb"


def test_intake_validates_without_reconcile(tmp_path: Path, monkeypatch):
    from test_packages import _make_package

    cfg_path = _write_cfg(tmp_path)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    inbox = tmp_path / "inbox" / "kb"
    inbox.mkdir(parents=True)
    raw["databases"]["kb"]["source"]["inbox"] = str(inbox)
    raw["databases"]["kb"]["source"]["source_path"] = str(tmp_path / "sources" / "kb")
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    _make_package(inbox, name="ready-kb1", database="kb", export_id="kb1")

    called = {"reconcile": False}
    monkeypatch.setattr(
        "contour.cli.reconcile",
        lambda *a, **k: called.__setitem__("reconcile", True) or "success",
    )
    rc = main(_argv(cfg_path, tmp_path / "db", tmp_path / "lock", "intake", "--database", "kb"))
    assert rc == 0
    assert called["reconcile"] is False
    accepted = list((tmp_path / "root" / "accepted" / "kb").glob("ready-*"))
    assert len(accepted) == 1


def test_intake_rejects_git_database(tmp_path: Path):
    cfg = _write_cfg(tmp_path)
    rc = main(
        _argv(cfg, tmp_path / "db", tmp_path / "lock", "intake", "--database", "ubd")
    )
    assert rc != 0


def test_backup_state_uses_sqlite_api(tmp_path: Path):
    cfg = _write_cfg(tmp_path)
    db = tmp_path / "state.db"
    init_db(str(db))
    rc = main(_argv(cfg, db, tmp_path / "lock", "backup-state"))
    assert rc == 0
    backups = list((tmp_path / "backups").glob("state-*.db"))
    assert len(backups) == 1
    # Ensure backup is a readable SQLite DB, not a byte-identical live lock race.
    conn = init_db(str(backups[0]))
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_missing_state_backup_does_not_create_state_or_prune(tmp_path: Path):
    from types import SimpleNamespace

    import contour.cli as cli

    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "state-20000101T000000Z.db"
    conn = __import__("sqlite3").connect(old)
    conn.execute("create table recovery_evidence (value text)")
    conn.commit()
    conn.close()
    source = tmp_path / "missing.db"
    cfg = SimpleNamespace(runtime=SimpleNamespace(backups_path=str(backups)))
    rc = cli._backup_state(cfg, SimpleNamespace(db_path=str(source)))
    assert rc != 0
    assert not source.exists()
    assert old.exists()


def test_backup_preserves_actual_state(tmp_path: Path):
    from types import SimpleNamespace

    import contour.cli as cli

    source = tmp_path / "state.db"
    conn = init_db(str(source))
    conn.execute("insert or replace into meta(key,value) values('review','saved')")
    conn.commit()
    conn.close()
    backups = tmp_path / "backups"
    cfg = SimpleNamespace(runtime=SimpleNamespace(backups_path=str(backups)))
    assert cli._backup_state(cfg, SimpleNamespace(db_path=str(source))) == 0
    with __import__("sqlite3").connect(next(backups.glob("state-*.db"))) as bconn:
        assert bconn.execute(
            "select value from meta where key='review'"
        ).fetchone() == ("saved",)


def test_status_can_inspect_disabled_database(tmp_path: Path, capsys):
    cfg = _write_cfg(tmp_path)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["databases"]["kb"]["enabled"] = False
    cfg.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    rc = main(_argv(cfg, tmp_path / "state.db", tmp_path / "lock", "status", "--database", "kb"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "database: kb" in out
    assert "enabled: false" in out


def test_update_still_rejects_disabled_database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("contour.cli.reconcile", lambda *a, **k: "success")
    cfg = _write_cfg(tmp_path)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["databases"]["kb"]["enabled"] = False
    cfg.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    rc = main(
        _argv(cfg, tmp_path / "db", tmp_path / "lock", "update", "--database", "kb")
    )
    assert rc != 0


def _seed_running_job(db: Path, *, job_id: int = 1, container_id: str = "cid-stuck",
                       exec_id: str = "") -> None:
    conn = init_db(str(db))
    try:
        conn.execute(
            "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
            ("abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, stage, status, container_id, exec_id)
            VALUES (1, 'graph', 'running', ?, ?)
            """,
            (container_id, exec_id),
        )
        conn.commit()
    finally:
        conn.close()


class _InspectDocker:
    """docker inspect fake returning a fixed container status."""

    def __init__(self, status: str) -> None:
        self.status = status

    def run(self, cmd: list[str], *, timeout: float | None = None):
        assert cmd[:2] == ["docker", "inspect"]
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.status}\n", stderr="")


class _ExecDocker:
    """Runner exposing inspect_exec for the exec path."""

    def __init__(self, payload: dict | Exception) -> None:
        self.payload = payload

    def inspect_exec(self, exec_id: str) -> dict:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_jobs_fail_marks_unfinished_failed_when_writer_exited(
    tmp_path: Path, monkeypatch
):
    cfg = _write_cfg(tmp_path)
    db = tmp_path / "state.db"
    lock = tmp_path / "sync.lock"
    _seed_running_job(db)

    monkeypatch.setattr("contour.cli.acquire", _memory_lock)
    monkeypatch.setattr("contour.cli._docker", lambda: _InspectDocker("exited"))

    rc = main(_argv(cfg, db, lock, "jobs", "fail", "1"))
    assert rc == 0
    conn = init_db(str(db))
    try:
        row = conn.execute(
            "SELECT status, exit_code, finished_at FROM jobs WHERE id = 1"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["exit_code"] is None
        assert row["finished_at"] is not None
    finally:
        conn.close()

    # Already finished → refuse; unknown id → refuse.
    assert main(_argv(cfg, db, lock, "jobs", "fail", "1")) != 0
    assert main(_argv(cfg, db, lock, "jobs", "fail", "99")) != 0


def test_jobs_fail_refuses_live_container_writer(tmp_path: Path, monkeypatch):
    cfg = _write_cfg(tmp_path)
    db = tmp_path / "state.db"
    _seed_running_job(db, container_id="live-writer")
    monkeypatch.setattr("contour.cli.acquire", _memory_lock)
    monkeypatch.setattr("contour.cli._docker", lambda: _InspectDocker("running"))

    rc = main(_argv(cfg, db, tmp_path / "lock", "jobs", "fail", "1"))
    assert rc != 0
    conn = init_db(str(db))
    try:
        assert conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()[
            "status"
        ] == "running"
    finally:
        conn.close()


def test_jobs_fail_refuses_unknown_writer_state(tmp_path: Path, monkeypatch):
    class BrokenDocker:
        def run(self, cmd: list[str], *, timeout: float | None = None):
            raise OSError("docker unreachable")

    cfg = _write_cfg(tmp_path)
    db = tmp_path / "state.db"
    _seed_running_job(db)
    monkeypatch.setattr("contour.cli.acquire", _memory_lock)
    monkeypatch.setattr("contour.cli._docker", lambda: BrokenDocker())

    assert main(_argv(cfg, db, tmp_path / "lock", "jobs", "fail", "1")) != 0


def test_jobs_fail_refuses_running_exec_and_allows_gone_exec(
    tmp_path: Path, monkeypatch
):
    from contour.dockerutil import ExecRecordGone

    cfg = _write_cfg(tmp_path)
    db = tmp_path / "state.db"
    lock = tmp_path / "sync.lock"

    _seed_running_job(db, container_id="bsl-indexer", exec_id="exec-1")
    monkeypatch.setattr("contour.cli.acquire", _memory_lock)
    monkeypatch.setattr(
        "contour.cli._docker", lambda: _ExecDocker({"Running": True, "ExitCode": None})
    )
    assert main(_argv(cfg, db, lock, "jobs", "fail", "1")) != 0

    monkeypatch.setattr(
        "contour.cli._docker", lambda: _ExecDocker(ExecRecordGone("gone"))
    )
    assert main(_argv(cfg, db, lock, "jobs", "fail", "1")) == 0
    conn = init_db(str(db))
    try:
        assert conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()[
            "status"
        ] == "failed"
    finally:
        conn.close()


def test_jobs_fail_force_requires_reason_and_records_it(
    tmp_path: Path, monkeypatch
):
    cfg = _write_cfg(tmp_path)
    db = tmp_path / "state.db"
    lock = tmp_path / "sync.lock"
    _seed_running_job(db, container_id="live-writer")
    monkeypatch.setattr("contour.cli.acquire", _memory_lock)
    monkeypatch.setattr("contour.cli._docker", lambda: _InspectDocker("running"))

    # --force without --reason is refused.
    assert main(_argv(cfg, db, lock, "jobs", "fail", "1", "--force")) != 0
    # Explicit override with a recorded reason succeeds despite the live check.
    rc = main(
        _argv(
            cfg, db, lock, "jobs", "fail", "1", "--force",
            "--reason", "writer verified gone manually by operator",
        )
    )
    assert rc == 0
    conn = init_db(str(db))
    try:
        assert conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()[
            "status"
        ] == "failed"
        note = conn.execute(
            "SELECT value FROM meta WHERE key = 'jobs_fail_override:1'"
        ).fetchone()
        assert note is not None
        assert "verified gone manually" in note["value"]
    finally:
        conn.close()


def _memory_lock(path, backend=None):
    return Lock(path, MemoryBackend())


def test_status_shows_exec_record_gone_note(tmp_path: Path, capsys):
    cfg = _write_cfg(tmp_path)
    db = tmp_path / "state.db"
    conn = init_db(str(db))
    try:
        set_meta(conn, "last_exec_record_gone", "2026-10-16 job=1 exec=e1")
    finally:
        conn.close()
    rc = main(_argv(cfg, db, tmp_path / "lock", "status"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "last_exec_record_gone: 2026-10-16 job=1 exec=e1" in out


def test_status_shows_alert_delivery_error(tmp_path: Path, capsys):
    cfg = _write_cfg(tmp_path)
    db = tmp_path / "state.db"
    conn = init_db(str(db))
    try:
        set_meta(conn, "alert_delivery_last_error", "2026-10-16 alert exit 1")
    finally:
        conn.close()
    rc = main(_argv(cfg, db, tmp_path / "lock", "status"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "alert_delivery_last_error: 2026-10-16 alert exit 1" in out


def test_config_load_error_prints_reason(tmp_path: Path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schedule: [not, a, mapping]\n", encoding="utf-8")
    rc = main(["--config", str(bad), "status"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "config error" in captured.err
