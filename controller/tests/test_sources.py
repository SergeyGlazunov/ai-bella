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
    (work / "file.txt").write_text("dirty", encoding="utf-8")
    provider = GitSourceProvider(
        _git_config(work, str(remote), branch),
        staging_root=staging,
        published_path=published,
        sources_root=sources_root,
    )
    with pytest.raises(SourceError, match="dirty"):
        provider.prepare()


def test_git_prepare_rejects_non_fast_forward(tmp_path: Path):
    remote, work, branch = _init_repo(tmp_path)
    sources_root, staging, published = _layout(tmp_path)

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "T")
    (other / "file.txt").write_text("remote-only", encoding="utf-8")
    _git(other, "add", "file.txt")
    _git(other, "commit", "-m", "remote")
    _git(other, "push", "origin", "HEAD")

    (work / "file.txt").write_text("local-only", encoding="utf-8")
    _git(work, "add", "file.txt")
    _git(work, "commit", "-m", "local")

    provider = GitSourceProvider(
        _git_config(work, str(remote), branch),
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
    assert prepared.staged_path.exists()
    assert prepared.staged_path != published
    assert (published / "keep.txt").read_text(encoding="utf-8") == before
    assert not (published / "Configuration.xml").exists()


def test_activate_uses_replace_and_preserves_previous_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("old", encoding="utf-8")
    (published / "old.txt").write_text("old", encoding="utf-8")

    staged = staging / "ubd" / "new"
    staged.mkdir(parents=True)
    (staged / ".contour-revision").write_text("new", encoding="utf-8")
    (staged / "new.txt").write_text("new", encoding="utf-8")

    prepared = PreparedSource(
        database="ubd",
        revision="new",
        staged_path=staged,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
    )

    real_replace = Path.replace
    calls: list[tuple[Path, Path]] = []

    def boom(self: Path, target: Path):
        calls.append((self, Path(target)))
        if len(calls) == 2:
            raise OSError("injected activate failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="injected"):
        activate_source(prepared, published)

    assert published.exists()
    assert (published / "old.txt").read_text(encoding="utf-8") == "old"
    assert calls, "activation must use Path.replace"


def test_activate_same_revision_is_idempotent(tmp_path: Path):
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("rev-a", encoding="utf-8")
    (published / "file.txt").write_text("a", encoding="utf-8")

    staged = staging / "ubd" / "rev-a"
    staged.mkdir(parents=True)
    (staged / ".contour-revision").write_text("rev-a", encoding="utf-8")
    (staged / "file.txt").write_text("a", encoding="utf-8")

    prepared = PreparedSource(
        database="ubd",
        revision="rev-a",
        staged_path=staged,
        previous_path=None,
        sources_root=sources_root,
        published_path=published,
    )
    prev = activate_source(prepared, published)
    assert prev is None
    assert (published / "file.txt").read_text(encoding="utf-8") == "a"
    assert staged.exists()


def test_rollback_restores_previous(tmp_path: Path):
    sources_root, staging, published = _layout(tmp_path)
    published.mkdir()
    (published / ".contour-revision").write_text("new", encoding="utf-8")
    (published / "new.txt").write_text("new", encoding="utf-8")

    previous = sources_root / "ubd.prev-old"
    previous.mkdir()
    (previous / ".contour-revision").write_text("old", encoding="utf-8")
    (previous / "old.txt").write_text("old", encoding="utf-8")

    rollback_source(published, previous, sources_root=sources_root)
    assert (published / "old.txt").read_text(encoding="utf-8") == "old"
    assert not previous.exists()


def test_paths_cannot_escape_sources_root(tmp_path: Path):
    sources_root, staging, published = _layout(tmp_path)
    escape = tmp_path / "outside"
    escape.mkdir()
    remote, work, branch = _init_repo(tmp_path)
    provider = GitSourceProvider(
        _git_config(work, str(remote), branch),
        staging_root=escape,
        published_path=published,
        sources_root=sources_root,
    )
    with pytest.raises(SourceError, match="sources"):
        provider.prepare()
