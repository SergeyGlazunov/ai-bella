"""Business-critical recovery checks for 471addd. No live Docker calls."""
import sys
import subprocess
from pathlib import Path

import pytest
import contour.pipeline as pipeline
sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[2] / 'tests'))
import contour.reconcile as rec
import contour.reads as reads_mod
import contour.adapters as adapters
from contour import cli, dockerutil
from contour.db import init_db
from contour.state import StateStore
from contour.runtime_policy import WriterStillAlive
from contour.lock import MemoryBackend
from test_task8_sources import _runtime_cfg
from test_reconcile_multidb import _v2_config, PingDocker
from test_packages import _make_package
from test_pipeline import RecordingReads, FakeProbes
from test_index_graph import FakeDockerRunner


def runner_setup(tmp_path, monkeypatch, docker=None, probes=None):
    cfg, inbox, published = _runtime_cfg(tmp_path)
    _make_package(inbox, name='ready-one', database='klon_opl', export_id='one',
        extra_files={'src/CommonModules/A/Ext/Module.bsl': b'// module'})
    conn = init_db(cfg.runtime.db_path)
    state = StateStore(conn)
    state.register_database('klon_opl', 'package')
    reads = RecordingReads()
    probes = probes or FakeProbes()
    monkeypatch.setattr(reads_mod, 'DatabaseReadController', lambda *a, **kw: reads)
    monkeypatch.setattr(adapters, 'build_probes', lambda *a, **kw: probes)
    docker = docker or FakeDockerRunner(conn=conn)
    def run():
        return rec._default_v2_database_runner(cfg.databases['klon_opl'], cfg=cfg,
            conn=conn, state=state, docker=docker, may_write=lambda: 'ok')
    return conn, state, reads, probes, docker, run


def test_retry_after_probe_failure_actually_rechecks_and_opens(tmp_path, monkeypatch):
    conn, state, reads, probes, docker, run = runner_setup(tmp_path, monkeypatch,
        probes=FakeProbes(fail=True))
    assert run() == 'failed'
    assert reads.closed
    probes.fail = False
    probes.ran = False
    result = run()
    assert result == 'success' and probes.ran and not reads.closed, (
        result, probes.ran, reads.closed, dict(state.get_database('klon_opl')))


def test_finished_long_bsl_continues_to_clean_without_new_index(tmp_path, monkeypatch):
    class LongBSL(FakeDockerRunner):
        release = False
        def wait_exec(self, container, exec_id):
            if not self.release:
                raise WriterStillAlive('300s observation expired')
            return super().wait_exec(container, exec_id)
    docker = LongBSL()
    conn, state, reads, probes, docker, run = runner_setup(tmp_path, monkeypatch, docker=docker)
    assert run() == 'deferred'
    # The detached writer completes between controller runs; real reaper consumes it.
    docker.release = True
    docker.exec_ids.clear()
    assert rec.reap_database_jobs(conn, 'klon_opl', docker) == 'idle'
    assert conn.execute('SELECT status FROM jobs').fetchone()[0] == 'ok'
    result = run()
    assert result == 'success', conn.execute('SELECT message FROM runs ORDER BY id DESC LIMIT 1').fetchone()[0]
    commands = [call for call in docker.calls if call[0] == 'create_exec']
    assert sum('index' in call for call in commands) == 1, commands
    assert sum('clean' in call for call in commands) == 1, commands


def test_failed_volume_inspect_is_not_ready(tmp_path, monkeypatch):
    cfg, _, _ = _runtime_cfg(tmp_path)
    conn = init_db(cfg.runtime.db_path)
    state = StateStore(conn)
    state.register_database('klon_opl', 'package')
    state.mark_layer('klon_opl', 'code_index', 'r1', 'ready', 'v|old', 'image')
    def missing(*args, **kwargs):
        raise RuntimeError('no such volume')
    monkeypatch.setattr(dockerutil, 'volume_identity', missing)
    assert not rec._layers_ready_with_live_storage(cfg.databases['klon_opl'], 'r1', conn, PingDocker())


def test_kb_idle_and_probe_have_kb_container_environment(tmp_path, monkeypatch):
    cfg = _v2_config(tmp_path)
    monkeypatch.delenv('BSL_CONTAINER', raising=False)
    calls = []
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs.get('env', {})))
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
    monkeypatch.setattr(dockerutil.subprocess, 'run', run)
    docker = rec._docker_for_database(cfg.databases['kb'], dockerutil.SubprocessRunner())
    docker.idle_probe()
    adapters.build_probes(cfg.databases['kb'], docker).code_index.check_bsl('CommonModules/A/Module.bsl')
    assert calls and all(env.get('BSL_CONTAINER') == 'bsl-kb' or 'BSL_CONTAINER=bsl-kb' in cmd
        for cmd, env in calls), calls


def test_profile_reload_failure_is_not_hidden_by_other_database(tmp_path, monkeypatch):
    import contour.watch_bsl as watch
    cfg = _v2_config(tmp_path)
    def broken(db):
        raise RuntimeError('KB daemon reload failed')
    monkeypatch.setattr(watch, 'reload_bsl_daemon', broken)
    reload = rec._watch_reload_for_database(cfg.databases['kb'], lambda: None)
    with pytest.raises(RuntimeError, match='KB daemon'):
        reload()


def test_single_database_open_does_not_crash(tmp_path, monkeypatch):
    cfg, _, _ = _runtime_cfg(tmp_path)
    monkeypatch.setattr(cli, 'load_config', lambda path: cfg)
    seen = []
    monkeypatch.setattr(cli, 'reopen_reads', lambda *a, **kw: (seen.append(kw['database']) or 'success'))
    assert cli.main(['open']) == 0
    assert seen == ['klon_opl']


def test_reaper_inspect_is_bounded():
    class Runner:
        def run(self, cmd, *, timeout=None):
            assert timeout is not None and timeout <= 30, ('unbounded inspect', cmd, timeout)
            return subprocess.CompletedProcess(cmd, 0, stdout='exited', stderr='')
    assert rec._container_status(Runner(), 'job-1') == 'exited'
