"""Per-database lifecycle pipeline (Task 7)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from contour.config import (
    AdapterCommands,
    AdaptersConfig,
    DatabaseConfig,
    GitSourceConfig,
    ServicesConfig,
    StorageConfig,
)
from contour.db import init_db
from contour.index_graph import graph_full_needed_key, set_graph_full_needed
from contour.pipeline import PipelineResult, run_database_pipeline
from contour.profiles import LayerResult
from contour.sources import PreparedSource, write_revision
from contour.state import StateStore
from contour.verify import Samples


def _services() -> ServicesConfig:
    return ServicesConfig(
        graph_compose_dir="/tmp/graph",
        graph_project="graph",
        graph_service="metadata-indexer",
        graph_mcp="mcp",
        bsl_compose_dir="/tmp/bsl",
        bsl_project="bsl",
        bsl_container="bsl",
        bsl_publish_port=8011,
        bsl_daemon_toml="/tmp/bsl/daemon.toml",
        webui_network="net",
    )


def _db(name: str, layers: frozenset[str]) -> DatabaseConfig:
    return DatabaseConfig(
        name=name,
        source=GitSourceConfig("git", "/repo", "origin", "main", "src"),
        required_layers=layers,
        services=_services(),
        storage=StorageConfig(graph_volume="g", code_index_volume="c"),
        adapters=AdaptersConfig(
            code_index=AdapterCommands(),
            graph=AdapterCommands(),
            idle_probe=[],
        ),
    )


def _tree(path: Path, revision: str, payload: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    write_revision(path, revision)
    (path / "data.txt").write_text(payload, encoding="utf-8")


@dataclass
class RecordingReads:
    closed: bool = False
    events: list[str] = field(default_factory=list)

    def close(self) -> None:
        self.closed = True
        self.events.append("close")

    def open(self) -> None:
        self.closed = False
        self.events.append("open")

    def is_closed(self) -> bool:
        return self.closed


@dataclass
class FakeSource:
    prepared: PreparedSource
    fail_prepare: bool = False

    def prepare(self) -> PreparedSource:
        if self.fail_prepare:
            raise RuntimeError("prepare failed")
        return self.prepared


@dataclass
class FakeLayers:
    results: list[LayerResult] | None = None
    fail_after: int | None = None
    seen_full_needed: bool | None = None
    events: list[str] = field(default_factory=list)

    def run(
        self,
        database: DatabaseConfig,
        revision: str,
        run_id: int,
        *,
        full_needed: bool = False,
        may_write: Callable[[], str] | None = None,
    ) -> list[LayerResult]:
        del may_write
        self.seen_full_needed = full_needed
        self.events.append("index")
        if self.fail_after == 0:
            raise RuntimeError("indexing failed")
        if self.results is not None:
            return list(self.results)
        out: list[LayerResult] = []
        for layer in ("code_index", "graph"):
            if layer not in database.required_layers:
                continue
            out.append(
                LayerResult(
                    layer=layer,
                    revision=revision,
                    storage_id=f"{layer}-storage",
                    indexer_version=f"{layer}-image",
                )
            )
            if self.fail_after is not None and len(out) >= self.fail_after:
                raise RuntimeError(f"{layer} failed")
        return out


@dataclass
class FakeProbe:
    status: str = "ready"
    events: list[str] = field(default_factory=list)

    def check_bsl(self, path: str) -> None:
        self.events.append(("bsl", path))

    def check_xml(self, path: str) -> None:
        self.events.append(("xml", path))

    def check_deletion(self, path: str) -> None:
        self.events.append(("del", path))


@dataclass
class FakeProbes:
    code_index: FakeProbe = field(default_factory=FakeProbe)
    graph: FakeProbe = field(default_factory=FakeProbe)
    fail: bool = False
    ran: bool = False

    def run_before_open(self, samples: Samples) -> None:
        self.ran = True
        if self.fail:
            raise RuntimeError("probe failed")
        from contour.verify import verify_indexes

        verify_indexes(samples, self)


def _prepared(tmp_path: Path, database: str, revision: str) -> PreparedSource:
    root = tmp_path / "sources"
    staging = root / ".staging" / database / "tok"
    published = root / database
    root.mkdir(parents=True, exist_ok=True)
    _tree(staging, revision, "next")
    return PreparedSource(
        database=database,
        revision=revision,
        staged_path=staging,
        previous_path=None,
        sources_root=root,
        published_path=published,
        kind="package",
        content_digest=None,
    )


def _setup(tmp_path: Path, name: str, layers: frozenset[str], revision: str = "r1"):
    conn = init_db(str(tmp_path / "state.db"))
    state = StateStore(conn)
    state.register_database(name, "git")
    database = _db(name, layers)
    prepared = _prepared(tmp_path, name, revision)
    # Digests optional for package kind only when set; force package digest skip
    # by using git kind for activate content check... activate requires digest for package.
    # Use kind=git with fake .git? Simpler: compute digest for package tree.
    from contour.sources import compute_staged_tree_digest

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
    return conn, state, database, prepared


def test_pipeline_transition_order_to_published(tmp_path: Path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    reads = RecordingReads()
    layers = FakeLayers()
    probes = FakeProbes()
    transitions: list[str] = []

    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        reads,
        layers,
        probes,
        conn=conn,
        transitions=transitions,
        samples=Samples(bsl_paths=["a.bsl"], xml_paths=[], deletions=[]),
    )
    assert result == PipelineResult("ubd", "r1", "published", "ok")
    assert transitions == [
        "received",
        "validated",
        "staged",
        "reads_closed",
        "indexing",
        "verified",
        "published",
    ]
    assert reads.events == ["close", "open"]
    assert probes.ran is True
    row = state.get_database("ubd")
    assert row["published_revision"] == "r1"
    assert row["lifecycle_status"] == "ready"


def test_failure_before_activation_leaves_reads_open(tmp_path: Path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    reads = RecordingReads()
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
    assert reads.is_closed() is False
    assert state.get_database("ubd")["published_revision"] is None


def test_failure_after_activation_leaves_reads_closed(tmp_path: Path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    reads = RecordingReads()
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        reads,
        FakeLayers(fail_after=1),
        FakeProbes(),
        conn=conn,
    )
    assert result.result == "failed"
    assert reads.is_closed() is True
    assert prepared.published_path.is_dir()
    assert (prepared.published_path / "data.txt").read_text(encoding="utf-8") == "next"
    assert state.get_database("ubd")["published_revision"] is None


def test_ubd_cannot_publish_without_both_layers(tmp_path: Path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    layers = FakeLayers(
        results=[
            LayerResult("code_index", "r1", "s", "i"),
        ]
    )
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        layers,
        FakeProbes(),
        conn=conn,
    )
    assert result.result == "failed"
    assert "graph" in result.message
    assert state.get_database("ubd")["published_revision"] is None


def test_klon_opl_cannot_publish_without_bsl(tmp_path: Path):
    conn, state, database, prepared = _setup(
        tmp_path, "klon_opl", frozenset({"code_index"})
    )
    layers = FakeLayers(results=[])
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        layers,
        FakeProbes(),
        conn=conn,
    )
    assert result.result == "failed"
    assert "code_index" in result.message


def test_skipped_mandatory_layer_fails(tmp_path: Path):
    conn, state, database, prepared = _setup(
        tmp_path, "kb", frozenset({"code_index", "graph"})
    )

    class SkippingLayers(FakeLayers):
        def run(self, database, revision, run_id, *, full_needed=False, may_write=None):
            raise ValueError("required layer skipped for kb: graph")

    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        SkippingLayers(),
        FakeProbes(),
        conn=conn,
    )
    assert result.result == "failed"
    assert "skipped" in result.message.lower() or "graph" in result.message


def test_probes_run_before_reads_reopen(tmp_path: Path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    order: list[str] = []

    class OrderedReads(RecordingReads):
        def open(self) -> None:
            order.append("open")
            super().open()

    ordered_reads = OrderedReads()

    class OrderedProbes(FakeProbes):
        def run_before_open(self, samples: Samples) -> None:
            order.append("probes")
            assert ordered_reads.is_closed() is True
            super().run_before_open(samples)

    run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        ordered_reads,
        FakeLayers(),
        OrderedProbes(),
        conn=conn,
        samples=Samples(bsl_paths=["x.bsl"], xml_paths=[], deletions=[]),
    )
    assert order == ["probes", "open"]


def test_publish_updates_sqlite_only_after_verification(tmp_path: Path):
    conn, state, database, prepared = _setup(
        tmp_path, "ubd", frozenset({"code_index", "graph"})
    )
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        FakeLayers(),
        FakeProbes(fail=True),
        conn=conn,
        samples=Samples(bsl_paths=["x.bsl"], xml_paths=[], deletions=[]),
    )
    assert result.result == "failed"
    assert state.get_database("ubd")["published_revision"] is None
    layers = conn.execute(
        "SELECT layer, status FROM layer_state WHERE database='ubd'"
    ).fetchall()
    assert layers  # indexing recorded
    assert all(row["status"] == "ready" for row in layers)


def test_pipeline_uses_saved_graph_full_flag(tmp_path: Path):
    conn, state, database, prepared = _setup(
        tmp_path, "kb", frozenset({"code_index", "graph"})
    )
    set_graph_full_needed(conn, "kb", True)
    layers = FakeLayers()
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        layers,
        FakeProbes(),
        conn=conn,
        samples=Samples(bsl_paths=[], xml_paths=[], deletions=[]),
    )
    assert result.result == "published"
    assert layers.seen_full_needed is True
    # Successful publish path should clear via layer runner in real stack;
    # fake layers do not clear — pipeline must at least pass the flag in.
    assert dict(conn.execute("SELECT key, value FROM meta")).get(
        graph_full_needed_key("kb")
    ) == "1"
