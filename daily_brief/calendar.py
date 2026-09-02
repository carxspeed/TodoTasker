"""Bounded iCalendar expansion and deterministic free-time calculation."""

from __future__ import annotations

import random
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from icalendar import Calendar

from .config import TimeInterval
from .models import CalendarEvent, CalendarSnapshot, CanvasEvent, FreeWindow
from .timeutils import utc_now


DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class SourceError:
    source: str
    code: str
    message: str


class CalendarSourceFailure(RuntimeError):
    def __init__(self, error: SourceError):
        self.error = error
        super().__init__(f"{error.code}: {error.message}")


def fetch_ical(
    secret_url: str,
    *,
    session: requests.Session | None = None,
    sleep=time_module.sleep,
) -> str:
    client = session or requests.Session()
    for attempt in range(1, 4):
        try:
            response = client.get(secret_url, timeout=(10, 60))
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 3:
                raise CalendarSourceFailure(
                    SourceError("calendar", "CONNECTION_FAILURE", "calendar feed could not be reached")
                ) from exc
            sleep((2 ** (attempt - 1)) + random.uniform(0, 0.25))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 3:
                raise CalendarSourceFailure(
                    SourceError("calendar", "TEMPORARY_FAILURE", f"calendar returned {response.status_code}")
                )
            raw_retry = response.headers.get("Retry-After")
            try:
                retry_after = float(raw_retry) if raw_retry else 2 ** (attempt - 1)
            except ValueError:
                retry_after = 2 ** (attempt - 1)
            if retry_after > 60:
                raise CalendarSourceFailure(
                    SourceError("calendar", "RETRY_AFTER_TOO_LONG", "calendar asked for a later retry")
                )
            sleep(retry_after)
            continue
        if response.status_code >= 400:
            raise CalendarSourceFailure(
                SourceError("calendar", "HTTP_ERROR", f"calendar returned {response.status_code}")
            )
        return response.text
    raise AssertionError("unreachable")


def _as_local(value, timezone: ZoneInfo) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("calendar datetime is missing a resolvable timezone")
        return value.astimezone(timezone), False
    if isinstance(value, date):
        return datetime.combine(value, time.min, timezone), True
    raise ValueError("calendar date/time is not recognized")


def _component_text(component, name: str, default: str = "") -> str:
    value = component.get(name)
    return str(value) if value is not None else default


def _normalize_component(
    component,
    *,
    timezone: ZoneInfo,
    informational_patterns: list[str],
    no_school_patterns: list[str],
    warnings: list[str],
) -> tuple[CalendarEvent | None, bool]:
    if _component_text(component, "STATUS").upper() == "CANCELLED":
        return None, False
    title = _component_text(component, "SUMMARY", "Untitled event").strip() or "Untitled event"
    try:
        start, all_day = _as_local(component.decoded("DTSTART"), timezone)
    except Exception:
        warnings.append(f"Excluded calendar event {title!r}: unknown or missing timezone")
        return None, False

    if component.get("DTEND") is not None:
        try:
            end, end_all_day = _as_local(component.decoded("DTEND"), timezone)
            all_day = all_day and end_all_day
        except Exception:
            warnings.append(f"Excluded calendar event {title!r}: invalid end timezone")
            return None, False
    elif all_day:
        end = start + timedelta(days=1)
    else:
        end = start

    lowered = title.casefold()
    no_school = all_day and any(pattern.casefold() in lowered for pattern in no_school_patterns)
    informational = all_day and any(
        pattern.casefold() in lowered for pattern in informational_patterns
    )
    transparent = _component_text(component, "TRANSP").upper() == "TRANSPARENT"
    busy = not transparent and not informational and not no_school and end > start
    return (
        CalendarEvent(
            title=title,
            start=start,
            end=end,
            source="ical",
            all_day=all_day,
            busy=busy,
        ),
        no_school,
    )


def _canvas_event(event: CanvasEvent, timezone: ZoneInfo) -> CalendarEvent:
    start = event.start.astimezone(timezone)
    if event.end is not None:
        end = event.end.astimezone(timezone)
    elif event.all_day:
        end = start + timedelta(days=1)
    else:
        end = start
    return CalendarEvent(
        title=event.title,
        start=start,
        end=end,
        source="canvas",
        all_day=event.all_day,
        busy=event.busy and end > start,
    )


def _interval_for(day: date, interval: TimeInterval, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    start_clock = time.fromisoformat(interval.start)
    end_clock = time.fromisoformat(interval.end)
    return (
        datetime.combine(day, start_clock, timezone),
        datetime.combine(day, end_clock, timezone),
    )


def _union(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals, key=lambda pair: (pair[0], pair[1])):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _free_windows(
    target_date: date,
    events: list[CalendarEvent],
    *,
    timezone: ZoneInfo,
    school_hours: dict[str, list[TimeInterval]],
    fixed_busy_windows: dict[str, list[TimeInterval]],
    suppress_school: bool,
) -> list[FreeWindow]:
    day_start = datetime.combine(target_date, time(6, 0), timezone)
    day_end = datetime.combine(target_date, time(22, 30), timezone)
    busy: list[tuple[datetime, datetime]] = []
    for event in events:
        if not event.busy:
            continue
        start = max(event.start.astimezone(timezone), day_start)
        end = min(event.end.astimezone(timezone), day_end)
        if end > start:
            busy.append((start, end))
    key = DAY_KEYS[target_date.weekday()]
    if not suppress_school:
        busy.extend(_interval_for(target_date, value, timezone) for value in school_hours.get(key, []))
    busy.extend(
        _interval_for(target_date, value, timezone) for value in fixed_busy_windows.get(key, [])
    )
    clipped = [
        (max(start, day_start), min(end, day_end))
        for start, end in busy
        if min(end, day_end) > max(start, day_start)
    ]
    cursor = day_start
    free: list[FreeWindow] = []
    for start, end in _union(clipped):
        if start > cursor:
            free.append(FreeWindow(start=cursor, end=start))
        cursor = max(cursor, end)
    if cursor < day_end:
        free.append(FreeWindow(start=cursor, end=day_end))
    return free


def build_calendar_snapshot(
    ical_text: str,
    target_date: date,
    *,
    timezone_name: str,
    school_hours: dict[str, list[TimeInterval]],
    fixed_busy_windows: dict[str, list[TimeInterval]],
    no_school_patterns: list[str],
    informational_patterns: list[str],
    canvas_events: list[CanvasEvent] | None = None,
    fetched_at: datetime | None = None,
) -> CalendarSnapshot:
    timezone = ZoneInfo(timezone_name)
    range_start = datetime.combine(target_date, time.min, timezone)
    range_end = range_start + timedelta(days=1)
    warnings: list[str] = []
    try:
        calendar = Calendar.from_ical(ical_text)
        expanded = recurring_ical_events.of(calendar).between(range_start, range_end)
    except Exception as exc:
        raise CalendarSourceFailure(
            SourceError("calendar", "INVALID_ICAL", "calendar feed could not be parsed")
        ) from exc

    events: list[CalendarEvent] = []
    suppress_school = False
    for component in expanded:
        event, no_school = _normalize_component(
            component,
            timezone=timezone,
            informational_patterns=informational_patterns,
            no_school_patterns=no_school_patterns,
            warnings=warnings,
        )
        suppress_school = suppress_school or no_school
        if event is not None:
            events.append(event)
    for raw in canvas_events or []:
        normalized = _canvas_event(raw, timezone)
        if normalized.end >= range_start and normalized.start < range_end:
            events.append(normalized)
    events.sort(key=lambda event: (event.start, event.end, event.source, event.title.casefold()))
    return CalendarSnapshot(
        target_date=target_date,
        fetched_at=fetched_at or utc_now(),
        events=events,
        free_windows=_free_windows(
            target_date,
            events,
            timezone=timezone,
            school_hours=school_hours,
            fixed_busy_windows=fixed_busy_windows,
            suppress_school=suppress_school,
        ),
        warnings=warnings,
    )

