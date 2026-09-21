"""Structured, phone-first notification planning."""

from __future__ import annotations

import re

from .models import (
    CanvasEnvelope,
    ClassificationOutput,
    DailyNotification,
    GuidanceResult,
    NotificationReminder,
    NotificationTask,
)
from .render import deterministic_guidance, select_focus_items


ASSESSMENT_RE = re.compile(
    r"\b(?:quiz(?:zes)?|test|exam(?:ination)?|assessment|mcq|multiple[ -]choice|"
    r"frq|free[ -]response|timed[ -](?:write|writing|essay)|midterm|final)\b",
    re.IGNORECASE,
)
NOTICE_MARKERS = (
    "couldn't refresh",
    "plan may be incomplete",
    "plan may be missing",
    "may be outdated",
    "default time estimate",
)


def _bounded(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _notification_task(item, guidance_by_key: dict[str, str], local_timezone) -> NotificationTask:
    due_at = item.due_at
    if due_at is not None and local_timezone is not None:
        due_at = due_at.astimezone(local_timezone)
    return NotificationTask(
        key=item.key,
        name=item.name,
        course=item.course,
        next_step=_bounded(
            guidance_by_key.get(item.key) or deterministic_guidance(item), 160
        ),
        due_at=due_at,
        effort_hours=item.effort_hours,
        kind=item.kind,
        url=item.url,
    )


def _reminders(
    canvas: CanvasEnvelope | None, classification: ClassificationOutput
) -> list[NotificationReminder]:
    if canvas is None:
        return []
    values: list[NotificationReminder] = []
    seen: set[tuple[str, str]] = set()
    for event in sorted(
        canvas.planner_events,
        key=lambda value: (value.date, value.course, value.title),
    ):
        if event.date != classification.target_date or not ASSESSMENT_RE.search(event.title):
            continue
        key = (event.course.casefold(), event.title.casefold())
        if key in seen:
            continue
        seen.add(key)
        values.append(
            NotificationReminder(
                title=event.title,
                course=event.course,
                date=event.date,
                text=_bounded(event.text, 160),
                url=event.url,
            )
        )
    for reminder in canvas.canvas_reminders:
        if reminder.date != classification.target_date:
            continue
        key = ("", reminder.title.casefold())
        if key in seen:
            continue
        seen.add(key)
        values.append(
            NotificationReminder(
                title=reminder.title,
                date=reminder.date,
                text=_bounded(reminder.text, 160),
            )
        )
    return values[:2]


def _notice(warnings: list[str] | None) -> str:
    for warning in warnings or []:
        lowered = warning.casefold()
        if any(marker in lowered for marker in NOTICE_MARKERS):
            return _bounded(warning, 180)
    return ""


def build_daily_notification(
    classification: ClassificationOutput,
    *,
    guidance: GuidanceResult | None = None,
    canvas: CanvasEnvelope | None = None,
    warnings: list[str] | None = None,
) -> DailyNotification:
    """Build a small action view without copying the full diagnostic brief."""

    selected = [*classification.must, *classification.smart, *classification.may]
    focus, reason = select_focus_items(classification, guidance)
    guidance_by_key = (
        {item.key: item.guidance for item in guidance.task_guidance}
        if guidance
        else {}
    )
    tasks = [
        _notification_task(item, guidance_by_key, classification.as_of.tzinfo)
        for item in focus[:3]
    ]
    return DailyNotification(
        target_date=classification.target_date,
        primary=tasks[0] if tasks else None,
        followups=tasks[1:],
        reminders=_reminders(canvas, classification),
        backlog_count=max(0, len(selected) - len(tasks)),
        verify_count=len(classification.verify),
        notice=_notice(warnings),
        focus_reason=_bounded(reason, 240),
    )


def apply_task_urls(
    notification: DailyNotification,
    task_urls: dict[str, str],
    *,
    aliases: dict[str, str] | None = None,
) -> DailyNotification:
    """Prefer exact Notion-row links while retaining source links as a fallback."""

    aliases = aliases or {}

    def linked(task: NotificationTask | None) -> NotificationTask | None:
        if task is None:
            return None
        source_id = aliases.get(task.key, task.key)
        url = task_urls.get(source_id, "").strip()
        return task.model_copy(update={"url": url}) if url else task

    return notification.model_copy(
        update={
            "primary": linked(notification.primary),
            "followups": [linked(task) for task in notification.followups],
        }
    )
