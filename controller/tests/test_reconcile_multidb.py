"""Task 8 multi-database reconcile (v2). Legacy UBD tests stay in test_reconcile.py."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from contour.config import load_config
from contour.db import init_db
from contour.lock import MemoryBackend
from contour.reconcile import RuntimePaths, aggregate_database_results, reconcile
from contour.state import StateStore

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"
VLAD = ZoneInfo("Asia/Vladivostok")


def _in_window() -> datetime:
    return datetime(2026, 9, 20, 3, 0, tzinfo=VLAD)


def _out_of_window() -> datetime:
    return datetime(2026, 9, 20, 12, 0, tzinfo=VLAD)


@dataclass
class PingDocker:
    """Satisfies dockerutil.ping without talking to a live engine."""

    def run(self, cmd: list[str], *, timeout: float | None = None):
        del timeout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _v2_config(tmp_path: Path, *, disable: frozenset[str] = frozenset()) -> object:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["runtime"]["root"] = str(tmp_path / "root")
    raw["runtime"]["db_path"] = str(tmp_path / "state.db")
    raw["runtime"]["lock_path"] = str(tmp_path / "sync.lock")
    raw["runtime"]["logs_path"] = str(tmp_path / "logs")
    raw["runtime"]["backups_path"] = str(tmp_path / "backups")
    for name in disable:
        raw["databases"][name]["enabled"] = False
    # Point UBD git at a temp path so accidental real work cannot hit live repos.
    ubd_repo = tmp_path / "ubd-src"
    ubd_repo.mkdir()
    raw["databases"]["ubd"]["source"]["path"] = str(ubd_repo)
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(str(cfg_path))


@dataclass
class RecordingRunner:
    outcomes: dict[str, str] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)
    default: str = "success"

    def __call__(self, database, *, cfg, conn, state, **kwargs) -> str:
        del cfg, conn, state, kwargs
        self.seen.append(database.name)
        return self.outcomes.get(database.name, self.default)


def test_v2_preserves_config_order(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    runner = RecordingRunner()
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "success"
    assert runner.seen == ["ubd", "kb", "klon_opl"]


def test_disabled_database_is_skipped(tmp_path: Path):
    cfg = _v2_config(tmp_path, disable=frozenset({"kb"}))
    runner = RecordingRunner()
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "success"
    assert runner.seen == ["ubd", "klon_opl"]
    assert "kb" not in runner.seen


def test_manual_database_filter(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    runner = RecordingRunner()
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database="kb",
        database_runner=runner,
    )
    assert result == "success"
    assert runner.seen == ["kb"]


def test_aggregate_failed_beats_later_success():
    assert (
        aggregate_database_results(
            [("kb", "failed"), ("klon_opl", "success"), ("ubd", "success")]
        )
        == "failed"
    )
    assert (
        aggregate_database_results([("ubd", "deferred"), ("kb", "success")])
        == "deferred"
    )
    assert aggregate_database_results([("ubd", "success")]) == "success"


def test_v2_failed_kb_does_not_mask_aggregate(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    runner = RecordingRunner(
        outcomes={"ubd": "success", "kb": "failed", "klon_opl": "success"}
    )
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "failed"
    assert runner.seen == ["ubd", "kb", "klon_opl"]


def test_v2_runtime_paths_prefer_cfg_runtime_when_cli_defaults_absent(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    runner = RecordingRunner()
    # No paths= — must use cfg.runtime.db_path / lock_path, not /srv/ai defaults.
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "success"
    assert Path(cfg.runtime.db_path).is_file()


def test_v2_external_skip_records_run_without_pipeline_double(tmp_path: Path):
    """Pre-pipeline skip (e.g. deferred) creates one finished run owned by the loop."""
    cfg = _v2_config(tmp_path, disable=frozenset({"kb", "klon_opl"}))

    def skip_runner(database, *, cfg, conn, state: StateStore, **kwargs) -> str:
        del cfg, kwargs
        run_id = state.start_run(database.name, "(pending)")
        state.finish_run(run_id, "deferred", "no new package")
        return "deferred"

    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=skip_runner,
    )
    assert result == "deferred"
    conn = init_db(str(tmp_path / "state.db"))
    rows = conn.execute(
        "SELECT database, result, message FROM runs WHERE database='ubd'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["result"] == "deferred"
    assert "no new package" in (rows[0]["message"] or "")


# --- Block 2: unfinished jobs / writer ownership ---------------------------------


def _insert_run_job(
    conn,
    *,
    database: str,
    stage: str,
    status: str,
    container_id: str | None = None,
    exec_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO runs (database, target_commit, started_at) VALUES (?, ?, ?)",
        (database, "rev", "2026-09-20T00:00:00Z"),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO jobs (
          database, run_id, stage, status, container_id, exec_id, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            database,
            run_id,
            stage,
            status,
            container_id,
            exec_id,
            "2026-09-20T00:00:00Z",
        ),
    )
    conn.commit()


@dataclass
class ScriptedDocker:
    """Minimal docker for reap_database_jobs state transitions."""

    inspect_status: str = "exited"
    wait_exit: int = 0
    exec_running: frozenset[str] = frozenset()
    fail_inspect: bool = False

    def run(self, cmd: list[str], *, timeout: float | None = None):
        del timeout
        if cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            if self.fail_inspect:
                raise RuntimeError("inspect unavailable")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{self.inspect_status}\n", stderr=""
            )
        if cmd[:2] == ["docker", "wait"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{self.wait_exit}\n", stderr=""
            )
        if cmd[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="log", stderr="")
        raise AssertionError(cmd)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("running", "busy"),
        ("exited_ok", "idle"),
        ("created_never_started", "idle"),
        ("unknown_inspect", "unknown"),
    ],
)
def test_reap_database_job_states(tmp_path: Path, case: str, expected: str):
    from contour.reconcile import reap_database_jobs

    conn = init_db(str(tmp_path / "state.db"))
    docker: ScriptedDocker
    if case == "running":
        _insert_run_job(
            conn,
            database="kb",
            stage="graph",
            status="running",
            container_id="cid-kb",
        )
        docker = ScriptedDocker(inspect_status="running")
    elif case == "exited_ok":
        _insert_run_job(
            conn,
            database="kb",
            stage="graph",
            status="running",
            container_id="cid-kb",
        )
        docker = ScriptedDocker(inspect_status="exited", wait_exit=0)
    elif case == "created_never_started":
        _insert_run_job(
            conn,
            database="kb",
            stage="graph",
            status="created",
            container_id="cid-kb",
        )
        docker = ScriptedDocker(inspect_status="created")
    else:
        _insert_run_job(
            conn,
            database="kb",
            stage="graph",
            status="running",
            container_id="cid-kb",
        )
        docker = ScriptedDocker(fail_inspect=True)
    assert reap_database_jobs(conn, "kb", docker) == expected
    if case == "exited_ok":
        row = conn.execute("SELECT status, exit_code FROM jobs").fetchone()
        assert row["status"] == "ok"
        assert row["exit_code"] == 0
    if case == "created_never_started":
        row = conn.execute("SELECT status, exit_code FROM jobs").fetchone()
        assert row["status"] == "failed"
        assert row["exit_code"] == -1


def test_kb_exit3_does_not_set_ubd_full(tmp_path: Path):
    from contour.index_graph import is_graph_full_needed
    from contour.reconcile import reap_database_jobs

    conn = init_db(str(tmp_path / "state.db"))
    _insert_run_job(
        conn,
        database="kb",
        stage="graph",
        status="running",
        container_id="cid-kb",
    )
    docker = ScriptedDocker(inspect_status="exited", wait_exit=3)
    assert reap_database_jobs(conn, "kb", docker) == "idle"
    assert is_graph_full_needed(conn, "kb") is True
    assert is_graph_full_needed(conn, "ubd") is False


def test_busy_writer_blocks_same_storage(tmp_path: Path):
    from contour.reconcile import writer_blocks_database

    cfg = _v2_config(tmp_path)
    # Force KB to share UBD's graph volume (misconfig / shared resource).
    kb = cfg.databases["kb"]
    ubd = cfg.databases["ubd"]
    shared = type(kb)(
        name=kb.name,
        source=kb.source,
        required_layers=kb.required_layers,
        services=kb.services,
        storage=type(kb.storage)(
            graph_volume=ubd.storage.graph_volume,
            code_index_volume=kb.storage.code_index_volume,
        ),
        adapters=kb.adapters,
        enabled=kb.enabled,
    )
    conn = init_db(str(tmp_path / "state.db"))
    _insert_run_job(
        conn,
        database="kb",
        stage="graph",
        status="running",
        container_id="cid-kb",
    )
    docker = ScriptedDocker(inspect_status="running")
    reason = writer_blocks_database(conn, ubd, [ubd, shared], docker)
    assert reason is not None
    assert "shared storage" in reason


def test_failed_kb_does_not_block_opl(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    runner = RecordingRunner(
        outcomes={"ubd": "success", "kb": "failed", "klon_opl": "success"}
    )
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "failed"
    assert runner.seen == ["ubd", "kb", "klon_opl"]


def test_legacy_reap_ignores_kb_jobs(tmp_path: Path):
    from contour.reconcile import reap_jobs

    conn = init_db(str(tmp_path / "state.db"))
    _insert_run_job(
        conn,
        database="kb",
        stage="graph",
        status="running",
        container_id="cid-kb",
    )
    # Legacy reap must stay idle despite a live KB writer in the same SQLite.
    assert reap_jobs(conn, ScriptedDocker(inspect_status="running")) == "idle"


# --- Block 5: write window / continue -------------------------------------------


def test_window_closes_between_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from contour.profiles import LayerResult, run_required_layers
    from contour.runtime_policy import WriteWindowClosed
    import contour.profiles as profiles_mod

    cfg = _v2_config(tmp_path)
    conn = init_db(str(tmp_path / "state.db"))
    state = StateStore(conn)
    state.register_database("ubd", "git")
    run_id = state.start_run("ubd", "r1")
    calls = {"n": 0}
    started: list[str] = []

    def may_write() -> str:
        calls["n"] += 1
        return "ok" if calls["n"] == 1 else "deferred_window"

    def fake_bsl(*args, **kwargs):
        del args, kwargs
        return LayerResult("code_index", "r1", "s", "i")

    def fake_graph(*args, **kwargs):
        del args, kwargs
        raise AssertionError("graph must not start after window closes")

    monkeypatch.setattr(profiles_mod, "run_bsl_layer", fake_bsl)
    monkeypatch.setattr(profiles_mod, "run_graph_layer", fake_graph)

    with pytest.raises(WriteWindowClosed):
        run_required_layers(
            cfg.databases["ubd"],
            revision="r1",
            run_id=run_id,
            conn=conn,
            runner=None,
            may_write=may_write,
            on_layer_start=started.append,
            on_layer_result=lambda _item: None,
            on_layer_error=lambda _layer, _exc: None,
        )
    assert started == ["code_index"]
    assert calls["n"] == 2


def test_pipeline_error_continues_next_database(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    seen: list[str] = []

    def boom(database, **kwargs):
        del kwargs
        seen.append(database.name)
        if database.name == "kb":
            raise RuntimeError("kb adapter exploded")
        return "success"

    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=boom,
    )
    assert result == "failed"
    assert seen == ["ubd", "kb", "klon_opl"]
    conn = init_db(str(tmp_path / "state.db"))
    kb = conn.execute(
        "SELECT result, message FROM runs WHERE database='kb'"
    ).fetchone()
    assert kb is not None
    assert kb["result"] == "failed"
    assert "exploded" in (kb["message"] or "")


def test_full_flag_survives_deferred_window(tmp_path: Path):
    from contour.index_graph import is_graph_full_needed, set_graph_full_needed

    cfg = _v2_config(tmp_path)
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    conn = init_db(paths.db_path)
    set_graph_full_needed(conn, "kb", True)
    conn.close()

    runner = RecordingRunner(default="success")
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_out_of_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "deferred"
    assert runner.seen == []
    conn = init_db(paths.db_path)
    assert is_graph_full_needed(conn, "kb") is True


def test_observation_timeout_does_not_start_duplicate(tmp_path: Path):
    from contour.runtime_policy import WriterStillAlive

    cfg = _v2_config(tmp_path, disable=frozenset({"ubd", "klon_opl"}))
    started: list[str] = []

    def runner(database, **kwargs):
        del kwargs
        started.append(database.name)
        raise WriterStillAlive("observation budget exceeded; writer still alive")

    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        database_runner=runner,
    )
    assert result == "deferred"
    assert started == ["kb"]


def test_transient_inspect_retry_is_bounded():
    from contour.dockerutil import run_readonly_with_retry
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, cmd, *, timeout=None):
            del cmd, timeout
            self.calls += 1
            raise TimeoutError("transient")

    flaky = Flaky()
    with pytest.raises(TimeoutError):
        run_readonly_with_retry(
            flaky,
            ["docker", "inspect", "x"],
            timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
            retries=DEFAULT_RUNTIME_POLICY.docker_transient_retries,
            pause_seconds=0,
        )
    assert flaky.calls == DEFAULT_RUNTIME_POLICY.docker_transient_retries


# --- Block 6: diagnostics -------------------------------------------------------


def test_nightly_aggregate_preserves_failure(tmp_path: Path):
    cfg = _v2_config(tmp_path)
    alerts: list[str] = []
    runner = RecordingRunner(
        outcomes={"ubd": "success", "kb": "failed", "klon_opl": "success"}
    )
    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=PingDocker(),
        alert_send=lambda _cfg, message: alerts.append(message),
        database_runner=runner,
    )
    assert result == "failed"
    assert alerts
    body = alerts[-1]
    assert "kb" in body
    assert "failed" in body


def test_alert_contains_database_stage_revision_log(tmp_path: Path):
    from contour.reconcile import format_nightly_alert

    conn = init_db(str(tmp_path / "state.db"))
    state = StateStore(conn)
    state.register_database("kb", "package")
    state.set_lifecycle("kb", "indexing", detail="graph boom")
    run_id = state.start_run("kb", "rev-kb")
    state.finish_run(run_id, "failed", "graph boom")
    conn.execute(
        """
        INSERT INTO jobs (
          database, run_id, stage, status, log_path, started_at, finished_at
        ) VALUES ('kb', ?, 'graph', 'failed', '/tmp/kb-graph.log', 't', 't')
        """,
        (run_id,),
    )
    conn.commit()
    text = format_nightly_alert(conn, [("kb", "failed")])
    assert "kb" in text
    assert "graph" in text
    assert "rev-kb" in text
    assert "/tmp/kb-graph.log" in text


def test_empty_samples_warning_is_visible():
    from contour.verify import EMPTY_LAYER_SAMPLE_WARNING, Samples, verify_indexes

    class Probe:
        status = "ready"
        seen: list[str]

        def __init__(self) -> None:
            self.seen = []

        def check_bsl(self, path: str) -> None:
            self.seen.append(path)

        def check_xml(self, path: str) -> None:
            self.seen.append(path)

        def check_deletion(self, path: str) -> None:
            self.seen.append(path)

    samples = Samples(
        bsl_paths=[],
        xml_paths=["a.xml"],
        deletions=[],
        integrity_unknown=False,
    )
    code = Probe()
    graph = Probe()
    warnings = verify_indexes(
        samples,
        type("P", (), {"code_index": code, "graph": graph})(),
        required_layers=("code_index", "graph"),
    )
    assert graph.seen == ["a.xml"]
    assert code.seen == []
    assert any(EMPTY_LAYER_SAMPLE_WARNING in item for item in warnings)
