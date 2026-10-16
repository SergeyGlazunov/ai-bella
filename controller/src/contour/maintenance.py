"""Retention and free-space maintenance for published sources (review 1.2).

Never removes the last published source package or state needed for current
recovery (design §10): the package matching the published revision and the
newest previous-source handle always stay.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_GIB = 1024**3


def check_free_space(root: Path, *, min_free_gb: float) -> None:
    """Raise RuntimeError when free space under ``root`` is below the threshold."""
    usage = shutil.disk_usage(str(root))
    free_gib = usage.free / _GIB
    if free_gib < float(min_free_gb):
        raise RuntimeError(
            f"free space {free_gib:.1f} GiB below threshold {min_free_gb} GiB "
            f"under {root}"
        )


def _read_manifest(package_dir: Path) -> dict | None:
    manifest = package_dir / "manifest.json"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _manifest_finished_at(package_dir: Path) -> datetime | None:
    raw = _read_manifest(package_dir)
    if not raw:
        return None
    finished = raw.get("finished_at")
    if not isinstance(finished, str):
        return None
    try:
        parsed = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _manifest_revision(package_dir: Path) -> str | None:
    raw = _read_manifest(package_dir)
    if not raw:
        return None
    checksum = raw.get("checksum")
    export_id = raw.get("export_id")
    if isinstance(checksum, str) and isinstance(export_id, str):
        if checksum and export_id:
            return f"{checksum}:{export_id}"
    return None


def prune_accepted_packages(
    accepted_root: Path,
    *,
    published_revision: str | None,
    keep: int = 3,
) -> list[str]:
    """Delete old accepted ``ready-*`` packages beyond the newest ``keep``.

    The package matching ``published_revision`` (current recovery source) is
    never removed. Candidates without a parseable manifest are left in place —
    the operator decides.
    """
    root = Path(accepted_root)
    if not root.is_dir():
        return []
    candidates: list[tuple[datetime, str, Path]] = []
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("ready-"):
            continue
        finished = _manifest_finished_at(child)
        if finished is None:
            continue
        candidates.append((finished, child.name, child))
    if len(candidates) <= keep:
        return []
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    removed: list[str] = []
    for _finished, name, path in candidates[keep:]:
        if published_revision and _manifest_revision(path) == published_revision:
            continue
        shutil.rmtree(path)
        removed.append(name)
    return removed


def prune_previous_sources(published_path: Path, *, keep: int = 1) -> list[str]:
    """Delete older ``<name>.prev-*`` snapshots, keeping the newest ``keep``.

    The newest previous snapshot is the documented rollback handle; older
    ones are dead weight once a newer revision published successfully.
    """
    published = Path(published_path)
    parent = published.parent
    if not parent.is_dir():
        return []
    prevs = sorted(
        (path for path in parent.glob(f"{published.name}.prev-*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for path in prevs[max(keep, 0) :]:
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def prune_run_history(conn: sqlite3.Connection, *, days: int = 90) -> int:
    """Delete finished runs/jobs older than ``days``; never touches unfinished work."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    cur = conn.execute(
        """
        DELETE FROM jobs WHERE status IN ('ok', 'failed', 'removed')
          AND run_id IN (SELECT id FROM runs WHERE started_at < ?)
        """,
        (cutoff,),
    )
    deleted = int(cur.rowcount or 0)
    cur = conn.execute(
        """
        DELETE FROM runs WHERE started_at < ?
          AND id NOT IN (SELECT DISTINCT run_id FROM jobs)
        """,
        (cutoff,),
    )
    deleted += int(cur.rowcount or 0)
    conn.commit()
    return deleted
