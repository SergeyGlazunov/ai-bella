from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _parse_daemon_reload_json(text: str) -> dict:
    """Extract the daemon-reload object from mixed stdout/stderr.

    Prefer an object that contains ``reloaded``; do not assume the first ``{``
    and last ``}`` form a single JSON value (logs may contain braces).
    """
    decoder = json.JSONDecoder()
    fallback: dict | None = None
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
    raise RuntimeError("daemon reload produced no JSON object")


def reload_bsl_daemon(cfg) -> None:
    """Reload bsl-indexer daemon for the database/services in ``cfg``.

    ``cfg`` may be a full Contour config or a ``DatabaseConfig``; both expose
    ``services.bsl_container``. Passes ``BSL_CONTAINER`` to integration scripts
    so multi-db profiles do not reload the default UBD indexer.
    """
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY

    container = cfg.services.bsl_container
    env = {**os.environ, "BSL_CONTAINER": container}
    budget = DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds
    here = Path(__file__).resolve()
    # Absolute install paths as plain strings so Windows Path does not rewrite
    # leading "/opt/..." when unit tests import this module on a Windows host.
    candidates = [
        "/opt/ai-contour/integrations/reload_bsl_watch.sh",
        # Legacy install path (pre-rename); kept so old stands still hit the script.
        "/opt/ai-sync/integrations/reload_bsl_watch.sh",
        str(here.parents[3] / "integrations" / "reload_bsl_watch.sh"),
    ]
    for script in candidates:
        if Path(script).is_file():
            subprocess.run(
                [script], check=True, env=env, timeout=budget
            )
            return
    result = subprocess.run(
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
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=budget,
    )
    text = (result.stdout or "") + (result.stderr or "")
    data = _parse_daemon_reload_json(text)
    if data.get("reloaded") is not True:
        raise RuntimeError(f"daemon reload reloaded!=true: {data.get('error')!r}")
