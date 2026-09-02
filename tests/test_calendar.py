from datetime import date, datetime, timezone

from daily_brief.calendar import build_calendar_snapshot
from daily_brief.config import TimeInterval
from daily_brief.models import CanvasEvent


def calendar(*events: str) -> str:
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN", *events, "END:VCALENDAR", ""])


def event(*lines: str) -> str:
    return "\r\n".join(["BEGIN:VEVENT", *lines, "END:VEVENT"])


SCHOOL = {day: [TimeInterval(start="07:30", end="14:30")] for day in ("mon", "tue", "wed", "thu", "fri")}


def build(text: str, target=date(2026, 9, 1), **kwargs):
    return build_calendar_snapshot(
        text,
        target,
        timezone_name="America/Los_Angeles",
        school_hours=SCHOOL,
        fixed_busy_windows={},
        no_school_patterns=["no school"],
        informational_patterns=["birthday"],
        fetched_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        **kwargs,
    )


def test_timed_event_and_school_hours_produce_union_complement() -> None:
    snapshot = build(
        calendar(
            event(
                "UID:one",
                "DTSTART;TZID=America/Los_Angeles:20260901T160000",
                "DTEND;TZID=America/Los_Angeles:20260901T170000",
                "SUMMARY:Practice",
            )
        )
    )
    assert [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in snapshot.free_windows] == [
        ("06:00", "07:30"),
        ("14:30", "16:00"),
        ("17:00", "22:30"),
    ]


def test_weekly_recurrence_and_exdate_are_expanded() -> None:
    recurring = event(
        "UID:weekly",
        "DTSTART;TZID=America/Los_Angeles:20260901T160000",
        "DTEND;TZID=America/Los_Angeles:20260901T170000",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        "EXDATE;TZID=America/Los_Angeles:20260908T160000",
        "SUMMARY:Weekly club",
    )
    assert len(build(calendar(recurring), date(2026, 9, 1)).events) == 1
    assert len(build(calendar(recurring), date(2026, 9, 8)).events) == 0
    assert len(build(calendar(recurring), date(2026, 9, 15)).events) == 1


def test_transparent_and_missing_timed_end_do_not_block() -> None:
    snapshot = build(
        calendar(
            event(
                "UID:transparent",
                "DTSTART;TZID=America/Los_Angeles:20260901T150000",
                "DTEND;TZID=America/Los_Angeles:20260901T160000",
                "TRANSP:TRANSPARENT",
                "SUMMARY:FYI",
            ),
            event(
                "UID:no-end",
                "DTSTART;TZID=America/Los_Angeles:20260901T170000",
                "SUMMARY:Reminder",
            ),
        )
    )
    assert all(not item.busy for item in snapshot.events)
    assert snapshot.free_windows[-1].start.strftime("%H:%M") == "14:30"


def test_opaque_all_day_blocks_day_but_birthday_is_informational() -> None:
    opaque = build(calendar(event("UID:off", "DTSTART;VALUE=DATE:20260901", "SUMMARY:Conference")))
    assert opaque.free_windows == []
    birthday = build(calendar(event("UID:bday", "DTSTART;VALUE=DATE:20260901", "SUMMARY:Arda Birthday")))
    assert birthday.events[0].busy is False
    assert birthday.free_windows[-1].start.strftime("%H:%M") == "14:30"


def test_no_school_suppresses_school_hours_without_blocking_day() -> None:
    snapshot = build(calendar(event("UID:break", "DTSTART;VALUE=DATE:20260901", "SUMMARY:No School")))
    assert len(snapshot.free_windows) == 1
    assert snapshot.free_windows[0].start.strftime("%H:%M") == "06:00"
    assert snapshot.free_windows[0].end.strftime("%H:%M") == "22:30"


def test_canvas_event_merges_but_reminders_are_not_an_input_type() -> None:
    canvas_event = CanvasEvent(
        title="Canvas seminar",
        start="2026-09-01T18:00:00-07:00",
        end="2026-09-01T19:00:00-07:00",
        all_day=False,
        busy=True,
    )
    snapshot = build(calendar(), canvas_events=[canvas_event])
    assert snapshot.events[0].source == "canvas"
    assert any(window.end.strftime("%H:%M") == "18:00" for window in snapshot.free_windows)

