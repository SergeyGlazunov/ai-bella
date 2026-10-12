from datetime import datetime
from pathlib import Path

import yaml
from zoneinfo import ZoneInfo

from contour.config import Config, load_config
from contour.schedule import may_write

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"
VLAD = ZoneInfo("Asia/Vladivostok")


def _make_config(tmp_path: Path, **schedule_overrides: object) -> Config:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["schedule"].update(schedule_overrides)
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(str(cfg_path))


def _now(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 20, hour, minute, tzinfo=VLAD)


def test_outside_window_at_noon_deferred(tmp_path: Path):
    cfg = _make_config(tmp_path)
    assert (
        may_write(now=_now(12, 0), cfg=cfg, is_timer=False, measure=False)
        == "deferred_window"
    )


def test_inside_window_null_warn_timer_and_cli_ok(tmp_path: Path):
    """Null duration_warn_seconds must not block automatic writes (assignment §3)."""
    cfg = _make_config(tmp_path, duration_warn_seconds=None, build_deadline_seconds=None)
    assert (
        may_write(now=_now(2, 0), cfg=cfg, is_timer=True, measure=False) == "ok"
    )
    assert (
        may_write(now=_now(2, 0), cfg=cfg, is_timer=False, measure=False) == "ok"
    )


def test_inside_window_warn_threshold_does_not_defer(tmp_path: Path):
    cfg = _make_config(tmp_path, duration_warn_seconds=60)
    assert (
        may_write(now=_now(2, 0), cfg=cfg, is_timer=True, measure=False) == "ok"
    )


def test_inside_window_with_deadline_ok(tmp_path: Path):
    cfg = _make_config(tmp_path, build_deadline_seconds=3600)
    assert (
        may_write(now=_now(2, 0), cfg=cfg, is_timer=False, measure=False) == "ok"
    )
