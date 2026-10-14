"""Operational regressions for Task 7 follow-up.

python -m pytest "../For Cursor/test_task7_review.py" -q
Run from controller/. No live Docker.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import contour.pipeline as pipeline
from contour.adapters import CommandProbe, CommandProbes
from contour.db import init_db
from contour.index_graph import set_graph_full_needed
from contour.pipeline import ProfilesLayerRunner, run_database_pipeline
from contour.profiles import LayerResult
from contour.sources import PreparedSource, compute_staged_tree_digest, write_revision
from contour.state import StateStore
from contour.verify import Samples

sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[2] / "tests"))
from test_index_graph import FakeDockerRunner
from test_pipeline import (
    FakeLayers,
    FakeProbes,
    FakeSource,
    RecordingReads,
    _prepared,
    _setup,
)


def _seed_ready_layers(conn, database: str, revision: str, layers: tuple[str, ...]) -> None:
    for layer in layers:
        conn.execute(
            """
            INSERT INTO layer_state (
              database, layer, status, applied_revision, storage_id,
              indexer_version, updated_at
            ) VALUES (?, ?, 'ready', ?, 'old-storage', 'old-image', 't')
            ON CONFLICT(database, layer) DO UPDATE SET
              status='ready', applied_revision=excluded.applied_revision
            """,
            (database, layer, revision),
        )
    conn.commit()


def test_retry_prepare_failure_keeps_prior_closed_reads(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    reads = RecordingReads()
    first = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        reads,
        FakeLayers(fail_after=1),
        FakeProbes(),
        conn=conn,
        samples=Samples(bsl_paths=["a.bsl"], xml_paths=[], deletions=[]),
    )
    assert first.result == "failed"
    assert reads.is_closed() is True
    prior_detail = state.get_database("ubd")["detail"]

    second = run_database_pipeline(
        database,
        FakeSource(prepared, fail_prepare=True),
        state,
        reads,
        FakeLayers(),
        FakeProbes(),
        conn=conn,
    )
    assert second.result == "failed"
    assert reads.is_closed() is True
    assert "prepare failed" in second.message
    # Prior recovery close must not be cleared by a pre-activation retry error.
    assert reads.events.count("open") == 0


def test_missing_samples_do_not_silently_publish(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    # Empty staged tree → sample_current_tree is empty + integrity_unknown.
    for path in prepared.staged_path.iterdir():
        if path.name != ".contour-revision":
            if path.is_file():
                path.unlink()
    prepared = PreparedSource(
        prepared.database,
        prepared.revision,
        prepared.staged_path,
        prepared.previous_path,
        sources_root=prepared.sources_root,
        published_path=prepared.published_path,
        kind="package",
        content_digest=compute_staged_tree_digest(prepared.staged_path),
    )
    boom = CommandProbe(
        check_bsl_cmd=["probe-bsl", "{path}"],
        check_deletion_cmd=["probe-del", "{path}"],
        runner=FakeDockerRunner(),
    )

    class BoomRunner(FakeDockerRunner):
        def run(self, cmd, *, timeout=None):
            raise RuntimeError(f"probe command invoked: {cmd}")

    boom.runner = BoomRunner()
    probes = CommandProbes(code_index=boom, graph=boom)
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        FakeLayers(),
        probes,
        conn=conn,
        samples=None,
    )
    assert result.result == "failed"
    assert state.get_database("ubd")["published_revision"] is None


def test_command_probe_failure_blocks_publish(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    (prepared.staged_path / "Module.bsl").write_text("// x", encoding="utf-8")
    prepared = PreparedSource(
        prepared.database,
        prepared.revision,
        prepared.staged_path,
        prepared.previous_path,
        sources_root=prepared.sources_root,
        published_path=prepared.published_path,
        kind="package",
        content_digest=compute_staged_tree_digest(prepared.staged_path),
    )

    class BoomRunner(FakeDockerRunner):
        def run(self, cmd, *, timeout=None):
            raise RuntimeError("probe boom")

    probes = CommandProbes(
        code_index=CommandProbe(
            check_bsl_cmd=["bsl", "{path}"],
            check_deletion_cmd=["del", "{path}"],
            runner=BoomRunner(),
        ),
        graph=CommandProbe(
            check_xml_cmd=["xml", "{path}"],
            check_deletion_cmd=["del", "{path}"],
            runner=BoomRunner(),
        ),
    )
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        FakeLayers(),
        probes,
        conn=conn,
        samples=Samples(bsl_paths=["Module.bsl"], xml_paths=[], deletions=[]),
    )
    assert result.result == "failed"
    assert "probe boom" in result.message


def test_klon_opl_does_not_require_graph_probe(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "klon_opl", frozenset({"code_index"})
    )
    probes = FakeProbes(graph=None)
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        FakeLayers(),
        probes,
        conn=conn,
        samples=Samples(
            bsl_paths=["a.bsl"], xml_paths=["x.xml"], deletions=["y.bsl"]
        ),
    )
    # XML must not require Graph for opl-only profile.
    assert result.result == "published"
    assert result.message == "ok"
    assert ("bsl", "a.bsl") in probes.code_index.events
    assert ("del", "y.bsl") in probes.code_index.events


def test_same_revision_rebuild_ignores_stale_graph_row(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    _seed_ready_layers(conn, "ubd", "r1", ("code_index", "graph"))
    layers = FakeLayers(
        results=[LayerResult("code_index", "r1", "s", "i")]
    )
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        layers,
        FakeProbes(),
        conn=conn,
        samples=Samples(bsl_paths=["a.bsl"], xml_paths=[], deletions=[]),
    )
    assert result.result == "failed"
    assert "graph" in result.message
    assert state.get_database("ubd")["published_revision"] is None


def test_partial_layer_failure_records_bsl_and_graph_separately(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        FakeLayers(fail_after=2),
        FakeProbes(),
        conn=conn,
        samples=Samples(bsl_paths=["a.bsl"], xml_paths=[], deletions=[]),
    )
    assert result.result == "failed"
    rows = {
        row["layer"]: row["status"]
        for row in conn.execute(
            "SELECT layer, status FROM layer_state WHERE database='ubd'"
        )
    }
    assert rows["code_index"] == "ready"
    assert rows["graph"] == "failed"


def test_runs_are_finished_on_success_and_failure(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    ok = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        FakeLayers(),
        FakeProbes(),
        conn=conn,
        samples=Samples(bsl_paths=["a.bsl"], xml_paths=[], deletions=[]),
    )
    assert ok.run_id is not None
    row = conn.execute(
        "SELECT result, finished_at, message FROM runs WHERE id=?", (ok.run_id,)
    ).fetchone()
    assert row["result"] == "published"
    assert row["finished_at"]

    prepared2 = _prepared(tmp_path / "second", "ubd", "r2")
    prepared2 = PreparedSource(
        prepared2.database,
        prepared2.revision,
        prepared2.staged_path,
        None,
        sources_root=prepared2.sources_root,
        published_path=prepared.published_path,
        kind="package",
        content_digest=compute_staged_tree_digest(prepared2.staged_path),
    )
    bad = run_database_pipeline(
        database,
        FakeSource(prepared2, fail_prepare=True),
        state,
        RecordingReads(),
        FakeLayers(),
        FakeProbes(),
        conn=conn,
    )
    assert bad.run_id is not None
    row = conn.execute(
        "SELECT result, finished_at, message FROM runs WHERE id=?", (bad.run_id,)
    ).fetchone()
    assert row["result"] == "failed"
    assert row["finished_at"]
    assert "prepare" in (row["message"] or "").lower()


def test_secondary_reads_state_error_preserves_primary(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )

    class BrokenReads(RecordingReads):
        def is_closed(self) -> bool:
            raise RuntimeError("docker inspect unavailable")

    result = run_database_pipeline(
        database,
        FakeSource(prepared, fail_prepare=True),
        state,
        BrokenReads(),
        FakeLayers(),
        FakeProbes(),
        conn=conn,
    )
    assert result.result == "failed"
    assert "prepare failed" in result.message
    assert "docker inspect unavailable" in result.message
    assert result.run_id is not None
