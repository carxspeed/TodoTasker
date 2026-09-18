from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from daily_brief.models import ClassificationOutput, ClassifiedItem, FocusPlan, GuidanceItem, GuidanceResult
from daily_brief.render import render_brief


def item(key, source="canvas", next_step=""):
    return ClassifiedItem(
        key=key,
        source=source,
        name=f"Canonical {key}",
        tier="must",
        effort="L",
        effort_hours=3,
        effort_source="notion" if source == "notion" else "points",
        next_step=next_step,
    )


def classification(items):
    return ClassificationOutput(
        target_date=date(2026, 9, 2),
        as_of=datetime(2026, 9, 2, 6, 30, tzinfo=ZoneInfo("America/Los_Angeles")),
        must=items,
        selected_effort_hours=sum(value.effort_hours for value in items),
        available_hours=3.5,
    )


def test_python_renders_every_canonical_title_and_effort() -> None:
    items = [item("assignment:1"), item("notion:2", source="notion")]
    text = render_brief(classification(items))
    assert "Canonical assignment:1 (~3h)" in text
    assert "Canonical notion:2 (~3h)" in text
    assert "Next step unknown — spend 10 minutes scoping it." in text


def test_guidance_is_inserted_only_under_matching_key() -> None:
    items = [item("assignment:1"), item("assignment:2")]
    guidance = GuidanceResult(
        overview="Overview",
        task_guidance=[GuidanceItem(key="assignment:2", guidance="Specific second step.")],
    )
    text = render_brief(classification(items), guidance=guidance)
    assert text.index("Specific second step.") > text.index("Canonical assignment:2")
    assert "Open the assignment" in text


def test_updated_header_is_explicit() -> None:
    assert "Updated this morning" in render_brief(classification([]), updated=True)


def test_capacity_hours_are_rounded_for_humans() -> None:
    current = classification([item("assignment:1")]).model_copy(
        update={"available_hours": 3.42101}
    )

    text = render_brief(current)

    assert "Selected ~3h of ~3.4h available" in text
    assert "3.42101" not in text


def test_canvas_tasks_show_course_and_local_deadline() -> None:
    canvas = item("assignment:1").model_copy(
        update={
            "course": "AP Physics",
            "due_at": datetime(2026, 9, 5, 4, tzinfo=timezone.utc),
        }
    )
    text = render_brief(classification([canvas]))
    assert "Course: AP Physics" in text
    assert "due Fri Sep 4, 21:00 PDT" in text


def test_focus_hides_the_large_backlog_but_preserves_the_primary_reason() -> None:
    items = [item(f"assignment:{number}") for number in range(1, 5)]
    guidance = GuidanceResult(
        focus=FocusPlan(
            primary_key="assignment:2",
            reason="It is the nearest assessment.",
            today_keys=["assignment:2", "assignment:3"],
        )
    )
    text = render_brief(classification(items), guidance=guidance)
    assert "Today's focus" in text
    assert "Why: It is the nearest assessment." in text
    assert "Canonical assignment:2" in text
    assert "Canonical assignment:3" in text
    assert "Canonical assignment:1" not in text
    assert "Defer without guilt: 2 other selected task(s)" in text
