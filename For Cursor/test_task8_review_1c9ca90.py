"""Failed rebuild must not reuse jobs from a previous volume. No live Docker."""
import sys
from pathlib import Path
from types import SimpleNamespace

import contour.pipeline as pipeline
sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[2] / 'tests'))
sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[3] / 'For Cursor'))
from test_task8_review_471addd import runner_setup
from contour import dockerutil


def index_count(docker):
    return sum(call[0] == 'create_exec' and 'index' in call for call in docker.calls)


def clean_count(docker):
    return sum(call[0] == 'create_exec' and 'clean' in call for call in docker.calls)


def test_retry_of_failed_rebuild_ignores_old_volume_success(tmp_path, monkeypatch):
    conn, state, reads, probes, docker, run = runner_setup(tmp_path, monkeypatch)
    assert run() == 'success'
    assert index_count(docker) == 1 and clean_count(docker) == 1

    monkeypatch.setattr(dockerutil, 'volume_identity', lambda *a, **kw: 'replacement|new')

    # Rebuild on the new volume: index starts, then fails before clean.
    calls = {'n': 0}
    real_wait = docker.wait_exec

    def fail_first_index(container, exec_id):
        calls['n'] += 1
        if calls['n'] == 1:
            return 1
        return real_wait(container, exec_id)

    monkeypatch.setattr(docker, 'wait_exec', fail_first_index)
    assert run() == 'failed'
    assert index_count(docker) == 2

    # Retry must not treat the old volume's ok jobs as success for this attempt.
    monkeypatch.setattr(docker, 'wait_exec', real_wait)
    assert run() == 'success'
    assert index_count(docker) == 3, docker.calls
    assert clean_count(docker) == 2, docker.calls
