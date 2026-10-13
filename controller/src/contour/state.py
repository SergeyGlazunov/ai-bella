from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


class StateStore:
    """Per-database revision and layer state.

    Every mutating method commits immediately. Callers that need a multi-step
    transaction should use the underlying ``conn`` and SQL directly, or extend
    this class later with an explicit transaction API.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

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
        self._conn.commit()

    def record_received(
        self, database: str, revision: str, artifact_id: str | None
    ) -> None:
        self._require(database)
        now = _utc_now()
        self._conn.execute(
            """
            UPDATE database_state
            SET received_revision = ?, updated_at = ?
            WHERE database = ?
            """,
            (revision, now, database),
        )
        if artifact_id is not None:
            self._conn.execute(
                """
                INSERT INTO artifacts (
                  id, database, revision, package_path, checksum,
                  received_at, validated_at, status
                ) VALUES (?, ?, ?, NULL, NULL, ?, NULL, 'received')
                ON CONFLICT(id) DO UPDATE SET
                  revision = excluded.revision,
                  received_at = excluded.received_at,
                  status = excluded.status
                """,
                (artifact_id, database, revision, now),
            )
        self._conn.commit()

    def record_staged(self, database: str, revision: str) -> None:
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
        self._conn.commit()

    def mark_layer(
        self,
        database: str,
        layer: str,
        revision: str,
        status: str,
        storage_id: str | None,
        indexer_version: str | None,
    ) -> None:
        self._require(database)
        now = _utc_now()
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
            (database, layer, status, revision, storage_id, indexer_version, now),
        )
        self._conn.commit()

    def mark_published(self, database: str, revision: str) -> None:
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
        self._conn.commit()

    def get_database(self, name: str) -> sqlite3.Row:
        return self._require(name)

    def required_recovery(self, database: str) -> bool:
        row = self._require(database)
        status = str(row["lifecycle_status"] or "")
        if status in {"needs_recovery", "failed", "recovering"}:
            return True
        staged = row["staged_revision"]
        published = row["published_revision"]
        if staged and staged != published:
            return True
        return False

    def start_run(self, database: str, target_revision: str) -> int:
        """Record a run for ``database`` at ``target_revision``; returns run id."""
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
        self._conn.commit()
        return int(cur.lastrowid)
