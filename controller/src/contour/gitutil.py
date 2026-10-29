from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def safe_remote(url: str) -> str:
    if "://" not in url:
        return url
    parsed = urlparse(url)
    if parsed.username is None and parsed.password is None:
        return url
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=host))


def _run_git(repo: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


def fetch(repo: str | Path) -> None:
    _run_git(repo, "fetch", "--prune", "origin")


# Local indexer / contour markers that must not block FF updates.
_IGNORE_UNTRACKED_NAMES = frozenset({".code-index", ".contour-revision"})


def _is_ignored_untracked(path: str) -> bool:
    """True for known local-only artifacts under a published source tree."""
    normalized = path.replace("\\", "/").strip().strip("/")
    if not normalized:
        return False
    parts = normalized.split("/")
    if any(part in _IGNORE_UNTRACKED_NAMES for part in parts):
        return True
    return False


def is_dirty(repo: str | Path) -> bool:
    repo_path = str(repo)
    working_tree = subprocess.run(
        ["git", "diff", "--quiet"],
        cwd=repo_path,
    )
    index = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_path,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    extras = [
        line
        for line in (untracked.stdout or "").splitlines()
        if line.strip() and not _is_ignored_untracked(line.strip())
    ]
    has_untracked = bool(extras)
    return working_tree.returncode != 0 or index.returncode != 0 or has_untracked


def ff_only(repo: str | Path, branch: str) -> str:
    _run_git(repo, "merge", "--ff-only", f"origin/{branch}")
    result = _run_git(repo, "rev-parse", "HEAD")
    return result.stdout.strip()


def rev_parse(repo: str | Path, ref: str = "HEAD") -> str:
    return _run_git(repo, "rev-parse", ref).stdout.strip()


def can_fast_forward(repo: str | Path, branch: str) -> bool:
    """True when ``HEAD`` is an ancestor of ``origin/<branch>`` (FF-only safe)."""
    tip = f"origin/{branch}"
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", tip],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
