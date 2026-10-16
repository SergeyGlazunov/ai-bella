from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contour import maintenance
from contour.db import init_db


def _accepted_pkg(root: Path, name: str, *, finished_at: str, revision: str) -> Path:
    path = root / name
    (path / "src").mkdir(parents=True, exist_ok=True)
    checksum, _, export_id = revision.partition(":")
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "finished_at": finished_at,
                "checksum": checksum,
                "export_id": export_id,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_check_free_space_passes_and_fails(tmp_path: Path):
    maintenance.check_free_space(tmp_path, min_free_gb=0.000001)
    with pytest.raises(RuntimeError, match="below threshold"):
        maintenance.check_free_space(tmp_path, min_free_gb=10**9)


def test_prune_accepted_keeps_newest_and_published(tmp_path: Path):
    root = tmp_path / "accepted" / "kb"
    root.mkdir(parents=True)
    pub_rev = "deadbeef:old-export"
    _accepted_pkg(root, "ready-old", finished_at="2026-10-01T00:00:00Z", revision=pub_rev)
    _accepted_pkg(root, "ready-b", finished_at="2026-10-05T00:00:00Z", revision="b:b")
    _accepted_pkg(root, "ready-c", finished_at="2026-10-10T00:00:00Z", revision="c:c")
    _accepted_pkg(root, "ready-d", finished_at="2026-10-15T00:00:00Z", revision="d:d")
    # Unparseable candidate stays (operator decides).
    (root / "ready-odd").mkdir()
    (root / "ready-odd" / "src").mkdir()
    (root / "not-ready").mkdir()

    removed = maintenance.prune_accepted_packages(
        root, published_revision=pub_rev, keep=2
    )
    # Sorted desc: d, c, b, old → beyond keep=2: b, old. old is published →
    # only b is removed.
    assert removed == ["ready-b"]
    assert (root / "ready-old").exists()
    assert (root / "ready-c").exists()
    assert (root / "ready-d").exists()
    assert (root / "ready-odd").exists()
    assert (root / "not-ready").exists()


def test_prune_accepted_noop_when_within_keep(tmp_path: Path):
    root = tmp_path / "accepted" / "kb"
    root.mkdir(parents=True)
    _accepted_pkg(root, "ready-a", finished_at="2026-10-01T00:00:00Z", revision="a:a")
    _accepted_pkg(root, "ready-b", finished_at="2026-10-02T00:00:00Z", revision="b:b")
    assert (
        maintenance.prune_accepted_packages(root, published_revision=None, keep=3)
        == []
    )
    assert maintenance.prune_accepted_packages(
        tmp_path / "missing", published_revision=None, keep=3
    ) == []


def test_prune_previous_sources_keeps_newest_handle(tmp_path: Path):
    parent = tmp_path / "sources"
    parent.mkdir()
    published = parent / "kb"
    published.mkdir()
    old_prev = parent / "kb.prev-old"
    new_prev = parent / "kb.prev-new"
    for path in (old_prev, new_prev):
        path.mkdir()
        (path / "src").mkdir()
    # old_prev is older by mtime.
    past = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(old_prev, (past, past))

    removed = maintenance.prune_previous_sources(published, keep=1)
    assert removed == ["kb.prev-old"]
    assert new_prev.exists()
    assert published.exists()


def test_prune_run_history_protects_unfinished_runs(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = init_db(str(db_path))
    try:
        old_finished = "2020-01-01T01:00:00Z"
        # ubd: unfinished run keeps the whole database history alive,
        # including its old finished failed run (BSL resume evidence).
        conn.execute(
            "INSERT INTO runs (id, database, target_commit, started_at,"
            " finished_at, result) VALUES (1, 'ubd', 'a', '2020-01-01T00:00:00Z',"
            " ?, 'failed')",
            (old_finished,),
        )
        conn.execute(
            "INSERT INTO jobs (run_id, stage, status) VALUES (1, 'bsl_index', 'ok')"
        )
        conn.execute(
            "INSERT INTO runs (id, database, target_commit, started_at)"
            " VALUES (2, 'ubd', 'b', '2020-01-02T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO jobs (run_id, stage, status) VALUES (2, 'bsl_clean', 'running')"
        )
        # kb: healthy database, old finished run → dead history.
        conn.execute(
            "INSERT INTO runs (id, database, target_commit, started_at,"
            " finished_at, result) VALUES (3, 'kb', 'c', '2020-01-01T00:00:00Z',"
            " ?, 'published')",
            (old_finished,),
        )
        conn.execute(
            "INSERT INTO jobs (run_id, stage, status) VALUES (3, 'bsl_index', 'ok')"
        )
        conn.commit()

        maintenance.prune_run_history(conn, days=90)
        remaining_runs = {row["id"] for row in conn.execute("SELECT id FROM runs")}
        remaining_jobs = {
            row["run_id"] for row in conn.execute("SELECT run_id FROM jobs")
        }
        # Unfinished ubd run and its sibling finished run both stay;
        # healthy kb history is deleted together with its job.
        assert remaining_runs == {1, 2}
        assert remaining_jobs == {1, 2}
    finally:
        conn.close()


def test_prune_run_history_keeps_unfinished_run_before_first_job():
    conn = init_db(":memory:")
    try:
        conn.execute(
            "INSERT INTO runs(id,database,target_revision,started_at)"
            " VALUES(1,'kb','r','2020-01-01T00:00:00Z')"
        )
        conn.commit()
        maintenance.prune_run_history(conn)
        assert conn.execute("SELECT id FROM runs WHERE id=1").fetchone() is not None
    finally:
        conn.close()


def test_prune_run_history_keeps_completed_stage_of_unfinished_run():
    conn = init_db(":memory:")
    try:
        conn.execute(
            "INSERT INTO runs(id,database,target_revision,started_at)"
            " VALUES(1,'ubd','r','2020-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO jobs(id,run_id,database,stage,status)"
            " VALUES(1,1,'ubd','bsl_index','ok')"
        )
        conn.execute(
            "INSERT INTO jobs(id,run_id,database,stage,status)"
            " VALUES(2,1,'ubd','bsl_clean','running')"
        )
        conn.commit()
        maintenance.prune_run_history(conn)
        assert conn.execute("SELECT id FROM jobs WHERE id=1").fetchone() is not None
    finally:
        conn.close()


def test_prune_run_history_protects_recovery_databases(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = init_db(str(db_path))
    try:
        conn.execute(
            "INSERT INTO runs (id, database, target_commit, started_at,"
            " finished_at, result) VALUES (1, 'kb', 'a', '2020-01-01T00:00:00Z',"
            " '2020-01-01T01:00:00Z', 'failed')"
        )
        conn.execute(
            "INSERT INTO layer_state (database, layer, status, updated_at)"
            " VALUES ('kb', 'code_index', 'failed', '2020-01-01T00:00:00Z')"
        )
        conn.commit()
        maintenance.prune_run_history(conn, days=90)
        assert conn.execute("SELECT id FROM runs WHERE id=1").fetchone() is not None
    finally:
        conn.close()


def test_prune_accepted_preserves_real_published_package(tmp_path: Path):
    from test_packages import _make_package
    from contour.packages import validate_package

    root = tmp_path / "accepted"
    root.mkdir()
    published = _make_package(
        root, name="ready-published", database="kb", export_id="published"
    )
    revision = validate_package(published, frozenset({"kb"})).revision
    for i in range(4):
        path = _make_package(
            root, name=f"ready-new-{i}", database="kb", export_id=f"new-{i}"
        )
        manifest = path / "manifest.json"
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["finished_at"] = f"2030-01-0{i + 1}T00:00:00Z"
        manifest.write_text(json.dumps(raw), encoding="utf-8")

    removed = maintenance.prune_accepted_packages(
        root, published_revision=revision, keep=3
    )
    assert len(removed) == 1
    assert published.exists()

