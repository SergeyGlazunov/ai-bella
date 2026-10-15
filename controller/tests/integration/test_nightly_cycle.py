"""Task 11: one complete nightly cycle for UBD, KB and klon_opl (fake Docker)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from contour.lock import MemoryBackend
from contour.reconcile import RuntimePaths, reconcile

from .conftest import load_v2_runtime_config, run_publish_cycle


@dataclass
class PingDocker:
    def run(self, cmd: list[str], *, timeout: float | None = None):
        del timeout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@dataclass
class RecordingDatabaseRunner:
    outcomes: dict[str, str] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)
    default: str = "success"

    def __call__(self, database, *, cfg, conn, state, **kwargs) -> str:
        del cfg, conn, state, kwargs
        self.seen.append(database.name)
        return self.outcomes.get(database.name, self.default)


def test_complete_cycle_publishes_ubd_kb_and_klon_opl(tmp_path: Path):
    """End-to-end publish path for all three schema-v2 profiles."""
    specs = (
        ("ubd", frozenset({"code_index", "graph"}), "git"),
        ("kb", frozenset({"code_index", "graph"}), "package"),
        ("klon_opl", frozenset({"code_index"}), "package"),
    )
    published: dict[str, str] = {}
    for name, layers, kind in specs:
        db_root = tmp_path / name
        db_root.mkdir()
        conn, state, _db, prepared, reads, probes, result = run_publish_cycle(
            db_root,
            name,
            layers=layers,
            revision=f"{name}-r1",
            kind=kind,
        )
        try:
            assert result.result == "published", (name, result.message)
            assert result.revision == f"{name}-r1"
            assert probes.ran is True
            assert reads.events == ["close", "open"]
            row = state.get_database(name)
            assert row["published_revision"] == f"{name}-r1"
            assert row["lifecycle_status"] == "ready"
            assert prepared.published_path.is_dir()
            published[name] = result.revision
            layers_rows = {
                r["layer"]: r["status"]
                for r in conn.execute(
                    "SELECT layer, status FROM layer_state WHERE database=?",
                    (name,),
                )
            }
            for layer in layers:
                assert layers_rows.get(layer) == "ready", (name, layers_rows)
        finally:
            conn.close()
    assert set(published) == {"ubd", "kb", "klon_opl"}


def test_reconcile_v2_runs_all_enabled_profiles_in_config_order(
    tmp_path: Path, in_window
):
    cfg, _ = load_v2_runtime_config(tmp_path)
    runner = RecordingDatabaseRunner()
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=in_window,
        paths=RuntimePaths(
            db_path=str(tmp_path / "state.db"),
            lock_path=str(tmp_path / "sync.lock"),
        ),
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "success"
    assert runner.seen == ["ubd", "kb", "klon_opl"]


def test_package_intake_then_pipeline_for_kb(tmp_path: Path):
    """Windows exporter handoff: validate/claim package, then publish via pipeline."""
    from test_packages import _make_package

    from contour.packages import claim_package, validate_package

    cfg, _ = load_v2_runtime_config(tmp_path)
    inbox = Path(cfg.databases["kb"].source.inbox)
    pkg = _make_package(inbox, name="ready-kb-night", database="kb", export_id="kb-night")
    validated = validate_package(pkg, frozenset({"kb"}))
    accepted = Path(cfg.runtime.root) / "accepted" / "kb"
    claimed = claim_package(validated, accepted)
    assert claimed.name.startswith("ready-")
    assert (claimed / "READY").is_file()

    conn, state, database, prepared, reads, probes, result = run_publish_cycle(
        tmp_path / "kb-pipe",
        "kb",
        layers=frozenset({"code_index", "graph"}),
        revision=validated.revision,
        kind="package",
    )
    try:
        assert result.result == "published"
        assert state.get_database("kb")["published_revision"] == validated.revision
        assert reads.events[-1] == "open"
        assert probes.ran is True
    finally:
        conn.close()
