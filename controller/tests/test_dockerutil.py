from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from contour import dockerutil


@dataclass
class FakeDockerRunner:
    container_id: str = "abc123def456"
    exit_code: int = 0
    calls: list[list[str]] = field(default_factory=list)
    ping_timeout: bool = False

    def run(self, cmd: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd[:2] == ["docker", "info"]:
            if self.ping_timeout:
                raise subprocess.TimeoutExpired(cmd, timeout or 0)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 4 and cmd[:2] == ["docker", "compose"] and "create" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="Container foo Created\n")
        if cmd[:2] == ["docker", "compose"] and "ps" in cmd and "-q" in cmd:
            if "--all" in cmd or "-a" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.container_id}\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "start"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.container_id, stderr="")
        if cmd[:2] == ["docker", "wait"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=str(self.exit_code), stderr="")
        raise AssertionError(f"unexpected docker command: {cmd}")


def test_build_create_cmd_excludes_rm():
    cmd = dockerutil.build_create_cmd(
        "/mnt/c/MCP/mcp_for_1c",
        "mcp_for_1c",
        ["--rm", "--no-deps", "--entrypoint", "python", "metadata-indexer", "/app/apply_changes.py"],
    )
    assert "--rm" not in cmd
    assert "--no-deps" not in cmd
    assert "--entrypoint" not in cmd
    assert "/app/apply_changes.py" not in cmd
    assert "metadata-indexer" in cmd
    assert "-p" in cmd
    idx = cmd.index("-p")
    assert cmd[idx + 1] == "mcp_for_1c"
    assert "create" in cmd


def test_build_create_cmd_rejects_rm_even_as_only_extra_flag():
    cmd = dockerutil.build_create_cmd("/compose", "bsl-indexer", ["--rm"])
    assert "--rm" not in cmd


def test_build_create_cmd_keeps_force_recreate():
    cmd = dockerutil.build_create_cmd(
        "/mnt/c/MCP/mcp_for_1c",
        "mcp_for_1c",
        ["--force-recreate", "metadata-indexer"],
    )
    assert "--force-recreate" in cmd
    assert "metadata-indexer" in cmd


def test_build_ps_cmd_includes_all():
    cmd = dockerutil.build_ps_cmd("/mnt/c/MCP/mcp_for_1c", "mcp_for_1c", "metadata-indexer")
    assert "--all" in cmd or "-a" in cmd
    assert "-q" in cmd
    assert cmd[-1] == "metadata-indexer"


def record_then_start(
    runner: FakeDockerRunner,
    compose_dir: str,
    project: str,
    args: list[str],
) -> list[str]:
    """Simulate reconcile ordering: create → record job id → start."""
    events: list[str] = []
    container_id = dockerutil.compose_create(compose_dir, project, args, runner=runner)
    events.append(f"record:{container_id}")
    dockerutil.start(container_id, runner=runner)
    events.append("start")
    return events


def test_record_then_start_order():
    runner = FakeDockerRunner()
    events = record_then_start(
        runner,
        "/mnt/c/MCP/mcp_for_1c",
        "mcp_for_1c",
        ["--no-deps", "--entrypoint", "python", "metadata-indexer", "/app/apply_changes.py"],
    )
    assert events[0].startswith("record:")
    assert events[1] == "start"
    create_idx = next(i for i, c in enumerate(runner.calls) if "create" in c)
    start_idx = next(i for i, c in enumerate(runner.calls) if c[:2] == ["docker", "start"])
    assert create_idx < start_idx


def test_compose_create_returns_container_id():
    runner = FakeDockerRunner(container_id="deadbeef")
    cid = dockerutil.compose_create(
        "/mnt/c/MCP/mcp_for_1c",
        "mcp_for_1c",
        ["--no-deps", "--entrypoint", "python", "metadata-indexer", "/app/apply_changes.py"],
        runner=runner,
    )
    assert cid == "deadbeef"
    create_cmds = [c for c in runner.calls if "create" in c]
    assert len(create_cmds) == 1
    assert "--rm" not in create_cmds[0]
    assert "--no-deps" not in create_cmds[0]
    assert "--entrypoint" not in create_cmds[0]
    assert "/app/apply_changes.py" not in create_cmds[0]
    assert "metadata-indexer" in create_cmds[0]
    ps_cmds = [c for c in runner.calls if "ps" in c]
    assert len(ps_cmds) == 1
    assert "--all" in ps_cmds[0] or "-a" in ps_cmds[0]


def test_start_invokes_docker_start():
    runner = FakeDockerRunner()
    dockerutil.start("abc123", runner=runner)
    assert runner.calls[-1] == ["docker", "start", "abc123"]


def test_wait_returns_exit_code():
    runner = FakeDockerRunner(exit_code=3)
    assert dockerutil.wait("abc123", runner=runner) == 3
    assert runner.calls[-1] == ["docker", "wait", "abc123"]


def test_wait_re_arms_chunk_until_container_exits():
    """A1: chunk expiry re-arms until the container finishes (same-night)."""

    class SlowThenExit:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, cmd: list[str], *, timeout: float | None = None):
            assert cmd[:2] == ["docker", "wait"]
            self.calls += 1
            if self.calls < 3:
                raise subprocess.TimeoutExpired(cmd, timeout or 0)
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")

    runner = SlowThenExit()
    assert dockerutil.wait("abc", runner=runner) == 0
    assert runner.calls == 3


def test_wait_total_timeout_raises_writer_still_alive():
    from contour.runtime_policy import WriterStillAlive

    class HangingWait:
        def run(self, cmd: list[str], *, timeout: float | None = None):
            raise subprocess.TimeoutExpired(cmd, timeout or 0)

    with pytest.raises(WriterStillAlive):
        dockerutil.wait("abc", runner=HangingWait(), timeout=0.05)


def test_wait_exec_uses_inspect_until_done():
    class PollRunner:
        def __init__(self) -> None:
            self.polls = 0

        def inspect_exec(self, exec_id: str) -> dict:
            self.polls += 1
            return {"Running": self.polls < 2, "ExitCode": 7}

    assert (
        dockerutil.wait_exec("c", "e1", runner=PollRunner(), poll_interval=0) == 7
    )


def test_wait_exec_total_timeout_raises_writer_still_alive():
    from contour.runtime_policy import WriterStillAlive

    class AlwaysRunning:
        def inspect_exec(self, exec_id: str) -> dict:
            return {"Running": True, "ExitCode": None}

    with pytest.raises(WriterStillAlive):
        dockerutil.wait_exec(
            "c", "e1", runner=AlwaysRunning(), poll_interval=0, timeout=0.05
        )


def test_wait_exec_propagates_exec_record_gone():
    class GoneRunner:
        def inspect_exec(self, exec_id: str) -> dict:
            raise dockerutil.ExecRecordGone(f"exec record gone: {exec_id}")

    with pytest.raises(dockerutil.ExecRecordGone):
        dockerutil.wait_exec("c", "e1", runner=GoneRunner(), poll_interval=0)


def test_ping_raises_on_hung_api():
    runner = FakeDockerRunner(ping_timeout=True)
    with pytest.raises(subprocess.TimeoutExpired):
        dockerutil.ping(runner=runner, timeout=1.0)


def test_ping_succeeds_when_api_responds():
    runner = FakeDockerRunner()
    dockerutil.ping(runner=runner)
    assert runner.calls[0][:2] == ["docker", "info"]
