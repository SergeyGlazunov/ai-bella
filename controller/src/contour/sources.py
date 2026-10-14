from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse, urlunparse
from urllib.request import url2pathname

from contour import gitutil
from contour.config import DatabaseConfig, GitSourceConfig, PackageSourceConfig
from contour.packages import ValidatedPackage, validate_package

REVISION_MARKER = ".contour-revision"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
SourceKind = Literal["git", "package"]


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
    kind: SourceKind = "package"
    content_digest: str | None = None


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
    if path.exists() and _is_reparse_point(path):
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


def ensure_path_chain_under_sources_root(path: Path, sources_root: Path) -> Path:
    """Ensure every existing ancestor of ``path`` is a real directory under root.

    Lexical ``Path`` joins are not treated as a boundary check: each existing
    segment is inspected for reparse points and resolved under ``sources_root``.
    Non-existent leaves are accepted only when their resolved parent stays under
    the root and the leaf name is a single safe path component.
    """
    root = _resolve(sources_root)
    path = Path(path)
    if path.name in ("", ".", "..") or "/" in path.name or "\\" in path.name:
        raise SourceError(f"unsafe path component: {path.name!r}")

    chain: list[Path] = []
    cur = path
    for _ in range(128):
        chain.append(cur)
        if cur.exists() and not _is_reparse_point(cur):
            try:
                if _resolve(cur) == root:
                    break
            except OSError as exc:
                raise SourceError(f"cannot resolve path chain at {cur}") from exc
        if cur.parent == cur:
            break
        cur = cur.parent
    else:
        raise SourceError(f"path chain too deep: {path}")

    for node in reversed(chain):
        if not node.exists():
            continue
        if _is_reparse_point(node):
            raise SourceError(f"reparse/symlink path not allowed: {node}")
        ensure_under_sources_root(node, sources_root)

    if path.exists():
        return ensure_under_sources_root(path, sources_root)

    parent = path.parent
    if not parent.exists():
        # Recursively ensure parents that should be created stay under root.
        parent_resolved = ensure_path_chain_under_sources_root(parent, sources_root)
    else:
        if _is_reparse_point(parent):
            raise SourceError(f"reparse/symlink path not allowed: {parent}")
        parent_resolved = ensure_under_sources_root(parent, sources_root)
    candidate = parent_resolved / path.name
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SourceError(
            f"path escapes sources root {root}: {candidate}"
        ) from exc
    return candidate


def _ensure_staging_database_dir(
    staging_root: Path, database: str, sources_root: Path
) -> Path:
    if not database or any(ch in database for ch in ("/", "\\", "\0", "..")):
        raise SourceError(f"unsafe database segment: {database!r}")
    staging_root = ensure_under_sources_root(staging_root, sources_root)
    db_dir = staging_root / database
    if db_dir.exists():
        if _is_reparse_point(db_dir):
            raise SourceError(f"reparse/symlink path not allowed: {db_dir}")
        return ensure_under_sources_root(db_dir, sources_root)
    # Create only as a real child of an already-verified staging_root.
    db_dir.mkdir(parents=False, exist_ok=True)
    if _is_reparse_point(db_dir):
        raise SourceError(f"reparse/symlink path not allowed: {db_dir}")
    return ensure_under_sources_root(db_dir, sources_root)


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


# Volatile Git metadata. Identity-bearing files (HEAD, config, hooks, refs)
# stay in the digest so a post-prepare edit cannot ride along into published.
_GIT_DIGEST_SKIP_DIRS = {".git/objects", ".git/logs", ".git/modules"}
_GIT_DIGEST_SKIP_FILES = {
    ".git/index",
    ".git/index.lock",
    ".git/FETCH_HEAD",
    ".git/ORIG_HEAD",
    ".git/COMMIT_EDITMSG",
    ".git/config.lock",
}
_URL_USERINFO = re.compile(r"://[^/\s]+@")


def _redact(text: str) -> str:
    return _URL_USERINFO.sub("://", text or "")


def compute_staged_tree_digest(root: Path) -> str:
    """Digest snapshot bytes, excluding the service marker and volatile Git files."""
    if not root.is_dir() or _is_reparse_point(root):
        raise SourceError(f"staged tree must be a real directory: {root}")
    root_resolved = _resolve(root)
    lines: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        parent = Path(dirpath)
        kept_dirs: list[str] = []
        for name in dirnames:
            entry = parent / name
            rel = entry.relative_to(root).as_posix()
            if _is_reparse_point(entry):
                raise SourceError(f"unsupported staged entry: {rel}")
            if rel in _GIT_DIGEST_SKIP_DIRS:
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in filenames:
            entry = parent / name
            rel = entry.relative_to(root).as_posix()
            if rel == REVISION_MARKER or rel in _GIT_DIGEST_SKIP_FILES:
                continue
            if rel.startswith(".git/objects/") or rel.startswith(".git/logs/"):
                continue
            if _is_reparse_point(entry):
                raise SourceError(f"unsupported staged entry: {rel}")
            mode = entry.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                raise SourceError(f"unsupported staged entry: {rel}")
            try:
                entry.resolve().relative_to(root_resolved)
            except ValueError as exc:
                raise SourceError(f"staged entry escapes tree: {rel}") from exc
            data = entry.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            lines.append(f"{rel}\0{len(data)}\0{digest}")
    lines.sort()
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _copy_source_tree(src: Path, dst: Path) -> None:
    """Copy a directory tree without following symlinks or junctions."""
    if _is_reparse_point(src) or not src.is_dir():
        raise SourceError(f"refusing to copy reparse or non-directory: {src}")
    dst.mkdir(parents=False, exist_ok=False)
    for entry in src.iterdir():
        target = dst / entry.name
        if _is_reparse_point(entry):
            raise SourceError(f"refusing to copy reparse point: {entry}")
        if entry.is_dir():
            _copy_source_tree(entry, target)
        elif entry.is_file():
            shutil.copyfile(entry, target)
        else:
            raise SourceError(f"unsupported source entry: {entry}")


def _unexpected_git_changes(repo: Path) -> str | None:
    """Return a status line for any change except the service revision marker.

    Ignored and untracked files count: they are still published with the tree.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--ignored=matching",
                "--untracked-files=all",
            ],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        raise SourceError("git status failed") from None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].replace("\\", "/").strip().strip('"') if len(line) >= 4 else line
        if path == REVISION_MARKER:
            continue
        return line
    return None


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


def _looks_like_url(value: str) -> bool:
    return "://" in value or value.startswith("git@")


def _normalize_local_path(value: str) -> str:
    try:
        resolved = str(Path(value).expanduser().resolve())
    except OSError:
        resolved = value
    if os.name == "nt":
        resolved = os.path.normcase(resolved)
    return resolved.rstrip("/\\")


def normalize_git_remote(url: str) -> str:
    """Compare remotes without leaking credentials.

    Local paths are resolved. ``file:`` URLs become local paths. Network URLs
    lose userinfo and a trailing ``.git``; the path of a local bare repo is kept.
    """
    cleaned = gitutil.safe_remote(url.strip()).rstrip("/")
    if cleaned.lower().startswith("file:"):
        return _normalize_local_path(url2pathname(urlparse(cleaned).path))
    if _looks_like_url(cleaned):
        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").lower()
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        path = parsed.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return urlunparse((parsed.scheme.lower(), host, path, "", "", ""))
    return _normalize_local_path(cleaned)


def _origin_matches_config(repo: Path, configured_remote: str) -> bool:
    try:
        actual = gitutil._run_git(repo, "remote", "get-url", "origin").stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise SourceError("cannot read git origin remote") from None
    return normalize_git_remote(actual) == normalize_git_remote(configured_remote)


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


def _validate_known_previous(
    known_previous: Path | None,
    *,
    sources_root: Path,
    published: Path,
    staged: Path | None,
) -> Path | None:
    if known_previous is None:
        return None
    previous = ensure_under_sources_root(known_previous, sources_root)
    if not previous.exists():
        raise SourceError(f"previous_path does not exist: {previous}")
    if _is_reparse_point(previous):
        raise SourceError(f"previous_path must not be a reparse point: {previous}")
    if not previous.is_dir():
        raise SourceError(f"previous_path is not a directory: {previous}")
    if read_revision(previous) is None:
        raise SourceError(f"previous_path is not a valid source snapshot: {previous}")
    prefix = f"{published.name}.prev-"
    if previous.parent != published.parent or not previous.name.startswith(prefix):
        raise SourceError("previous_path does not belong to this published source")
    if not previous.name[len(prefix) :]:
        raise SourceError("previous_path does not belong to this published source")
    if _paths_overlap(previous, published) or (
        staged is not None and staged.exists() and _paths_overlap(previous, staged)
    ):
        raise SourceError("previous_path overlaps published/staged")
    return previous


def _verify_staged_snapshot(prepared: PreparedSource, staged: Path) -> None:
    if prepared.content_digest is None:
        raise SourceError("staged snapshot is missing content digest")
    marker_rev = read_revision(staged)
    if marker_rev != prepared.revision:
        raise SourceError(
            f"staged revision marker {marker_rev!r} != prepared {prepared.revision!r}"
        )
    actual = compute_staged_tree_digest(staged)
    if actual != prepared.content_digest:
        raise SourceError("staged content digest mismatch")
    if prepared.kind != "git":
        return
    git_dir = staged / ".git"
    if not git_dir.is_dir() or _is_reparse_point(git_dir):
        raise SourceError("staged git snapshot missing .git")
    try:
        head = gitutil.rev_parse(staged, "HEAD")
    except subprocess.CalledProcessError:
        raise SourceError("staged git snapshot has no HEAD") from None
    if head != prepared.revision:
        raise SourceError(
            f"staged HEAD {head} does not match prepared {prepared.revision}"
        )
    if _unexpected_git_changes(staged) is not None:
        raise SourceError("staged git snapshot is dirty")


def _snapshot_matches(prepared: PreparedSource, path: Path) -> bool:
    try:
        _verify_staged_snapshot(prepared, path)
    except SourceError:
        return False
    return True


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

    staged_raw = Path(prepared.staged_path)
    staged: Path | None = None
    if staged_raw.exists():
        if _is_reparse_point(staged_raw):
            raise SourceError(f"staged path must not be a reparse point: {staged_raw}")
        staged = ensure_under_sources_root(staged_raw, sources_root)
        _assert_disjoint(staged, published)

    known_previous = _validate_known_previous(
        Path(prepared.previous_path) if prepared.previous_path is not None else None,
        sources_root=sources_root,
        published=published,
        staged=staged,
    )

    # Idempotent only when published bytes match the prepared snapshot.
    # A forged revision marker must not skip publishing the staged tree.
    if published.exists() and _snapshot_matches(prepared, published):
        return known_previous

    if staged is None:
        raise SourceError(f"staged path missing: {staged_raw}")

    _verify_staged_snapshot(prepared, staged)

    if not published.exists():
        # Fresh install / resume with optional known previous backup handle.
        try:
            staged.replace(published)
        except Exception:
            raise
        return known_previous

    if _is_reparse_point(published):
        raise SourceError("published path must not be a reparse point")

    if known_previous is not None:
        # Live published plus a separate known previous is an unsupported state:
        # compensating from previous would silently lose the entry published tree.
        raise SourceError(
            "cannot activate: live published with known previous handle"
        )

    old_rev = read_revision(published) or "unknown"
    previous = published.parent / f"{published.name}.prev-{safe_revision_token(old_rev)}"
    if previous.exists():
        raise SourceError(f"previous source already exists: {previous}")
    published.replace(previous)

    try:
        if not staged.exists():
            raise SourceError(f"staged path missing: {staged}")
        staged.replace(published)
    except Exception as exc:
        # Compensate from the aside created above. If that also fails, name
        # every recovery path — do not leave the entry tree anonymous.
        if previous.exists() and not published.exists():
            try:
                previous.replace(published)
            except Exception:
                raise SourceError(
                    f"activation failed; entry tree remains at {previous}; "
                    f"staged at {staged}"
                ) from None
        raise exc
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
        if not _origin_matches_config(repo, self._config.remote):
            # Do not embed raw remote URLs (may contain userinfo).
            raise SourceError("git origin does not match configured remote")
        try:
            gitutil.fetch(repo)
        except subprocess.CalledProcessError as exc:
            detail = _redact((exc.stderr or "").strip())
            raise SourceError(f"git fetch failed: {detail}") from None
        if not gitutil.can_fast_forward(repo, self._config.branch):
            raise SourceError("update is not a fast-forward")

        revision = gitutil.rev_parse(repo, f"origin/{self._config.branch}")
        token = safe_revision_token(revision)
        db_dir = _ensure_staging_database_dir(
            staging_root, self._database, sources_root
        )
        final = ensure_path_chain_under_sources_root(db_dir / token, sources_root)
        if final.exists() and _paths_overlap(final, published):
            raise SourceError("staging target overlaps published source")

        temp = ensure_path_chain_under_sources_root(
            db_dir / f".tmp-{uuid.uuid4().hex}", sources_root
        )
        if temp.exists():
            shutil.rmtree(temp)
        try:
            # Re-check immediately before write: parent must still be real/under root.
            ensure_path_chain_under_sources_root(temp, sources_root)
            ensure_path_chain_under_sources_root(final, sources_root)
            try:
                subprocess.run(
                    ["git", "clone", "--no-hardlinks", "--quiet", str(repo), str(temp)],
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
            except subprocess.CalledProcessError as exc:
                detail = _redact((exc.stderr or "").strip())
                raise SourceError(f"git staging failed: {detail}") from None
            _exclude_revision_marker_from_git(temp)
            write_revision(temp, revision)
            head = gitutil.rev_parse(temp, "HEAD")
            if head != revision:
                raise SourceError(
                    f"staged HEAD {head} does not match target {revision}"
                )
            if gitutil.is_dirty(temp):
                raise SourceError("staged git checkout became dirty")
            temp = ensure_under_sources_root(temp, sources_root)
            final = ensure_path_chain_under_sources_root(final, sources_root)
            _promote_dir(temp, final)
            final = ensure_under_sources_root(final, sources_root)
            digest = compute_staged_tree_digest(final)
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
            kind="git",
            content_digest=digest,
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
        db_dir = _ensure_staging_database_dir(staging_root, database, sources_root)
        final = ensure_path_chain_under_sources_root(db_dir / token, sources_root)
        if final.exists() and _paths_overlap(final, published):
            raise SourceError("staging target overlaps published source")

        src = self._package.path / "src"
        if not src.is_dir() or _is_reparse_point(src):
            raise SourceError("validated package missing real src/")

        temp = ensure_path_chain_under_sources_root(
            db_dir / f".tmp-{uuid.uuid4().hex}", sources_root
        )
        if temp.exists():
            shutil.rmtree(temp)
        try:
            ensure_path_chain_under_sources_root(temp, sources_root)
            ensure_path_chain_under_sources_root(final, sources_root)
            _copy_source_tree(src, temp)
            write_revision(temp, revision)
            if read_revision(temp) != revision:
                raise SourceError("failed to write staged revision marker")
            digest = compute_staged_tree_digest(temp)
            temp = ensure_under_sources_root(temp, sources_root)
            final = ensure_path_chain_under_sources_root(final, sources_root)
            _promote_dir(temp, final)
            final = ensure_under_sources_root(final, sources_root)
            if compute_staged_tree_digest(final) != digest:
                raise SourceError("staged package digest changed during promote")
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
            kind="package",
            content_digest=digest,
        )


def contour_sources_root(cfg) -> Path:
    runtime = getattr(cfg, "runtime", None)
    if runtime is None or not runtime.root:
        raise SourceError("cfg.runtime.root is required for source layout")
    return Path(runtime.root) / "sources"


def staging_root_for(cfg) -> Path:
    return contour_sources_root(cfg) / ".staging"


def published_path_for(database: DatabaseConfig) -> Path:
    source = database.source
    if isinstance(source, GitSourceConfig):
        return Path(source.path)
    if isinstance(source, PackageSourceConfig):
        return Path(source.source_path)
    raise SourceError(f"unsupported source type for {database.name}")


class PublishedRecoveryProvider:
    """Stage a local copy of the already-published tree (no remote/inbox)."""

    def __init__(
        self,
        database: str,
        *,
        published_path: Path,
        staging_root: Path,
        sources_root: Path,
        revision: str | None = None,
    ) -> None:
        self._database = database
        self._published_path = Path(published_path)
        self._staging_root = Path(staging_root)
        self._sources_root = Path(sources_root)
        self._revision = revision

    def prepare(self) -> PreparedSource:
        sources_root = _resolve(self._sources_root)
        staging_root = ensure_under_sources_root(self._staging_root, sources_root)
        published = ensure_under_sources_root(self._published_path, sources_root)
        _assert_disjoint(staging_root, published)
        if not published.is_dir():
            raise SourceError("published source missing for recovery")
        actual = read_revision(published)
        if not actual:
            raise SourceError("published source has no revision marker")
        if self._revision is not None and self._revision != actual:
            raise SourceError(
                f"recovery revision mismatch: requested {self._revision!r} "
                f"but published marker is {actual!r}"
            )
        revision = actual
        token = safe_revision_token(revision)
        db_dir = _ensure_staging_database_dir(
            staging_root, self._database, sources_root
        )
        final = ensure_path_chain_under_sources_root(db_dir / token, sources_root)
        if final.exists() and _paths_overlap(final, published):
            raise SourceError("staging target overlaps published source")
        temp = ensure_path_chain_under_sources_root(
            db_dir / f".tmp-{uuid.uuid4().hex}", sources_root
        )
        if temp.exists():
            shutil.rmtree(temp)
        try:
            _copy_source_tree(published, temp)
            write_revision(temp, revision)
            digest = compute_staged_tree_digest(temp)
            _promote_dir(temp, final)
            final = ensure_under_sources_root(final, sources_root)
        except Exception:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
            raise
        return PreparedSource(
            database=self._database,
            revision=revision,
            staged_path=final,
            previous_path=None,
            sources_root=sources_root,
            published_path=published,
            kind="package",
            content_digest=digest,
        )


def build_source_provider(
    database: DatabaseConfig,
    cfg,
    *,
    package: ValidatedPackage | None = None,
    recovery: bool = False,
    revision: str | None = None,
) -> SourceProvider:
    """Factory for the SourceProvider used by one database attempt."""
    sources_root = contour_sources_root(cfg)
    sources_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root_for(cfg)
    staging.mkdir(parents=True, exist_ok=True)
    published = published_path_for(database)
    if recovery:
        return PublishedRecoveryProvider(
            database.name,
            published_path=published,
            staging_root=staging,
            sources_root=sources_root,
            revision=revision,
        )
    source = database.source
    if isinstance(source, GitSourceConfig):
        return GitSourceProvider(
            source,
            staging_root=staging,
            published_path=published,
            sources_root=sources_root,
            database=database.name,
        )
    if isinstance(source, PackageSourceConfig):
        if package is None:
            raise SourceError(f"no package selected for {database.name}")
        return PackageSourceProvider(
            package,
            staging_root=staging,
            published_path=published,
            sources_root=sources_root,
        )
    raise SourceError(f"unsupported source type for {database.name}")
