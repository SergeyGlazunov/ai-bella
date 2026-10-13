from __future__ import annotations

import sqlite3

META_KEYS = ("reads_close_requested", "reads_closed")
SCHEMA_VERSION = 2

# Legacy components table kept for pre-Task-6 reconcile/CLI until they use StateStore.
LEGACY_COMPONENTS = """
CREATE TABLE IF NOT EXISTS components (
  name TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  applied_commit TEXT,
  storage_id TEXT,
  indexer_version TEXT,
  container_id TEXT,
  exec_id TEXT,
  detail TEXT,
  updated_at TEXT NOT NULL
);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS database_state (
  database TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  received_revision TEXT,
  staged_revision TEXT,
  published_revision TEXT,
  lifecycle_status TEXT NOT NULL,
  detail TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS layer_state (
  database TEXT NOT NULL,
  layer TEXT NOT NULL,
  status TEXT NOT NULL,
  applied_revision TEXT,
  storage_id TEXT,
  indexer_version TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (database, layer)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  database TEXT NOT NULL,
  revision TEXT NOT NULL,
  package_path TEXT,
  checksum TEXT,
  received_at TEXT,
  validated_at TEXT,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  database TEXT NOT NULL DEFAULT 'ubd',
  target_revision TEXT,
  target_commit TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  result TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  database TEXT NOT NULL DEFAULT 'ubd',
  run_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  container_id TEXT,
  exec_id TEXT,
  started_at TEXT,
  finished_at TEXT,
  exit_code INTEGER,
  log_path TEXT
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {column[1] for column in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_meta(conn: sqlite3.Connection) -> None:
    for key in META_KEYS:
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            (key, ""),
        )


def _create_v2_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(LEGACY_COMPONENTS)
    conn.executescript(SCHEMA_V2)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Copy UBD component revisions into v2 tables; keep legacy components."""
    _create_v2_tables(conn)
    if _table_exists(conn, "runs"):
        _add_column_if_missing(conn, "runs", "database", "TEXT NOT NULL DEFAULT 'ubd'")
        _add_column_if_missing(conn, "runs", "target_revision", "TEXT")
        conn.execute(
            """
            UPDATE runs
            SET target_revision = target_commit
            WHERE target_revision IS NULL AND target_commit IS NOT NULL
            """
        )
    if _table_exists(conn, "jobs"):
        _add_column_if_missing(conn, "jobs", "database", "TEXT NOT NULL DEFAULT 'ubd'")

    published = None
    if _table_exists(conn, "components"):
        row = conn.execute(
            """
            SELECT applied_commit FROM components
            WHERE applied_commit IS NOT NULL AND applied_commit != ''
            ORDER BY name
            LIMIT 1
            """
        ).fetchone()
        if row is not None:
            published = str(row["applied_commit"])

        conn.execute(
            """
            INSERT INTO database_state (
              database, source_kind, received_revision, staged_revision,
              published_revision, lifecycle_status, detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(database) DO UPDATE SET
              published_revision = COALESCE(
                excluded.published_revision, database_state.published_revision
              ),
              updated_at = excluded.updated_at
            """,
            (
                "ubd",
                "git",
                published,
                None,
                published,
                "ready",
                None,
                "1970-01-01T00:00:00Z",
            ),
        )
        # Prefer a real updated_at from components when present.
        ts = conn.execute(
            "SELECT updated_at FROM components ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if ts is not None:
            conn.execute(
                "UPDATE database_state SET updated_at = ? WHERE database = ?",
                (str(ts["updated_at"]), "ubd"),
            )

        for comp in conn.execute("SELECT * FROM components"):
            layer = str(comp["name"])
            if layer not in {"graph", "code_index"}:
                continue
            conn.execute(
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
                    "ubd",
                    layer,
                    str(comp["status"]),
                    comp["applied_commit"],
                    comp["storage_id"],
                    comp["indexer_version"],
                    str(comp["updated_at"]),
                ),
            )


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])

    if version > SCHEMA_VERSION:
        conn.close()
        raise ValueError(f"unsupported schema user_version: {version}")

    if version == SCHEMA_VERSION:
        _create_v2_tables(conn)
    elif version == 1 or (
        version == 0 and _table_exists(conn, "components") and not _table_exists(conn, "database_state")
    ):
        _migrate_v1_to_v2(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    else:
        # Fresh database (user_version 0, no legacy tables).
        _create_v2_tables(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    _ensure_meta(conn)
    conn.commit()
    return conn


def get_component(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM components WHERE name = ?",
        (name,),
    ).fetchone()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return ""
    return str(row["value"])
