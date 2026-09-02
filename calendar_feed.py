"""Read-only calendar snapshot CLI."""

from __future__ import annotations

import argparse
from datetime import date

from daily_brief.calendar import CalendarSourceFailure, build_calendar_snapshot, fetch_ical
from daily_brief.config import ConfigurationError, load_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", type=date.fromisoformat, default=date.today())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        settings = load_settings(required=("ICAL_URL",))
        snapshot = build_calendar_snapshot(
            fetch_ical(settings.ical_url),
            args.target_date,
            timezone_name=settings.timezone,
            school_hours=settings.school_hours,
            fixed_busy_windows=settings.fixed_busy_windows,
            no_school_patterns=settings.no_school_patterns,
            informational_patterns=settings.informational_all_day_patterns,
        )
        print(snapshot.model_dump_json())
        return 0
    except (ConfigurationError, CalendarSourceFailure) as exc:
        print(f"CALENDAR_ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

