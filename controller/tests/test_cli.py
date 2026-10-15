from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from contour.cli import main
from contour.db import init_db
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
        return "success"

    monkeypatch.setattr("contour.cli.reopen_reads", fake_reopen)
    cfg = _write_cfg(tmp_path)
    rc = main(
        _argv(cfg, tmp_path / "db", tmp_path / "lock", "open", "--database", "ubd")
    )
    assert rc == 0
    assert seen["called"] is True


def test_update_without_measure_is_not_measure(tmp_path: Path, monkeypatch):
    seen: dict[str, object] = {}

    def fake_reconcile(cfg, *, timer, measure, **kwargs):
        seen["timer"] = timer
        seen["measure"] = measure
        return "deferred"

    monkeypatch.setattr("contour.cli.reconcile", fake_reconcile)
    cfg = _write_cfg(tmp_path, build_deadline_seconds=None)
    rc = main(_argv(cfg, tmp_path / "db", tmp_path / "lock", "update"))
    assert rc == 0
    assert seen["timer"] is False
    assert seen["measure"] is False


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


def test_cli_update_missing_adapters_does_not_close(tmp_path: Path, monkeypatch):
    from test_reconcile import (
        FakeDocker,
        FakeGit,
        _make_config,
        _now,
        _opts,
    )

    cfg_obj = _make_config(tmp_path, drop_adapters=True, build_deadline_seconds=None)
    git = FakeGit()
    docker = FakeDocker()
    opts, _, _, db_path = _opts(tmp_path, cfg_obj, git, docker)

    rc = main(
        [
            "--config",
            str(tmp_path / "contour.yaml"),
            "--db",
            str(db_path),
            "--lock",
            str(opts["paths"].lock_path),
            "update",
        ]
    )
    assert rc != 0
    assert git.writes == []
    assert not any(c[:2] == ["docker", "stop"] for c in docker.calls)


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
