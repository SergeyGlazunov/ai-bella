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


def test_prune_run_history_keeps_unfinished(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = init_db(str(db_path))
    try:
        old = "2020-01-01T00:00:00Z"
        conn.execute(
            "INSERT INTO runs (id, database, target_commit, started_at, finished_at,"
            " result) VALUES (1, 'ubd', 'a', ?, '2020-01-01T01:00:00Z', 'failed')",
            (old,),
        )
        conn.execute(
            "INSERT INTO runs (id, database, target_commit, started_at)"
            " VALUES (2, 'ubd', 'b', ?)",
            (old,),
        )
        conn.execute(
            "INSERT INTO jobs (run_id, stage, status) VALUES (1, 'graph', 'ok')"
        )
        conn.execute(
            "INSERT INTO jobs (run_id, stage, status) VALUES (2, 'graph', 'running')"
        )
        conn.commit()

        deleted = maintenance.prune_run_history(conn, days=90)
        assert deleted >= 1
        remaining_runs = {
            row["id"] for row in conn.execute("SELECT id FROM runs")
        }
        remaining_jobs = {
            row["run_id"] for row in conn.execute("SELECT run_id FROM jobs")
        }
        # Finished old run+job deleted; unfinished run and its job survive.
        assert remaining_runs == {2}
        assert remaining_jobs == {2}
    finally:
        conn.close()
