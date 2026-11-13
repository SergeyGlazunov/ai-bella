from __future__ import annotations

from unittest.mock import patch

import pytest

from contour.bsl_ctl import idle_bsl, reload_bsl


class _R:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_idle_bsl_true_when_running_ready():
    payload = '{"status":"running","paths":[{"path":"/repos/SRC","status":"ready"}]}'

    def fake_run(cmd, **kwargs):
        assert "daemon" in cmd and "status" in cmd
        return _R(stdout=payload)

    with patch("contour.dockerutil.SubprocessRunner.run", side_effect=fake_run):
        assert idle_bsl("bsl-test") is True


def test_idle_bsl_false_when_indexing():
    payload = '{"status":"running","paths":[{"path":"/repos/SRC","status":"indexing"}]}'

    with patch(
        "contour.dockerutil.SubprocessRunner.run",
        return_value=_R(stdout=payload),
    ):
        assert idle_bsl("bsl-test") is False


def test_reload_bsl_ok():
    with patch(
        "contour.dockerutil.SubprocessRunner.run",
        return_value=_R(stdout='{"reloaded": true}'),
    ):
        reload_bsl("bsl-test")


def test_reload_bsl_rejects_false():
    with patch(
        "contour.dockerutil.SubprocessRunner.run",
        return_value=_R(stdout='{"reloaded": false, "error": "busy"}'),
    ):
        with pytest.raises(RuntimeError, match="reloaded!=true"):
            reload_bsl("bsl-test")
