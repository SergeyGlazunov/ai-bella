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
    (path / "Configuration.xml").write_text(f"<cfg>{content}</cfg>", encoding="utf-8")


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


def sealed(database, revision, staged, previous, root, published):
    return PreparedSource(
        database,
        revision,
        staged,
        previous,
        sources_root=root,
        published_path=published,
        kind="package",
        content_digest=None,
    )


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


def test_git_prepare_keeps_configured_upstream(tmp_path):
    root, staging, published, remote, provider = git_provider(tmp_path)
    prepared = provider.prepare()
    assert git(prepared.staged_path, "remote", "get-url", "origin") == str(remote)


def test_git_two_upstream_cycles(tmp_path):
    root, staging, published, remote, provider = git_provider(tmp_path)
    first = provider.prepare()
    activate_source(first, published)
    assert (published / "data.txt").read_text(encoding="utf-8") == "r1"

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


def test_rollback_failure_keeps_published_when_mirror_fails(tmp_path, monkeypatch):
    root, staging = layout(tmp_path)
    published, previous = root / "kb", root / "kb.prev-r1"
    tree(published, "r2", "current")
    tree(previous, "r1", "previous")

    def fail_mirror(*_args, **_kwargs):
        raise OSError("injected restore failure")

    monkeypatch.setattr("contour.sources._mirror_tree_into", fail_mirror)
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


def test_mutated_package_manifest_identity_still_detected(tmp_path):
    """Checksum-light revalidate still catches export_id / database identity drift."""
    root, staging = layout(tmp_path)
    package = validated_package(tmp_path)
    manifest = json.loads((package.path / "manifest.json").read_text(encoding="utf-8"))
    manifest["export_id"] = "mutated-export"
    (package.path / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    provider = PackageSourceProvider(
        package,
        staging_root=staging,
        published_path=root / "kb",
        sources_root=root,
    )
    with pytest.raises(SourceError, match="changed after validation"):
        provider.prepare()


def test_activation_with_known_previous_still_publishes_in_place(tmp_path):
    root, staging = layout(tmp_path)
    published, previous = root / "kb", root / "kb.prev-r1"
    staged = tmp_path / "accepted" / "src"
    tree(previous, "r1", "previous")
    tree(staged, "r2", "next-longer-payload")
    prepared = sealed("kb", "r2", staged, previous, root, published)
    assert activate_source(prepared, published) is None
    assert (published / "data.txt").read_text(encoding="utf-8") == "next-longer-payload"
    assert previous.exists()


def test_same_revision_activation_retry_is_idempotent(tmp_path):
    root, staging = layout(tmp_path)
    published = root / "kb"
    staged = tmp_path / "accepted" / "src"
    tree(published, "r2", "next")
    tree(staged, "r2", "next")
    prepared = sealed("kb", "r2", staged, None, root, published)
    assert activate_source(prepared, published) is None
    assert activate_source(prepared, published) is None
    assert (published / "data.txt").read_text(encoding="utf-8") == "next"
    assert staged.exists()


def test_failed_activation_keeps_published_tree(tmp_path, monkeypatch):
    root, staging = layout(tmp_path)
    published = root / "kb"
    staged = tmp_path / "accepted" / "src"
    tree(published, "r1", "previous")
    tree(staged, "r2", "next-longer")

    def fail_mirror(*_args, **_kwargs):
        raise OSError("injected activation failure")

    monkeypatch.setattr("contour.sources._mirror_tree_into", fail_mirror)
    with pytest.raises(OSError):
        activate_source(sealed("kb", "r2", staged, None, root, published), published)
    assert (published / "data.txt").read_text(encoding="utf-8") == "previous"


def test_package_prepare_points_at_package_src(tmp_path):
    root, staging = layout(tmp_path)
    package = validated_package(tmp_path)
    prepared = PackageSourceProvider(
        package,
        staging_root=staging,
        published_path=root / "kb",
        sources_root=root,
    ).prepare()
    assert prepared.staged_path == package.path / "src"
    assert prepared.content_digest is None
    assert (prepared.staged_path / "Configuration.xml").read_bytes() == (
        package.path / "src" / "Configuration.xml"
    ).read_bytes()
