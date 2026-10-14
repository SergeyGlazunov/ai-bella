from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from contour.config import DatabaseConfig, GitSourceConfig, PackageSourceConfig
from contour.index_graph import is_graph_full_needed
from contour.profiles import (
    LayerResult,
    profile_from_config,
    run_required_layers,
)
from contour.sources import PreparedSource, SourceProvider, activate_source
from contour.state import StateStore
from contour.runtime_policy import WriteWindowClosed
from contour.verify import (
    Samples,
    sample_current_tree,
    verify_indexes,
    verify_run_layer_results,
)


@dataclass(frozen=True)
class PipelineResult:
    database: str
    revision: str
    result: Literal["published", "deferred", "failed"]
    message: str
    run_id: int | None = None


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
        on_layer_start: Callable[[str], None] | None = None,
        on_layer_result: Callable[[LayerResult], None] | None = None,
        on_layer_error: Callable[[str, BaseException], None] | None = None,
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
        on_layer_start: Callable[[str], None] | None = None,
        on_layer_result: Callable[[LayerResult], None] | None = None,
        on_layer_error: Callable[[str, BaseException], None] | None = None,
    ) -> list[LayerResult]:
        return run_required_layers(
            database,
            revision=revision,
            run_id=run_id,
            conn=self.conn,
            runner=self.docker_runner,  # type: ignore[arg-type]
            may_write=may_write or self.may_write,
            full_needed=full_needed,
            on_layer_start=on_layer_start,
            on_layer_result=on_layer_result,
            on_layer_error=on_layer_error,
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
    reads the saved per-database meta flag and passes it as ``full_needed``.
    """
    name = database.name
    revision = ""
    activated = False
    close_attempted = False
    run_id: int | None = None
    log = transitions if transitions is not None else []
    reads_closed_on_entry = True
    entry_read_error: str | None = None
    try:
        reads_closed_on_entry = bool(reads.is_closed())
    except Exception as read_exc:
        entry_read_error = str(read_exc) or read_exc.__class__.__name__
        reads_closed_on_entry = True

    def note(stage: str) -> None:
        log.append(stage)
        state.set_lifecycle(name, stage)

    try:
        run_id = state.start_run(name, "(pending)")
        prepared = source.prepare()
        revision = prepared.revision
        state.update_run_revision(run_id, revision)
        state.record_received(name, revision, None)
        note("received")
        note("validated")
        state.record_staged(name, revision)
        note("staged")

        # Mark attempt before close(): partial apply_close may leave reads
        # unavailable even when the call raises.
        close_attempted = True
        reads.close()
        note("reads_closed")

        activate_source(prepared, prepared.published_path)
        activated = True

        note("indexing")
        full_needed = is_graph_full_needed(conn, name)
        profile = profile_from_config(database)

        def on_start(layer: str) -> None:
            state.mark_layer(name, layer, revision, "building", None, None)

        def on_result(item: LayerResult) -> None:
            state.mark_layer(
                name,
                item.layer,
                item.revision,
                "ready",
                item.storage_id,
                item.indexer_version,
            )

        def on_error(layer: str, exc: BaseException) -> None:
            del exc
            state.mark_layer(name, layer, revision, "failed", None, None)

        results = layers.run(
            database,
            revision,
            run_id,
            full_needed=full_needed,
            may_write=may_write,
            on_layer_start=on_start,
            on_layer_result=on_result,
            on_layer_error=on_error,
        )
        # Publish gate uses this run's results only — not stale ready rows.
        verify_run_layer_results(results, profile.required_layers, revision)

        probe_samples = _resolve_samples(
            samples, prepared, database, profile.required_layers
        )
        _run_probes(probes, probe_samples, profile.required_layers)
        note("verified")

        reads.open()
        log.append("published")
        state.mark_published(name, revision)
        state.finish_run(run_id, "published", "ok")
        return PipelineResult(name, revision, "published", "ok", run_id=run_id)
    except WriteWindowClosed as exc:
        message = str(exc) or "deferred_window"
        if entry_read_error:
            message = f"{message}; also could not read prior reads state: {entry_read_error}"
        if not activated and close_attempted and reads_closed_on_entry is False:
            try:
                reads.open()
            except Exception as open_exc:
                message = f"{message}; also failed to reopen reads: {open_exc}"
        if run_id is not None:
            try:
                state.finish_run(run_id, "deferred", message)
            except Exception:
                pass
        try:
            state.set_lifecycle(name, "deferred", detail=message)
        except Exception:
            pass
        return PipelineResult(
            name, revision or "unknown", "deferred", message, run_id=run_id
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        if entry_read_error:
            message = f"{message}; also could not read prior reads state: {entry_read_error}"
        if activated:
            _finish_failed(state, name, run_id, message)
            return PipelineResult(
                name, revision or "unknown", "failed", message, run_id=run_id
            )
        # Pre-activation failure: reopen only if THIS attempt touched previously
        # open reads (including partial close). Do not clear a close left by an
        # unfinished earlier run, and do not open when entry state was unknown.
        if close_attempted and reads_closed_on_entry is False:
            try:
                reads.open()
            except Exception as open_exc:
                message = f"{message}; also failed to reopen reads: {open_exc}"
        _finish_failed(state, name, run_id, message)
        return PipelineResult(
            name, revision or "unknown", "failed", message, run_id=run_id
        )


def _finish_failed(
    state: StateStore, database: str, run_id: int | None, message: str
) -> None:
    try:
        state.set_lifecycle(database, "failed", detail=message)
    except Exception:
        pass
    if run_id is not None:
        try:
            state.finish_run(run_id, "failed", message)
        except Exception:
            pass


def _resolve_samples(
    samples: Samples | None,
    prepared: PreparedSource,
    database: DatabaseConfig,
    required_layers: Sequence[str],
) -> Samples:
    if samples is not None:
        return samples
    root = prepared.staged_path if prepared.staged_path.exists() else prepared.published_path
    workspace = ""
    src = database.source
    if isinstance(src, GitSourceConfig):
        workspace = src.workspace_subdir or ""
    elif isinstance(src, PackageSourceConfig):
        # Staged package snapshot is already the src/ tree.
        workspace = ""
    return sample_current_tree(
        root,
        workspace_subdir=workspace,
        required_layers=required_layers,
    )


def _run_probes(
    probes: object, samples: Samples, required_layers: Sequence[str]
) -> None:
    runner = getattr(probes, "run_before_open", None)
    if callable(runner):
        try:
            runner(samples, required_layers=required_layers)
            return
        except TypeError:
            runner(samples)
            return
    verify_indexes(samples, probes, required_layers=required_layers)  # type: ignore[arg-type]
