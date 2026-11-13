from __future__ import annotations

from unittest.mock import patch

import pytest

from contour.bsl_ctl import _parse_reload_json, reload_bsl
from contour.watch_bsl import reload_bsl_daemon


def test_parse_reload_json_prefers_reloaded_object():
    text = 'warn {not json}\n{"other": 1}\nlog brace }\n{"reloaded": true, "n": 2}\n'
    assert _parse_reload_json(text) == {"reloaded": True, "n": 2}


def test_parse_reload_json_ignores_braces_in_noise():
    text = 'trace {frame: 1} before\n{"reloaded": false, "error": "busy"}\n'
    data = _parse_reload_json(text)
    assert data["reloaded"] is False
    assert data["error"] == "busy"


def test_parse_reload_json_missing_raises():
    with pytest.raises(RuntimeError, match="no JSON"):
        _parse_reload_json("no objects here")


def test_reload_bsl_daemon_parses_noisy_json():
    class Cfg:
        class services:
            bsl_container = "bsl-test"

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["docker", "exec", "-e"]
        assert "bsl-test" in cmd
        return type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": 'noise {x}\n{"reloaded": true}\n',
                "stderr": "warn {y}\n",
            },
        )()

    with patch("contour.dockerutil.SubprocessRunner.run", side_effect=fake_run):
        reload_bsl_daemon(Cfg())


def test_reload_bsl_rejects_reloaded_false():
    def fake_run(cmd, **kwargs):
        return type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": '{"reloaded": false, "error": "busy"}',
                "stderr": "",
            },
        )()

    with patch("contour.dockerutil.SubprocessRunner.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="reloaded!=true"):
            reload_bsl("bsl-test")


def test_reload_bsl_daemon_uses_python_not_bash_script():
    class Cfg:
        class services:
            bsl_container = "bsl-test"

    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return type(
            "R",
            (),
            {"returncode": 0, "stdout": '{"reloaded": true}', "stderr": ""},
        )()

    with patch("contour.dockerutil.SubprocessRunner.run", side_effect=fake_run):
        reload_bsl_daemon(Cfg())
    assert seen
    assert seen[0][0] == "docker"
    assert "bsl-indexer" in seen[0]
    assert "reload" in seen[0]
    assert not any(str(part).endswith(".sh") for part in seen[0])
