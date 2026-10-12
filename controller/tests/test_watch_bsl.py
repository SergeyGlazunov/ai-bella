from __future__ import annotations

from pathlib import Path

import pytest

from contour.watch_bsl import pause_watch, restore_watch, watch_is_paused

SAMPLE_TOML = """[[paths]]
path = "/repos/SRC"
"""

LIVE_DAEMON_TOML = r"C:\MCP\code_index\deploy\daemon.toml"


def test_pause_watch_removes_src_path(tmp_path: Path):
    toml_path = tmp_path / "daemon.toml"
    toml_path.write_text(SAMPLE_TOML, encoding="utf-8")
    assert str(toml_path) != LIVE_DAEMON_TOML

    seen: list[str] = []

    def reload() -> None:
        seen.append(toml_path.read_text(encoding="utf-8"))

    pause_watch(str(toml_path), reload=reload)

    assert seen, "reload must be called after write"
    assert "/repos/SRC" not in seen[0]
    assert "/repos/SRC" not in toml_path.read_text(encoding="utf-8")


def test_pause_watch_strips_src_table_with_toml_array(tmp_path: Path):
    toml_path = tmp_path / "daemon.toml"
    toml_path.write_text(
        'listen = "0.0.0.0:8001"\n\n'
        "[[paths]]\n"
        'path = "/repos/SRC"\n'
        'ignore = ["*.ext", "tmp/**"]\n\n'
        "[[paths]]\n"
        'path = "/repos/OTHER"\n',
        encoding="utf-8",
    )
    pause_watch(str(toml_path), reload=lambda: None)
    text = toml_path.read_text(encoding="utf-8")
    assert "/repos/SRC" not in text
    assert 'ignore = ["*.ext", "tmp/**"]' not in text
    assert "/repos/OTHER" in text
    assert "listen" in text


def test_pause_watch_missing_file_raises(tmp_path: Path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(FileNotFoundError):
        pause_watch(str(missing), reload=lambda: None)


def test_pause_watch_strips_only_src_path_section(tmp_path: Path):
    toml_path = tmp_path / "daemon.toml"
    toml_path.write_text(
        'listen = "0.0.0.0:8001"\n\n'
        '[[paths]]\npath = "/repos/SRC"\n\n'
        '[[paths]]\npath = "/repos/OTHER"\n',
        encoding="utf-8",
    )
    pause_watch(str(toml_path), reload=lambda: None)
    text = toml_path.read_text(encoding="utf-8")
    assert "/repos/SRC" not in text
    assert "/repos/OTHER" in text
    assert "listen" in text


def test_watch_is_paused_when_inspect_has_no_src():
    assert watch_is_paused(lambda: "paths = []\n") is True
    assert watch_is_paused(lambda: 'path = "/repos/SRC"\n') is False


def test_restore_watch_copies_backup_then_reloads(tmp_path: Path):
    toml_path = tmp_path / "daemon.toml"
    backup = tmp_path / "daemon.toml.bak"
    toml_path.write_text("paused\n", encoding="utf-8")
    backup.write_text(SAMPLE_TOML, encoding="utf-8")
    seen: list[str] = []

    def reload() -> None:
        seen.append(toml_path.read_text(encoding="utf-8"))

    restore_watch(str(toml_path), str(backup), reload=reload)

    assert seen, "reload must be called after restore"
    assert "/repos/SRC" in seen[0]
    assert toml_path.read_text(encoding="utf-8") == SAMPLE_TOML
