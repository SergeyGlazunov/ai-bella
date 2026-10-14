from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from contour.config import GitSourceConfig
from contour import gitutil
from contour.packages import ValidatedPackage

REVISION_MARKER = ".contour-revision"


class SourceError(ValueError):
    """Source staging or activation failed."""


@dataclass(frozen=True)
class PreparedSource:
    database: str
    revision: str
    staged_path: Path
    previous_path: Path | None


class SourceProvider(Protocol):
    def prepare(self) -> PreparedSource: ...


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve()


def ensure_under_sources_root(path: Path, sources_root: Path) -> Path:
    resolved = _resolve(path)
    root = _resolve(sources_root)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SourceError(
            f"path escapes sources root {root}: {resolved}"
        ) from exc
    return resolved


def read_revision(path: Path) -> str | None:
    marker = path / REVISION_MARKER
    if not marker.is_file():
        return None
    text = marker.read_text(encoding="utf-8").strip()
    return text or None


def write_revision(path: Path, revision: str) -> None:
    (path / REVISION_MARKER).write_text(revision + "\n", encoding="utf-8")


def activate_source(prepared: PreparedSource, published_path: Path) -> Path | None:
    """Publish ``prepared.staged_path`` onto ``published_path`` via rename.

    Returns the previous published directory path when one was moved aside,
    otherwise ``None``. Same-revision retries are no-ops.
    """
    published = _resolve(published_path)
    staged = _resolve(prepared.staged_path)
    if published.exists() and read_revision(published) == prepared.revision:
        return None

    previous: Path | None = None
    if published.exists():
        old_rev = read_revision(published) or "unknown"
        previous = published.parent / f"{published.name}.prev-{old_rev}"
        if previous.exists():
            raise SourceError(f"previous source already exists: {previous}")
        published.replace(previous)

    try:
        staged.replace(published)
    except Exception:
        if previous is not None and previous.exists() and not published.exists():
            previous.replace(published)
        raise
    return previous


def rollback_source(published_path: Path, previous_path: Path) -> None:
    """Restore ``previous_path`` as the published source directory."""
    published = _resolve(published_path)
    previous = _resolve(previous_path)
    if not previous.exists():
        raise SourceError(f"previous source missing: {previous}")
    failed = published.parent / f"{published.name}.failed-rollback"
    if published.exists():
        if failed.exists():
            shutil.rmtree(failed)
        published.replace(failed)
    previous.replace(published)
    if failed.exists():
        shutil.rmtree(failed)


class GitSourceProvider:
    """Stage a Git revision without mutating the published source tree."""

    def __init__(
        self,
        config: GitSourceConfig,
        *,
        staging_root: Path,
        published_path: Path,
        sources_root: Path,
        database: str = "ubd",
    ) -> None:
        self._config = config
        self._staging_root = Path(staging_root)
        self._published_path = Path(published_path)
        self._sources_root = Path(sources_root)
        self._database = database

    def prepare(self) -> PreparedSource:
        ensure_under_sources_root(self._staging_root, self._sources_root)
        ensure_under_sources_root(self._published_path, self._sources_root)

        repo = Path(self._config.path)
        if gitutil.is_dirty(repo):
            raise SourceError("git working tree is dirty")
        gitutil.fetch(repo)
        if not gitutil.can_fast_forward(repo, self._config.branch):
            raise SourceError("update is not a fast-forward")

        revision = gitutil.rev_parse(repo, f"origin/{self._config.branch}")
        staged = ensure_under_sources_root(
            self._staging_root / self._database / revision,
            self._sources_root,
        )
        if staged.exists():
            shutil.rmtree(staged)
        subprocess.run(
            ["git", "clone", "--quiet", str(repo), str(staged)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "checkout", "--quiet", "--detach", revision],
            cwd=str(staged),
            check=True,
            capture_output=True,
            text=True,
        )
        write_revision(staged, revision)
        return PreparedSource(
            database=self._database,
            revision=revision,
            staged_path=staged,
            previous_path=None,
        )


class PackageSourceProvider:
    """Stage package ``src/`` into a revision directory; never touch published."""

    def __init__(
        self,
        package: ValidatedPackage,
        *,
        staging_root: Path,
        published_path: Path,
        sources_root: Path,
    ) -> None:
        self._package = package
        self._staging_root = Path(staging_root)
        self._published_path = Path(published_path)
        self._sources_root = Path(sources_root)

    def prepare(self) -> PreparedSource:
        ensure_under_sources_root(self._staging_root, self._sources_root)
        ensure_under_sources_root(self._published_path, self._sources_root)

        database = self._package.manifest.database
        revision = self._package.revision
        # Avoid path separators in revision for directory names.
        safe_rev = revision.replace(":", "_").replace("/", "_")
        staged = ensure_under_sources_root(
            self._staging_root / database / safe_rev,
            self._sources_root,
        )
        if staged.exists():
            shutil.rmtree(staged)
        src = self._package.path / "src"
        if not src.is_dir():
            raise SourceError("validated package missing src/")
        shutil.copytree(src, staged)
        write_revision(staged, revision)
        return PreparedSource(
            database=database,
            revision=revision,
            staged_path=staged,
            previous_path=None,
        )
