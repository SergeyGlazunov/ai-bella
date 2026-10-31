"""Follow-up cases against in-place sources (no .staging digest path)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from contour.config import GitSourceConfig
from contour.sources import (
    GitSourceProvider,
    PackageSourceProvider,
    PreparedSource,
    SourceError,
    activate_source,
    read_revision,
)
from test_task5_review import git, git_provider, layout, tree, validated_package


def test_first_package_activation_needs_no_previous_source(tmp_path):
    root, staging = layout(tmp_path)
    package = validated_package(tmp_path)
    published = root / "kb"
    prepared = PackageSourceProvider(
        package,
        staging_root=staging,
        published_path=published,
        sources_root=root,
    ).prepare()
    assert prepared.previous_path is None
    assert not published.exists()
    prev = activate_source(prepared, published)
    assert prev is None
    assert published.is_dir()
    assert read_revision(published) == prepared.revision
    assert (published / "Configuration.xml").read_bytes() == (
        package.path / "src" / "Configuration.xml"
    ).read_bytes()
    assert prepared.staged_path.exists()


def test_first_publication_retry_without_backup_is_idempotent(tmp_path):
    root, staging = layout(tmp_path)
    package = validated_package(tmp_path)
    published = root / "kb"
    prepared = PackageSourceProvider(
        package,
        staging_root=staging,
        published_path=published,
        sources_root=root,
    ).prepare()
    activate_source(prepared, published)
    assert prepared.staged_path.exists()
    retry = PreparedSource(
        prepared.database,
        prepared.revision,
        prepared.staged_path,
        None,
        sources_root=root,
        published_path=published,
        kind=prepared.kind,
        content_digest=None,
    )
    assert activate_source(retry, published) is None
    assert read_revision(published) == prepared.revision


def test_invalid_previous_handle_is_rejected_before_mutation(tmp_path):
    root, staging = layout(tmp_path)
    published = root / "kb"
    tree(published, "r2", "current")
    staged = tmp_path / "accepted" / "src"
    tree(staged, "r3", "next-longer")
    missing = root / "kb.prev-missing"
    prepared = PreparedSource(
        "kb",
        "r3",
        staged,
        missing,
        sources_root=root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    with pytest.raises(SourceError, match="previous"):
        activate_source(prepared, published)
    assert (published / "data.txt").read_text(encoding="utf-8") == "current"
    assert not missing.exists()


def test_activation_with_known_backup_publishes_in_place(tmp_path):
    root, staging = layout(tmp_path)
    published = root / "kb"
    previous = root / "kb.prev-r1"
    staged = tmp_path / "accepted" / "src"
    tree(published, "r2", "current")
    tree(previous, "r1", "previous")
    tree(staged, "r3", "next-longer-payload")
    published_inode = published.stat().st_ino
    prepared = PreparedSource(
        "kb",
        "r3",
        staged,
        previous,
        sources_root=root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    assert activate_source(prepared, published) is None
    assert published.stat().st_ino == published_inode
    assert (published / "data.txt").read_text(encoding="utf-8") == "next-longer-payload"
    assert (previous / "data.txt").read_text(encoding="utf-8") == "previous"


def test_wrong_existing_git_origin_does_not_silently_override_config(tmp_path):
    root, staging = layout(tmp_path)
    remote_a = tmp_path / "remote-a.git"
    remote_b = tmp_path / "remote-b.git"
    remote_a.mkdir()
    remote_b.mkdir()
    git(remote_a, "init", "--bare", "--initial-branch=main")
    git(remote_b, "init", "--bare", "--initial-branch=main")
    published = root / "ubd"
    git(root, "clone", str(remote_a), str(published))
    git(published, "config", "user.email", "test@example.invalid")
    git(published, "config", "user.name", "Review")
    (published / "data.txt").write_text("from-a", encoding="utf-8")
    git(published, "add", "data.txt")
    git(published, "commit", "-m", "a")
    git(published, "push", "origin", "main")
    config = GitSourceConfig("git", str(published), str(remote_b), "main", "")
    provider = GitSourceProvider(
        config,
        staging_root=staging,
        published_path=published,
        sources_root=root,
    )
    with pytest.raises(SourceError, match="origin|remote"):
        provider.prepare()


def test_same_revision_with_configuration_skips_mirror(tmp_path):
    root, staging = layout(tmp_path)
    published = root / "kb"
    staged = tmp_path / "accepted" / "src"
    tree(published, "r2", "stale")
    tree(staged, "r2", "fresh-should-not-apply")
    published_inode = published.stat().st_ino
    prepared = PreparedSource(
        "kb",
        "r2",
        staged,
        None,
        sources_root=root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    assert activate_source(prepared, published) is None
    assert published.stat().st_ino == published_inode
    assert (published / "data.txt").read_text(encoding="utf-8") == "stale"


def test_unrelated_previous_is_rejected_before_mutation(tmp_path):
    root, staging = layout(tmp_path)
    published = root / "kb"
    staged = tmp_path / "accepted" / "src"
    unrelated = root / "elsewhere"
    tree(unrelated, "r1", "other")
    tree(staged, "r2", "next-longer")
    prepared = PreparedSource(
        "kb",
        "r2",
        staged,
        unrelated,
        sources_root=root,
        published_path=published,
        kind="package",
        content_digest=None,
    )
    with pytest.raises(SourceError, match="previous"):
        activate_source(prepared, published)
    assert not published.exists()
    assert (unrelated / "data.txt").read_text(encoding="utf-8") == "other"
    assert (staged / "data.txt").read_text(encoding="utf-8") == "next-longer"


def test_equivalent_file_uri_remote_matches_existing_origin(tmp_path):
    root, staging, published, remote, provider = git_provider(tmp_path)
    origin = git(published, "remote", "get-url", "origin")
    config = GitSourceConfig(
        "git", str(published), Path(origin).resolve().as_uri(), "main", ""
    )
    equivalent = GitSourceProvider(
        config,
        staging_root=staging,
        published_path=published,
        sources_root=root,
    )
    prepared = equivalent.prepare()
    assert prepared.revision
    assert prepared.staged_path == published


def test_git_failure_does_not_leak_remote_userinfo(tmp_path, monkeypatch):
    root, staging, published, remote, provider = git_provider(tmp_path)

    def leaky_fetch(repo):
        raise subprocess.CalledProcessError(
            128,
            ["git", "fetch", "https://user:s3cret@example.invalid/repo.git"],
            stderr="fatal: https://user:s3cret@example.invalid/repo.git",
        )

    monkeypatch.setattr("contour.gitutil.fetch", leaky_fetch)
    with pytest.raises(SourceError) as caught:
        provider.prepare()
    assert "s3cret" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_activation_mirror_failure_keeps_published(tmp_path, monkeypatch):
    root, staging = layout(tmp_path)
    published = root / "kb"
    staged = tmp_path / "accepted" / "src"
    tree(published, "r1", "current")
    tree(staged, "r2", "next-longer")

    def boom(*_args, **_kwargs):
        raise OSError("injected activate failure")

    monkeypatch.setattr("contour.sources._mirror_tree_into", boom)
    with pytest.raises(OSError, match="injected"):
        activate_source(
            PreparedSource(
                "kb",
                "r2",
                staged,
                None,
                sources_root=root,
                published_path=published,
                kind="package",
                content_digest=None,
            ),
            published,
        )
    assert (published / "data.txt").read_text(encoding="utf-8") == "current"
    assert (staged / "data.txt").read_text(encoding="utf-8") == "next-longer"
    assert not any(path.name.startswith("kb.prev-") for path in root.iterdir())


def test_package_reparse_planted_after_validation_is_rejected_on_activate(
    tmp_path, monkeypatch
):
    root, staging = layout(tmp_path)
    package = validated_package(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("SECRET-BYTES", encoding="utf-8")
    published = root / "kb"

    def plant(path, allowed, **kwargs):
        from contour.packages import validate_package as real_validate

        result = real_validate(path, allowed, **kwargs)
        link = Path(path) / "src" / "leakdir"
        if os.name == "nt":
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
                text=True,
            )
            if created.returncode:
                pytest.skip("NTFS junction unavailable")
        else:
            link.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr("contour.sources.validate_package", plant)
    prepared = PackageSourceProvider(
        package,
        staging_root=staging,
        published_path=published,
        sources_root=root,
    ).prepare()
    with pytest.raises(SourceError):
        activate_source(prepared, published)
    leaked = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and "SECRET-BYTES" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == []
