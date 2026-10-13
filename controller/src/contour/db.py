from __future__ import annotations

import sqlite3

META_KEYS = ("reads_close_requested", "reads_closed")
SCHEMA_VERSION = 2
LEGACY_LAYERS = frozenset({"graph", "code_index"})

# Individual statements — never executescript inside a migration transaction
# (executescript commits and breaks rollback).
_V2_DDL = (
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS database_state (
      database TEXT PRIMARY KEY,
      source_kind TEXT NOT NULL,
      received_revision TEXT,
      staged_revision TEXT,
      published_revision TEXT,
      lifecycle_status TEXT NOT NULL,
      detail TEXT,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS layer_state (
      database TEXT NOT NULL,
      layer TEXT NOT NULL,
      status TEXT NOT NULL,
      applied_revision TEXT,
      storage_id TEXT,
      indexer_version TEXT,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (database, layer)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
      id TEXT PRIMARY KEY,
      database TEXT NOT NULL,
      revision TEXT NOT NULL,
      package_path TEXT,
      checksum TEXT,
      received_at TEXT,
      validated_at TEXT,
      status TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY,
      database TEXT NOT NULL DEFAULT 'ubd',
      target_revision TEXT,
      target_commit TEXT,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      result TEXT,
      message TEXT
    )
    """,
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {column[1] for column in conn.execute(f"PRAGMA table_info({table})")}


def _schema_v2_complete(conn: sqlite3.Connection) -> bool:
    for table in (
        "database_state",
        "layer_state",
        "artifacts",
        "runs",
        "jobs",
        "meta",
    ):
        if not _table_exists(conn, table):
            return False
    runs_cols = _column_names(conn, "runs")
    if not {"database", "target_revision", "target_commit", "started_at"} <= runs_cols:
        return False
    if "database" not in _column_names(conn, "jobs"):
        return False
    return True


def _ensure_meta(conn: sqlite3.Connection) -> None:
    for key in META_KEYS:
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            (key, ""),
        )


def _create_v2_tables(conn: sqlite3.Connection) -> None:
    for statement in _V2_DDL:
        conn.execute(statement)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _meta_get(conn: sqlite3.Connection, key: str) -> str:
    if not _table_exists(conn, "meta"):
        return ""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return ""
    return str(row["value"] if isinstance(row, sqlite3.Row) else row[0])


def _assess_legacy_ubd(
    layers: list[tuple[str, str, str | None]],
    *,
    reads_closed: str,
    reads_close_requested: str,
) -> tuple[str | None, str, str | None]:
    """Return (published_revision, lifecycle_status, detail)."""
    reasons: list[str] = []
    if reads_closed == "1" or reads_close_requested == "1":
        reasons.append("reads_close_incomplete")

    by_name = {name: (status, rev) for name, status, rev in layers}
    missing = LEGACY_LAYERS - set(by_name)
    if missing:
        reasons.append("missing_layers:" + ",".join(sorted(missing)))

    ready_revs: set[str] = set()
    for name, status, rev in layers:
        if name not in LEGACY_LAYERS:
            continue
        if status != "ready":
            reasons.append(f"layer_{name}_{status}")
        elif rev:
            ready_revs.add(rev)
        else:
            reasons.append(f"layer_{name}_empty_revision")

    if len(ready_revs) > 1:
        reasons.append("mixed_revisions")

    if not reasons and ready_revs and set(by_name) >= LEGACY_LAYERS:
        published = next(iter(ready_revs))
        return published, "ready", None
    detail = ";".join(reasons) if reasons else "uncertain_legacy_state"
    return None, "needs_recovery", detail


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Create/repair v2 objects and copy UBD component state. Caller owns the txn."""
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

    if not _table_exists(conn, "components"):
        return

    layer_rows: list[tuple[str, str, str | None]] = []
    updated_at = "1970-01-01T00:00:00Z"
    for comp in conn.execute("SELECT * FROM components"):
        layer = str(comp["name"])
        status = str(comp["status"])
        rev = comp["applied_commit"]
        rev_s = str(rev) if rev not in (None, "") else None
        if layer in LEGACY_LAYERS:
            layer_rows.append((layer, status, rev_s))
        updated_at = str(comp["updated_at"]) or updated_at
        if layer not in LEGACY_LAYERS:
            continue
        # applied_revision keeps the stored prior commit even when status is
        # building/failed — it is not proof the layer is ready.
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
                status,
                rev_s,
                comp["storage_id"],
                comp["indexer_version"],
                str(comp["updated_at"]),
            ),
        )

    published, lifecycle, detail = _assess_legacy_ubd(
        layer_rows,
        reads_closed=_meta_get(conn, "reads_closed"),
        reads_close_requested=_meta_get(conn, "reads_close_requested"),
    )
    conn.execute(
        """
        INSERT INTO database_state (
          database, source_kind, received_revision, staged_revision,
          published_revision, lifecycle_status, detail, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(database) DO UPDATE SET
          source_kind = excluded.source_kind,
          received_revision = excluded.received_revision,
          staged_revision = excluded.staged_revision,
          published_revision = excluded.published_revision,
          lifecycle_status = excluded.lifecycle_status,
          detail = excluded.detail,
          updated_at = excluded.updated_at
        """,
        (
            "ubd",
            "git",
            published,
            None,
            published,
            lifecycle,
            detail,
            updated_at,
        ),
    )


def _needs_schema_work(conn: sqlite3.Connection, version: int) -> bool:
    if not _schema_v2_complete(conn):
        return True
    if version != SCHEMA_VERSION:
        return True
    return False


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise ValueError(f"unsupported schema user_version: {version}")

        if not _needs_schema_work(conn, version):
            _ensure_meta(conn)
            conn.commit()
            return conn

        # Manual txn: executescript is forbidden here; isolation_level=None so
        # BEGIN/COMMIT/ROLLBACK are fully under our control for DDL+data.
        previous_isolation = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if _table_exists(conn, "components") or (
                    _table_exists(conn, "database_state")
                    and not _schema_v2_complete(conn)
                ):
                    _migrate_v1_to_v2(conn)
                else:
                    _create_v2_tables(conn)

                if not _schema_v2_complete(conn):
                    raise RuntimeError("schema v2 incomplete after migration")
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                _ensure_meta(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.isolation_level = previous_isolation
        return conn
    except Exception:
        conn.close()
        raise


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
