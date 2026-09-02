from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from daily_brief.models import CalendarEvent, DailyBriefState, FreeWindow


def test_state_schema_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        DailyBriefState.model_validate({"schema_version": 2, "surprise": True})


def test_nested_dates_and_datetimes_round_trip() -> None:
    state = DailyBriefState.model_validate(
        {
            "schema_version": 2,
            "capacity": {"value": "low", "set_for": "2026-09-02"},
            "source_last_ok": {"canvas": "2026-09-01T20:00:00Z"},
        }
    )
    restored = DailyBriefState.model_validate_json(state.model_dump_json())
    assert restored.capacity is not None
    assert restored.capacity.set_for == date(2026, 9, 2)
    assert restored.source_last_ok.canvas == datetime(2026, 9, 1, 20, tzinfo=timezone.utc)


def test_calendar_contract_rejects_reverse_ranges() -> None:
    with pytest.raises(ValidationError):
        FreeWindow(
            start="2026-09-01T12:00:00Z",
            end="2026-09-01T11:00:00Z",
        )
    with pytest.raises(ValidationError):
        CalendarEvent(
            title="bad",
            start="2026-09-01T12:00:00Z",
            end="2026-09-01T11:00:00Z",
            source="ical",
            all_day=False,
            busy=True,
        )

