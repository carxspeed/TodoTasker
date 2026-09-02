from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from daily_brief.classifier import classify
from daily_brief.models import (
    CanvasAssignment,
    Capacity,
    FreeWindow,
    NotionWorkItem,
    Promotion,
    SeenAssignment,
)


TZ = ZoneInfo("America/Los_Angeles")
TARGET = date(2026, 9, 2)
AS_OF = datetime(2026, 9, 2, 6, 30, tzinfo=TZ)


def canvas_item(
    key: str,
    *,
    due_hours: float | None,
    points: float | None = 10,
    status: str = "unsubmitted",
) -> CanvasAssignment:
    due = AS_OF + timedelta(hours=due_hours) if due_hours is not None else None
    number = int("".join(filter(str.isdigit, key)) or 1)
    return CanvasAssignment(
        key=key,
        source_key=key,
        object_id=number,
        assignment_id=number,
        course_id=1,
        name=f"Task {key}",
        course="Physics",
        kind="assignment",
        due_at=due,
        points=points,
        url="https://canvas.test/task",
        submission_status=status,
        needs_confirmation=status == "unknown",
    )


def notion_item(
    key: str,
    *,
    cadence: str | None = None,
    last_touched: date | None = None,
    deadline: date | None = None,
    effort: str | None = None,
) -> NotionWorkItem:
    return NotionWorkItem(
        key=key,
        page_id=key.removeprefix("notion:"),
        url="https://notion.test/page",
        name=f"Project {key}",
        cadence=cadence,
        last_touched=last_touched,
        deadline=deadline,
        effort=effort,
    )


def run(canvas=(), notion=(), **kwargs):
    return classify(
        canvas,
        notion,
        target_date=TARGET,
        as_of=AS_OF,
        timezone_name="America/Los_Angeles",
        **kwargs,
    )


def selected_tier(result, key):
    for tier in ("must", "smart", "may"):
        if any(item.key == key for item in getattr(result, tier)):
            return tier
    return None


def test_canvas_24_hour_boundary_is_must_and_missing_due_is_may() -> None:
    result = run([canvas_item("assignment:1", due_hours=24), canvas_item("assignment:2", due_hours=None)])
    assert selected_tier(result, "assignment:1") == "must"
    assert selected_tier(result, "assignment:2") == "may"


def test_only_explicit_large_override_gets_48_hour_must_rule() -> None:
    result = run(
        [canvas_item("assignment:1", due_hours=47, points=50), canvas_item("assignment:2", due_hours=47)],
        effort_overrides={"assignment:2": "L"},
        capacity=Capacity(value="high", set_for=TARGET),
    )
    assert selected_tier(result, "assignment:1") == "smart"
    assert selected_tier(result, "assignment:2") == "must"
    assert result.must[0].effort_source == "override"


def test_urgent_unknown_submission_is_verify_and_never_must() -> None:
    result = run([canvas_item("assignment:1", due_hours=3, status="unknown")])
    assert [item.key for item in result.verify] == ["assignment:1"]
    assert not result.must
    assert not result.smart
    assert result.dropped_count == 0


def test_notion_null_cadence_without_deadline_is_may() -> None:
    result = run(notion=[notion_item("notion:a")])
    assert selected_tier(result, "notion:a") == "may"
    assert result.may[0].effort == "M"


def test_notion_cadence_and_date_only_deadline_rules() -> None:
    result = run(
        notion=[
            notion_item("notion:a", cadence="Weekly", last_touched=TARGET - timedelta(days=7)),
            notion_item("notion:b", cadence="Daily", last_touched=None),
            notion_item("notion:c", deadline=TARGET, effort="S"),
            notion_item("notion:d", deadline=TARGET + timedelta(days=1), effort="L"),
        ]
    )
    assert result.momentum_deferred is not None
    assert result.momentum_deferred.key == "notion:a"
    assert result.momentum_deferred.tier == "smart"
    assert selected_tier(result, "notion:b") == "must"
    assert selected_tier(result, "notion:c") == "must"
    assert selected_tier(result, "notion:d") == "must"


def test_every_semantic_must_survives_low_capacity() -> None:
    items = [canvas_item(f"assignment:{index}", due_hours=index, points=1) for index in range(1, 6)]
    result = run(items, capacity=Capacity(value="low", set_for=TARGET))
    assert len(result.must) == 5
    assert result.overloaded is True
    assert result.capacity_exceeded_by_tasks == 2
    assert result.unscheduled_required_count == 2


def test_scalar_hours_use_disconnected_windows_and_clip_past_time() -> None:
    windows = [
        FreeWindow(start=datetime(2026, 9, 2, 5, tzinfo=TZ), end=datetime(2026, 9, 2, 7, 30, tzinfo=TZ)),
        FreeWindow(start=datetime(2026, 9, 2, 9, tzinfo=TZ), end=datetime(2026, 9, 2, 10, tzinfo=TZ)),
        FreeWindow(start=datetime(2026, 9, 2, 11, tzinfo=TZ), end=datetime(2026, 9, 2, 12, tzinfo=TZ)),
    ]
    result = run(
        [canvas_item("assignment:1", due_hours=2, points=50)],
        capacity=Capacity(value="high", set_for=TARGET),
        free_windows=windows,
        calendar_target_date=TARGET,
    )
    assert result.available_hours == pytest.approx(3.0)
    assert result.unscheduled_required_count == 0


def test_mismatched_calendar_uses_nominal_capacity() -> None:
    result = run(
        [canvas_item("assignment:1", due_hours=100)],
        free_windows=[],
        calendar_target_date=TARGET - timedelta(days=1),
    )
    assert result.available_hours == 3.5
    assert "capacity_based_on_nominal_hours" in result.warnings


def seen(key: str, first_day: date) -> SeenAssignment:
    return SeenAssignment(
        first_seen=datetime.combine(first_day, datetime.min.time(), TZ),
        last_seen=AS_OF,
        course_id=1,
        name=key,
        kind="assignment",
        assignment_id=1,
        source_key=key,
    )


def test_staleness_promotion_uses_oldest_and_is_reused_same_day() -> None:
    items = [canvas_item("assignment:1", due_hours=72), canvas_item("assignment:2", due_hours=80)]
    seen_items = {
        "assignment:1": seen("assignment:1", TARGET - timedelta(days=4)),
        "assignment:2": seen("assignment:2", TARGET - timedelta(days=5)),
    }
    first = run(items, seen_assignments=seen_items)
    assert first.promoted is not None and first.promoted.key == "assignment:2"
    second = run(items, seen_assignments=seen_items, existing_promotion=first.promoted)
    assert second.promoted == first.promoted
    assert [item.key for item in second.must] == ["assignment:2"]


def test_missing_existing_promotion_does_not_select_replacement() -> None:
    item = canvas_item("assignment:2", due_hours=80)
    result = run(
        [item],
        seen_assignments={"assignment:2": seen("assignment:2", TARGET - timedelta(days=5))},
        existing_promotion=Promotion(date=TARGET, key="assignment:gone", reason="old"),
    )
    assert result.promoted is None
    assert selected_tier(result, "assignment:2") == "smart"


def test_unknown_and_existing_must_are_not_promotion_candidates() -> None:
    items = [
        canvas_item("assignment:1", due_hours=10),
        canvas_item("assignment:2", due_hours=72, status="unknown"),
    ]
    result = run(
        items,
        seen_assignments={key: seen(key, TARGET - timedelta(days=5)) for key in ("assignment:1", "assignment:2")},
    )
    assert result.promoted is None


def test_momentum_is_deferred_after_required_work_and_not_readded_optional() -> None:
    required = [canvas_item("assignment:1", due_hours=1, points=20)]
    momentum = notion_item(
        "notion:m", cadence="Weekly", last_touched=TARGET - timedelta(days=10), effort="M"
    )
    result = run(required, [momentum], capacity=Capacity(value="low", set_for=TARGET))
    assert result.momentum_deferred is not None
    assert result.momentum_deferred.key == "notion:m"
    assert selected_tier(result, "notion:m") is None


def test_repeated_inputs_are_byte_equivalent() -> None:
    items = [canvas_item("assignment:2", due_hours=30), canvas_item("assignment:1", due_hours=2)]
    first = run(items).model_dump_json()
    second = run(items).model_dump_json()
    assert first == second


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        classify([], [], target_date=TARGET, as_of=datetime(2026, 9, 2), timezone_name="America/Los_Angeles")
