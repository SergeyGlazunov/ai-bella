from __future__ import annotations

from unittest.mock import patch

import pytest

from contour.watch_bsl import _parse_daemon_reload_json, reload_bsl_daemon


def test_parse_reload_json_prefers_reloaded_object():
    text = 'warn {not json}\n{"other": 1}\nlog brace }\n{"reloaded": true, "n": 2}\n'
    assert _parse_daemon_reload_json(text) == {"reloaded": True, "n": 2}


def test_parse_reload_json_ignores_braces_in_noise():
    text = 'trace {frame: 1} before\n{"reloaded": false, "error": "busy"}\n'
    data = _parse_daemon_reload_json(text)
    assert data["reloaded"] is False
    assert data["error"] == "busy"


def test_parse_reload_json_missing_raises():
    with pytest.raises(RuntimeError, match="no JSON"):
        _parse_daemon_reload_json("no objects here")


def test_reload_bsl_daemon_docker_fallback_parses_noisy_json(monkeypatch):
    class Cfg:
        class services:
            bsl_container = "bsl-test"

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "docker"
        return type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": 'noise {x}\n{"reloaded": true}\n',
                "stderr": "warn {y}\n",
            },
        )()

    monkeypatch.setattr("contour.watch_bsl.Path.is_file", lambda self: False)
    with patch("contour.watch_bsl.subprocess.run", side_effect=fake_run):
        reload_bsl_daemon(Cfg())


def test_reload_bsl_daemon_rejects_reloaded_false(monkeypatch):
    class Cfg:
        class services:
            bsl_container = "bsl-test"

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

    monkeypatch.setattr("contour.watch_bsl.Path.is_file", lambda self: False)
    with patch("contour.watch_bsl.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="reloaded!=true"):
            reload_bsl_daemon(Cfg())


def test_reload_bsl_daemon_script_path_prefers_ai_contour(monkeypatch):
    class Cfg:
        class services:
            bsl_container = "bsl-test"

    seen: list[str] = []

    def fake_is_file(self):
        return str(self).replace("\\", "/") == (
            "/opt/ai-contour/integrations/reload_bsl_watch.sh"
        )

    def fake_run(cmd, **kwargs):
        seen.append(str(cmd[0]).replace("\\", "/"))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("contour.watch_bsl.Path.is_file", fake_is_file)
    with patch("contour.watch_bsl.subprocess.run", side_effect=fake_run):
        reload_bsl_daemon(Cfg())
    assert seen == ["/opt/ai-contour/integrations/reload_bsl_watch.sh"]
