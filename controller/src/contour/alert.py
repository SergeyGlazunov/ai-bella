from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ALERT_TIMEOUT_SECONDS = 30


def command_is_runnable(argv0: str) -> bool:
    """True if alert.command[0] exists and can be executed."""
    if not argv0:
        return False
    if shutil.which(argv0):
        return True
    path = Path(argv0)
    if not path.is_file():
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def send(cfg, message: str) -> None:
    """Run cfg.alert.command with message on stdin. Never put payload in argv.

    Never raises. Non-zero exit and timeouts are recorded on cfg if possible
    but must not hide the original reconcile error.
    """
    command = getattr(getattr(cfg, "alert", None), "command", None)
    if not command:
        return
    try:
        result = subprocess.run(
            command,
            input=message,
            text=True,
            capture_output=True,
            check=False,
            timeout=ALERT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError, TimeoutError):
        _note_delivery(cfg, "alert delivery failed")
        return
    except Exception:
        _note_delivery(cfg, "alert delivery failed")
        return
    if result.returncode != 0:
        _note_delivery(cfg, f"alert.command exit {result.returncode}")


def _note_delivery(cfg, text: str) -> None:
    notes = getattr(cfg, "_alert_delivery_errors", None)
    if notes is None:
        try:
            object.__setattr__(cfg, "_alert_delivery_errors", [text])
        except (AttributeError, TypeError):
            return
    else:
        notes.append(text)
