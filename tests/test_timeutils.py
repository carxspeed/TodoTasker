from datetime import date, datetime, time, timezone

import pytest

from daily_brief.timeutils import local_date, local_datetime, parse_external_timestamp


def test_external_timestamp_normalizes_to_utc() -> None:
    value = parse_external_timestamp("2026-09-01T18:30:00-07:00")
    assert value == datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)


def test_naive_external_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_external_timestamp("2026-09-01T18:30:00")


def test_local_date_arithmetic_uses_configured_zone() -> None:
    instant = datetime(2026, 9, 2, 2, 0, tzinfo=timezone.utc)
    assert local_date(instant, "America/Los_Angeles") == date(2026, 9, 1)
    wall = local_datetime(date(2026, 11, 1), time(6, 30), "America/Los_Angeles")
    assert wall.utcoffset() is not None

