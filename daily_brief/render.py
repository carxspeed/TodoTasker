"""Canonical Python-owned brief rendering."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo

from .models import CalendarSnapshot, CanvasEnvelope, ClassificationOutput, ClassifiedItem, GuidanceResult


def deterministic_guidance(item: ClassifiedItem) -> str:
    if item.source == "notion" and (not item.next_step.strip() or "unknown" in item.next_step.casefold()):
        return "Next step unknown — spend 10 minutes scoping it."
    if item.source == "notion":
        return f"Start with this next step: {item.next_step.strip()}"
    if item.user_notes.strip():
        return f"Continue from your Notion note: {item.user_notes.strip()}"
    return "Open the assignment, review the requirements, and complete the first concrete part."


def _format_due(value: datetime | None, display_timezone: tzinfo) -> str:
    if value is None:
        return "unknown deadline"
    local = value.astimezone(display_timezone)
    zone = local.tzname() or "local"
    return f"{local.strftime('%a %b')} {local.day}, {local.strftime('%H:%M')} {zone}"


def _render_task(
    item: ClassifiedItem, guidance: dict[str, str], display_timezone: tzinfo
) -> list[str]:
    due = ""
    if item.due_at is not None:
        due = f" — due {_format_due(item.due_at, display_timezone)}"
    lines = [f"- {item.name} (~{item.effort_hours:g}h){due}"]
    if item.source == "canvas" and item.course:
        lines.append(f"  Course: {item.course}")
    lines.append(f"  {guidance.get(item.key) or deterministic_guidance(item)}")
    return lines


def _focus_items(
    classification: ClassificationOutput, guidance: GuidanceResult | None
) -> tuple[list[ClassifiedItem], str]:
    """Keep the delivered brief humane while the full source tables remain intact."""
    selected = [*classification.must, *classification.smart, *classification.may]
    by_key = {item.key: item for item in selected}
    if guidance and guidance.focus:
        keys = guidance.focus.today_keys
        if (
            keys
            and keys[0] == guidance.focus.primary_key
            and len(keys) == len(set(keys))
            and all(key in by_key for key in keys)
        ):
            return [by_key[key] for key in keys], guidance.focus.reason
    if not selected:
        return [], ""
    primary = selected[0]
    return selected[:3], "Start with the nearest required task, then continue only if time remains."


def render_brief(
    classification: ClassificationOutput,
    *,
    guidance: GuidanceResult | None = None,
    canvas: CanvasEnvelope | None = None,
    calendar: CalendarSnapshot | None = None,
    warnings: list[str] | None = None,
    updated: bool = False,
) -> str:
    display_timezone = classification.as_of.tzinfo
    assert display_timezone is not None
    lines = [
        f"Daily Brief — {classification.target_date.isoformat()}",
        "Updated this morning" if updated else "Prepared for your day",
    ]
    all_warnings = [*(warnings or []), *classification.warnings]
    if all_warnings:
        lines.extend(["", "Warnings"])
        lines.extend(f"- ⚠️ {warning}" for warning in all_warnings)
    if classification.verify:
        lines.extend(["", "Verify urgently"])
        for item in classification.verify:
            due = _format_due(item.due_at, display_timezone)
            lines.append(f"- {item.name} ({item.course}; {due}) {item.url}".rstrip())
    guidance_by_key = {
        item.key: item.guidance for item in guidance.task_guidance
    } if guidance else {}
    if guidance and guidance.overview:
        lines.extend(["", guidance.overview])
    focus_items, focus_reason = _focus_items(classification, guidance)
    if focus_items:
        lines.extend(["", "Today's focus"])
        primary = focus_items[0]
        lines.append(f"Start here: {primary.name}")
        lines.append(f"Why: {focus_reason}")
        lines.extend(_render_task(primary, guidance_by_key, display_timezone))
        if len(focus_items) > 1:
            lines.append("If you finish")
            for item in focus_items[1:]:
                lines.extend(_render_task(item, guidance_by_key, display_timezone))
        deferred = max(0, len(classification.must) + len(classification.smart) + len(classification.may) - len(focus_items))
        if deferred:
            lines.append(f"Defer without guilt: {deferred} other selected task(s) remain in your Notion backlog.")
    if classification.momentum_deferred:
        item = classification.momentum_deferred
        lines.extend(
            [
                "",
                "Momentum deferred",
                f"- {item.name} (~{item.effort_hours:g}h) — visible, but not scheduled within today's capacity.",
            ]
        )
    lines.extend(
        [
            "",
            "Capacity",
            f"- Selected ~{classification.selected_effort_hours:g}h of ~{classification.available_hours:g}h available.",
        ]
    )
    if classification.overloaded:
        lines.append(
            f"- OVERLOADED: {classification.unscheduled_required_count} required item(s) are not fully covered."
        )
    if calendar is not None:
        lines.extend(["", "Free windows"])
        if calendar.free_windows:
            lines.extend(
                f"- {window.start.strftime('%H:%M')}–{window.end.strftime('%H:%M')}"
                for window in calendar.free_windows
            )
        else:
            lines.append("- No open window in the configured day.")
    if canvas is not None:
        end_date = classification.target_date + timedelta(days=7)
        deduped = {}
        for event in canvas.planner_events:
            normalized_title = re.sub(r"\s+", " ", event.title).strip().casefold()
            key = (event.course, event.date, normalized_title, event.url)
            if classification.target_date <= event.date <= end_date:
                deduped[key] = event
        if deduped:
            lines.extend(["", "Coming up"])
            for event in sorted(deduped.values(), key=lambda value: (value.date, value.course, value.title)):
                lines.append(f"- {event.date.isoformat()} — {event.course}: {event.title}")
        reminders = [
            reminder
            for reminder in canvas.canvas_reminders
            if classification.target_date <= reminder.date <= end_date
        ]
        if reminders:
            lines.extend(["", "Reminders"])
            lines.extend(f"- {item.date.isoformat()} — {item.title}: {item.text}" for item in reminders)
        if canvas.announcements:
            lines.extend(["", "Announcements"])
            lines.extend(
                f"- {item.course}: {item.title} — {item.text}" for item in canvas.announcements
            )
    return "\n".join(lines).rstrip() + "\n"
