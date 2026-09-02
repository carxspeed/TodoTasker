"""Timezone-safe date and timestamp helpers."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc


def parse_external_timestamp(value: str | datetime) -> datetime:
    """Parse an external timestamp and normalize it to aware UTC."""

    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("external timestamp must include a timezone")
    return parsed.astimezone(UTC)


def local_datetime(day: date, clock: time, timezone_name: str) -> datetime:
    if clock.tzinfo is not None:
        raise ValueError("clock must be naive local wall time")
    return datetime.combine(day, clock, ZoneInfo(timezone_name))


def local_date(value: datetime, timezone_name: str) -> date:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(ZoneInfo(timezone_name)).date()


def utc_now() -> datetime:
    return datetime.now(UTC)

