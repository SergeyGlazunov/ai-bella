"""Resume across updates and first-build storage changes; no live Docker."""
import sys
from pathlib import Path
import contour.pipeline as pipeline
sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[2] / 'tests'))
sys.path.insert(0, str(Path(pipeline.__file__).resolve().parents[3] / 'For Cursor'))
from test_task8_review_471addd import runner_setup
from test_index_graph import FakeDockerRunner
from test_packages import _make_package
from contour.runtime_policy import WriterStillAlive
from contour import dockerutil
import contour.reconcile as rec


class DelayedDocker(FakeDockerRunner):
    release = True

    def wait_exec(self, container, exec_id):
        if not self.release:
            raise WriterStillAlive('observation expired')
        return super().wait_exec(container, exec_id)


def finish_background(conn, docker):
    docker.release = True
    docker.exec_ids.clear()
    assert rec.reap_database_jobs(conn, 'klon_opl', docker) == 'idle'


def index_count(docker):
    return sum(call[0] == 'create_exec' and 'index' in call for call in docker.calls)


def test_resume_new_revision_after_previous_publish(tmp_path, monkeypatch):
    docker = DelayedDocker()
    conn, state, reads, probes, docker, run = runner_setup(tmp_path, monkeypatch, docker=docker)
    assert run() == 'success'
    old_revision = state.get_database('klon_opl')['published_revision']
    _make_package(tmp_path / 'inbox' / 'klon_opl', name='ready-two',
        database='klon_opl', export_id='two',
        extra_files={'src/CommonModules/A/Ext/Module.bsl': b'// updated module'},
        manifest_overrides={'finished_at': '2026-10-14T00:02:00Z'})
    docker.release = False
    assert run() == 'deferred'
    assert state.get_database('klon_opl')['published_revision'] == old_revision
    assert index_count(docker) == 2
    finish_background(conn, docker)
    assert run() == 'success'
    assert index_count(docker) == 2, 'R2 index ran again because applied_revision still referred to R1'


def test_first_build_does_not_resume_success_on_removed_volume(tmp_path, monkeypatch):
    docker = DelayedDocker()
    docker.release = False
    conn, state, reads, probes, docker, run = runner_setup(tmp_path, monkeypatch, docker=docker)
    assert run() == 'deferred'
    finish_background(conn, docker)
    assert index_count(docker) == 1
    # No layer has reached ready yet. The volume is replaced before recovery.
    monkeypatch.setattr(dockerutil, 'volume_identity', lambda *a, **kw: 'replacement|new')
    assert run() == 'success'
    assert index_count(docker) == 2, 'missing recorded identity allowed skipping index on replacement storage'
