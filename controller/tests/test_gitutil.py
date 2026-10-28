from __future__ import annotations

import subprocess
from pathlib import Path

from contour import gitutil


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    return remote


def _clone_with_initial_commit(workspace: Path, remote: Path, *, content: str = "initial") -> Path:
    clone = workspace / "clone"
    _git(workspace, "clone", str(remote), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "file.txt").write_text(content, encoding="utf-8")
    _git(clone, "add", "file.txt")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "push", "-u", "origin", "HEAD")
    return clone


def _rev_parse(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _current_branch(repo: Path) -> str:
    return _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()


def test_safe_remote_strips_userinfo():
    assert gitutil.safe_remote("https://oauth2:x@host/r") == "https://host/r"


def test_is_dirty_true_when_working_tree_modified(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)
    (repo / "file.txt").write_text("dirty", encoding="utf-8")
    assert gitutil.is_dirty(repo) is True


def test_is_dirty_true_when_index_staged(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)
    (repo / "new.txt").write_text("staged", encoding="utf-8")
    _git(repo, "add", "new.txt")
    assert gitutil.is_dirty(repo) is True


def test_is_dirty_true_when_untracked(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)
    (repo / "scratch.tmp").write_text("local", encoding="utf-8")
    assert gitutil.is_dirty(repo) is True


def test_is_dirty_false_when_clean(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)
    assert gitutil.is_dirty(repo) is False


def test_is_dirty_false_when_only_code_index_untracked(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)
    code_index = repo / "src" / ".code-index"
    code_index.mkdir(parents=True)
    (code_index / "index.db").write_text("local", encoding="utf-8")
    (repo / ".contour-revision").write_text("abc", encoding="utf-8")
    assert gitutil.is_dirty(repo) is False


def test_is_dirty_true_when_code_index_plus_other_untracked(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)
    (repo / "src" / ".code-index").mkdir(parents=True)
    (repo / "scratch.tmp").write_text("local", encoding="utf-8")
    assert gitutil.is_dirty(repo) is True


def test_ff_only_moves_head_on_clean_fast_forward(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)
    sha_before = _rev_parse(repo)

    writer = tmp_path / "writer"
    _git(tmp_path, "clone", str(remote), str(writer))
    _git(writer, "config", "user.email", "test@example.com")
    _git(writer, "config", "user.name", "Test")
    (writer / "file.txt").write_text("second commit", encoding="utf-8")
    _git(writer, "add", "file.txt")
    _git(writer, "commit", "-m", "second")
    sha_remote = _rev_parse(writer)
    _git(writer, "push", "origin", "HEAD")

    branch = _current_branch(repo)
    gitutil.fetch(repo)
    sha_after = gitutil.ff_only(repo, branch)

    assert sha_before != sha_after
    assert sha_after == sha_remote
    assert _rev_parse(repo) == sha_remote


def test_can_fast_forward_true_when_origin_ahead(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)
    writer = tmp_path / "writer-ff"
    _git(tmp_path, "clone", str(remote), str(writer))
    _git(writer, "config", "user.email", "test@example.com")
    _git(writer, "config", "user.name", "Test")
    (writer / "file.txt").write_text("ahead", encoding="utf-8")
    _git(writer, "add", "file.txt")
    _git(writer, "commit", "-m", "ahead")
    _git(writer, "push", "origin", "HEAD")
    branch = _current_branch(repo)
    gitutil.fetch(repo)
    assert gitutil.can_fast_forward(repo, branch) is True


def test_can_fast_forward_false_when_diverged(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)
    writer = tmp_path / "writer-div"
    _git(tmp_path, "clone", str(remote), str(writer))
    _git(writer, "config", "user.email", "test@example.com")
    _git(writer, "config", "user.name", "Test")
    (writer / "file.txt").write_text("remote", encoding="utf-8")
    _git(writer, "add", "file.txt")
    _git(writer, "commit", "-m", "remote")
    _git(writer, "push", "origin", "HEAD")
    (repo / "file.txt").write_text("local", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "local")
    branch = _current_branch(repo)
    gitutil.fetch(repo)
    assert gitutil.can_fast_forward(repo, branch) is False


def test_fetch_prunes_and_updates_remote_tracking(tmp_path: Path):
    remote = _init_bare_remote(tmp_path)
    repo = _clone_with_initial_commit(tmp_path, remote)

    writer = tmp_path / "writer2"
    _git(tmp_path, "clone", str(remote), str(writer))
    _git(writer, "config", "user.email", "test@example.com")
    _git(writer, "config", "user.name", "Test")
    (writer / "file.txt").write_text("ahead", encoding="utf-8")
    _git(writer, "add", "file.txt")
    _git(writer, "commit", "-m", "ahead")
    _git(writer, "push", "origin", "HEAD")

    branch = _current_branch(repo)
    gitutil.fetch(repo)
    tracked = _git(repo, "rev-parse", f"origin/{branch}").stdout.strip()
    assert tracked == _rev_parse(writer)
