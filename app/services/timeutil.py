"""Time helpers.

All stored timestamps are naive UTC. Reset-boundary logic lives here so that
"has the daily allowance rolled over?" is answered the same way everywhere,
and so tests can freeze time by passing ``now`` explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """Naive UTC timestamp, matching how the columns are stored."""
    return datetime.now(UTC).replace(tzinfo=None)


def start_of_utc_day(moment: datetime | None = None) -> datetime:
    moment = moment or utcnow()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_utc_month(moment: datetime | None = None) -> datetime:
    moment = moment or utcnow()
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_utc_day(moment: datetime | None = None) -> datetime:
    return start_of_utc_day(moment) + timedelta(days=1)


def next_utc_month(moment: datetime | None = None) -> datetime:
    start = start_of_utc_month(moment)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def has_reset_elapsed(next_reset_at: datetime | None, now: datetime | None = None) -> bool:
    """True when a provider-stated reset time has passed.

    Returns ``False`` when no reset time is recorded. The caller must not
    assume a refresh happened without a stated boundary - an unknown reset
    is unknown, not "probably now".
    """
    if next_reset_at is None:
        return False
    return (now or utcnow()) >= next_reset_at


def humanize_delta(target: datetime | None, now: datetime | None = None) -> str:
    """Render a short "in 3h 20m" / "2d ago" string for the dashboard."""
    if target is None:
        return "unknown"
    now = now or utcnow()
    delta = target - now
    past = delta.total_seconds() < 0
    seconds = abs(int(delta.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60

    if days:
        text = f"{days}d {hours}h"
    elif hours:
        text = f"{hours}h {minutes}m"
    elif minutes:
        text = f"{minutes}m"
    else:
        text = "under a minute"
    return f"{text} ago" if past else f"in {text}"


def format_duration(seconds: float | None) -> str:
    """Format a clip or total duration as ``m:ss`` / ``h:mm:ss``."""
    if seconds is None:
        return "unknown"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
