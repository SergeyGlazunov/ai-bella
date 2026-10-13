from pathlib import Path

import pytest

from contour.db import init_db
from contour.state import StateStore


def _store(tmp_path: Path) -> tuple[StateStore, object]:
    conn = init_db(str(tmp_path / "state.db"))
    return StateStore(conn), conn


def test_register_and_get_database(tmp_path: Path):
    store, conn = _store(tmp_path)
    try:
        store.register_database("ubd", "git")
        row = store.get_database("ubd")
        assert row["database"] == "ubd"
        assert row["source_kind"] == "git"
        assert row["published_revision"] is None or row["published_revision"] == ""
    finally:
        conn.close()


def test_unknown_database_rejected(tmp_path: Path):
    store, conn = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="unknown database"):
            store.get_database("nope")
        with pytest.raises(ValueError, match="unknown database"):
            store.record_received("nope", "r1", None)
    finally:
        conn.close()


def test_run_records_database_and_target_revision(tmp_path: Path):
    store, conn = _store(tmp_path)
    try:
        store.register_database("kb", "package")
        run_id = store.start_run("kb", "export-42")
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row["database"] == "kb"
        assert row["target_revision"] == "export-42"
    finally:
        conn.close()


def test_published_revision_changes_only_after_mark_published(tmp_path: Path):
    store, conn = _store(tmp_path)
    try:
        store.register_database("ubd", "git")
        store.record_received("ubd", "rev-a", artifact_id=None)
        store.record_staged("ubd", "rev-a")
        store.mark_layer(
            "ubd", "graph", "rev-a", "ready", storage_id="g", indexer_version="1"
        )
        store.mark_layer(
            "ubd",
            "code_index",
            "rev-a",
            "ready",
            storage_id="c",
            indexer_version="1",
        )
        before = store.get_database("ubd")
        assert before["published_revision"] in (None, "")
        assert before["staged_revision"] == "rev-a"
        assert before["received_revision"] == "rev-a"

        store.mark_published("ubd", "rev-a")
        after = store.get_database("ubd")
        assert after["published_revision"] == "rev-a"
    finally:
        conn.close()


def test_required_recovery_when_staged_not_published(tmp_path: Path):
    store, conn = _store(tmp_path)
    try:
        store.register_database("ubd", "git")
        assert store.required_recovery("ubd") is False
        store.record_staged("ubd", "rev-b")
        assert store.required_recovery("ubd") is True
        store.mark_published("ubd", "rev-b")
        assert store.required_recovery("ubd") is False
    finally:
        conn.close()
