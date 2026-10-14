import json
import os
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


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def layout(tmp_path):
    root = tmp_path / "sources"
    staging = root / ".staging"
    staging.mkdir(parents=True)
    return root, staging


def tree(path, revision, content):
    path.mkdir(parents=True)
    (path / ".contour-revision").write_text(revision, encoding="utf-8")
    (path / "data.txt").write_text(content, encoding="utf-8")


def git_provider(tmp_path):
    root, staging = layout(tmp_path)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    published = root / "ubd"
    git(root, "clone", str(remote), str(published))
    git(published, "config", "user.email", "test@example.invalid")
    git(published, "config", "user.name", "Review")
    (published / "data.txt").write_text("r1", encoding="utf-8")
    git(published, "add", "data.txt")
    git(published, "commit", "-m", "initial")
    git(published, "push", "origin", "main")
    config = GitSourceConfig("git", str(published), str(remote), "main", "")
    provider = GitSourceProvider(
        config,
        staging_root=staging,
        published_path=published,
        sources_root=root,
    )
    return root, staging, published, remote, provider


def validated_package(tmp_path):
    package = tmp_path / "package"
    (package / "src").mkdir(parents=True)
    (package / "src" / "Configuration.xml").write_bytes(b"<Configuration/>")
    (package / "READY").write_bytes(b"")
    manifest = dict(
        schema_version=1,
        database="kb",
        export_id="e1",
        started_at="2026-10-14T00:00:00Z",
        finished_at="2026-10-14T00:01:00Z",
        platform_version="8.3.25",
        operations={
            "DumpConfigToFiles": "ok",
            "UpdateCfg": "ok",
            "UpdateDBCfg": "ok",
        },
        file_count=1,
        checksum=compute_source_checksum(package),
    )
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return validate_package(package, frozenset({"kb"}))


def test_git_second_cycle_is_not_dirty_from_service_marker(tmp_path):
    root, staging, published, remote, provider = git_provider(tmp_path)
    prepared = provider.prepare()
    activate_source(prepared, published)
    provider.prepare()  # must be a valid no-change retry


def test_staged_git_keeps_configured_upstream(tmp_path):
    root, staging, published, remote, provider = git_provider(tmp_path)
    prepared = provider.prepare()
    assert git(prepared.staged_path, "remote", "get-url", "origin") == str(remote)


def test_git_two_upstream_cycles(tmp_path):
    root, staging, published, remote, provider = git_provider(tmp_path)
    first = provider.prepare()
    activate_source(first, published)
    assert (published / "data.txt").read_text(encoding="utf-8") == "r1"

    # New upstream commit via a separate clone.
    writer = tmp_path / "writer"
    git(tmp_path, "clone", str(remote), str(writer))
    git(writer, "config", "user.email", "test@example.invalid")
    git(writer, "config", "user.name", "Review")
    (writer / "data.txt").write_text("r2", encoding="utf-8")
    git(writer, "add", "data.txt")
    git(writer, "commit", "-m", "second")
    git(writer, "push", "origin", "main")

    second = provider.prepare()
    assert second.revision != first.revision
    activate_source(second, published)
    assert (published / "data.txt").read_text(encoding="utf-8") == "r2"


def test_rollback_failure_restores_current_published_tree(tmp_path, monkeypatch):
    root, staging = layout(tmp_path)
    published, previous = root / "kb", root / "kb.prev-r1"
    tree(published, "r2", "current")
    tree(previous, "r1", "previous")
    real = Path.replace

    def fail_old(self, target):
        if self == previous:
            raise OSError("injected restore failure")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", fail_old)
    with pytest.raises(OSError):
        rollback_source(published, previous, sources_root=root)
    assert (published / "data.txt").read_text(encoding="utf-8") == "current"
    assert (previous / "data.txt").read_text(encoding="utf-8") == "previous"


def test_rollback_refuses_same_directory_without_destroying_it(tmp_path):
    root, staging = layout(tmp_path)
    published = root / "kb"
    tree(published, "r1", "current")
    with pytest.raises(SourceError):
        rollback_source(published, published, sources_root=root)
    assert (published / "data.txt").read_text(encoding="utf-8") == "current"


def test_prepare_staging_inside_published_is_rejected(tmp_path):
    root, staging = layout(tmp_path)
    published = root / "kb"
    tree(published, "r0", "current")
    provider = PackageSourceProvider(
        validated_package(tmp_path),
        staging_root=published / ".staging",
        published_path=published,
        sources_root=root,
    )
    before = {p.relative_to(published).as_posix() for p in published.rglob("*")}
    with pytest.raises(SourceError):
        provider.prepare()
    assert {p.relative_to(published).as_posix() for p in published.rglob("*")} == before


def test_activation_checks_destination_is_within_prepared_source_root(tmp_path):
    root, staging = layout(tmp_path)
    provider = PackageSourceProvider(
        validated_package(tmp_path),
        staging_root=staging,
        published_path=root / "kb",
        sources_root=root,
    )
    prepared = provider.prepare()
    outside = tmp_path / "outside"
    with pytest.raises(SourceError):
        activate_source(prepared, outside)
    assert not outside.exists()


def test_mutated_package_is_not_staged_under_old_revision(tmp_path):
    root, staging = layout(tmp_path)
    package = validated_package(tmp_path)
    (package.path / "src" / "Configuration.xml").write_bytes(b"<Changed/>")
    provider = PackageSourceProvider(
        package,
        staging_root=staging,
        published_path=root / "kb",
        sources_root=root,
    )
    with pytest.raises((SourceError, ValueError)):
        provider.prepare()


def test_repeat_prepare_preserves_existing_staging_on_copy_failure(
    tmp_path, monkeypatch
):
    root, staging = layout(tmp_path)
    provider = PackageSourceProvider(
        validated_package(tmp_path),
        staging_root=staging,
        published_path=root / "kb",
        sources_root=root,
    )
    prepared = provider.prepare()
    old = (prepared.staged_path / "Configuration.xml").read_bytes()

    def failed_copy(*args, **kwargs):
        raise OSError("injected staging copy failure")

    monkeypatch.setattr("contour.sources.shutil.copytree", failed_copy)
    with pytest.raises(OSError):
        provider.prepare()
    assert (prepared.staged_path / "Configuration.xml").read_bytes() == old


def test_activation_retry_keeps_known_previous_path(tmp_path):
    root, staging = layout(tmp_path)
    published, previous, staged = root / "kb", root / "kb.prev-r1", staging / "kb" / "r2"
    tree(previous, "r1", "previous")
    tree(staged, "r2", "next")
    prepared = PreparedSource(
        "kb",
        "r2",
        staged,
        previous,
        sources_root=root,
        published_path=published,
    )
    assert activate_source(prepared, published) == previous


def test_same_revision_activation_retry_returns_existing_backup(tmp_path):
    root, staging = layout(tmp_path)
    published, previous, staged = root / "kb", root / "kb.prev-r1", staging / "kb" / "r2"
    tree(published, "r1", "previous")
    tree(staged, "r2", "next")
    prepared = PreparedSource(
        "kb",
        "r2",
        staged,
        None,
        sources_root=root,
        published_path=published,
    )
    actual_previous = activate_source(prepared, published)
    retry = PreparedSource(
        "kb",
        "r2",
        staged,
        actual_previous,
        sources_root=root,
        published_path=published,
    )
    assert activate_source(retry, published) == actual_previous


@pytest.mark.skipif(os.name != "nt", reason="Windows package revision path")
def test_second_package_activation_handles_revision_colon(tmp_path):
    root, staging = layout(tmp_path)
    package = validated_package(tmp_path)
    published = root / "kb"
    tree(published, package.revision, "previous")
    staged = staging / "kb" / "next"
    tree(staged, "next-revision", "next")
    previous = activate_source(
        PreparedSource(
            "kb",
            "next-revision",
            staged,
            None,
            sources_root=root,
            published_path=published,
        ),
        published,
    )
    assert previous is not None and previous.is_dir()


def test_failed_activation_restores_previous_tree(tmp_path, monkeypatch):
    root, staging = layout(tmp_path)
    published, staged = root / "kb", staging / "kb" / "r2"
    tree(published, "r1", "previous")
    tree(staged, "r2", "next")
    real = Path.replace

    def fail_stage(self, target):
        if self == staged:
            raise OSError("injected activation failure")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", fail_stage)
    with pytest.raises(OSError):
        activate_source(
            PreparedSource(
                "kb",
                "r2",
                staged,
                None,
                sources_root=root,
                published_path=published,
            ),
            published,
        )
    assert (published / "data.txt").read_text(encoding="utf-8") == "previous"


def test_package_prepare_matches_source_bytes(tmp_path):
    root, staging = layout(tmp_path)
    package = validated_package(tmp_path)
    prepared = PackageSourceProvider(
        package,
        staging_root=staging,
        published_path=root / "kb",
        sources_root=root,
    ).prepare()
    assert (prepared.staged_path / "Configuration.xml").read_bytes() == (
        package.path / "src" / "Configuration.xml"
    ).read_bytes()


@pytest.mark.skipif(os.name != "posix", reason="Linux staging symlink")
def test_staging_symlink_cannot_delete_published_source(tmp_path):
    root, staging = layout(tmp_path)
    package = validated_package(tmp_path)
    published = root / "kb"
    tree(published, "r0", "current")
    alias = staging / "kb" / safe_revision_token(package.revision)
    alias.parent.mkdir(parents=True)
    alias.symlink_to(published, target_is_directory=True)
    provider = PackageSourceProvider(
        package,
        staging_root=staging,
        published_path=published,
        sources_root=root,
    )
    with pytest.raises(SourceError):
        provider.prepare()
    assert (published / "data.txt").read_text(encoding="utf-8") == "current"


def test_activation_stage_marker_must_match_prepared_revision(tmp_path):
    root, staging = layout(tmp_path)
    published, staged = root / "kb", staging / "kb" / "r2"
    tree(published, "r1", "current")
    tree(staged, "unexpected-revision", "unexpected")
    with pytest.raises(SourceError):
        activate_source(
            PreparedSource(
                "kb",
                "r2",
                staged,
                None,
                sources_root=root,
                published_path=published,
            ),
            published,
        )
    assert (published / "data.txt").read_text(encoding="utf-8") == "current"
