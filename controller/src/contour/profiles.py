from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from contour import dockerutil
from contour.config import VALID_LAYERS, DatabaseConfig
from contour.dockerutil import DockerRunner
from contour.index_bsl import index_bsl
from contour.index_graph import index_graph

# Stable execution order for required layers.
_LAYER_ORDER = ("code_index", "graph")


@dataclass(frozen=True)
class LayerResult:
    layer: str
    revision: str
    storage_id: str
    indexer_version: str


@dataclass(frozen=True)
class DatabaseProfile:
    name: str
    required_layers: tuple[str, ...]


def profile_from_config(config: DatabaseConfig) -> DatabaseProfile:
    """Build an ordered profile from ``DatabaseConfig.required_layers``."""
    unknown = set(config.required_layers) - VALID_LAYERS
    if unknown:
        raise ValueError(
            f"unknown layer in profile {config.name!r}: {sorted(unknown)[0]}"
        )
    ordered = tuple(layer for layer in _LAYER_ORDER if layer in config.required_layers)
    if not ordered:
        raise ValueError(f"profile {config.name!r} has no required layers")
    return DatabaseProfile(name=config.name, required_layers=ordered)


def run_bsl_layer(
    database: DatabaseConfig,
    revision: str,
    run_id: int,
    conn: sqlite3.Connection,
    runner: DockerRunner | None = None,
) -> LayerResult:
    """Run the code_index layer for ``database`` and return a verified result."""
    _ensure_run_revision(conn, run_id, database.name, revision)
    index_bsl(database, conn, run_id, runner=runner)
    storage_id = dockerutil.volume_identity(
        database.storage.code_index_volume, runner=runner
    )
    indexer_version = dockerutil.image_id(
        database.services.bsl_container, runner=runner
    )
    return LayerResult(
        layer="code_index",
        revision=revision,
        storage_id=storage_id,
        indexer_version=indexer_version,
    )


def run_graph_layer(
    database: DatabaseConfig,
    revision: str,
    run_id: int,
    conn: sqlite3.Connection,
    runner: DockerRunner | None = None,
    may_write: Callable[[], str] | None = None,
    *,
    full_needed: bool = False,
    **graph_kwargs: Any,
) -> LayerResult:
    """Run the graph layer; preserve exit-code-3 → FULL contract.

    ``may_write`` returns ``\"ok\"`` to allow FULL, or a short reason string
    that forbids it (same contract as ``may_write_decision``).
    ``full_needed`` applies a previously saved FULL requirement (recovery).
    """
    _ensure_run_revision(conn, run_id, database.name, revision)
    writer_container = index_graph(
        database,
        conn,
        run_id,
        runner=runner,
        may_write_fn=may_write,
        full_needed=full_needed,
        **graph_kwargs,
    )
    storage_id = dockerutil.volume_identity(
        database.storage.graph_volume, runner=runner
    )
    indexer_version = dockerutil.image_id(writer_container, runner=runner)
    return LayerResult(
        layer="graph",
        revision=revision,
        storage_id=storage_id,
        indexer_version=indexer_version,
    )


def run_required_layers(
    database: DatabaseConfig,
    *,
    revision: str,
    run_id: int,
    conn: sqlite3.Connection,
    runner: DockerRunner | None = None,
    only_layers: Sequence[str] | None = None,
    may_write: Callable[[], str] | None = None,
    full_needed: bool = False,
) -> list[LayerResult]:
    """Run required layers in order, or fail if a required layer is skipped."""
    profile = profile_from_config(database)
    selected = tuple(only_layers) if only_layers is not None else profile.required_layers
    missing = [layer for layer in profile.required_layers if layer not in selected]
    if missing:
        raise ValueError(
            f"required layer skipped for {database.name}: {missing[0]}"
        )
    extra = [layer for layer in selected if layer not in profile.required_layers]
    if extra:
        raise ValueError(
            f"required layer not in profile {database.name}: {extra[0]}"
        )

    results: list[LayerResult] = []
    for layer in profile.required_layers:
        if layer == "code_index":
            results.append(
                run_bsl_layer(database, revision, run_id, conn, runner=runner)
            )
        elif layer == "graph":
            results.append(
                run_graph_layer(
                    database,
                    revision,
                    run_id,
                    conn,
                    runner=runner,
                    may_write=may_write,
                    full_needed=full_needed,
                )
            )
        else:  # pragma: no cover - guarded by profile_from_config
            raise ValueError(f"unsupported layer: {layer}")
    return results


def _ensure_run_revision(
    conn: sqlite3.Connection,
    run_id: int,
    database: str,
    revision: str,
) -> None:
    row = conn.execute(
        "SELECT database, target_revision FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown run_id: {run_id}")
    run_db = str(row["database"] if hasattr(row, "keys") else row[0])
    run_rev = row["target_revision"] if hasattr(row, "keys") else row[1]
    if run_db != database:
        raise ValueError(
            f"run {run_id} belongs to database {run_db!r}, not {database!r}"
        )
    if run_rev not in (None, "") and str(run_rev) != revision:
        raise ValueError(
            f"run {run_id} target_revision {run_rev!r} != {revision!r}"
        )
    if run_rev in (None, ""):
        conn.execute(
            "UPDATE runs SET target_revision = ?, target_commit = ? WHERE id = ?",
            (revision, revision, run_id),
        )
        conn.commit()
