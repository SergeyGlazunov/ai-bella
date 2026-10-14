from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from contour import gitutil
from contour.config import GitSourceConfig
from contour.packages import ValidatedPackage, validate_package

REVISION_MARKER = ".contour-revision"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class SourceError(ValueError):
    """Source staging or activation failed."""


@dataclass(frozen=True)
class PreparedSource:
    database: str
    revision: str
    staged_path: Path
    previous_path: Path | None
    sources_root: Path
    published_path: Path


class SourceProvider(Protocol):
    def prepare(self) -> PreparedSource: ...


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve()


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink() or os.path.islink(path):
            return True
    except OSError:
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError:
            pass
    try:
        st = os.lstat(path)
    except OSError:
        return False
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def safe_revision_token(revision: str) -> str:
    """Filesystem-safe opaque token for path components (keeps original in marker)."""
    if not revision or any(ch in revision for ch in ("/", "\\", "\0")):
        raise SourceError(f"unsafe revision for path use: {revision!r}")
    digest = hashlib.sha256(revision.encode("utf-8")).hexdigest()[:20]
    return digest


def ensure_under_sources_root(path: Path, sources_root: Path) -> Path:
    if _is_reparse_point(path) and path.exists():
        # Reject reparse before resolve so we do not follow into targets.
        raise SourceError(f"reparse/symlink path not allowed: {path}")
    resolved = _resolve(path)
    root = _resolve(sources_root)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SourceError(
            f"path escapes sources root {root}: {resolved}"
        ) from exc
    return resolved


def _paths_overlap(a: Path, b: Path) -> bool:
    try:
        ra, rb = _resolve(a), _resolve(b)
    except OSError:
        return True
    if ra == rb:
        return True
    try:
        ra.relative_to(rb)
        return True
    except ValueError:
        pass
    try:
        rb.relative_to(ra)
        return True
    except ValueError:
        pass
    return False


def _assert_disjoint(*paths: Path) -> None:
    items = [p for p in paths if p is not None]
    for i, left in enumerate(items):
        for right in items[i + 1 :]:
            if _paths_overlap(left, right):
                raise SourceError(
                    f"source paths must not overlap: {left} and {right}"
                )


def read_revision(path: Path) -> str | None:
    marker = path / REVISION_MARKER
    if not marker.is_file() or _is_reparse_point(marker):
        return None
    text = marker.read_text(encoding="utf-8").strip()
    if not text or any(ch in text for ch in ("/", "\\", "\0")):
        raise SourceError(f"invalid revision marker at {marker}")
    return text


def write_revision(path: Path, revision: str) -> None:
    if any(ch in revision for ch in ("/", "\\", "\0")):
        raise SourceError(f"unsafe revision marker value: {revision!r}")
    (path / REVISION_MARKER).write_text(revision + "\n", encoding="utf-8")


def _exclude_revision_marker_from_git(repo: Path) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = REVISION_MARKER
    existing = ""
    if exclude.is_file():
        existing = exclude.read_text(encoding="utf-8")
    if line not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(line + "\n")


def _promote_dir(temp: Path, final: Path) -> None:
    """Replace ``final`` with ``temp`` only after ``temp`` is fully prepared."""
    if _is_reparse_point(final) or _is_reparse_point(temp):
        raise SourceError("cannot promote reparse/symlink staging paths")
    if not final.exists():
        temp.replace(final)
        return
    backup = final.parent / f".bak-{uuid.uuid4().hex}"
    final.replace(backup)
    try:
        temp.replace(final)
    except Exception:
        if backup.exists() and not final.exists():
            backup.replace(final)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def activate_source(prepared: PreparedSource, published_path: Path) -> Path | None:
    """Publish staged snapshot onto ``published_path`` via rename.

    Returns the previous published directory when one was moved aside or when a
    known ``prepared.previous_path`` remains the recovery handle. Same-revision
    retries preserve that handle.
    """
    sources_root = _resolve(prepared.sources_root)
    published = ensure_under_sources_root(published_path, sources_root)
    intended = ensure_under_sources_root(prepared.published_path, sources_root)
    if published != intended:
        raise SourceError(
            f"published path mismatch: got {published}, expected {intended}"
        )
    staged = Path(prepared.staged_path)
    if staged.exists() and _is_reparse_point(staged):
        raise SourceError(f"staged path must not be a reparse point: {staged}")
    if staged.exists():
        staged = ensure_under_sources_root(staged, sources_root)
        _assert_disjoint(staged, published)
        marker_rev = read_revision(staged)
        if marker_rev != prepared.revision:
            raise SourceError(
                f"staged revision marker {marker_rev!r} != prepared {prepared.revision!r}"
            )
    elif not (
        prepared.previous_path is not None
        and published.exists()
        and read_revision(published) == prepared.revision
    ):
        raise SourceError(f"staged path missing: {staged}")

    known_previous: Path | None = None
    if prepared.previous_path is not None:
        known_previous = ensure_under_sources_root(
            prepared.previous_path, sources_root
        )
        if _paths_overlap(known_previous, published) or (
            staged.exists() and _paths_overlap(known_previous, staged)
        ):
            raise SourceError("previous_path overlaps published/staged")

    if published.exists() and read_revision(published) == prepared.revision:
        return known_previous

    previous = known_previous
    if not published.exists():
        if previous is None or not staged.exists():
            raise SourceError("cannot activate: published missing without staged/previous")
        try:
            staged.replace(published)
        except Exception:
            raise
        return previous

    if _is_reparse_point(published):
        raise SourceError("published path must not be a reparse point")
    if previous is None:
        old_rev = read_revision(published) or "unknown"
        previous = published.parent / f"{published.name}.prev-{safe_revision_token(old_rev)}"
        if previous.exists():
            raise SourceError(f"previous source already exists: {previous}")
        published.replace(previous)
    else:
        # Live published still present while caller already holds a previous handle
        # (unexpected for normal retry); move live tree to a unique aside.
        aside = published.parent / f"{published.name}.aside-{uuid.uuid4().hex}"
        published.replace(aside)

    try:
        if not staged.exists():
            raise SourceError(f"staged path missing: {staged}")
        staged.replace(published)
    except Exception:
        if previous is not None and previous.exists() and not published.exists():
            previous.replace(published)
        raise
    return previous


def rollback_source(
    published_path: Path,
    previous_path: Path,
    *,
    sources_root: Path,
) -> None:
    """Restore ``previous_path`` as the published source directory."""
    root = _resolve(sources_root)
    published = ensure_under_sources_root(published_path, root)
    previous = ensure_under_sources_root(previous_path, root)
    if _resolve(published) == _resolve(previous):
        raise SourceError("rollback refuses the same directory for previous/published")
    _assert_disjoint(published, previous)
    if _is_reparse_point(published) or _is_reparse_point(previous):
        raise SourceError("rollback refuses reparse/symlink paths")
    if not previous.exists():
        raise SourceError(f"previous source missing: {previous}")

    failed = published.parent / f"{published.name}.failed-rollback"
    moved_current = False
    if published.exists():
        if failed.exists():
            raise SourceError(f"failed-rollback path already exists: {failed}")
        published.replace(failed)
        moved_current = True

    try:
        previous.replace(published)
    except Exception:
        if moved_current and failed.exists() and not published.exists():
            try:
                failed.replace(published)
            except Exception as compensate_exc:
                raise SourceError(
                    f"rollback failed; current at {failed}, previous at {previous}: "
                    f"{compensate_exc}"
                ) from compensate_exc
        raise

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
        sources_root = _resolve(self._sources_root)
        staging_root = Path(self._staging_root)
        published = Path(self._published_path)
        if _is_reparse_point(staging_root):
            raise SourceError("staging_root must not be a reparse/symlink")
        staging_root = ensure_under_sources_root(staging_root, sources_root)
        published = ensure_under_sources_root(published, sources_root)
        _assert_disjoint(staging_root, published)

        repo = Path(self._config.path)
        if gitutil.is_dirty(repo):
            raise SourceError("git working tree is dirty")
        gitutil.fetch(repo)
        if not gitutil.can_fast_forward(repo, self._config.branch):
            raise SourceError("update is not a fast-forward")

        revision = gitutil.rev_parse(repo, f"origin/{self._config.branch}")
        token = safe_revision_token(revision)
        final = staging_root / self._database / token
        if _is_reparse_point(final):
            raise SourceError(f"staged path must not be a reparse point: {final}")
        if final.exists() and _paths_overlap(final, published):
            raise SourceError("staging target overlaps published source")

        temp = staging_root / self._database / f".tmp-{uuid.uuid4().hex}"
        if temp.exists():
            shutil.rmtree(temp)
        try:
            subprocess.run(
                ["git", "clone", "--quiet", str(repo), str(temp)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "checkout", "--quiet", "--detach", revision],
                cwd=str(temp),
                check=True,
                capture_output=True,
                text=True,
            )
            # Keep configured upstream, not the local clone source.
            subprocess.run(
                ["git", "remote", "set-url", "origin", self._config.remote],
                cwd=str(temp),
                check=True,
                capture_output=True,
                text=True,
            )
            _exclude_revision_marker_from_git(temp)
            write_revision(temp, revision)
            head = gitutil.rev_parse(temp, "HEAD")
            if head != revision:
                raise SourceError(
                    f"staged HEAD {head} does not match target {revision}"
                )
            _promote_dir(temp, final)
        except Exception:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
            raise

        # Exclude marker on published checkout too (same path as config.path).
        if published.exists() and (published / ".git").exists():
            _exclude_revision_marker_from_git(published)

        return PreparedSource(
            database=self._database,
            revision=revision,
            staged_path=final,
            previous_path=None,
            sources_root=sources_root,
            published_path=published,
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
        sources_root = _resolve(self._sources_root)
        staging_root = Path(self._staging_root)
        published = Path(self._published_path)
        if _is_reparse_point(staging_root):
            raise SourceError("staging_root must not be a reparse/symlink")
        staging_root = ensure_under_sources_root(staging_root, sources_root)
        published = ensure_under_sources_root(published, sources_root)
        _assert_disjoint(staging_root, published)

        # Immutable ownership: re-validate on-disk package before staging.
        current = validate_package(
            self._package.path,
            frozenset({self._package.manifest.database}),
        )
        if (
            current.revision != self._package.revision
            or current.manifest != self._package.manifest
        ):
            raise SourceError("package changed after validation")

        database = current.manifest.database
        revision = current.revision
        token = safe_revision_token(revision)
        final = staging_root / database / token
        if _is_reparse_point(final):
            raise SourceError(f"staged path must not be a reparse point: {final}")
        if final.exists() and _paths_overlap(final, published):
            raise SourceError("staging target overlaps published source")

        src = self._package.path / "src"
        if not src.is_dir() or _is_reparse_point(src):
            raise SourceError("validated package missing real src/")

        temp = staging_root / database / f".tmp-{uuid.uuid4().hex}"
        if temp.exists():
            shutil.rmtree(temp)
        try:
            shutil.copytree(src, temp)
            write_revision(temp, revision)
            if read_revision(temp) != revision:
                raise SourceError("failed to write staged revision marker")
            _promote_dir(temp, final)
        except Exception:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
            raise

        return PreparedSource(
            database=database,
            revision=revision,
            staged_path=final,
            previous_path=None,
            sources_root=sources_root,
            published_path=published,
        )
