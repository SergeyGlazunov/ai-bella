from __future__ import annotations

import subprocess
from dataclasses import dataclass

from contour.dockerutil import DockerRunner, SubprocessRunner


class MissingAdapters(RuntimeError):
    """Configured index/idle adapters are required before closing reads."""


def _require_cmd(section: str, name: str, command: list[str] | None) -> list[str]:
    if not command:
        raise MissingAdapters(f"adapters.{section}.{name} is required")
    if not all(isinstance(item, str) and item for item in command):
        raise MissingAdapters(f"adapters.{section}.{name} must be a list of strings")
    return list(command)


def _render(command: list[str], path: str) -> list[str]:
    return [part.replace("{path}", path) for part in command]


@dataclass
class CommandProbe:
    status: str = "ready"
    check_bsl_cmd: list[str] | None = None
    check_xml_cmd: list[str] | None = None
    check_deletion_cmd: list[str] | None = None
    runner: DockerRunner | None = None

    def _run(self, command: list[str] | None, path: str, kind: str) -> None:
        if not command:
            raise MissingAdapters(f"{kind} adapter command missing")
        docker = self.runner if self.runner is not None else SubprocessRunner()
        docker.run(_render(command, path))

    def check_bsl(self, path: str) -> None:
        if not self.check_bsl_cmd:
            raise MissingAdapters("check_bsl adapter command missing")
        self._run(self.check_bsl_cmd, path, "check_bsl")

    def check_xml(self, path: str) -> None:
        if not self.check_xml_cmd:
            raise MissingAdapters("check_xml adapter command missing")
        self._run(self.check_xml_cmd, path, "check_xml")

    def check_deletion(self, path: str) -> None:
        self._run(self.check_deletion_cmd, path, "check_deletion")


@dataclass
class CommandProbes:
    code_index: CommandProbe
    graph: CommandProbe


class CommandEngine:
    """Docker runner that uses a configured idle-probe command. Not a boolean stub."""

    def __init__(
        self,
        idle_cmd: list[str],
        *,
        runner: DockerRunner | None = None,
    ) -> None:
        if not idle_cmd:
            raise MissingAdapters("adapters.idle_probe is required")
        self._idle_cmd = list(idle_cmd)
        self._runner = runner if runner is not None else SubprocessRunner()

    def idle_configured(self) -> bool:
        return True

    def run(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._runner.run(cmd, timeout=timeout)

    def idle_probe(self) -> bool:
        try:
            result = self._runner.run(self._idle_cmd)
        except Exception:
            return False
        return int(getattr(result, "returncode", 1) or 0) == 0


def build_probes(cfg, docker) -> CommandProbes:
    adapters = getattr(cfg, "adapters", None)
    if adapters is None:
        raise MissingAdapters("adapters section is required")
    code_raw = getattr(adapters, "code_index", None)
    graph_raw = getattr(adapters, "graph", None)
    if code_raw is None or graph_raw is None:
        raise MissingAdapters("adapters.code_index and adapters.graph are required")
    code_index = CommandProbe(
        check_bsl_cmd=_require_cmd("code_index", "check_bsl", getattr(code_raw, "check_bsl", None)),
        check_deletion_cmd=_require_cmd(
            "code_index", "check_deletion", getattr(code_raw, "check_deletion", None)
        ),
        runner=docker,
    )
    graph = CommandProbe(
        check_bsl_cmd=getattr(graph_raw, "check_bsl", None),
        check_xml_cmd=_require_cmd("graph", "check_xml", getattr(graph_raw, "check_xml", None)),
        check_deletion_cmd=_require_cmd(
            "graph", "check_deletion", getattr(graph_raw, "check_deletion", None)
        ),
        runner=docker,
    )
    return CommandProbes(code_index=code_index, graph=graph)


def require_idle_adapter(docker) -> None:
    configured = getattr(docker, "idle_configured", None)
    if callable(configured):
        if not configured():
            raise MissingAdapters("idle_probe adapter is not configured")
        return
    if not callable(getattr(docker, "idle_probe", None)):
        raise MissingAdapters("idle_probe adapter is not configured")
