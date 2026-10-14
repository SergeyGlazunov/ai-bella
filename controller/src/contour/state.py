from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

T = TypeVar("T")

_LAYER_SUCCESS = frozenset({"ready"})
_LAYER_UNFINISHED = frozenset({"building", "failed", "unknown"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


class StateStore:
    """Per-database revision and layer state.

    Every mutating method runs in its own transaction and commits on success.
    On failure the transaction is rolled back so a later successful call cannot
    commit leftover changes from the failed operation.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _tx(self, work: Callable[[], T]) -> T:
        try:
            result = work()
            self._conn.commit()
            return result
        except Exception:
            self._conn.rollback()
            raise

    def _require(self, name: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM database_state WHERE database = ?",
            (name,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown database: {name}")
        return row

    def register_database(self, name: str, source_kind: str) -> None:
        if not name or not str(name).strip():
            raise ValueError("database name must be non-empty")

        def work() -> None:
            now = _utc_now()
            self._conn.execute(
                """
                INSERT INTO database_state (
                  database, source_kind, received_revision, staged_revision,
                  published_revision, lifecycle_status, detail, updated_at
                ) VALUES (?, ?, NULL, NULL, NULL, 'ready', NULL, ?)
                ON CONFLICT(database) DO UPDATE SET
                  source_kind = excluded.source_kind,
                  updated_at = excluded.updated_at
                """,
                (name, source_kind, now),
            )

        self._tx(work)

    def record_received(
        self, database: str, revision: str, artifact_id: str | None
    ) -> None:
        def work() -> None:
            self._require(database)
            now = _utc_now()
            if artifact_id is not None:
                existing = self._conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["database"]) != database
                        or str(existing["revision"]) != revision
                    ):
                        raise ValueError(
                            f"artifact id collision: {artifact_id!r} owned by "
                            f"{existing['database']}/{existing['revision']}"
                        )
                    # Idempotent re-delivery: keep validated status and metadata.
                else:
                    self._conn.execute(
                        """
                        INSERT INTO artifacts (
                          id, database, revision, package_path, checksum,
                          received_at, validated_at, status
                        ) VALUES (?, ?, ?, NULL, NULL, ?, NULL, 'received')
                        """,
                        (artifact_id, database, revision, now),
                    )
            self._conn.execute(
                """
                UPDATE database_state
                SET received_revision = ?, updated_at = ?
                WHERE database = ?
                """,
                (revision, now, database),
            )

        self._tx(work)

    def record_staged(self, database: str, revision: str) -> None:
        def work() -> None:
            self._require(database)
            now = _utc_now()
            self._conn.execute(
                """
                UPDATE database_state
                SET staged_revision = ?, lifecycle_status = 'staged', updated_at = ?
                WHERE database = ?
                """,
                (revision, now, database),
            )

        self._tx(work)

    def set_lifecycle(
        self, database: str, status: str, *, detail: str | None = None
    ) -> None:
        """Record an operational lifecycle stage for dashboards and recovery."""

        def work() -> None:
            self._require(database)
            now = _utc_now()
            self._conn.execute(
                """
                UPDATE database_state
                SET lifecycle_status = ?, detail = ?, updated_at = ?
                WHERE database = ?
                """,
                (status, detail, now, database),
            )

        self._tx(work)

    def mark_layer(
        self,
        database: str,
        layer: str,
        revision: str,
        status: str,
        storage_id: str | None,
        indexer_version: str | None,
    ) -> None:
        def work() -> None:
            self._require(database)
            now = _utc_now()
            prior = self._conn.execute(
                """
                SELECT applied_revision FROM layer_state
                WHERE database = ? AND layer = ?
                """,
                (database, layer),
            ).fetchone()
            if status in _LAYER_SUCCESS:
                applied = revision
            elif prior is not None:
                applied = prior["applied_revision"]
            else:
                applied = None
            self._conn.execute(
                """
                INSERT INTO layer_state (
                  database, layer, status, applied_revision, storage_id,
                  indexer_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(database, layer) DO UPDATE SET
                  status = excluded.status,
                  applied_revision = excluded.applied_revision,
                  storage_id = excluded.storage_id,
                  indexer_version = excluded.indexer_version,
                  updated_at = excluded.updated_at
                """,
                (
                    database,
                    layer,
                    status,
                    applied,
                    storage_id,
                    indexer_version,
                    now,
                ),
            )

        self._tx(work)

    def mark_published(self, database: str, revision: str) -> None:
        def work() -> None:
            self._require(database)
            now = _utc_now()
            self._conn.execute(
                """
                UPDATE database_state
                SET published_revision = ?,
                    staged_revision = NULL,
                    lifecycle_status = 'ready',
                    updated_at = ?
                WHERE database = ?
                """,
                (revision, now, database),
            )

        self._tx(work)

    def get_database(self, name: str) -> sqlite3.Row:
        return self._require(name)

    def required_recovery(self, database: str) -> bool:
        row = self._require(database)
        status = str(row["lifecycle_status"] or "")
        if status in {"needs_recovery", "failed", "recovering", "staged"}:
            return True
        staged = row["staged_revision"]
        published = row["published_revision"]
        # Incomplete publish cycle (including same-revision rebuild).
        if staged not in (None, ""):
            return True
        layers = self._conn.execute(
            "SELECT layer, status, applied_revision FROM layer_state WHERE database = ?",
            (database,),
        ).fetchall()
        for layer in layers:
            layer_status = str(layer["status"] or "")
            if layer_status in _LAYER_UNFINISHED:
                return True
            if (
                published
                and layer_status == "ready"
                and layer["applied_revision"]
                and str(layer["applied_revision"]) != str(published)
            ):
                return True
        return False

    def start_run(self, database: str, target_revision: str) -> int:
        """Record a run for ``database`` at ``target_revision``; returns run id."""

        def work() -> int:
            self._require(database)
            now = _utc_now()
            cur = self._conn.execute(
                """
                INSERT INTO runs (
                  database, target_revision, target_commit, started_at
                ) VALUES (?, ?, ?, ?)
                """,
                (database, target_revision, target_revision, now),
            )
            return int(cur.lastrowid)

        return self._tx(work)

    def update_run_revision(self, run_id: int, target_revision: str) -> None:
        def work() -> None:
            self._conn.execute(
                """
                UPDATE runs
                SET target_revision = ?, target_commit = ?
                WHERE id = ?
                """,
                (target_revision, target_revision, run_id),
            )

        self._tx(work)

    def finish_run(self, run_id: int, result: str, message: str) -> None:
        def work() -> None:
            now = _utc_now()
            self._conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, result = ?, message = ?
                WHERE id = ?
                """,
                (now, result, message, run_id),
            )

        self._tx(work)
