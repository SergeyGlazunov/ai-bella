from pathlib import Path

from contour.db import get_component, get_meta, init_db, set_meta


def test_init_db_creates_components_primary_key(tmp_path: Path):
    db_path = tmp_path / "contour.db"
    conn = init_db(str(db_path))
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='components'"
        ).fetchone()
        assert table is not None

        columns = conn.execute("PRAGMA table_info(components)").fetchall()
        col_names = [column[1] for column in columns]
        assert col_names == [
            "name",
            "status",
            "applied_commit",
            "storage_id",
            "indexer_version",
            "container_id",
            "exec_id",
            "detail",
            "updated_at",
        ]
        pk_columns = [column[1] for column in columns if column[5]]
        assert pk_columns == ["name"]
    finally:
        conn.close()


def test_init_db_creates_runs_and_jobs_tables(tmp_path: Path):
    db_path = tmp_path / "contour.db"
    conn = init_db(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"components", "runs", "jobs", "meta"}.issubset(tables)

        runs_columns = [
            column[1]
            for column in conn.execute("PRAGMA table_info(runs)").fetchall()
        ]
        assert runs_columns == [
            "id",
            "target_commit",
            "started_at",
            "finished_at",
            "result",
            "message",
        ]

        jobs_columns = [
            column[1]
            for column in conn.execute("PRAGMA table_info(jobs)").fetchall()
        ]
        assert jobs_columns == [
            "id",
            "run_id",
            "stage",
            "status",
            "container_id",
            "exec_id",
            "started_at",
            "finished_at",
            "exit_code",
            "log_path",
        ]
    finally:
        conn.close()


def test_init_db_inserts_empty_meta_keys(tmp_path: Path):
    db_path = tmp_path / "contour.db"
    conn = init_db(str(db_path))
    try:
        assert get_meta(conn, "reads_close_requested") == ""
        assert get_meta(conn, "reads_closed") == ""
    finally:
        conn.close()


def test_set_meta_reads_close_requested_round_trips(tmp_path: Path):
    db_path = tmp_path / "contour.db"
    conn = init_db(str(db_path))
    try:
        set_meta(conn, "reads_close_requested", "1")
        assert get_meta(conn, "reads_close_requested") == "1"
        assert get_meta(conn, "reads_closed") == ""
    finally:
        conn.close()


def test_get_component_returns_none_when_missing(tmp_path: Path):
    db_path = tmp_path / "contour.db"
    conn = init_db(str(db_path))
    try:
        assert get_component(conn, "source") is None
    finally:
        conn.close()


def test_get_component_returns_row(tmp_path: Path):
    db_path = tmp_path / "contour.db"
    conn = init_db(str(db_path))
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
