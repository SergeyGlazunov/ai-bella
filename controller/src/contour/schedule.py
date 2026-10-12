from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from contour.config import Config


def _parse_hhmm(value: str) -> time:
    hour, minute = map(int, value.split(":", 1))
    return time(hour, minute)


def _local_time(now: datetime, timezone: str) -> time:
    tz = ZoneInfo(timezone)
    if now.tzinfo is None:
        local = now.replace(tzinfo=tz)
    else:
        local = now.astimezone(tz)
    return local.time()


def _in_window(now: datetime, cfg: Config) -> bool:
    start = _parse_hhmm(cfg.schedule.window_start)
    end = _parse_hhmm(cfg.schedule.window_end)
    current = _local_time(now, cfg.schedule.timezone)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def elapsed_in_window(now: datetime, cfg: Config) -> float:
    """Seconds since today's (or overnight) window_start. 0 if before start."""
    tz = ZoneInfo(cfg.schedule.timezone)
    if now.tzinfo is None:
        local = now.replace(tzinfo=tz)
    else:
        local = now.astimezone(tz)
    start = _parse_hhmm(cfg.schedule.window_start)
    start_dt = datetime.combine(local.date(), start, tzinfo=tz)
    if local < start_dt:
        start_dt -= timedelta(days=1)
    return max(0.0, (local - start_dt).total_seconds())


def seconds_until_window_end(now: datetime, cfg: Config) -> float:
    tz = ZoneInfo(cfg.schedule.timezone)
    if now.tzinfo is None:
        local = now.replace(tzinfo=tz)
    else:
        local = now.astimezone(tz)
    end = _parse_hhmm(cfg.schedule.window_end)
    end_dt = datetime.combine(local.date(), end, tzinfo=tz)
    start = _parse_hhmm(cfg.schedule.window_start)
    if start > end and local.time() >= start:
        end_dt += timedelta(days=1)
    return max(0.0, (end_dt - local).total_seconds())


def remaining_write_seconds(
    *,
    now: datetime,
    cfg: Config,
    measure: bool,
    elapsed_monotonic: float = 0.0,
) -> float:
    """Seconds left in the window to *start* a new stage. Does not kill running jobs."""
    del measure, elapsed_monotonic
    if not _in_window(now, cfg):
        return 0.0
    return seconds_until_window_end(now, cfg)


def may_write(*, now: datetime, cfg: Config, is_timer: bool, measure: bool) -> str:
    """Window gates starting work. Duration warn is not a write lock."""
    del is_timer, measure
    if not _in_window(now, cfg):
        return "deferred_window"
    return "ok"


def duration_warn_seconds(cfg: Config) -> int | None:
    warn = getattr(cfg.schedule, "duration_warn_seconds", None)
    if warn is not None:
        return warn
    return cfg.schedule.build_deadline_seconds
