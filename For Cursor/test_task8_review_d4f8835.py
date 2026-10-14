"""Task 8 operational review. No real Docker or stand paths are used."""
from dataclasses import replace
from pathlib import Path
import sys

import pytest
import contour.pipeline as pipeline
from contour import cli, dockerutil
from contour.db import init_db, set_meta, get_meta
from contour.lock import MemoryBackend
from contour.reads import DatabaseReadController, scoped_read_key
from contour.reconcile import reconcile, writer_blocks_database, format_nightly_alert
from contour.sources import PublishedRecoveryProvider, write_revision, read_revision
from contour.profiles import run_required_layers
from contour.verify import Samples

sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[2] / "tests"))
from test_reconcile_multidb import _v2_config, _in_window, _out_of_window, PingDocker, ScriptedDocker, _insert_run_job
from test_task8_sources import _runtime_cfg
from test_pipeline import _setup, FakeSource, FakeLayers, FakeProbes
from test_reads import FlexibleReadsDocker
from test_index_graph import FakeDockerRunner


def test_default_runner_handles_empty_inbox_without_placeholder(tmp_path):
    cfg, _, _ = _runtime_cfg(tmp_path)
    messages = []
    result = reconcile(cfg, timer=False, measure=False, now=_in_window(),
        docker=PingDocker(), lock_backend=MemoryBackend(), alert_send=lambda cfg, text: messages.append(text))
    assert result != "failed", messages
    assert not any("not wired" in message for message in messages)


def test_failed_alert_does_not_abort_remaining_databases(tmp_path):
    cfg = _v2_config(tmp_path)
    seen = []
    def run(db, **kwargs):
        seen.append(db.name)
        if db.name == "kb":
            raise RuntimeError("KB indexing failed")
        return "success"
    def broken_alert(*args):
        raise RuntimeError("notification service unavailable")
    escaped = None
    try:
        reconcile(cfg, timer=False, measure=False, now=_in_window(), docker=PingDocker(),
            lock_backend=MemoryBackend(), database_runner=run, alert_send=broken_alert)
    except RuntimeError as exc:
        escaped = str(exc)
    assert seen == ["ubd", "kb", "klon_opl"], (seen, escaped)


def test_open_database_filter_reaches_the_actual_operation(tmp_path, monkeypatch):
    cfg = _v2_config(tmp_path)
    captured = []
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    def reopen(config, **kwargs):
        captured.append((config, kwargs))
        return "success"
    monkeypatch.setattr(cli, "reopen_reads", reopen)
    cli.main(["--config", "unused.yaml", "open", "--database", "kb"])
    assert captured
    config, kwargs = captured[0]
    assert kwargs.get("database") == "kb" or config.services == cfg.databases["kb"].services, (
        "KB selection disappeared; legacy opener receives primary UBD config", kwargs
    )


def test_docker_wait_has_bounded_observation_budget():
    class Runner:
        def run(self, cmd, *, timeout=None):
            assert timeout is not None and timeout <= 300, "docker wait can block forever"
            return type("R", (), {"stdout": "0\n", "returncode": 0})()
    dockerutil.wait("owned-job", runner=Runner())


def test_bsl_clean_checks_window_again_after_index(tmp_path):
    conn, _, database, _ = _setup(tmp_path, "klon_opl", frozenset({"code_index"}))
    conn.execute("INSERT INTO runs (database,target_revision,started_at) VALUES ('klon_opl','r1','now')")
    conn.commit()
    class Runner(FakeDockerRunner):
        window_open = True
        def wait_exec(self, container, exec_id):
            result = super().wait_exec(container, exec_id)
            self.window_open = False
            return result
    runner = Runner(conn=conn)
    try:
        run_required_layers(database, revision="r1", run_id=1, conn=conn, runner=runner,
            may_write=lambda: "ok" if runner.window_open else "deferred_window")
    except Exception:
        pass
    assert not any("clean" in cmd for cmd in runner.calls if cmd[0] == "create_exec"), runner.calls


def test_pipeline_preserves_one_previously_closed_layer(tmp_path):
    conn, state, database, prepared = _setup(tmp_path, "kb", frozenset({"code_index", "graph"}))
    set_meta(conn, scoped_read_key("reads_closed", "kb", "graph"), "1")
    docker = FlexibleReadsDocker(graph_status="exited", connected_networks={database.services.webui_network})
    reads = DatabaseReadController(database, conn, docker)
    # Ordinary post-prepare damage causes activation failure before index writes.
    (prepared.staged_path / "data.txt").write_text("incomplete copy", encoding="utf-8")
    result = pipeline.run_database_pipeline(database, FakeSource(prepared), state,
        reads, FakeLayers(), FakeProbes(), conn=conn, samples=Samples(["x.bsl"], [], []))
    assert result.result == "failed"
    assert docker.graph_status == "exited", "Recovery-closed Graph was reopened on another layer's pre-activation failure"


def test_ubd_open_clears_or_migrates_legacy_closed_state(tmp_path):
    cfg = _v2_config(tmp_path)
    conn = init_db(str(tmp_path / "state.db"))
    set_meta(conn, "reads_closed", "1")
    docker = FlexibleReadsDocker(graph_status="exited", connected_networks=set())
    reads = DatabaseReadController(cfg.databases["ubd"], conn, docker)
    reads.open()
    assert not reads.is_closed(), "Reads physically reopened but legacy flag keeps reporting closed"


def test_read_close_requires_idle_indexer(tmp_path):
    cfg = _v2_config(tmp_path)
    conn = init_db(str(tmp_path / "state.db"))
    class BusyDocker(FlexibleReadsDocker):
        def idle_probe(self):
            return False
    docker = BusyDocker()
    reads = DatabaseReadController(cfg.databases["klon_opl"], conn, docker)
    with pytest.raises(Exception):
        reads.close()


def test_shared_bsl_container_blocks_writer_even_with_distinct_volume_names(tmp_path):
    cfg = _v2_config(tmp_path)
    conn = init_db(str(tmp_path / "state.db"))
    ubd = cfg.databases["ubd"]
    kb = cfg.databases["kb"]
    kb = replace(kb, services=replace(kb.services, bsl_container=ubd.services.bsl_container),
        storage=replace(kb.storage, code_index_volume="other-c", graph_volume="other-g"))
    _insert_run_job(conn, database="ubd", stage="bsl_index", status="running", container_id=ubd.services.bsl_container)
    reason = writer_blocks_database(conn, kb, [ubd, kb], ScriptedDocker(inspect_status="running"))
    assert reason is not None


def test_recovery_rejects_revision_that_does_not_match_saved_source(tmp_path):
    root = tmp_path / "sources"
    published = root / "kb"
    published.mkdir(parents=True)
    staging = root / ".staging"
    staging.mkdir()
    write_revision(published, "actual-r2")
    (published / "Module.bsl").write_text("// r2 bytes", encoding="utf-8")
    provider = PublishedRecoveryProvider("kb", published_path=published, staging_root=staging,
        sources_root=root, revision="requested-r1")
    with pytest.raises(ValueError):
        provider.prepare()
    assert read_revision(published) == "actual-r2"


def test_completed_jobs_are_reaped_even_outside_write_window(tmp_path):
    cfg = _v2_config(tmp_path)
    conn = init_db(cfg.runtime.db_path)
    _insert_run_job(conn, database="kb", stage="graph", status="running", container_id="kb-finished")
    reconcile(cfg, timer=True, measure=False, now=_out_of_window(), docker=ScriptedDocker(),
        lock_backend=MemoryBackend(), database_runner=lambda db, **kwargs: "success", alert_send=lambda *args: None)
    row = conn.execute("SELECT status,exit_code FROM jobs WHERE database='kb'").fetchone()
    assert row["status"] == "ok" and row["exit_code"] == 0, dict(row)


def test_alert_does_not_attach_previous_run_job_log(tmp_path):
    conn = init_db(str(tmp_path / "state.db"))
    _insert_run_job(conn, database="kb", stage="graph_full", status="ok", container_id="old-job")
    conn.execute("UPDATE jobs SET log_path='old-success.log'")
    conn.execute("INSERT INTO runs (database,target_revision,started_at,result,message) VALUES ('kb','(pending)','now','failed','stage=prepare fetch failed')")
    conn.commit()
    message = format_nightly_alert(conn, [("kb", "failed")])
    assert "old-success.log" not in message and "stage=graph_full" not in message, message


def test_newest_package_compares_instants_not_timestamp_text(tmp_path):
    from test_packages import _make_package
    from contour.packages import choose_newest_valid_package
    _make_package(tmp_path, name="ready-older", database="klon_opl", export_id="older",
        manifest_overrides={"finished_at": "2026-10-14T10:01:00+10:00"})
    _make_package(tmp_path, name="ready-newer", database="klon_opl", export_id="newer",
        manifest_overrides={"finished_at": "2026-10-14T01:01:00Z"})
    chosen, warnings = choose_newest_valid_package(tmp_path, "klon_opl")
    assert not warnings
    assert chosen.manifest.export_id == "newer", chosen.manifest.finished_at
