"""Throwaway-container BSL stages (2026-10-28 stand incident).

The bsl daemon container binds ``/repos/SRC`` to the published host path.
Activation rotates that directory (rename), but a bind mount pins the OLD
inode: every ``docker exec`` into the daemon — the index/clean stages and the
verify probe — kept seeing the pre-rotation tree. Deltas containing ADDED
files failed verification (runs 68/71: BusinessProcesses/ИсполнениеЗадач).
Stages must run in throwaway containers mounting the CURRENT host path.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import yaml

import contour.index_bsl as index_bsl
from contour.config import Config, load_config
from contour.db import init_db, set_meta
from contour.index_bsl import _capture_live_identity, index_bsl as run_index

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"


def _make_config(tmp_path: Path) -> Config:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(str(cfg_path))


def _init_conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(str(tmp_path / "contour.db"))


class RecordingRunner:
    """Duck-typed DockerRunner: records commands, answers inspect queries."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, cmd, *, timeout=None, env=None):
        self.commands.append(list(cmd))
        joined = " ".join(cmd)
        out = ""
        if joined.startswith("docker volume inspect"):
            out = "vol-code-index|2026-10-01T00:00:00Z\n"
        elif "docker inspect" in joined:
            out = "sha256:deadbeef\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def _patch_wait(monkeypatch, exit_code=0):
    seen: list[str] = []

    def fake_wait(container_id, *, runner=None, observe_cap=None, on_observe=None):
        seen.append(container_id)
        return exit_code

    # index_bsl binds wait at import time; patch the module attribute it uses.
    monkeypatch.setattr(index_bsl, "wait", fake_wait)
    return seen


def _start_run(conn: sqlite3.Connection, run_id: int, revision: str) -> None:
    conn.execute(
        "INSERT INTO runs (id, database, target_revision, started_at)"
        " VALUES (?, 'ubd', ?, '2026-10-28T00:00:00Z')",
        (run_id, revision),
    )
    conn.commit()


def test_index_stage_uses_throwaway_container(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path)
    ubd = cfg.databases["ubd"]
    expected_root = Path(ubd.source.path)
    sub = getattr(ubd.source, "workspace_subdir", "") or ""
    expected_src = str(expected_root / sub if sub else expected_root)

    runner = RecordingRunner()
    conn = _init_conn(tmp_path)
    _start_run(conn, 1, "c08c5d4a11bb812dbf125d1d3ee44a33a7823f23")
    seen = _patch_wait(monkeypatch)

    run_index(ubd, conn, run_id=1, runner=runner, resume=False, container_runner=True)

    create = next((c for c in runner.commands if c[:2] == ["docker", "create"]), None)
    assert create is not None, "no docker create issued"
    joined = " ".join(create)
    assert "--network none" in joined
    assert f"-v {expected_src}:/repos/SRC" in joined
    assert "sha256:deadbeef" in joined
    assert "bsl-indexer index --path /repos/SRC" in joined
    assert seen, "wait() not called for the throwaway container"
    assert any(
        c[:2] == ["docker", "rm"] for c in runner.commands
    ), "throwaway container not removed"
    row = conn.execute(
        "SELECT status, exit_code FROM jobs WHERE stage = 'bsl_index'"
    ).fetchone()
    assert row is not None
    assert row[0] == "ok" and row[1] == 0


def test_clean_stage_also_uses_throwaway(tmp_path: Path, monkeypatch):
    cfg = _make_config(tmp_path)
    ubd = cfg.databases["ubd"]
    runner = RecordingRunner()
    conn = _init_conn(tmp_path)
    _start_run(conn, 2, "c08c5d4a11bb812dbf125d1d3ee44a33a7823f23")
    _patch_wait(monkeypatch)

    # Record a completed attempt for this revision so bsl_index resumes and
    # only bsl_clean runs — clean must go through a throwaway too.
    set_meta(
        conn,
        "bsl_attempt:ubd",
        '{"revision": "c08c5d4a11bb812dbf125d1d3ee44a33a7823f23",'
        ' "storage": "vol-code-index|2026-10-01T00:00:00Z",'
        ' "image": "sha256:deadbeef#bsl-runner-v2", "job_floor": 0}',
    )
    conn.execute(
        "INSERT INTO jobs (id, database, run_id, stage, status, exit_code)"
        " VALUES (1, 'ubd', 2, 'bsl_index', 'ok', 0)"
    )
    conn.commit()

    run_index(ubd, conn, run_id=2, runner=runner, resume=True, container_runner=True)

    creates = [c for c in runner.commands if c[:2] == ["docker", "create"]]
    assert creates, "no throwaway created for clean stage"
    assert "clean --path /repos/SRC" in " ".join(creates[-1])


def test_capture_live_identity_carries_runner_marker(tmp_path: Path):
    cfg = _make_config(tmp_path)
    ubd = cfg.databases["ubd"]
    storage, image = _capture_live_identity(ubd, RecordingRunner())
    assert index_bsl._RUNNER_MARKER in image


def test_resume_rejects_attempt_recorded_before_runner_change(tmp_path: Path):
    """Attempts recorded by the legacy exec runner must not satisfy resume."""
    cfg = _make_config(tmp_path)
    ubd = cfg.databases["ubd"]
    conn = _init_conn(tmp_path)
    conn.execute(
        "INSERT INTO layer_state (database, layer, status, applied_revision,"
        " updated_at)"
        " VALUES ('ubd', 'code_index', 'building', NULL, '2026-10-28T00:00:00Z')"
    )
    set_meta(
        conn,
        "bsl_attempt:ubd",
        '{"revision": "c08c5d4a11bb812dbf125d1d3ee44a33a7823f23",'
        ' "storage": "vol-code-index|2026-10-01T00:00:00Z",'
        ' "image": "sha256:oldimage", "job_floor": 5}',
    )
    conn.commit()

    from contour.index_bsl import should_resume_bsl_stages

    assert (
        should_resume_bsl_stages(
            ubd,
            conn,
            revision="c08c5d4a11bb812dbf125d1d3ee44a33a7823f23",
            runner=RecordingRunner(),
        )
        is False
    )
