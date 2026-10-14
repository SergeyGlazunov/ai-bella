from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from contour.config import DatabaseConfig
from contour.index_graph import is_graph_full_needed
from contour.profiles import (
    LayerResult,
    profile_from_config,
    run_required_layers,
)
from contour.sources import SourceProvider, activate_source
from contour.state import StateStore
from contour.verify import Samples, verify_indexes, verify_layers_at_revision


@dataclass(frozen=True)
class PipelineResult:
    database: str
    revision: str
    result: Literal["published", "deferred", "failed"]
    message: str


@runtime_checkable
class ReadController(Protocol):
    def close(self) -> None: ...

    def open(self) -> None: ...

    def is_closed(self) -> bool: ...


@runtime_checkable
class LayerRunnerRegistry(Protocol):
    def run(
        self,
        database: DatabaseConfig,
        revision: str,
        run_id: int,
        *,
        full_needed: bool = False,
        may_write: Callable[[], str] | None = None,
    ) -> list[LayerResult]: ...


@dataclass
class ProfilesLayerRunner:
    """Default registry: profiles + SQLite FULL flag for Graph recovery."""

    conn: sqlite3.Connection
    docker_runner: object | None = None
    may_write: Callable[[], str] | None = None

    def run(
        self,
        database: DatabaseConfig,
        revision: str,
        run_id: int,
        *,
        full_needed: bool = False,
        may_write: Callable[[], str] | None = None,
    ) -> list[LayerResult]:
        return run_required_layers(
            database,
            revision=revision,
            run_id=run_id,
            conn=self.conn,
            runner=self.docker_runner,  # type: ignore[arg-type]
            may_write=may_write or self.may_write,
            full_needed=full_needed,
        )


def run_database_pipeline(
    database: DatabaseConfig,
    source: SourceProvider,
    state: StateStore,
    reads: ReadController,
    layers: LayerRunnerRegistry,
    probes: object,
    *,
    conn: sqlite3.Connection,
    may_write: Callable[[], str] | None = None,
    samples: Samples | None = None,
    transitions: list[str] | None = None,
) -> PipelineResult:
    """Run one database through prepare → activate → index → verify → publish.

    Scheduling and the global writer lock stay outside. Graph FULL recovery
    reads the saved per-database meta flag unless the layer runner is given an
    explicit override via its ``full_needed`` argument from this function.
    """
    name = database.name
    revision = ""
    activated = False
    log = transitions if transitions is not None else []

    def note(stage: str) -> None:
        log.append(stage)
        state.set_lifecycle(name, stage)

    try:
        prepared = source.prepare()
        revision = prepared.revision
        state.record_received(name, revision, None)
        note("received")
        note("validated")
        state.record_staged(name, revision)
        note("staged")

        reads.close()
        note("reads_closed")

        activate_source(prepared, prepared.published_path)
        activated = True

        run_id = state.start_run(name, revision)
        note("indexing")
        full_needed = is_graph_full_needed(conn, name)
        results = layers.run(
            database,
            revision,
            run_id,
            full_needed=full_needed,
            may_write=may_write,
        )
        _record_layer_results(state, name, results)

        profile = profile_from_config(database)
        verify_layers_at_revision(conn, name, revision, profile.required_layers)

        probe_samples = samples or Samples(
            bsl_paths=[], xml_paths=[], deletions=[]
        )
        _run_probes(probes, probe_samples)
        note("verified")

        reads.open()
        log.append("published")
        state.mark_published(name, revision)
        return PipelineResult(name, revision, "published", "ok")
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        if activated:
            # Source already moved; keep reads closed for recovery.
            try:
                state.set_lifecycle(name, "failed", detail=message)
            except Exception:
                pass
            return PipelineResult(name, revision or "unknown", "failed", message)
        # Failure before successful activation: previous published data stays
        # available — reopen if we already closed.
        if reads.is_closed():
            try:
                reads.open()
            except Exception:
                pass
        try:
            state.set_lifecycle(name, "failed", detail=message)
        except Exception:
            pass
        return PipelineResult(name, revision or "unknown", "failed", message)


def _record_layer_results(
    state: StateStore, database: str, results: Sequence[LayerResult]
) -> None:
    for item in results:
        state.mark_layer(
            database,
            item.layer,
            item.revision,
            "ready",
            item.storage_id,
            item.indexer_version,
        )


def _run_probes(probes: object, samples: Samples) -> None:
    runner = getattr(probes, "run_before_open", None)
    if callable(runner):
        runner(samples)
        return
    verify_indexes(samples, probes)  # type: ignore[arg-type]
