"""Operational regressions for be6f3a2; run with the controller installed.

python -m pytest "../For Cursor/test_task6_review.py" -q
Run from controller/ in the target project. No live Docker is used.
"""
from pathlib import Path
import subprocess
import sys

import pytest
import contour.profiles as profiles
from contour.db import init_db

# Reuse the project's injectable Docker backend and profile fixtures.
sys.path.insert(0, str(Path(profiles.__file__).resolve().parents[2] / "tests"))
from test_index_graph import FakeDockerRunner
from test_profiles import _db


class IdentityRunner(FakeDockerRunner):
    generation = "first"

    def run(self, cmd, *, timeout=None):
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{cmd[-1]}|created-{self.generation}\n", stderr=""
            )
        if cmd[:2] == ["docker", "inspect"] and "{{.Image}}" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"sha256:{self.generation}\n", stderr=""
            )
        return super().run(cmd, timeout=timeout)


def new_run(conn, database, revision="rev-1"):
    cur = conn.execute(
        "INSERT INTO runs (database, target_revision, started_at) VALUES (?,?,?)",
        (database, revision, "2026-10-14T00:00:00Z"),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.mark.parametrize("layer", ["code_index", "graph"])
@pytest.mark.parametrize("field", ["storage_id", "indexer_version"])
def test_result_tracks_actual_storage_and_writer(tmp_path, layer, field):
    conn = init_db(str(tmp_path / "state.db"))
    database = _db("kb", frozenset({"code_index", "graph"}))
    runner = IdentityRunner(conn=conn)
    adapter = profiles.run_bsl_layer if layer == "code_index" else profiles.run_graph_layer
    first = adapter(database, "rev-1", new_run(conn, "kb"), conn, runner=runner)
    # A recreated volume / updated indexer, with unchanged configuration names
    # and source revision, must not look like the same successful index.
    runner.generation = "second"
    second = adapter(database, "rev-1", new_run(conn, "kb"), conn, runner=runner)
    assert getattr(first, field) != getattr(second, field)


def test_ubd_success_preserves_kb_pending_full(tmp_path):
    conn = init_db(str(tmp_path / "state.db"))
    kb = _db("kb", frozenset({"code_index", "graph"}))
    ubd = _db("ubd", frozenset({"code_index", "graph"}))
    with pytest.raises(RuntimeError, match="FULL skipped"):
        profiles.run_graph_layer(
            kb, "rev-1", new_run(conn, "kb"), conn,
            runner=FakeDockerRunner(conn=conn, wait_codes=[3]),
            may_write=lambda: "deferred_window",
        )
    before = dict(conn.execute("SELECT key, value FROM meta"))
    pending = {key: value for key, value in before.items()
               if "graph_full_needed" in key and value == "1"}
    assert pending, "The pending KB FULL must survive for recovery"
    profiles.run_graph_layer(
        ubd, "rev-1", new_run(conn, "ubd"), conn,
        runner=FakeDockerRunner(conn=conn, wait_codes=[0]),
    )
    after = dict(conn.execute("SELECT key, value FROM meta"))
    assert all(after.get(key) == value for key, value in pending.items()), (
        "UBD success cleared another database's pending FULL", before, after
    )


def test_successful_kb_full_clears_only_kb_flag(tmp_path):
    conn = init_db(str(tmp_path / "state.db"))
    kb = _db("kb", frozenset({"code_index", "graph"}))
    ubd = _db("ubd", frozenset({"code_index", "graph"}))
    with pytest.raises(RuntimeError, match="FULL skipped"):
        profiles.run_graph_layer(
            ubd, "rev-1", new_run(conn, "ubd"), conn,
            runner=FakeDockerRunner(conn=conn, wait_codes=[3]),
            may_write=lambda: "deferred_window",
        )
    with pytest.raises(RuntimeError, match="FULL skipped"):
        profiles.run_graph_layer(
            kb, "rev-1", new_run(conn, "kb"), conn,
            runner=FakeDockerRunner(conn=conn, wait_codes=[3]),
            may_write=lambda: "deferred_window",
        )
    profiles.run_graph_layer(
        kb, "rev-2", new_run(conn, "kb", "rev-2"), conn,
        runner=FakeDockerRunner(conn=conn, wait_codes=[3, 0]),
        may_write=lambda: "ok",
    )
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    assert meta.get("graph_full_needed") == "1"
    assert meta.get("graph_full_needed:kb", "") != "1"


@pytest.mark.parametrize("name", ["ubd", "kb", "klon_opl"])
def test_all_profiles_record_correct_jobs_and_layer_order(tmp_path, name):
    conn = init_db(str(tmp_path / "state.db"))
    layers = frozenset({"code_index"} if name == "klon_opl"
                       else {"code_index", "graph"})
    database = _db(name, layers)
    runner = FakeDockerRunner(conn=conn)
    run_id = new_run(conn, name)
    results = profiles.run_required_layers(
        database, revision="rev-1", run_id=run_id, conn=conn, runner=runner,
    )
    assert [result.layer for result in results] == (
        ["code_index"] if name == "klon_opl" else ["code_index", "graph"]
    )
    rows = conn.execute(
        "SELECT j.database, j.stage, j.status, r.target_revision "
        "FROM jobs j JOIN runs r ON r.id=j.run_id WHERE j.run_id=? ORDER BY j.id",
        (run_id,),
    ).fetchall()
    expected_stages = ["bsl_index", "bsl_clean"]
    if name != "klon_opl":
        expected_stages.append("graph")
    assert [row["stage"] for row in rows] == expected_stages
    assert all(row["database"] == name and row["status"] == "ok"
               and row["target_revision"] == "rev-1" for row in rows)
    assert all(exec_id == saved for exec_id, saved
               in runner.job_id_at_exec_start.items())
    assert all(cid == saved for cid, saved in runner.job_id_at_start.items())
