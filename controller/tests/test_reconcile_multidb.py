"""Task 8 multi-database reconcile (v2). Legacy UBD tests stay in test_reconcile.py."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from contour.config import load_config
from contour.db import init_db
from contour.lock import MemoryBackend
from contour.reconcile import RuntimePaths, aggregate_database_results, reconcile
from contour.state import StateStore

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"


@dataclass
class PingDocker:
    """Satisfies dockerutil.ping without talking to a live engine."""

    def run(self, cmd: list[str], *, timeout: float | None = None):
        del timeout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _v2_config(tmp_path: Path, *, disable: frozenset[str] = frozenset()) -> object:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["runtime"]["root"] = str(tmp_path / "root")
    raw["runtime"]["db_path"] = str(tmp_path / "state.db")
    raw["runtime"]["lock_path"] = str(tmp_path / "sync.lock")
    raw["runtime"]["logs_path"] = str(tmp_path / "logs")
    raw["runtime"]["backups_path"] = str(tmp_path / "backups")
    for name in disable:
        raw["databases"][name]["enabled"] = False
    # Point UBD git at a temp path so accidental real work cannot hit live repos.
    ubd_repo = tmp_path / "ubd-src"
    ubd_repo.mkdir()
    raw["databases"]["ubd"]["source"]["path"] = str(ubd_repo)
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(str(cfg_path))


@dataclass
class RecordingRunner:
    outcomes: dict[str, str] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)
    default: str = "success"

    def __call__(self, database, *, cfg, conn, state, **kwargs) -> str:
        del cfg, conn, state, kwargs
        self.seen.append(database.name)
        return self.outcomes.get(database.name, self.default)


def test_v2_preserves_config_order(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    runner = RecordingRunner()
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "success"
    assert runner.seen == ["ubd", "kb", "klon_opl"]


def test_disabled_database_is_skipped(tmp_path: Path):
    cfg = _v2_config(tmp_path, disable=frozenset({"kb"}))
    runner = RecordingRunner()
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "success"
    assert runner.seen == ["ubd", "klon_opl"]
    assert "kb" not in runner.seen


def test_manual_database_filter(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    runner = RecordingRunner()
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database="kb",
        database_runner=runner,
    )
    assert result == "success"
    assert runner.seen == ["kb"]


def test_aggregate_failed_beats_later_success():
    assert (
        aggregate_database_results(
            [("kb", "failed"), ("klon_opl", "success"), ("ubd", "success")]
        )
        == "failed"
    )
    assert (
        aggregate_database_results([("ubd", "deferred"), ("kb", "success")])
        == "deferred"
    )
    assert aggregate_database_results([("ubd", "success")]) == "success"


def test_v2_failed_kb_does_not_mask_aggregate(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    runner = RecordingRunner(
        outcomes={"ubd": "success", "kb": "failed", "klon_opl": "success"}
    )
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "failed"
    assert runner.seen == ["ubd", "kb", "klon_opl"]


def test_v2_runtime_paths_prefer_cfg_runtime_when_cli_defaults_absent(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    runner = RecordingRunner()
    # No paths= — must use cfg.runtime.db_path / lock_path, not /srv/ai defaults.
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "success"
    assert Path(cfg.runtime.db_path).is_file()


def test_v2_external_skip_records_run_without_pipeline_double(tmp_path: Path):
    """Pre-pipeline skip (e.g. deferred) creates one finished run owned by the loop."""
    cfg = _v2_config(tmp_path, disable=frozenset({"kb", "klon_opl"}))

    def skip_runner(database, *, cfg, conn, state: StateStore, **kwargs) -> str:
        del cfg, kwargs
        run_id = state.start_run(database.name, "(pending)")
        state.finish_run(run_id, "deferred", "no new package")
        return "deferred"

    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=skip_runner,
    )
    assert result == "deferred"
    conn = init_db(str(tmp_path / "state.db"))
    rows = conn.execute(
        "SELECT database, result, message FROM runs WHERE database='ubd'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["result"] == "deferred"
    assert "no new package" in (rows[0]["message"] or "")


def test_legacy_reconcile_contract_unchanged(tmp_path: Path):
    """schema_version=1 keeps the historical UBD-only reconcile path."""
    from test_reconcile import FakeDocker, FakeGit, _make_config, _now, _opts

    cfg = _make_config(tmp_path)
    assert cfg.schema_version == 1
    git = FakeGit()
    docker = FakeDocker()
    opts, _alerts, _, _ = _opts(tmp_path, cfg, git, docker)
    result = reconcile(
        cfg, timer=False, measure=False, now=_now(12, 0), **opts
    )
    assert result == "deferred"
    assert git.writes == []
    assert "fetch" in git.calls
