"""Regression checks for 96f2960. No real Docker or stand mutation."""
import sys
from pathlib import Path
from dataclasses import replace
import subprocess
import contour.pipeline as pipeline
sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[2] / 'tests'))
sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[3] / 'For Cursor'))
from test_task8_review_471addd import runner_setup
from test_reconcile_multidb import _v2_config
from contour import dockerutil
from contour.adapters import build_probes
from contour.index_graph import copy_job_logs


def test_recreated_bsl_storage_really_runs_index_again(tmp_path, monkeypatch):
    conn, state, reads, probes, docker, run = runner_setup(tmp_path, monkeypatch)
    assert run() == 'success'
    before = len([c for c in docker.calls if c[0] == 'create_exec'])
    assert before == 2
    monkeypatch.setattr(dockerutil, 'volume_identity', lambda *a, **kw: 'new-volume|new-date')
    assert run() == 'success'
    after = len([c for c in docker.calls if c[0] == 'create_exec'])
    assert after > before, 'recovery published new storage without executing index/clean'


def test_explicit_neo4j_target_is_not_invented_from_volume(tmp_path, monkeypatch):
    cfg = _v2_config(tmp_path)
    # Existing integration script supports this explicit deployment setting.
    monkeypatch.setenv('NEO4J_CONTAINER', 'neo4j')
    captured = []
    def run(cmd, **kwargs):
        captured.append(kwargs['env']['NEO4J_CONTAINER'])
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
    monkeypatch.setattr(dockerutil.subprocess, 'run', run)
    probes = build_probes(cfg.databases['kb'], dockerutil.SubprocessRunner())
    probes.graph.check_xml('Catalogs/Foo.xml')
    assert captured == ['neo4j'], captured


def test_log_collection_has_timeout(tmp_path, monkeypatch):
    calls = []
    class Runner:
        def run(self, cmd, *, timeout=None):
            calls.append(timeout)
            return subprocess.CompletedProcess(cmd, 0, stdout='log', stderr='')
    import contour.index_graph as graph
    monkeypatch.setattr(graph, 'SRV_LOGS', tmp_path)
    copy_job_logs(Runner(), 'finished-writer')
    assert calls and calls[0] is not None and calls[0] <= 30, calls
