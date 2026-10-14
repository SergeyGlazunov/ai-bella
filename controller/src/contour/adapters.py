from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from contour.dockerutil import DockerRunner, SubprocessRunner
from contour.runtime_policy import DEFAULT_RUNTIME_POLICY


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


def profile_adapter_env(database) -> dict[str, str]:
    """Environment so idle/probe scripts target this profile's containers."""
    env: dict[str, str] = {}
    services = getattr(database, "services", None)
    if services is not None:
        bsl = getattr(services, "bsl_container", None) or ""
        if bsl:
            env["BSL_CONTAINER"] = str(bsl)
    storage = getattr(database, "storage", None)
    if storage is not None:
        graph_vol = (getattr(storage, "graph_volume", None) or "").strip()
        if graph_vol:
            neo = graph_vol[:-5] if graph_vol.endswith("-data") else graph_vol
            if neo:
                env["NEO4J_CONTAINER"] = neo
    return env


def _merge_env(extra: dict[str, str] | None) -> dict[str, str] | None:
    if not extra:
        return None
    return {**os.environ, **extra}


@dataclass
class CommandProbe:
    status: str = "ready"
    check_bsl_cmd: list[str] | None = None
    check_xml_cmd: list[str] | None = None
    check_deletion_cmd: list[str] | None = None
    runner: DockerRunner | None = None
    env: dict[str, str] = field(default_factory=dict)

    def _run(self, command: list[str] | None, path: str, kind: str) -> None:
        if not command:
            raise MissingAdapters(f"{kind} adapter command missing")
        docker = self.runner if self.runner is not None else SubprocessRunner()
        timeout = DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds
        merged = _merge_env(self.env)
        run = getattr(docker, "run")
        try:
            run(_render(command, path), timeout=timeout, env=merged)
        except TypeError:
            # Fakes / older runners may not accept env=.
            run(_render(command, path), timeout=timeout)

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
    graph: CommandProbe | None = None


class CommandEngine:
    """Docker runner that uses a configured idle-probe command. Not a boolean stub."""

    def __init__(
        self,
        idle_cmd: list[str],
        *,
        runner: DockerRunner | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if not idle_cmd:
            raise MissingAdapters("adapters.idle_probe is required")
        self._idle_cmd = list(idle_cmd)
        self._runner = runner if runner is not None else SubprocessRunner()
        self._env = dict(env or {})

    def idle_configured(self) -> bool:
        return True

    def run(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged_extra = {**self._env, **(env or {})}
        merged = _merge_env(merged_extra) if merged_extra else env
        try:
            return self._runner.run(cmd, timeout=timeout, env=merged)
        except TypeError:
            return self._runner.run(cmd, timeout=timeout)

    def idle_probe(self) -> bool:
        try:
            result = self.run(
                self._idle_cmd,
                timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
            )
        except Exception:
            return False
        return int(getattr(result, "returncode", 1) or 0) == 0


def build_probes(
    cfg,
    docker,
    *,
    required_layers: frozenset[str] | None = None,
) -> CommandProbes:
    adapters = getattr(cfg, "adapters", None)
    if adapters is None:
        raise MissingAdapters("adapters section is required")
    layers = required_layers or frozenset({"code_index", "graph"})
    env = profile_adapter_env(cfg)
    # Prefer env already bound on a profile-scoped CommandEngine.
    engine_env = getattr(docker, "_env", None)
    if isinstance(engine_env, dict) and engine_env:
        env = {**env, **engine_env}
    code_raw = getattr(adapters, "code_index", None)
    if "code_index" in layers and code_raw is None:
        raise MissingAdapters("adapters.code_index is required")
    code_index = CommandProbe(
        check_bsl_cmd=_require_cmd(
            "code_index", "check_bsl", getattr(code_raw, "check_bsl", None)
        ),
        check_deletion_cmd=_require_cmd(
            "code_index", "check_deletion", getattr(code_raw, "check_deletion", None)
        ),
        runner=docker,
        env=env,
    )
    graph = None
    if "graph" in layers:
        graph_raw = getattr(adapters, "graph", None)
        if graph_raw is None:
            raise MissingAdapters("adapters.graph is required")
        graph = CommandProbe(
            check_bsl_cmd=getattr(graph_raw, "check_bsl", None),
            check_xml_cmd=_require_cmd(
                "graph", "check_xml", getattr(graph_raw, "check_xml", None)
            ),
            check_deletion_cmd=_require_cmd(
                "graph", "check_deletion", getattr(graph_raw, "check_deletion", None)
            ),
            runner=docker,
            env=env,
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
