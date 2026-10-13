"""Independent Task 3 review checks; run against an installed contour checkout."""
import sqlite3
from unittest.mock import patch

import pytest

from contour import db
from contour.state import StateStore

V1_SCHEMA = """
CREATE TABLE components (
 name TEXT PRIMARY KEY, status TEXT NOT NULL, applied_commit TEXT,
 storage_id TEXT, indexer_version TEXT, container_id TEXT, exec_id TEXT,
 detail TEXT, updated_at TEXT NOT NULL);
CREATE TABLE runs (
 id INTEGER PRIMARY KEY, target_commit TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, result TEXT, message TEXT);
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, stage TEXT NOT NULL,
 status TEXT NOT NULL, container_id TEXT, exec_id TEXT, started_at TEXT,
 finished_at TEXT, exit_code INTEGER, log_path TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def legacy(path, components, version=0, closed=False):
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    for name, status, revision in components:
        conn.execute(
            "INSERT INTO components VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)",
            (name, status, revision, "volume|t0", "image-v1", "2026-10-14T00:00:00Z"),
        )
    conn.execute(
        "INSERT INTO runs VALUES (7, 'old-sha', 't0', 't1', 'success', 'old message')"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (11, 7, 'bsl_index', 'ok', NULL, 'exec-7', 't0', 't1', 0, '/old.log')"
    )
    conn.execute("INSERT INTO meta VALUES ('reads_closed', ?)", ("1" if closed else "",))
    conn.execute(
        "INSERT INTO meta VALUES ('reads_close_requested', ?)",
        ("1" if closed else "",),
    )
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("version", [0, 1])
def test_consistent_legacy_history_and_revisions_survive(tmp_path, version):
    path = tmp_path / "state.db"
    legacy(
        path,
        [("code_index", "ready", "r1"), ("graph", "ready", "r1")],
        version=version,
    )
    conn = db.init_db(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    run = conn.execute("SELECT * FROM runs WHERE id=7").fetchone()
    assert (run["database"], run["target_commit"], run["target_revision"], run["message"]) == (
        "ubd",
        "old-sha",
        "old-sha",
        "old message",
    )
    job = conn.execute("SELECT * FROM jobs WHERE id=11").fetchone()
    assert (job["database"], job["run_id"], job["exit_code"], job["log_path"]) == (
        "ubd",
        7,
        0,
        "/old.log",
    )
    assert StateStore(conn).get_database("ubd")["published_revision"] == "r1"
    conn.close()


@pytest.mark.parametrize(
    "interrupt_target",
    [
        "_create_v2_tables",
        "_add_column_if_missing",
        "_assess_legacy_ubd",
    ],
)
def test_interrupted_unversioned_migration_retries_without_losing_data(
    tmp_path, interrupt_target
):
    path = tmp_path / "state.db"
    legacy(path, [("code_index", "ready", "r1"), ("graph", "ready", "r1")])
    with patch.object(
        db,
        interrupt_target,
        side_effect=sqlite3.OperationalError("injected migration interruption"),
    ):
        with pytest.raises(sqlite3.OperationalError):
            db.init_db(str(path))
    conn = db.init_db(str(path))
    columns = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {"database", "target_revision"} <= columns, columns
    assert StateStore(conn).get_database("ubd")["published_revision"] == "r1"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()


@pytest.mark.parametrize(
    "components,closed",
    [
        ([("code_index", "ready", "r2"), ("graph", "ready", "r1")], False),
        ([("code_index", "building", "r1"), ("graph", "ready", "r1")], False),
        ([("code_index", "ready", "r1"), ("graph", "ready", "r1")], True),
    ],
)
def test_uncertain_legacy_state_requires_recovery(tmp_path, components, closed):
    path = tmp_path / "state.db"
    legacy(path, components, closed=closed)
    conn = db.init_db(str(path))
    store = StateStore(conn)
    assert store.required_recovery("ubd"), dict(store.get_database("ubd"))
    conn.close()


def test_same_revision_failed_rebuild_requires_recovery(tmp_path):
    conn = db.init_db(str(tmp_path / "state.db"))
    store = StateStore(conn)
    store.register_database("ubd", "git")
    store.record_received("ubd", "r1", None)
    store.record_staged("ubd", "r1")
    store.mark_layer("ubd", "graph", "r1", "ready", "v1", "i1")
    store.mark_layer("ubd", "code_index", "r1", "ready", "v2", "i2")
    store.mark_published("ubd", "r1")
    store.record_staged("ubd", "r1")
    store.mark_layer("ubd", "graph", "r1", "failed", "replacement-volume", "i1")
    assert store.required_recovery("ubd"), dict(store.get_database("ubd"))
    conn.close()


def test_artifact_id_cannot_silently_change_owner(tmp_path):
    conn = db.init_db(str(tmp_path / "state.db"))
    store = StateStore(conn)
    store.register_database("kb", "package")
    store.register_database("klon_opl", "package")
    store.record_received("kb", "kb-r1", "export-42")
    try:
        store.record_received("klon_opl", "opl-r1", "export-42")
    except ValueError:
        pass  # Rejecting a collision is an acceptable contract.
    row = conn.execute("SELECT * FROM artifacts WHERE id='export-42'").fetchone()
    assert (row["database"], row["revision"]) == ("kb", "kb-r1"), dict(row)
    conn.close()


def test_identical_delivery_does_not_reset_validated_artifact(tmp_path):
    conn = db.init_db(str(tmp_path / "state.db"))
    store = StateStore(conn)
    store.register_database("kb", "package")
    store.record_received("kb", "r1", "export-42")
    conn.execute(
        "UPDATE artifacts SET status='validated', validated_at='t1', "
        "checksum='hash-r1', package_path='/accepted/export-42' WHERE id='export-42'"
    )
    conn.commit()
    store.record_received("kb", "r1", "export-42")
    row = conn.execute("SELECT * FROM artifacts WHERE id='export-42'").fetchone()
    assert row["status"] == "validated", dict(row)
    conn.close()


def test_failed_received_update_cannot_be_committed_by_next_call(tmp_path):
    conn = db.init_db(str(tmp_path / "state.db"))
    store = StateStore(conn)
    store.register_database("kb", "package")
    store.record_received("kb", "r1", "export-1")
    conn.executescript(
        "CREATE TRIGGER fail_artifact BEFORE INSERT ON artifacts "
        "BEGIN SELECT RAISE(ABORT, 'injected artifact failure'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.record_received("kb", "r2", "export-2")
    store.register_database("ubd", "git")
    assert store.get_database("kb")["received_revision"] == "r1"
    assert not conn.in_transaction
    conn.close()


def test_future_schema_is_refused(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=3")
    conn.close()
    with pytest.raises(ValueError, match="unsupported"):
        db.init_db(str(path))


def test_building_layer_keeps_last_successful_applied_revision(tmp_path):
    conn = db.init_db(str(tmp_path / "state.db"))
    store = StateStore(conn)
    store.register_database("ubd", "git")
    store.mark_layer("ubd", "graph", "r1", "ready", "v1", "i1")
    store.mark_layer("ubd", "graph", "r2", "building", None, None)
    row = conn.execute(
        "SELECT * FROM layer_state WHERE database='ubd' AND layer='graph'"
    ).fetchone()
    assert row["status"] == "building"
    assert row["applied_revision"] == "r1", dict(row)
    conn.close()


def test_opl_without_graph_is_cold_start_not_recovery(tmp_path):
    conn = db.init_db(str(tmp_path / "state.db"))
    store = StateStore(conn)
    store.register_database("klon_opl", "package")
    assert store.required_recovery("klon_opl") is False
    store.mark_layer("klon_opl", "code_index", "r1", "ready", "vol", "i1")
    store.mark_published("klon_opl", "r1")
    assert store.required_recovery("klon_opl") is False
    conn.close()


def test_layer_identity_is_independent_between_databases(tmp_path):
    conn = db.init_db(str(tmp_path / "state.db"))
    store = StateStore(conn)
    for database, revision in [("ubd", "git-sha"), ("kb", "package-hash")]:
        store.register_database(database, "git" if database == "ubd" else "package")
        store.mark_layer(database, "graph", revision, "ready", database + "-vol", "i1")
    rows = {
        row["database"]: row["applied_revision"]
        for row in conn.execute("SELECT * FROM layer_state")
    }
    assert rows == {"ubd": "git-sha", "kb": "package-hash"}
    conn.close()


def test_reinitializing_v2_preserves_published_and_artifact_state(tmp_path):
    path = tmp_path / "state.db"
    conn = db.init_db(str(path))
    store = StateStore(conn)
    store.register_database("kb", "package")
    store.record_received("kb", "r1", "export-1")
    store.record_staged("kb", "r1")
    store.mark_published("kb", "r1")
    conn.close()
    conn = db.init_db(str(path))
    assert StateStore(conn).get_database("kb")["published_revision"] == "r1"
    assert (
        conn.execute("SELECT revision FROM artifacts WHERE id='export-1'").fetchone()[0]
        == "r1"
    )
    conn.close()
