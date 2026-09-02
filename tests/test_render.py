from datetime import date, datetime
from zoneinfo import ZoneInfo

from daily_brief.models import ClassificationOutput, ClassifiedItem, GuidanceItem, GuidanceResult
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

