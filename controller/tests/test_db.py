from pathlib import Path

import sqlite3

from contour.db import get_component, get_meta, init_db, set_meta

V1_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  target_commit TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  result TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
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


def _open_v1_with_ubd(tmp_path: Path) -> Path:
    db_path = tmp_path / "v1.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(V1_SCHEMA)
    conn.execute(
        """
        INSERT INTO components (
          name, status, applied_commit, storage_id, indexer_version,
          container_id, exec_id, detail, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "graph",
            "ready",
            "abc123ubd",
            "vol|t0",
            "1.0",
            None,
            None,
            None,
            "2026-09-20T00:00:00Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO components (
          name, status, applied_commit, storage_id, indexer_version,
          container_id, exec_id, detail, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "code_index",
            "ready",
            "abc123ubd",
            "bsl|t0",
            "1.0",
            None,
            None,
            None,
            "2026-09-20T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?), (?, ?)",
        ("reads_close_requested", "", "reads_closed", ""),
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    return db_path


def test_init_db_new_database_is_schema_v2(tmp_path: Path):
    conn = init_db(str(tmp_path / "new.db"))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 2
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "database_state",
            "layer_state",
            "artifacts",
            "runs",
            "jobs",
            "meta",
            "components",
        }.issubset(tables)

        runs_columns = [
            column[1] for column in conn.execute("PRAGMA table_info(runs)").fetchall()
        ]
        assert "database" in runs_columns
        assert "target_revision" in runs_columns
        assert "target_commit" in runs_columns

        jobs_columns = [
            column[1] for column in conn.execute("PRAGMA table_info(jobs)").fetchall()
        ]
        assert "database" in jobs_columns
    finally:
        conn.close()


def test_migrate_v1_preserves_ubd_applied_commit(tmp_path: Path):
    db_path = _open_v1_with_ubd(tmp_path)
    conn = init_db(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        db_row = conn.execute(
            "SELECT * FROM database_state WHERE database = ?", ("ubd",)
        ).fetchone()
        assert db_row is not None
        assert db_row["published_revision"] == "abc123ubd"

        layers = {
            row["layer"]: row["applied_revision"]
            for row in conn.execute(
                "SELECT layer, applied_revision FROM layer_state WHERE database = ?",
                ("ubd",),
            )
        }
        assert layers["graph"] == "abc123ubd"
        assert layers["code_index"] == "abc123ubd"

        # Legacy components row still readable for pre-Task-6 reconcile.
        legacy = get_component(conn, "graph")
        assert legacy is not None
        assert legacy["applied_commit"] == "abc123ubd"
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path):
    db_path = _open_v1_with_ubd(tmp_path)
    conn1 = init_db(str(db_path))
    conn1.close()
    conn2 = init_db(str(db_path))
    try:
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == 2
        count = conn2.execute("SELECT COUNT(*) FROM database_state").fetchone()[0]
        assert count == 1
        layers = conn2.execute(
            "SELECT COUNT(*) FROM layer_state WHERE database = ?", ("ubd",)
        ).fetchone()[0]
        assert layers == 2
    finally:
        conn2.close()


def test_layer_state_unique_database_layer(tmp_path: Path):
    conn = init_db(str(tmp_path / "u.db"))
    try:
        conn.execute(
            """
            INSERT INTO layer_state (
              database, layer, status, applied_revision, storage_id,
              indexer_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("ubd", "graph", "ready", "r1", None, None, "t0"),
        )
        conn.commit()
        try:
            conn.execute(
                """
                INSERT INTO layer_state (
                  database, layer, status, applied_revision, storage_id,
                  indexer_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("ubd", "graph", "ready", "r2", None, None, "t1"),
            )
            conn.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised
    finally:
        conn.close()


def test_init_db_inserts_empty_meta_keys(tmp_path: Path):
    conn = init_db(str(tmp_path / "contour.db"))
    try:
        assert get_meta(conn, "reads_close_requested") == ""
        assert get_meta(conn, "reads_closed") == ""
    finally:
        conn.close()


def test_set_meta_reads_close_requested_round_trips(tmp_path: Path):
    conn = init_db(str(tmp_path / "contour.db"))
    try:
        set_meta(conn, "reads_close_requested", "1")
        assert get_meta(conn, "reads_close_requested") == "1"
        assert get_meta(conn, "reads_closed") == ""
    finally:
        conn.close()


def test_get_component_returns_none_when_missing(tmp_path: Path):
    conn = init_db(str(tmp_path / "contour.db"))
    try:
        assert get_component(conn, "source") is None
    finally:
        conn.close()


def test_get_component_returns_row(tmp_path: Path):
    conn = init_db(str(tmp_path / "contour.db"))
    try:
        conn.execute(
            """
            INSERT INTO components (
              name, status, applied_commit, storage_id, indexer_version,
              container_id, exec_id, detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "graph",
                "ready",
                "abc123",
                "mcp_for_1c_neo4j-data|2026-01-01T00:00:00Z",
                "1.0",
                "container-id",
                "exec-id",
                None,
                "2026-09-20T00:00:00Z",
            ),
        )
        conn.commit()
        row = get_component(conn, "graph")
        assert row is not None
        assert row["name"] == "graph"
        assert row["status"] == "ready"
        assert row["storage_id"] == "mcp_for_1c_neo4j-data|2026-01-01T00:00:00Z"
    finally:
        conn.close()
