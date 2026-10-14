from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

SRC_WATCH_PATH = "/repos/SRC"
_STAMP_SUFFIX = ".contour-pause-hash"


def reload_bsl_daemon(cfg) -> None:
    """Reload bsl-indexer daemon for the database/services in ``cfg``.

    ``cfg`` may be a full Contour config or a ``DatabaseConfig``; both expose
    ``services.bsl_container``. Passes ``BSL_CONTAINER`` to integration scripts
    so multi-db profiles do not reload the default UBD indexer.
    """
    container = cfg.services.bsl_container
    env = {**os.environ, "BSL_CONTAINER": container}
    candidates = [
        Path("/opt/ai-sync/integrations/reload_bsl_watch.sh"),
        Path(__file__).resolve().parents[3] / "integrations" / "reload_bsl_watch.sh",
    ]
    for script in candidates:
        if script.is_file():
            subprocess.run([str(script)], check=True, env=env)
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
    )
    text = (result.stdout or "") + (result.stderr or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("daemon reload produced no JSON")
    data = json.loads(text[start : end + 1])
    if data.get("reloaded") is not True:
        raise RuntimeError(f"daemon reload reloaded!=true: {data.get('error')!r}")


def _is_src_paths_table(block: str) -> bool:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("path") and "=" in stripped:
            _, _, value = stripped.partition("=")
            value = value.strip().strip("'\"")
            return value == SRC_WATCH_PATH
    return False


def _strip_src_paths(text: str) -> str:
    parts = re.split(r"(?=^\[\[paths\]\])", text, flags=re.MULTILINE)
    kept: list[str] = []
    for part in parts:
        if part.startswith("[[paths]]") and _is_src_paths_table(part):
            continue
        kept.append(part)
    stripped = "".join(kept)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip() + ("\n" if stripped.strip() else "")


def _stamp_path(toml_path: str) -> Path:
    return Path(toml_path + _STAMP_SUFFIX)


def pause_watch(toml_path: str, reload: Callable) -> None:
    path = Path(toml_path)
    if not path.is_file():
        raise FileNotFoundError(f"daemon.toml not found: {toml_path}")
    written = _strip_src_paths(path.read_text(encoding="utf-8"))
    path.write_text(written, encoding="utf-8")
    _stamp_path(toml_path).write_text(
        sha256(written.encode("utf-8")).hexdigest(), encoding="utf-8"
    )
    reload()


def watch_is_paused(inspect_fn) -> bool:
    return SRC_WATCH_PATH not in inspect_fn()


def restore_watch(toml_path: str, backup: str, reload: Callable) -> None:
    path = Path(toml_path)
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    stamp = _stamp_path(toml_path)
    if stamp.is_file():
        expected = stamp.read_text(encoding="utf-8").strip()
        actual = sha256(current.encode("utf-8")).hexdigest()
        if expected and actual != expected:
            raise RuntimeError(
                "daemon.toml changed after pause; refusing to overwrite with backup"
            )
    path.write_text(Path(backup).read_text(encoding="utf-8"), encoding="utf-8")
    if stamp.is_file():
        stamp.unlink()
    reload()
