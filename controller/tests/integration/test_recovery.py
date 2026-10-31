"""Task 11: recovery and failure acceptance (fake Docker, temp SQLite)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from contour.db import init_db
from contour.index_graph import (
    index_graph,
    is_graph_full_needed,
    set_graph_full_needed,
)
from contour.packages import PackageError, validate_package
from contour.pipeline import run_database_pipeline
from contour.reconcile import WriterStateUnknown, reap_jobs
from contour.state import StateStore
from contour.verify import Samples

from test_index_graph import FakeDockerRunner, _init_run, _make_config
from test_packages import _make_package

from .conftest import (
    FakeLayers,
    FakeProbes,
    FakeSource,
    RecordingReads,
    database_config,
    load_v2_runtime_config,
    prepared_source,
    run_publish_cycle,
)


def test_corrupted_package_is_refused(tmp_path: Path):
    pkg = _make_package(tmp_path, name="bad", checksum="0" * 64)
    with pytest.raises(PackageError, match="checksum"):
        validate_package(pkg, frozenset({"kb"}))


def test_incomplete_package_without_ready_is_ignored_by_intake(tmp_path: Path):
    from contour.cli import main

    cfg, cfg_path = load_v2_runtime_config(tmp_path)
    inbox = Path(cfg.databases["kb"].source.inbox)
    _make_package(inbox, name="incomplete", database="kb", ready=False)
    rc = main(
        [
            "--config",
            str(cfg_path),
            "--db",
            str(tmp_path / "state.db"),
            "--lock",
            str(tmp_path / "sync.lock"),
            "intake",
            "--database",
            "kb",
        ]
    )
    assert rc != 0
    accepted = list((Path(cfg.runtime.root) / "accepted" / "kb").glob("ready-*"))
    assert accepted == []


def test_crash_after_source_activation_reopens_reads(tmp_path: Path):
    conn, state, _db, prepared, reads, _probes, result = run_publish_cycle(
        tmp_path,
        "kb",
        layers=frozenset({"code_index", "graph"}),
        layer_runner=FakeLayers(fail_after=1),
    )
    try:
        assert result.result == "failed"
        assert reads.is_closed() is False
        assert prepared.published_path.is_dir()
        assert (prepared.published_path / "data.txt").is_file()
        assert state.get_database("kb")["published_revision"] is None
    finally:
        conn.close()


def test_resumed_completed_container_job_is_reaped_idle(tmp_path: Path):
    conn = init_db(str(tmp_path / "state.db"))
    try:
        conn.execute(
            "INSERT INTO runs (database, target_revision, started_at) VALUES (?,?,?)",
            ("ubd", "abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (database, run_id, stage, status, container_id)
            VALUES ('ubd', 1, 'graph', 'created', 'cid-done')
            """
        )
        conn.commit()

        class WaitDocker:
            def run(self, cmd: list[str], *, timeout: float | None = None):
                if cmd[:2] == ["docker", "inspect"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="exited\n", stderr=""
                    )
                if cmd[:2] == ["docker", "wait"]:
                    return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
                if cmd[:2] == ["docker", "logs"]:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="reaped\n", stderr=""
                    )
                raise AssertionError(cmd)

        assert reap_jobs(conn, WaitDocker()) == "idle"
        row = conn.execute(
            "SELECT status, exit_code, finished_at, log_path FROM jobs WHERE id=1"
        ).fetchone()
        assert row["status"] == "ok"
        assert row["exit_code"] == 0
        assert row["finished_at"]
        assert row["log_path"]
    finally:
        conn.close()


def test_unknown_writer_refusal_blocks_new_work(tmp_path: Path):
    conn = init_db(str(tmp_path / "state.db"))
    try:
        conn.execute(
            "INSERT INTO runs (database, target_revision, started_at) VALUES (?,?,?)",
            ("ubd", "abc", "2026-09-20T00:00:00Z"),
        )
        conn.execute(
            """
            INSERT INTO jobs (database, run_id, stage, status, container_id)
            VALUES ('ubd', 1, 'graph', 'created', 'cid-unknown')
            """
        )
        conn.commit()

        class MysteryDocker:
            def run(self, cmd: list[str], *, timeout: float | None = None):
                if cmd[:2] == ["docker", "inspect"]:
                    raise RuntimeError("engine unavailable")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with pytest.raises(WriterStateUnknown):
            reap_jobs(conn, MysteryDocker())
    finally:
        conn.close()


def test_graph_exit_3_then_full_clears_flag(tmp_path: Path):
    cfg = _make_config(tmp_path)
    conn, run_id = _init_run(tmp_path)
    runner = FakeDockerRunner(wait_codes=[3, 0], conn=conn)
    try:
        index_graph(
            cfg,
            conn,
            run_id,
            runner=runner,
            may_write_decision="ok",
            remaining_budget=60.0,
        )
        assert is_graph_full_needed(conn, "ubd") is False
        stages = [
            row["stage"]
            for row in conn.execute(
                "SELECT stage FROM jobs WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]
        assert stages == ["graph", "graph_full"]
        codes = [
            row["exit_code"]
            for row in conn.execute(
                "SELECT exit_code FROM jobs WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]
        assert codes == [3, 0]
    finally:
        conn.close()


def test_failed_mandatory_bsl_stage_blocks_publish(tmp_path: Path):
    conn, state, _db, _prepared, reads, probes, result = run_publish_cycle(
        tmp_path,
        "klon_opl",
        layers=frozenset({"code_index"}),
        layer_runner=FakeLayers(fail_layer="code_index"),
    )
    try:
        assert result.result == "failed"
        assert "code_index" in result.message
        assert state.get_database("klon_opl")["published_revision"] is None
        assert reads.is_closed() is False
        assert probes.ran is False
        row = conn.execute(
            "SELECT status FROM layer_state WHERE database=? AND layer=?",
            ("klon_opl", "code_index"),
        ).fetchone()
        assert row is not None
        assert row["status"] == "failed"
    finally:
        conn.close()


def test_reads_do_not_reopen_before_probes_pass(tmp_path: Path):
    order: list[str] = []

    class OrderedReads(RecordingReads):
        def open(self) -> None:
            order.append("open")
            super().open()

    class OrderedProbes(FakeProbes):
        def run_before_open(self, samples: Samples, required_layers=None):
            order.append("probes")
            assert ordered_reads.is_closed() is True
            return super().run_before_open(samples, required_layers=required_layers)

    ordered_reads = OrderedReads()
    conn, _state, _database, _prepared, _r, probes, result = run_publish_cycle(
        tmp_path,
        "ubd",
        layers=frozenset({"code_index", "graph"}),
        kind="git",
        reads=ordered_reads,
        probes=OrderedProbes(),
    )
    try:
        assert result.result == "published"
        assert order == ["probes", "open"]
        assert probes.ran is True
    finally:
        conn.close()


def test_probe_failure_reopens_reads_and_stays_unpublished(tmp_path: Path):
    conn, state, _db, _prepared, reads, probes, result = run_publish_cycle(
        tmp_path,
        "kb",
        layers=frozenset({"code_index", "graph"}),
        probes=FakeProbes(fail=True),
    )
    try:
        assert result.result == "failed"
        assert reads.is_closed() is False
        assert state.get_database("kb")["published_revision"] is None
        assert probes.ran is True
    finally:
        conn.close()


def test_saved_graph_full_flag_is_passed_into_layers(tmp_path: Path):
    conn = init_db(str(tmp_path / "state.db"))
    state = StateStore(conn)
    state.register_database("kb", "package")
    set_graph_full_needed(conn, "kb", True)
    database = database_config(
        tmp_path, "kb", layers=frozenset({"code_index", "graph"})
    )
    prepared = prepared_source(tmp_path, "kb", "r-full")
    layers = FakeLayers()
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        RecordingReads(),
        layers,
        FakeProbes(),
        conn=conn,
        samples=Samples(bsl_paths=["a.bsl"], xml_paths=[], deletions=[]),
        after_activate=lambda *_: None,
    )
    try:
        assert result.result == "published"
        assert layers.seen_full_needed is True
    finally:
        conn.close()
