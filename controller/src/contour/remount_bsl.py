"""Remount BSL bind after rename-activate; refuse stale/empty mounts.

Docker bind-mounts pin an inode. ``activate_source`` renames the published
tree, so a long-lived ``bsl-indexer`` container can keep serving a
``.prev-*`` or ``//deleted`` directory while the host path already points
at the new tree. The watcher then sees zero files and deletes the SQLite
index. Contour must restart the container and assert mount health before
indexing or restoring the watcher.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Sequence

from contour.config import DatabaseConfig, GitSourceConfig, PackageSourceConfig
from contour.dockerutil import DockerRunner, SubprocessRunner

SRC_MOUNT_POINT = "/repos/SRC"
DEFAULT_MIN_BSL_FILES = 1
DEFAULT_MIN_INDEX_FILES = 1


class BslMountError(RuntimeError):
    """BSL ``/repos/SRC`` mount is missing, stale, or empty."""


def published_workspace(database: DatabaseConfig, published_path: Path) -> Path:
    """Host directory bind-mounted as ``/repos/SRC`` for this database."""
    published = Path(published_path)
    source = database.source
    if isinstance(source, GitSourceConfig) and source.workspace_subdir:
        return published / source.workspace_subdir
    if isinstance(source, PackageSourceConfig):
        candidate = published / "src"
        if candidate.is_dir():
            return candidate
    candidate = published / "src"
    if candidate.is_dir():
        return candidate
    return published


def raw_src_mount_root(mountinfo: str) -> str | None:
    """Return the host-side root for ``/repos/SRC`` without health checks."""
    for line in mountinfo.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[4] == SRC_MOUNT_POINT:
            return parts[3]
    return None


def parse_src_mount_root(mountinfo: str) -> str:
    """Return the host-side root for ``/repos/SRC``, or raise if unsafe."""
    root = raw_src_mount_root(mountinfo)
    if root is None:
        raise BslMountError("no /repos/SRC mount in mountinfo")
    if "//deleted" in root or root.endswith("/deleted"):
        raise BslMountError(
            f"/repos/SRC mount root is deleted: {root}"
        )
    if ".prev-" in root:
        raise BslMountError(
            f"/repos/SRC mount still on previous snapshot: {root}"
        )
    return root


def _normalize_path(path: str | Path) -> str:
    text = str(path).replace("\\", "/").rstrip("/")
    return text


def _paths_match(mounted_root: str, published_src: Path) -> bool:
    left = _normalize_path(mounted_root)
    right = _normalize_path(published_src)
    try:
        left = _normalize_path(Path(mounted_root).resolve())
    except OSError:
        pass
    try:
        right = _normalize_path(Path(published_src).resolve())
    except OSError:
        pass
    return left == right


def _read_mountinfo(runner: DockerRunner, container: str) -> str:
    result = runner.run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            f"grep ' {SRC_MOUNT_POINT} ' /proc/self/mountinfo || true",
        ]
    )
    return result.stdout or ""


def _count_bsl_in_container(runner: DockerRunner, container: str) -> int:
    result = runner.run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            f'find {SRC_MOUNT_POINT} -name "*.bsl" 2>/dev/null | wc -l',
        ]
    )
    text = (result.stdout or "").strip().splitlines()
    if not text:
        return 0
    try:
        return int(text[-1].strip())
    except ValueError as exc:
        raise BslMountError(
            f"cannot parse bsl count from container: {result.stdout!r}"
        ) from exc


def assert_bsl_mount_healthy(
    runner: DockerRunner,
    container: str,
    *,
    published_src: Path,
    min_bsl_files: int = DEFAULT_MIN_BSL_FILES,
) -> str:
    """Assert container ``/repos/SRC`` matches ``published_src`` and has files."""
    mountinfo = _read_mountinfo(runner, container)
    root = parse_src_mount_root(mountinfo)
    if not _paths_match(root, published_src):
        raise BslMountError(
            f"/repos/SRC mount {root!r} does not match published "
            f"{published_src!r} (stale bind after activate)"
        )
    count = _count_bsl_in_container(runner, container)
    if count < int(min_bsl_files):
        raise BslMountError(
            f"/repos/SRC has {count} .bsl files; need >= {min_bsl_files}"
        )
    return root


def remount_bsl_src(
    runner: DockerRunner,
    container: str,
    *,
    published_src: Path,
    min_bsl_files: int = DEFAULT_MIN_BSL_FILES,
    settle_attempts: int = 30,
    settle_interval: float = 0.5,
) -> str:
    """Restart the BSL container so the bind remounts the current published tree."""
    runner.run(["docker", "restart", container])
    for attempt in range(max(1, settle_attempts)):
        status = runner.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}",
                container,
            ]
        )
        if (status.stdout or "").strip() == "running":
            break
        if attempt + 1 < settle_attempts and settle_interval > 0:
            time.sleep(settle_interval)
    else:
        raise BslMountError(
            f"container {container} not running after restart"
        )
    # Brief settle so mountinfo reflects the new bind.
    if settle_interval > 0:
        time.sleep(min(settle_interval, 1.0))
    return assert_bsl_mount_healthy(
        runner,
        container,
        published_src=published_src,
        min_bsl_files=min_bsl_files,
    )


def assert_code_index_nonempty(
    published_src: Path,
    *,
    min_files: int = DEFAULT_MIN_INDEX_FILES,
) -> int:
    """Refuse to open reads when the on-disk code index has no files."""
    db_path = Path(published_src) / ".code-index" / "index.db"
    if not db_path.is_file():
        raise BslMountError(f"code index missing: {db_path}")
    if db_path.stat().st_size < 64:
        raise BslMountError(f"code index empty file: {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BslMountError(f"cannot open code index {db_path}: {exc}") from exc
    try:
        try:
            row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
        except sqlite3.Error as exc:
            raise BslMountError(
                f"code index has no usable files table: {exc}"
            ) from exc
        count = int(row[0]) if row else 0
    finally:
        conn.close()
    if count < int(min_files):
        raise BslMountError(
            f"code index empty: files={count}, need >= {min_files} ({db_path})"
        )
    return count


def list_bsl_mount_roots(
    runner: DockerRunner, containers: Sequence[str]
) -> list[str]:
    """Best-effort mount roots for prune safety (includes stale/deleted)."""
    roots: list[str] = []
    for container in containers:
        try:
            mountinfo = _read_mountinfo(runner, container)
            root = raw_src_mount_root(mountinfo)
            if root:
                roots.append(root)
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            continue
    return roots


def remount_after_activate(
    database: DatabaseConfig,
    published_path: Path,
    *,
    runner: DockerRunner | None = None,
    min_bsl_files: int = DEFAULT_MIN_BSL_FILES,
) -> str | None:
    """Pipeline hook: assert mount health; remount only if unhealthy.

    In-place activate keeps the published inode stable, so a restart every
    night is unnecessary. Restart remains an emergency fix for stale binds
    left by older rename-activate runs.
    """
    if "code_index" not in database.required_layers:
        return None
    docker = runner if runner is not None else SubprocessRunner()
    src = published_workspace(database, published_path)
    container = database.services.bsl_container
    try:
        return assert_bsl_mount_healthy(
            docker,
            container,
            published_src=src,
            min_bsl_files=min_bsl_files,
        )
    except BslMountError:
        return remount_bsl_src(
            docker,
            container,
            published_src=src,
            min_bsl_files=min_bsl_files,
        )
