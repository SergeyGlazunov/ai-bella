from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from contour.config import GitSourceConfig
from contour.packages import compute_source_checksum, validate_package
from contour.sources import (
    GitSourceProvider,
    PackageSourceProvider,
    PreparedSource,
    SourceError,
    activate_source,
    rollback_source,
    safe_revision_token,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(remote), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "file.txt").write_text("v1", encoding="utf-8")
    _git(work, "add", "file.txt")
    _git(work, "commit", "-m", "v1")
    _git(work, "push", "-u", "origin", "HEAD")
    branch = _git(work, "symbolic-ref", "--short", "HEAD").stdout.strip()
    return remote, work, branch


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    sources_root = tmp_path / "srv" / "ai-contour" / "sources"
    sources_root.mkdir(parents=True)
    staging = sources_root / ".staging"
    staging.mkdir()
    published = sources_root / "ubd"
    return sources_root, staging, published


def _git_config(path: Path, remote_url: str, branch: str) -> GitSourceConfig:
    return GitSourceConfig(
        type="git",
        path=str(path),
        remote=remote_url,
        branch=branch,
        workspace_subdir="",
    )


def test_git_prepare_rejects_dirty_tree(tmp_path: Path):
    remote, work, branch = _init_repo(tmp_path)
    sources_root, staging, published = _layout(tmp_path)
    # Published checkout is the live git work tree under sources_root.
    published.mkdir(parents=True)
    _git(tmp_path, "clone", str(remote), str(published))
    (published / "file.txt").write_text("dirty", encoding="utf-8")
    provider = GitSourceProvider(
        _git_config(published, str(remote), branch),
        staging_root=staging,
        published_path=published,
        sources_root=sources_root,
    )
    with pytest.raises(SourceError, match="dirty"):
        provider.prepare()


def test_git_prepare_rejects_non_fast_forward(tmp_path: Path):
    remote, work, branch = _init_repo(tmp_path)
    sources_root, staging, published = _layout(tmp_path)
    _git(tmp_path, "clone", str(remote), str(published))
    _git(published, "config", "user.email", "t@example.com")
    _git(published, "config", "user.name", "T")

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "T")
    (other / "file.txt").write_text("remote-only", encoding="utf-8")
    _git(other, "add", "file.txt")
    _git(other, "commit", "-m", "remote")
    _git(other, "push", "origin", "HEAD")

    (published / "file.txt").write_text("local-only", encoding="utf-8")
    _git(published, "add", "file.txt")
    _git(published, "commit", "-m", "local")

    provider = GitSourceProvider(
        _git_config(published, str(remote), branch),
        staging_root=staging,
        published_path=published,
        sources_root=sources_root,
    )
    with pytest.raises(SourceError, match="fast-forward|ff"):
        provider.prepare()


def test_package_prepare_does_not_write_published(tmp_path: Path):
    sources_root, staging, _ = _layout(tmp_path)
    published = sources_root / "kb"
    published.mkdir()
    (published / "keep.txt").write_text("live", encoding="utf-8")
    before = (published / "keep.txt").read_text(encoding="utf-8")

    pkg = tmp_path / "pkg"
    (pkg / "src").mkdir(parents=True)
    (pkg / "src" / "Configuration.xml").write_bytes(b"<Configuration/>")
    (pkg / "READY").write_bytes(b"")
    raw = {
        "schema_version": 1,
        "database": "kb",
        "export_id": "e1",
        "started_at": "2026-10-14T00:00:00Z",
        "finished_at": "2026-10-14T00:01:00Z",
        "platform_version": "8.3.25",
        "operations": {
            "DumpConfigToFiles": "ok",
            "UpdateCfg": "ok",
            "UpdateDBCfg": "ok",
        },
        "file_count": 1,
        "checksum": compute_source_checksum(pkg),
    }
    (pkg / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    validated = validate_package(pkg, frozenset({"kb"}))

    provider = PackageSourceProvider(
        validated,
        staging_root=staging,
        published_path=published,
        sources_root=sources_root,
    )
    prepared = provider.prepare()
    assert prepared.staged_path == pkg / "src"
    assert prepared.content_digest is None
    assert prepared.staged_path != published
    assert (published / "keep.txt").read_text(encoding="utf-8") == before
    assert not (published / "Configuration.xml").exists()


def test_git_activate_keeps_branch_not_detached(tmp_path: Path):
    """activate_source must leave published on the configured branch tip."""
    remote, work, branch = _init_repo(tmp_path)
    sources_root, staging, published = _layout(tmp_path)
    _git(tmp_path, "clone", str(remote), str(published))
    (work / "file.txt").write_text("v2", encoding="utf-8")
    _git(work, "add", "file.txt")
    _git(work, "commit", "-m", "v2")
    _git(work, "push", "origin", "HEAD")
    _git(published, "fetch", "origin")
    tip = _git(published, "rev-parse", f"origin/{branch}").stdout.strip()
    # Simulate the old bug: detached HEAD before activate.
    _git(published, "checkout", "--detach", "HEAD")

    prepared = PreparedSource(
        database="ubd",
        revision=tip,
        staged_path=published,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
        kind="git",
        content_digest=None,
        git_branch=branch,
    )
    assert activate_source(prepared, published) is None
    assert _git(published, "symbolic-ref", "--short", "HEAD").stdout.strip() == branch
    assert _git(published, "rev-parse", "HEAD").stdout.strip() == tip
    assert (published / ".contour-revision").read_text(encoding="utf-8").strip() == tip


def test_activate_mirrors_in_place_same_directory(tmp_path: Path):
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("old", encoding="utf-8")
    (published / "old.txt").write_text("old", encoding="utf-8")
    published_inode = published.stat().st_ino

    # Accepted-style source (outside .staging) must not be deleted after mirror.
    accepted = tmp_path / "accepted" / "ready-e1" / "src"
    accepted.mkdir(parents=True)
    (accepted / "new.txt").write_text("new-bytes-longer", encoding="utf-8")
    (accepted / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    prepared = PreparedSource(
        database="ubd",
        revision="new",
        staged_path=accepted,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    assert activate_source(prepared, published) is None
    assert published.stat().st_ino == published_inode
    assert (published / "new.txt").read_text(encoding="utf-8") == "new-bytes-longer"
    assert not (published / "old.txt").exists()
    assert accepted.exists()


def test_activate_preserves_code_index(tmp_path: Path):
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("old", encoding="utf-8")
    (published / "old.txt").write_text("old", encoding="utf-8")
    code_index = published / ".code-index"
    code_index.mkdir()
    (code_index / "index.db").write_bytes(b"sqlite-keep")

    staged = tmp_path / "accepted" / "src"
    staged.mkdir(parents=True)
    (staged / "new.txt").write_text("new-content", encoding="utf-8")
    (staged / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    prepared = PreparedSource(
        database="ubd",
        revision="new",
        staged_path=staged,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    activate_source(prepared, published)
    assert (published / ".code-index" / "index.db").read_bytes() == b"sqlite-keep"
    assert (published / "new.txt").read_text(encoding="utf-8") == "new-content"


def test_activate_preserves_nested_code_index_under_src(tmp_path: Path):
    """BSL index lives at workspace_subdir/.code-index, not repo root."""
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / "src").mkdir()
    (published / "src" / "Configuration.xml").write_text("<old/>", encoding="utf-8")
    nested = published / "src" / ".code-index"
    nested.mkdir()
    (nested / "index.db").write_bytes(b"nested-keep")

    staged = tmp_path / "accepted" / "src-root"
    (staged / "src").mkdir(parents=True)
    (staged / "src" / "Configuration.xml").write_text(
        "<new-longer-payload/>", encoding="utf-8"
    )
    (staged / "src" / "CommonModules").mkdir()
    (staged / "src" / "CommonModules" / "Foo.bsl").write_text("// foo", encoding="utf-8")

    prepared = PreparedSource(
        database="ubd",
        revision="new",
        staged_path=staged,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    activate_source(prepared, published)
    assert (published / "src" / ".code-index" / "index.db").read_bytes() == b"nested-keep"
    assert (published / "src" / "Configuration.xml").read_text(
        encoding="utf-8"
    ) == "<new-longer-payload/>"
    assert (published / "src" / "CommonModules" / "Foo.bsl").read_text(
        encoding="utf-8"
    ) == "// foo"


def test_mirror_skips_unchanged_size_and_mtime(tmp_path: Path):
    import os

    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("r1", encoding="utf-8")
    (published / "Configuration.xml").write_text("<same/>", encoding="utf-8")
    (published / "keep.bin").write_bytes(b"payload")
    staged = tmp_path / "accepted" / "src"
    staged.mkdir(parents=True)
    (staged / "Configuration.xml").write_text("<same/>", encoding="utf-8")
    (staged / "keep.bin").write_bytes(b"payload")
    for name in ("Configuration.xml", "keep.bin"):
        dst = published / name
        src = staged / name
        os.utime(src, (dst.stat().st_atime, dst.stat().st_mtime))
    before_ino = (published / "keep.bin").stat().st_ino
    prepared = PreparedSource(
        database="ubd",
        revision="r2",
        staged_path=staged,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    activate_source(prepared, published)
    assert (published / "keep.bin").stat().st_ino == before_ino


def test_activate_failure_keeps_published_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("old", encoding="utf-8")
    (published / "old.txt").write_text("old", encoding="utf-8")

    staged = tmp_path / "accepted" / "src"
    staged.mkdir(parents=True)
    (staged / "new.txt").write_text("new", encoding="utf-8")
    (staged / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    prepared = PreparedSource(
        database="ubd",
        revision="new",
        staged_path=staged,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
        kind="package",
        content_digest=None,
    )

    def boom(*_args, **_kwargs):
        raise OSError("injected activate failure")

    monkeypatch.setattr("contour.sources._mirror_tree_into", boom)
    with pytest.raises(OSError, match="injected"):
        activate_source(prepared, published)

    assert published.exists()
    assert (published / "old.txt").read_text(encoding="utf-8") == "old"
    assert staged.exists()


def test_activate_same_revision_is_idempotent(tmp_path: Path):
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("rev-a", encoding="utf-8")
    (published / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    (published / "file.txt").write_text("a", encoding="utf-8")

    staged = tmp_path / "accepted" / "src"
    staged.mkdir(parents=True)
    (staged / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    (staged / "file.txt").write_text("a", encoding="utf-8")

    prepared = PreparedSource(
        database="ubd",
        revision="rev-a",
        staged_path=staged,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    assert activate_source(prepared, published) is None
    assert (published / "file.txt").read_text(encoding="utf-8") == "a"
    assert staged.exists()


def test_activate_ignores_leftover_prev_directory(tmp_path: Path):
    """In-place activate does not rename published; leftover .prev-* is ignored."""
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("rev-old", encoding="utf-8")
    (published / "old.txt").write_text("old", encoding="utf-8")

    stuck = sources_root / f"ubd.prev-{safe_revision_token('rev-old')}"
    stuck.mkdir()
    (stuck / ".contour-revision").write_text("rev-old", encoding="utf-8")
    (stuck / "stale.txt").write_text("stale", encoding="utf-8")

    staged = tmp_path / "accepted" / "src"
    staged.mkdir(parents=True)
    (staged / "new.txt").write_text("new-longer", encoding="utf-8")
    (staged / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")

    prepared = PreparedSource(
        database="ubd",
        revision="rev-new",
        staged_path=staged,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    assert activate_source(prepared, published) is None
    assert (published / "new.txt").read_text(encoding="utf-8") == "new-longer"
    assert not (published / "old.txt").exists()
    assert stuck.exists()
    assert (stuck / "stale.txt").read_text(encoding="utf-8") == "stale"


def test_rollback_restores_previous(tmp_path: Path):
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("new", encoding="utf-8")
    (published / "new.txt").write_text("new", encoding="utf-8")
    published_inode = published.stat().st_ino

    previous = sources_root / "ubd.prev-old"
    previous.mkdir()
    (previous / ".contour-revision").write_text("old", encoding="utf-8")
    (previous / "old.txt").write_text("old", encoding="utf-8")

    rollback_source(published, previous, sources_root=sources_root)
    assert published.stat().st_ino == published_inode
    assert (published / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (published / "new.txt").exists()
    assert not previous.exists()


def test_git_prepare_returns_published_without_staging(tmp_path: Path):
    remote, _work, branch = _init_repo(tmp_path)
    sources_root, staging, published = _layout(tmp_path)
    _git(tmp_path, "clone", str(remote), str(published))
    provider = GitSourceProvider(
        _git_config(published, str(remote), branch),
        staging_root=staging,
        published_path=published,
        sources_root=sources_root,
    )
    prepared = provider.prepare()
    assert prepared.kind == "git"
    assert prepared.content_digest is None
    assert prepared.staged_path == published
    assert prepared.published_path == published
    assert not any(staging.rglob("*"))
