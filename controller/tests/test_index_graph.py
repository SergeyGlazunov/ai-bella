from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
import json

import pytest
import yaml

from contour.config import Config, load_config
from contour.db import init_db
from contour.index_bsl import index_bsl
from contour.index_graph import index_graph

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"
LIVE_GRAPH_COMPOSE = Path(r"C:\MCP\mcp_for_1c")

BSL_INDEX_CMD = ["bsl-indexer", "index", "--path", "/repos/SRC"]
BSL_CLEAN_CMD = ["bsl-indexer", "clean", "--path", "/repos/SRC"]
BSL_ENV = "CODE_INDEX_HOME=/data/code-index-home"


@dataclass
class FakeDockerRunner:
    """Injectable backend: never talks to a live Engine."""

    calls: list[list[str]] = field(default_factory=list)
    wait_codes: list[int] = field(default_factory=lambda: [0])
    log_text: str = ""
    conn: sqlite3.Connection | None = None
    job_id_at_start: dict[str, str | None] = field(default_factory=dict)
    exec_ids: list[str] = field(default_factory=list)
    exec_exit: dict[str, int] = field(default_factory=dict)
    created_execs: list[str] = field(default_factory=list)
    started_execs: list[str] = field(default_factory=list)
    job_id_at_exec_start: dict[str, str | None] = field(default_factory=dict)
    _wait_i: int = 0
    _exec_i: int = 0

    def create_exec(
        self, container: str, cmd: list[str], env: list[str] | None = None
    ) -> str:
        self.calls.append(
            ["create_exec", container, *(env or []), *cmd]
        )
        self._exec_i += 1
        exec_id = f"exec-{self._exec_i}"
        self.created_execs.append(exec_id)
        self.exec_exit[exec_id] = 0
        return exec_id

    def start_exec(self, exec_id: str, detach: bool = True) -> None:
        self.calls.append(["start_exec", exec_id, str(detach)])
        self.started_execs.append(exec_id)
        self.exec_ids.append(exec_id)
        if self.conn is not None:
            row = self.conn.execute(
                "SELECT exec_id FROM jobs WHERE exec_id = ?", (exec_id,)
            ).fetchone()
            self.job_id_at_exec_start[exec_id] = (
                None if row is None else str(row["exec_id"])
            )

    def wait_exec(self, container: str, exec_id: str) -> int:
        if exec_id in self.exec_ids:
            self.exec_ids.remove(exec_id)
        return self.exec_exit.get(exec_id, 0)

    def run(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if cmd[:2] == ["docker", "exec"]:
            raise AssertionError("R04: must not use docker exec -d; use create_exec/start_exec")
        if len(cmd) >= 4 and cmd[:2] == ["docker", "compose"] and "create" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "compose"] and "ps" in cmd:
            # Realistic same-service ps: one replica plus a decoy line. Using this
            # blob (or a canned second id) as the FULL job id is the review bug.
            return subprocess.CompletedProcess(
                cmd, 0, stdout="cid-delta\ncid-from-ps\n", stderr=""
            )
        if cmd[:3] == ["docker", "volume", "inspect"]:
            volume = cmd[-1]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{volume}|created-test\n", stderr=""
            )
        if cmd[:2] == ["docker", "inspect"]:
            fmt = ""
            if "-f" in cmd:
                fmt = cmd[cmd.index("-f") + 1]
            name = cmd[-1]
            if "ExecIDs" in fmt:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(self.exec_ids) + "\n", stderr=""
                )
            if "{{.Image}}" in fmt:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="sha256:test-image\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=f"cid-{name}\n", stderr="")
        if cmd[:2] == ["docker", "start"]:
            cid = cmd[2]
            if self.conn is not None:
                row = self.conn.execute(
                    "SELECT container_id FROM jobs WHERE container_id = ?",
                    (cid,),
                ).fetchone()
                self.job_id_at_start[cid] = None if row is None else str(row["container_id"])
            return subprocess.CompletedProcess(cmd, 0, stdout=cid, stderr="")
        if cmd[:2] == ["docker", "wait"]:
            code = self.wait_codes[min(self._wait_i, len(self.wait_codes) - 1)]
            self._wait_i += 1
            return subprocess.CompletedProcess(
                cmd, 0, stdout=str(code), stderr=self.log_text
            )
        if cmd[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.log_text, stderr="")
        raise AssertionError(f"unexpected docker command: {cmd}")


def _make_config(tmp_path: Path) -> Config:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text(
        "services:\n  metadata-indexer:\n    image: alpine\n",
        encoding="utf-8",
    )
    assert compose_dir.resolve() != LIVE_GRAPH_COMPOSE.resolve()
    raw["databases"]["ubd"]["services"]["graph_compose_dir"] = str(compose_dir)
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(str(cfg_path))


def _init_run(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    conn = init_db(str(tmp_path / "contour.db"))
    cur = conn.execute(
        "INSERT INTO runs (target_commit, started_at) VALUES (?, ?)",
        ("deadbeef", "2026-09-20T00:00:00Z"),
    )
    conn.commit()
    return conn, int(cur.lastrowid)


def _create_cmds(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["docker", "compose"] and "create" in c]


def _file_args(cmd: list[str]) -> list[str]:
    files: list[str] = []
    i = 0
    while i < len(cmd) - 1:
        if cmd[i] == "-f":
            files.append(cmd[i + 1])
            i += 2
            continue
        i += 1
    return files


def _override_text(cmd: list[str]) -> str:
    chunks: list[str] = []
    for path in _file_args(cmd):
        text = Path(path).read_text(encoding="utf-8")
        if "metadata-indexer" in text:
            chunks.append(text)
    return "\n".join(chunks)


def _job_override_path(cmd: list[str]) -> str:
    files = _file_args(cmd)
    assert files, f"no -f files in {cmd}"
    return files[-1]


def _override_container_name(cmd: list[str]) -> str | None:
    # Compose merge tags (!reset) are not plain YAML; neutralize for asserts.
    raw = Path(_job_override_path(cmd)).read_text(encoding="utf-8")
    data = yaml.safe_load(raw.replace("!reset", "null")) or {}
    names = [
        spec.get("container_name")
        for spec in (data.get("services") or {}).values()
        if isinstance(spec, dict) and spec.get("container_name")
    ]
    return str(names[0]) if names else None


def _create_service(cmd: list[str]) -> str:
    i = cmd.index("create")
    for arg in cmd[i + 1 :]:
        if not arg.startswith("-"):
            return arg
    raise AssertionError(f"no service in {cmd}")


def test_index_bsl_exec_index_then_clean_without_force(tmp_path: Path):
    cfg = _make_config(tmp_path)
    conn, run_id = _init_run(tmp_path)
    runner = FakeDockerRunner(conn=conn)
    try:
        index_bsl(cfg, conn, run_id, runner=runner)
        creates = [c for c in runner.calls if c and c[0] == "create_exec"]
        starts = [c for c in runner.calls if c and c[0] == "start_exec"]
        assert len(creates) == 2
        assert len(starts) == 2
        assert creates[0][-3:] == ["index", "--path", "/repos/SRC"]
        assert creates[1][-3:] == ["clean", "--path", "/repos/SRC"]
        assert BSL_ENV in creates[0]
        assert creates[0][1] == "bsl-indexer"
        assert runner.created_execs == ["exec-1", "exec-2"]
        assert runner.started_execs == ["exec-1", "exec-2"]
        assert runner.job_id_at_exec_start["exec-1"] == "exec-1"
        assert runner.job_id_at_exec_start["exec-2"] == "exec-2"
        for cmd in runner.calls:
            assert "--force" not in cmd
            assert "--rm" not in cmd
        jobs = conn.execute(
            "SELECT stage, status, exec_id, exit_code FROM jobs ORDER BY id"
        ).fetchall()
        assert [row["stage"] for row in jobs] == ["bsl_index", "bsl_clean"]
        assert [row["status"] for row in jobs] == ["ok", "ok"]
        assert all(row["exec_id"] for row in jobs)
        assert [row["exit_code"] for row in jobs] == [0, 0]
        waits = [c for c in runner.calls if c[:2] == ["docker", "wait"]]
        assert waits == []
    finally:
        conn.close()


def test_index_graph_exit_3_then_0_takes_full(tmp_path: Path):
    cfg = _make_config(tmp_path)
    conn, run_id = _init_run(tmp_path)
    runner = FakeDockerRunner(wait_codes=[3, 0], conn=conn)
    try:
        index_graph(cfg, conn, run_id, runner=runner)
        creates = _create_cmds(runner.calls)
        assert len(creates) == 2
        for cmd in runner.calls:
            assert "--rm" not in cmd
            assert "-e" not in cmd
            assert "--env" not in cmd
            assert "--entrypoint" not in cmd
            assert "--force-recreate" not in cmd
        delta_text = _override_text(creates[0])
        assert "apply_changes.py" in delta_text
        assert "METADATA_FORCE_REINDEX" not in delta_text
        full_text = _override_text(creates[1])
        assert "METADATA_FORCE_REINDEX" in full_text
        assert "true" in full_text
        assert "apply_changes.py" not in full_text
        delta_name = _override_container_name(creates[0])
        full_name = _override_container_name(creates[1])
        assert delta_name
        assert full_name
        assert delta_name != full_name
        assert _create_service(creates[0]) != _create_service(creates[1])
        delta_cid = f"cid-{delta_name}"
        full_cid = f"cid-{full_name}"
        assert full_cid not in {"cid-delta", "cid-from-ps", "cid-full"}
        assert runner.job_id_at_start[delta_cid] == delta_cid
        assert runner.job_id_at_start[full_cid] == full_cid
        jobs = conn.execute(
            "SELECT container_id, exit_code FROM jobs ORDER BY id"
        ).fetchall()
        assert [row["container_id"] for row in jobs] == [delta_cid, full_cid]
        assert [row["exit_code"] for row in jobs] == [3, 0]
        starts = [c for c in runner.calls if c[:2] == ["docker", "start"]]
        create_idx = next(i for i, c in enumerate(runner.calls) if "create" in c)
        first_start = next(i for i, c in enumerate(runner.calls) if c[:2] == ["docker", "start"])
        assert create_idx < first_start
        assert starts[0] == ["docker", "start", delta_cid]
        assert starts[1] == ["docker", "start", full_cid]
        assert "-p" in creates[0]
        assert creates[0][creates[0].index("-p") + 1] == cfg.services.graph_project
        assert "metadata-indexer" in delta_text
        assert "metadata-indexer" in full_text
    finally:
        conn.close()


def test_index_graph_exit_0_with_force_reindex_log_skips_full(tmp_path: Path):
    cfg = _make_config(tmp_path)
    conn, run_id = _init_run(tmp_path)
    runner = FakeDockerRunner(
        wait_codes=[0],
        log_text="force reindex",
        conn=conn,
    )
    try:
        index_graph(cfg, conn, run_id, runner=runner)
        creates = _create_cmds(runner.calls)
        assert len(creates) == 1
        for cmd in runner.calls:
            assert "--rm" not in cmd
            assert "-e" not in cmd
        assert "METADATA_FORCE_REINDEX" not in _override_text(creates[0])
        assert "apply_changes.py" in _override_text(creates[0])
        jobs = conn.execute("SELECT container_id, exit_code FROM jobs").fetchall()
        assert len(jobs) == 1
        assert jobs[0]["exit_code"] == 0
    finally:
        conn.close()


def test_index_graph_exit_2_fails_without_full(tmp_path: Path):
    cfg = _make_config(tmp_path)
    conn, run_id = _init_run(tmp_path)
    runner = FakeDockerRunner(wait_codes=[2], conn=conn)
    try:
        with pytest.raises(RuntimeError):
            index_graph(cfg, conn, run_id, runner=runner)
        creates = _create_cmds(runner.calls)
        assert len(creates) == 1
        for cmd in runner.calls:
            assert "--rm" not in cmd
            assert "-e" not in cmd
        assert "METADATA_FORCE_REINDEX" not in _override_text(creates[0])
        jobs = conn.execute("SELECT exit_code FROM jobs").fetchall()
        assert [row["exit_code"] for row in jobs] == [2]
    finally:
        conn.close()


def test_index_graph_full_skipped_when_may_write_deferred(tmp_path: Path):
    cfg = _make_config(tmp_path)
    conn, run_id = _init_run(tmp_path)
    runner = FakeDockerRunner(wait_codes=[3], conn=conn)
    try:
        with pytest.raises(RuntimeError, match="(?i)full|deferred|skip"):
            index_graph(
                cfg,
                conn,
                run_id,
                runner=runner,
                may_write_decision="deferred_window",
                remaining_budget=60.0,
            )
        creates = _create_cmds(runner.calls)
        assert len(creates) == 1
        assert "METADATA_FORCE_REINDEX" not in _override_text(creates[0])
        assert "apply_changes.py" in _override_text(creates[0])
    finally:
        conn.close()


def test_copy_job_logs_uses_runtime_logs_path(tmp_path: Path):
    from contour.index_graph import copy_job_logs

    logs = tmp_path / "runs"
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["runtime"]["logs_path"] = str(logs)
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text(
        "services:\n  metadata-indexer:\n    image: alpine\n",
        encoding="utf-8",
    )
    raw["databases"]["ubd"]["services"]["graph_compose_dir"] = str(compose_dir)
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(str(cfg_path))
    runner = FakeDockerRunner(log_text="graph boom\n")
    path = copy_job_logs(runner, "cid-review", cfg=cfg)
    assert path is not None
    written = Path(path)
    assert written.parent == logs
    assert written.read_text(encoding="utf-8") == "graph boom\n"


def test_index_graph_persists_log_under_runtime_logs_path(tmp_path: Path):
    logs = tmp_path / "runs"
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["runtime"]["logs_path"] = str(logs)
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text(
        "services:\n  metadata-indexer:\n    image: alpine\n",
        encoding="utf-8",
    )
    raw["databases"]["ubd"]["services"]["graph_compose_dir"] = str(compose_dir)
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(str(cfg_path))
    conn, run_id = _init_run(tmp_path)
    runner = FakeDockerRunner(wait_codes=[0], log_text="ok-log", conn=conn)
    try:
        index_graph(cfg, conn, run_id, runner=runner)
        row = conn.execute(
            "SELECT log_path FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        log_path = Path(row["log_path"])
        assert log_path.parent == logs
        assert log_path.is_file()
    finally:
        conn.close()


def test_profile_graph_uses_configured_logs(tmp_path: Path, monkeypatch):
    """Production multi-db path: DatabaseConfig alone; logs_path via runner."""
    from dataclasses import replace

    from contour.pipeline import ProfilesLayerRunner
    from contour.profiles import run_graph_layer

    cfg = _make_config(tmp_path)
    logs = tmp_path / "configured-runs"
    cfg = replace(cfg, runtime=replace(cfg.runtime, logs_path=str(logs)))
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setattr("contour.index_graph._DEFAULT_LOG_DIRS", (fallback,))
    ubd = replace(cfg.databases["ubd"], required_layers=frozenset({"graph"}))
    conn, run_id = _init_run(tmp_path)
    try:
        # Boundary the review reported: run_graph_layer with DatabaseConfig.
        run_graph_layer(
            ubd,
            "deadbeef",
            run_id,
            conn,
            runner=FakeDockerRunner(conn=conn, log_text="ok"),
            logs_path=str(logs),
        )
        row = conn.execute(
            "SELECT log_path FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert Path(row["log_path"]).parent == logs

        # Same wiring through ProfilesLayerRunner (production reconcile path).
        conn.execute("DELETE FROM jobs")
        conn.commit()
        run_id2 = int(
            conn.execute(
                "INSERT INTO runs (database, target_revision, started_at) "
                "VALUES (?, ?, ?)",
                ("ubd", "deadbeef2", "2026-09-20T00:00:00Z"),
            ).lastrowid
        )
        conn.commit()
        layers = ProfilesLayerRunner(
            conn=conn,
            docker_runner=FakeDockerRunner(conn=conn, log_text="ok2"),
            logs_path=str(logs),
        )
        layers.run(ubd, "deadbeef2", run_id2)
        row2 = conn.execute(
            "SELECT log_path FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert Path(row2["log_path"]).parent == logs
    finally:
        conn.close()


def test_log_directory_failure_preserves_successful_job(tmp_path: Path):
    from dataclasses import replace

    cfg = _make_config(tmp_path)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    cfg = replace(cfg, runtime=replace(cfg.runtime, logs_path=str(blocked / "runs")))
    conn, run_id = _init_run(tmp_path)
    try:
        index_graph(
            cfg,
            conn,
            run_id,
            runner=FakeDockerRunner(conn=conn, wait_codes=[0]),
        )
        row = conn.execute(
            "SELECT status, exit_code FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "ok" and row["exit_code"] == 0
    finally:
        conn.close()
