"""Remount / mount-health gates after rename-activate (BSL bind inode)."""
from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from contour.remount_bsl import (
    BslMountError,
    assert_bsl_mount_healthy,
    assert_code_index_nonempty,
    parse_src_mount_root,
    published_workspace,
    remount_after_activate,
    remount_bsl_src,
)


@dataclass
class FakeDocker:
    mountinfo: str = ""
    bsl_count: str = "0"
    status: str = "running"
    restarts: list[str] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def run(
        self, cmd: list[str], *, timeout: float | None = None, env=None
    ) -> subprocess.CompletedProcess[str]:
        del timeout, env
        self.calls.append(list(cmd))
        if cmd[:2] == ["docker", "restart"]:
            self.restarts.append(cmd[2])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["docker", "inspect", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, self.status + "\n", "")
        if "mountinfo" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, self.mountinfo, "")
        if any("*.bsl" in part or "*.bsl" in " ".join(cmd) for part in cmd):
            return subprocess.CompletedProcess(cmd, 0, self.bsl_count + "\n", "")
        # find | wc -l style via sh -c
        joined = " ".join(cmd)
        if "find" in joined and "bsl" in joined:
            return subprocess.CompletedProcess(cmd, 0, self.bsl_count + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_parse_src_mount_root_happy():
    line = (
        "955 884 8:48 /srv/ai-contour/sources/ubd/src /repos/SRC "
        "rw,relatime - ext4 /dev/sdd rw"
    )
    assert parse_src_mount_root(line) == "/srv/ai-contour/sources/ubd/src"


def test_parse_src_mount_root_rejects_deleted():
    line = (
        "957 882 8:48 /srv/.../stale-prev-ubd-abc/src//deleted /repos/SRC "
        "rw - ext4 /dev/sdd rw"
    )
    with pytest.raises(BslMountError, match="deleted"):
        parse_src_mount_root(line)


def test_parse_src_mount_root_rejects_prev():
    line = (
        "1 2 8:48 /srv/ai-contour/sources/ubd.prev-deadbeef/src /repos/SRC "
        "rw - ext4 /dev/sdd rw"
    )
    with pytest.raises(BslMountError, match="prev"):
        parse_src_mount_root(line)


def test_assert_healthy_requires_matching_published(tmp_path: Path):
    published = tmp_path / "ubd" / "src"
    published.mkdir(parents=True)
    (published / "a.bsl").write_text("x", encoding="utf-8")
    docker = FakeDocker(
        mountinfo=(
            f"1 2 8:48 {published} /repos/SRC rw - ext4 /dev/sdd rw\n"
        ),
        bsl_count="1",
    )
    assert_bsl_mount_healthy(
        docker, "bsl", published_src=published, min_bsl_files=1
    )


def test_assert_healthy_rejects_stale_mount(tmp_path: Path):
    published = tmp_path / "ubd" / "src"
    published.mkdir(parents=True)
    (published / "a.bsl").write_text("x", encoding="utf-8")
    stale = tmp_path / "ubd.prev-old" / "src"
    docker = FakeDocker(
        mountinfo=f"1 2 8:48 {stale} /repos/SRC rw - ext4 /dev/sdd rw\n",
        bsl_count="0",
    )
    with pytest.raises(BslMountError, match="prev|match|stale"):
        assert_bsl_mount_healthy(
            docker, "bsl", published_src=published, min_bsl_files=1
        )


def test_assert_healthy_rejects_zero_bsl(tmp_path: Path):
    published = tmp_path / "ubd" / "src"
    published.mkdir(parents=True)
    docker = FakeDocker(
        mountinfo=f"1 2 8:48 {published} /repos/SRC rw - ext4 /dev/sdd rw\n",
        bsl_count="0",
    )
    with pytest.raises(BslMountError, match="bsl"):
        assert_bsl_mount_healthy(
            docker, "bsl", published_src=published, min_bsl_files=1
        )


def test_remount_restarts_then_asserts(tmp_path: Path):
    published = tmp_path / "ubd" / "src"
    published.mkdir(parents=True)
    (published / "a.bsl").write_text("x", encoding="utf-8")
    docker = FakeDocker(
        mountinfo=f"1 2 8:48 {published} /repos/SRC rw - ext4 /dev/sdd rw\n",
        bsl_count="1",
    )
    remount_bsl_src(docker, "ai2-bsl-indexer", published_src=published, min_bsl_files=1)
    assert docker.restarts == ["ai2-bsl-indexer"]


def test_assert_code_index_nonempty(tmp_path: Path):
    src = tmp_path / "src"
    db_dir = src / ".code-index"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)"
    )
    conn.execute("INSERT INTO files(path) VALUES ('a.bsl')")
    conn.commit()
    conn.close()
    assert_code_index_nonempty(src, min_files=1)


def test_assert_code_index_rejects_empty(tmp_path: Path):
    src = tmp_path / "src"
    db_dir = src / ".code-index"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(BslMountError, match="empty|files"):
        assert_code_index_nonempty(src, min_files=1)


def _db_with_src(path: str = "/srv/sources/ubd"):
    from contour.config import (
        AdapterCommands,
        AdaptersConfig,
        DatabaseConfig,
        GitSourceConfig,
        ServicesConfig,
        StorageConfig,
    )

    return DatabaseConfig(
        name="ubd",
        source=GitSourceConfig("git", path, "origin", "main", "src"),
        required_layers=frozenset({"code_index"}),
        services=ServicesConfig(
            graph_compose_dir="/g",
            graph_project="g",
            graph_service="s",
            graph_mcp="m",
            bsl_compose_dir="/b",
            bsl_project="b",
            bsl_container="ai2-bsl-indexer",
            bsl_publish_port=8011,
            bsl_daemon_toml="/t",
            webui_network="n",
        ),
        storage=StorageConfig("gv", "cv"),
        adapters=AdaptersConfig(AdapterCommands(), AdapterCommands(), []),
    )


def test_published_workspace_git_subdir():
    db = _db_with_src("/srv/sources/ubd")
    assert published_workspace(db, Path("/srv/sources/ubd")) == Path(
        "/srv/sources/ubd/src"
    )


def test_remount_after_activate_skips_restart_when_healthy(tmp_path: Path):
    published = tmp_path / "ubd" / "src"
    published.mkdir(parents=True)
    (published / "a.bsl").write_text("x", encoding="utf-8")
    docker = FakeDocker(
        mountinfo=f"1 2 8:48 {published} /repos/SRC rw - ext4 /dev/sdd rw\n",
        bsl_count="1",
    )
    db = _db_with_src(str(tmp_path / "ubd"))
    remount_after_activate(db, tmp_path / "ubd", runner=docker, min_bsl_files=1)
    assert docker.restarts == []


def test_remount_after_activate_restarts_when_stale(tmp_path: Path):
    published = tmp_path / "ubd" / "src"
    published.mkdir(parents=True)
    (published / "a.bsl").write_text("x", encoding="utf-8")
    stale = tmp_path / "ubd.prev-old" / "src"
    docker = FakeDocker(
        mountinfo=f"1 2 8:48 {stale} /repos/SRC rw - ext4 /dev/sdd rw\n",
        bsl_count="0",
    )
    # After restart, fake still returns stale unless we flip mountinfo on restart.
    calls_before = {"n": 0}

    def run(cmd, *, timeout=None, env=None):
        result = FakeDocker.run(docker, cmd, timeout=timeout, env=env)
        if cmd[:2] == ["docker", "restart"]:
            docker.mountinfo = (
                f"1 2 8:48 {published} /repos/SRC rw - ext4 /dev/sdd rw\n"
            )
            docker.bsl_count = "1"
        calls_before["n"] += 1
        return result

    docker.run = run  # type: ignore[method-assign]
    db = _db_with_src(str(tmp_path / "ubd"))
    remount_after_activate(db, tmp_path / "ubd", runner=docker, min_bsl_files=1)
    assert docker.restarts == ["ai2-bsl-indexer"]
