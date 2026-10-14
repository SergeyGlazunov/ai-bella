"""Operational follow-up checks. Run against controller at 9b81858; no live Docker."""
import sys
import pytest
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import contour.pipeline as pipeline
sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[2] / 'tests'))
import contour.reconcile as rec
from contour import cli, dockerutil
from contour.db import init_db, set_meta
from contour.state import StateStore
from contour.sources import write_revision
from contour.reads import DatabaseReadController
from contour.lock import MemoryBackend
from test_task8_sources import _runtime_cfg
from test_reconcile_multidb import _v2_config, PingDocker
from test_packages import _make_package
from test_pipeline import _setup
from test_reads import FlexibleReadsDocker


def setup_runner(tmp_path):
    cfg, inbox, published = _runtime_cfg(tmp_path)
    conn = init_db(cfg.runtime.db_path)
    state = StateStore(conn)
    state.register_database('klon_opl', 'package')
    def run():
        return rec._default_v2_database_runner(cfg.databases['klon_opl'], cfg=cfg,
            conn=conn, state=state, docker=PingDocker(), may_write=lambda: 'ok')
    return cfg, inbox, published, conn, state, run


def test_retry_uses_accepted_package_after_first_failure(tmp_path, monkeypatch):
    cfg, inbox, published, conn, state, run = setup_runner(tmp_path)
    _make_package(inbox, name='ready-new', database='klon_opl', export_id='new')
    calls = []
    def failed_pipeline(*args, **kwargs):
        calls.append(args[1])
        return SimpleNamespace(result='failed')
    monkeypatch.setattr(pipeline, 'run_database_pipeline', failed_pipeline)
    assert run() == 'failed'
    run()
    assert len(calls) == 2, 'claimed package was lost to retry: accepted is never inspected'


def test_empty_inbox_recovers_existing_failed_source(tmp_path, monkeypatch):
    cfg, inbox, published, conn, state, run = setup_runner(tmp_path)
    write_revision(published, 'r1')
    state.mark_layer('klon_opl', 'code_index', 'r1', 'failed', None, None)
    assert state.required_recovery('klon_opl')
    calls = []
    monkeypatch.setattr(pipeline, 'run_database_pipeline',
        lambda *a, **kw: (calls.append(a[1]) or SimpleNamespace(result='published')))
    run()
    assert calls, 'recovery is skipped solely because inbox is empty'


def test_previous_inbox_package_is_not_applied_after_newer(tmp_path, monkeypatch):
    cfg, inbox, published, conn, state, run = setup_runner(tmp_path)
    _make_package(inbox, name='ready-old', database='klon_opl', export_id='old',
        manifest_overrides={'finished_at': '2026-10-14T00:01:00Z'})
    _make_package(inbox, name='ready-new', database='klon_opl', export_id='new',
        manifest_overrides={'finished_at': '2026-10-14T00:05:00Z'})
    revisions = []
    def publish(db, source, state, *args, **kwargs):
        prepared = source.prepare()
        revisions.append(prepared.revision)
        write_revision(published, prepared.revision)
        state.mark_layer(db.name, 'code_index', prepared.revision, 'ready', 'vol|old', 'img')
        state.mark_published(db.name, prepared.revision)
        return SimpleNamespace(result='published')
    monkeypatch.setattr(pipeline, 'run_database_pipeline', publish)
    run()
    run()
    assert len(revisions) == 1, f'older pending package applied after newest: {revisions}'


def test_same_revision_checks_live_storage_before_noop(tmp_path, monkeypatch):
    cfg, inbox, published, conn, state, run = setup_runner(tmp_path)
    from contour.packages import choose_newest_valid_package
    _make_package(inbox, name='ready-same', database='klon_opl', export_id='same')
    package, _ = choose_newest_valid_package(inbox, 'klon_opl')
    write_revision(published, package.revision)
    state.mark_layer('klon_opl', 'code_index', package.revision, 'ready', 'vol|old', 'img')
    state.mark_published('klon_opl', package.revision)
    monkeypatch.setattr(dockerutil, 'volume_identity', lambda *a, **kw: 'vol|recreated')
    monkeypatch.setattr(dockerutil, 'image_id', lambda *a, **kw: 'img')
    calls = []
    monkeypatch.setattr(pipeline, 'run_database_pipeline',
        lambda *a, **kw: (calls.append(a[1]) or SimpleNamespace(result='published')))
    run()
    assert calls, 'SQLite ready state hides recreated empty storage'


def test_legacy_closed_state_is_preserved_on_close_failure(tmp_path):
    conn, state, db, prepared = _setup(tmp_path, 'ubd', frozenset({'code_index', 'graph'}))
    set_meta(conn, 'reads_closed', '1')
    docker = FlexibleReadsDocker(graph_status='exited', connected_networks=set())
    ctrl = DatabaseReadController(db, conn, docker)
    ctrl.close_attempted = True
    ctrl.reopen_if_safe_after_pre_activation_failure()
    assert docker.graph_status == 'exited', 'legacy closed state was read before migration'


def test_bsl_observation_uses_same_budget_as_graph(monkeypatch):
    from contour.runtime_policy import WriterStillAlive
    ticks = iter([0, 301, 86401])
    monkeypatch.setattr(dockerutil.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(dockerutil.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(dockerutil, '_exec_inspect', lambda *a: {'Running': True})
    with pytest.raises(WriterStillAlive):
        dockerutil.wait_exec('bsl', 'exec-1', runner=PingDocker())


def test_cli_reload_is_profile_scoped(tmp_path, monkeypatch):
    cfg = _v2_config(tmp_path)
    monkeypatch.setattr(cli, 'load_config', lambda path: cfg)
    seen = []
    monkeypatch.setattr(cli, '_bsl_watch_reload', lambda config: seen.append(config.services.bsl_container))
    def reconcile(config, **kwargs):
        kwargs['watch_reload']()
        return 'success'
    monkeypatch.setattr(cli, 'reconcile', reconcile)
    cli.main(['update', '--database', 'kb'])
    assert seen == [cfg.databases['kb'].services.bsl_container], seen


def test_cli_does_not_share_first_database_idle_adapter(tmp_path, monkeypatch):
    from contour.adapters import CommandEngine
    cfg = _v2_config(tmp_path)
    for name, db in cfg.databases.items():
        cfg.databases[name] = replace(db, adapters=replace(db.adapters, idle_probe=['idle-' + name]))
    monkeypatch.setattr(cli, 'load_config', lambda path: cfg)
    used = []
    monkeypatch.setattr(CommandEngine, 'idle_probe', lambda self: (used.append(self._idle_cmd) or True))
    monkeypatch.setattr(rec.dockerutil, 'ping', lambda *a, **kw: None)
    monkeypatch.setattr(rec, 'may_write', lambda **kw: 'ok')
    def runner(db, **kwargs):
        kwargs['docker'].idle_probe()
        return 'success'
    monkeypatch.setattr(rec, '_default_v2_database_runner', runner)
    actual = rec.reconcile
    monkeypatch.setattr(cli, 'reconcile', lambda config, **kw:
        actual(config, lock_backend=MemoryBackend(), alert_send=lambda *a: None, **kw))
    cli.main(['update'])
    assert used == [db.adapters.idle_probe for db in cfg.databases.values() if db.enabled]


def test_open_refuses_failed_layer(tmp_path):
    cfg, inbox, published = _runtime_cfg(tmp_path)
    conn = init_db(cfg.runtime.db_path)
    state = StateStore(conn)
    state.register_database('klon_opl', 'package')
    state.mark_layer('klon_opl', 'code_index', 'r1', 'failed', None, None)
    conn.close()
    docker = FlexibleReadsDocker(connected_networks=set())
    result = rec.reopen_reads(cfg, database='klon_opl', docker=docker,
        lock_backend=MemoryBackend(), alert_send=lambda *a: None)
    assert result == 'failed', 'open reports success for known broken index'


def test_reaped_full_clears_pending_full_flag(tmp_path):
    from test_reconcile_multidb import ScriptedDocker, _insert_run_job
    from contour.index_graph import set_graph_full_needed, is_graph_full_needed
    conn = init_db(str(tmp_path / 'state.db'))
    _insert_run_job(conn, database='kb', stage='graph_full', status='created',
        container_id='full-job', exec_id=None)
    set_graph_full_needed(conn, 'kb', True)
    rec.reap_database_jobs(conn, 'kb', ScriptedDocker(inspect_status='exited'))
    assert conn.execute('SELECT exit_code FROM jobs').fetchone()[0] == 0
    assert not is_graph_full_needed(conn, 'kb'), 'successful detached FULL will be repeated'
