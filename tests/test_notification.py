from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from daily_brief.canvas import load_fixture
from daily_brief.models import (
    ClassificationOutput,
    ClassifiedItem,
    FocusPlan,
    GuidanceItem,
    GuidanceResult,
    PlannerEvent,
)
from daily_brief.notification import apply_task_urls, build_daily_notification


TARGET = date(2026, 9, 20)


def task(key: str, name: str) -> ClassifiedItem:
    return ClassifiedItem(
        key=key,
        source="canvas",
        name=name,
        tier="must",
        effort="M",
        effort_hours=1.5,
        effort_source="points",
        course="Calculus",
    )


def classification(items: list[ClassifiedItem]) -> ClassificationOutput:
    return ClassificationOutput(
        target_date=TARGET,
        as_of=datetime(2026, 9, 20, 6, 30, tzinfo=ZoneInfo("America/Los_Angeles")),
        must=items,
    )


def test_notification_contains_only_the_focus_and_backlog_count() -> None:
    items = [task(f"assignment:{index}", f"Task {index}") for index in range(1, 7)]
    guidance = GuidanceResult(
        task_guidance=[
            GuidanceItem(key="assignment:2", guidance="Complete the first five problems.")
        ],
        focus=FocusPlan(
            primary_key="assignment:2",
            today_keys=["assignment:2", "assignment:3", "assignment:4"],
            reason="It is due first.",
        ),
    )

    result = build_daily_notification(classification(items), guidance=guidance)

    assert result.primary and result.primary.key == "assignment:2"
    assert result.primary.next_step == "Complete the first five problems."
    assert [item.key for item in result.followups] == ["assignment:3", "assignment:4"]
    assert result.backlog_count == 3


def test_same_day_assessment_is_a_reminder_not_the_primary_task() -> None:
    current = classification([task("assignment:homework", "Calculus homework")])
    canvas = load_fixture("fixtures/sample_todo.json").model_copy(
        update={
            "planner_events": [
                PlannerEvent(
                    course="Calculus",
                    title="Quiz 3",
                    date=TARGET,
                    text="Sections 12.1–12.3",
                    url="https://canvas.test/quiz-3",
                )
            ]
        }
    )

    result = build_daily_notification(current, canvas=canvas)

    assert result.primary and result.primary.name == "Calculus homework"
    assert [reminder.title for reminder in result.reminders] == ["Quiz 3"]


def test_internal_diagnostics_do_not_become_notification_notices() -> None:
    result = build_daily_notification(
        classification([]),
        warnings=[
            "Partial Canvas response retained compatible cached components from a timestamp",
            "Canvas couldn't refresh; today's plan may be missing recent changes.",
        ],
    )

    assert result.notice == "Canvas couldn't refresh; today's plan may be missing recent changes."
    assert "compatible cached" not in result.model_dump_json()


def test_exact_notion_urls_replace_source_links_and_follow_aliases() -> None:
    current = build_daily_notification(classification([task("planner:quiz", "Study Quiz")]))

    linked = apply_task_urls(
        current,
        {"assignment:quiz": "https://notion.test/quiz-row"},
        aliases={"planner:quiz": "assignment:quiz"},
    )

    assert linked.primary and linked.primary.url == "https://notion.test/quiz-row"


def test_notification_converts_utc_deadline_to_the_users_local_date() -> None:
    item = task("assignment:late", "Late assignment").model_copy(
        update={"due_at": datetime(2026, 9, 9, 4, 0, tzinfo=timezone.utc)}
    )

    result = build_daily_notification(classification([item]))

    assert result.primary is not None
    assert result.primary.due_at == datetime(
        2026, 9, 8, 21, 0, tzinfo=ZoneInfo("America/Los_Angeles")
    )
