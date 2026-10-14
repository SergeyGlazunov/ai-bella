"""Regressions for Task 7 follow-up on 6735ff3.

python -m pytest "../For Cursor/test_task7_followup_6735ff3.py" -q
Run from controller/. No live Docker.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import contour.pipeline as pipeline
from contour.pipeline import run_database_pipeline
from contour.sources import PreparedSource, compute_staged_tree_digest
from contour.verify import Samples, sample_current_tree

sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[2] / "tests"))
from test_index_graph import FakeDockerRunner
from test_pipeline import (
    FakeLayers,
    FakeProbes,
    FakeSource,
    RecordingReads,
    _setup,
)


def _with_digest(prepared: PreparedSource) -> PreparedSource:
    return PreparedSource(
        prepared.database,
        prepared.revision,
        prepared.staged_path,
        prepared.previous_path,
        sources_root=prepared.sources_root,
        published_path=prepared.published_path,
        kind="package",
        content_digest=compute_staged_tree_digest(prepared.staged_path),
    )


def _xml_heavy_tree(staged: Path, *, under_src: bool = True) -> None:
    root = staged / "src" if under_src else staged
    root.mkdir(parents=True, exist_ok=True)
    for i in range(32):
        (root / f"A{i:03d}.xml").write_text(f"<Meta{i}/>", encoding="utf-8")
    (root / "ZModule.bsl").write_text("// module", encoding="utf-8")


@dataclass
class TrackingProbe:
    status: str = "ready"
    bsl_checked: list[str] = field(default_factory=list)
    xml_checked: list[str] = field(default_factory=list)
    missing_bsl: frozenset[str] = frozenset({"ZModule.bsl"})

    def check_bsl(self, path: str) -> None:
        self.bsl_checked.append(path)
        if path in self.missing_bsl:
            raise RuntimeError(f"module missing from index: {path}")

    def check_xml(self, path: str) -> None:
        self.xml_checked.append(path)

    def check_deletion(self, path: str) -> None:
        del path


@dataclass
class TrackingProbes:
    code_index: TrackingProbe = field(default_factory=TrackingProbe)
    graph: TrackingProbe | None = field(default_factory=TrackingProbe)

    def run_before_open(self, samples: Samples, required_layers=None) -> None:
        from contour.verify import verify_indexes

        verify_indexes(samples, self, required_layers=required_layers)


@dataclass
class PartialCloseReads(RecordingReads):
    """close() applies partial downtime then fails (Graph MCP stopped, BSL net fails)."""

    def close(self) -> None:
        self.closed = True
        self.events.append("close")
        raise RuntimeError("bsl network disconnect failed")


def test_sample_current_tree_keeps_bsl_when_xml_fills_old_limit(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    _xml_heavy_tree(root, under_src=False)
    samples = sample_current_tree(
        root, required_layers=("code_index", "graph"), limit=32
    )
    assert "ZModule.bsl" in samples.bsl_paths
    assert samples.xml_paths
    assert len(samples.xml_paths) <= 16


def test_kb_xml_heavy_auto_samples_still_run_bsl_probe(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "kb", frozenset({"code_index", "graph"})
    )
    _xml_heavy_tree(prepared.staged_path)
    prepared = _with_digest(prepared)
    probes = TrackingProbes()
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
    assert "ZModule.bsl" in result.message or probes.code_index.bsl_checked
    assert "ZModule.bsl" in probes.code_index.bsl_checked
    assert state.get_database("kb")["published_revision"] is None


def test_klon_opl_auto_samples_run_bsl_despite_leading_xml(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "klon_opl", frozenset({"code_index"})
    )
    _xml_heavy_tree(prepared.staged_path)
    prepared = _with_digest(prepared)
    probes = TrackingProbes(graph=None)
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
    assert probes.code_index.bsl_checked == ["ZModule.bsl"]
    assert probes.code_index.xml_checked == []
    assert state.get_database("klon_opl")["published_revision"] is None


def test_partial_close_reopens_reads_that_were_open(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    reads = PartialCloseReads(closed=False)
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        reads,
        FakeLayers(),
        FakeProbes(),
        conn=conn,
        samples=Samples(bsl_paths=["a.bsl"], xml_paths=[], deletions=[]),
    )
    assert result.result == "failed"
    assert "bsl network disconnect failed" in result.message
    assert reads.is_closed() is False
    assert reads.events == ["close", "open"]


def test_prior_closed_reads_stay_closed_on_prepare_failure(tmp_path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    reads = RecordingReads(closed=True)
    result = run_database_pipeline(
        database,
        FakeSource(prepared, fail_prepare=True),
        state,
        reads,
        FakeLayers(),
        FakeProbes(),
        conn=conn,
    )
    assert result.result == "failed"
    assert reads.is_closed() is True
    assert "open" not in reads.events
