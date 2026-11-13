"""BSL daemon control in-process (no bash wrappers for idle/reload)."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from contour.dockerutil import DockerRunner, SubprocessRunner
from contour.runtime_policy import DEFAULT_RUNTIME_POLICY

_BUSY = frozenset(
    {"indexing", "reindexing", "initial_indexing", "stopping_workers", "error"}
)


def _parse_json_object(text: str) -> dict[str, Any]:
    """First JSON object in noisy daemon output (status --json)."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise RuntimeError("no JSON object in daemon output")


def _parse_reload_json(text: str) -> dict[str, Any]:
    """Prefer the object that carries ``reloaded`` amid log noise."""
    decoder = json.JSONDecoder()
    fallback: dict[str, Any] | None = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if "reloaded" in data:
            return data
        if fallback is None:
            fallback = data
    if fallback is not None:
        return fallback
    raise RuntimeError("no JSON object in daemon output")


def idle_bsl(
    container: str,
    *,
    runner: DockerRunner | None = None,
) -> bool:
    """True when bsl-indexer daemon reports running and no path is busy."""
    docker = runner if runner is not None else SubprocessRunner()
    budget = DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds
    try:
        result = docker.run(
            [
                "docker",
                "exec",
                container,
                "bsl-indexer",
                "daemon",
                "status",
                "--json",
            ],
            timeout=budget,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    try:
        data = _parse_json_object((result.stdout or "") + (result.stderr or ""))
    except RuntimeError:
        return False
    if data.get("status") != "running":
        return False
    paths = data.get("paths")
    if not isinstance(paths, list):
        return False
    for item in paths:
        if not isinstance(item, dict):
            return False
        st = item.get("status")
        if st in _BUSY:
            return False
        if st is not None and st != "ready":
            return False
    return True


def reload_bsl(
    container: str,
    *,
    runner: DockerRunner | None = None,
) -> None:
    """Reload bsl-indexer daemon; require ``reloaded: true``."""
    docker = runner if runner is not None else SubprocessRunner()
    budget = DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds
    result = docker.run(
        [
            "docker",
            "exec",
            "-e",
            "CODE_INDEX_HOME=/data/code-index-home",
            container,
            "bsl-indexer",
            "daemon",
            "reload",
        ],
        timeout=budget,
    )
    text = (result.stdout or "") + (result.stderr or "")
    data = _parse_reload_json(text)
    if data.get("reloaded") is not True:
        raise RuntimeError(f"daemon reload reloaded!=true: {data.get('error')!r}")
