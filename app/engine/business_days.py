"""Business-day waits and owner-local send windows (A4, A7).

Weekends are excluded; company holidays are not assumed in v1.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_business_day(d: datetime) -> bool:
    return d.weekday() < 5


def add_business_days(baseline: datetime, days: int, tz: str) -> datetime:
    """Keep the baseline's local clock time and move forward `days` weekdays."""
    local = baseline.astimezone(ZoneInfo(tz))
    added = 0
    while added < days:
        local += timedelta(days=1)
        if is_business_day(local):
            added += 1
    return local.astimezone(UTC)


def next_send_slot(at: datetime, tz: str, start: str = "08:30", end: str = "17:30") -> datetime:
    """The earliest moment at or after `at` inside the owner's Monday-Friday send window."""
    zone = ZoneInfo(tz)
    local = at.astimezone(zone)
    s, e = _parse_hhmm(start), _parse_hhmm(end)
    for _ in range(14):
        if is_business_day(local):
            day_start = datetime.combine(local.date(), s, zone)
            day_end = datetime.combine(local.date(), e, zone)
            if local < day_start:
                return day_start.astimezone(UTC)
            if local < day_end:
                return local.astimezone(UTC)
        local = datetime.combine(local.date() + timedelta(days=1), time(0, 0), zone)
    raise RuntimeError("No send slot found within two weeks")  # pragma: no cover


def in_send_window(at: datetime, tz: str, start: str = "08:30", end: str = "17:30") -> bool:
    return next_send_slot(at, tz, start, end) == at.astimezone(UTC)


def first_check_at(baseline: datetime, wait_business_days: int, tz: str, start: str, end: str) -> datetime:
    """Baseline + wait in business days, then moved into the send window.

    A wait of 0 (meeting_no_show) means the next permitted slot the same day, else the next workday.
    """
    due = add_business_days(baseline, wait_business_days, tz) if wait_business_days > 0 else baseline
    return next_send_slot(due, tz, start, end)


def same_local_day(a: datetime, b: datetime, tz: str) -> bool:
    zone = ZoneInfo(tz)
    return a.astimezone(zone).date() == b.astimezone(zone).date()


def local_day_bounds(at: datetime, tz: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    d = at.astimezone(zone).date()
    start = datetime.combine(d, time(0, 0), zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)
