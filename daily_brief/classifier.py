"""Deterministic Must/Smart/May classification and capacity selection."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

from .models import (
    CalendarSnapshot,
    CanvasAssignment,
    Capacity,
    ClassificationOutput,
    ClassifiedItem,
    FreeWindow,
    NotionWorkItem,
    Promotion,
    SeenAssignment,
)


EFFORT_HOURS = {"S": 0.5, "M": 1.5, "L": 3.0}
TASK_CAPS = {"low": 3, "normal": 6, "high": 8}
HOUR_CAPS = {"low": 1.5, "normal": 3.5, "high": 6.0}
CADENCE_DAYS = {"Daily": 1.0, "2x/week": 3.5, "Weekly": 7.0, "Biweekly": 14.0}
TIER_SCORE = {"may": 0, "smart": 1, "must": 2}


def _canvas_effort(points: float | None, override: str | None) -> tuple[str, str]:
    if override in EFFORT_HOURS:
        return override, "override"
    if points is None:
        return "M", "points"
    if points <= 5:
        return "S", "points"
    if points <= 29:
        return "M", "points"
    return "L", "points"


def _deadline_delta(due_at: datetime | None, as_of: datetime) -> timedelta | None:
    if due_at is None:
        return None
    return due_at.astimezone(timezone.utc) - as_of.astimezone(timezone.utc)


def _deadline_tier(delta: timedelta | None, *, explicit_large: bool) -> str:
    if delta is None:
        return "may"
    if delta <= timedelta(hours=24):
        return "must"
    if explicit_large and delta <= timedelta(hours=48):
        return "must"
    return "smart"


def _classify_canvas(
    item: CanvasAssignment, as_of: datetime, effort_overrides: dict[str, str]
) -> tuple[ClassifiedItem, bool]:
    effort, effort_source = _canvas_effort(item.points, effort_overrides.get(item.key))
    delta = _deadline_delta(item.due_at, as_of)
    urgent_verify = (
        item.submission_status == "unknown"
        and delta is not None
        and delta <= timedelta(hours=24)
    )
    if item.submission_status == "unknown":
        tier = "smart"
    else:
        tier = _deadline_tier(delta, explicit_large=effort_source == "override" and effort == "L")
    return (
        ClassifiedItem(
            key=item.key,
            source="canvas",
            name=item.name,
            tier=tier,
            effort=effort,
            effort_hours=EFFORT_HOURS[effort],
            effort_source=effort_source,
            kind=item.kind,
            due_at=item.due_at,
            course=item.course,
            description=item.description,
            url=item.url,
            points=item.points,
            submission_status=item.submission_status,
            needs_confirmation=item.needs_confirmation,
        ),
        urgent_verify,
    )


def _notion_deadline(day: date | None, timezone_name: str) -> datetime | None:
    if day is None:
        return None
    return datetime.combine(day, time(23, 59, 59), ZoneInfo(timezone_name))


def _classify_notion(
    item: NotionWorkItem, target_date: date, as_of: datetime, timezone_name: str
) -> ClassifiedItem:
    effort = item.effort or "M"
    cadence = item.cadence.strip() if item.cadence else ""
    real_cadence = cadence if cadence in CADENCE_DAYS else None
    if real_cadence is None:
        overdue_periods = 0.0
        cadence_tier = "may"
    elif item.last_touched is None:
        overdue_periods = 2.0
        cadence_tier = "must"
    else:
        overdue_periods = max(0.0, (target_date - item.last_touched).days / CADENCE_DAYS[real_cadence])
        cadence_tier = "must" if overdue_periods >= 2 else "smart" if overdue_periods >= 1 else "may"
    due_at = _notion_deadline(item.deadline, timezone_name)
    deadline_tier = _deadline_tier(
        _deadline_delta(due_at, as_of), explicit_large=item.effort == "L"
    )
    if item.deadline is None:
        deadline_tier = "may"
    tier = max((cadence_tier, deadline_tier), key=TIER_SCORE.get)
    return ClassifiedItem(
        key=item.key,
        source="notion",
        name=item.name,
        tier=tier,
        effort=effort,
        effort_hours=EFFORT_HOURS[effort],
        effort_source="notion",
        kind=item.type or "",
        due_at=due_at,
        deadline=item.deadline,
        description="",
        next_step=item.next_step,
        url=item.url,
        overdue_periods=overdue_periods,
    )


def _due_sort(item: ClassifiedItem) -> datetime:
    return item.due_at.astimezone(timezone.utc) if item.due_at else datetime.max.replace(tzinfo=timezone.utc)


def _must_sort(item: ClassifiedItem):
    return (_due_sort(item), -item.overdue_periods, item.key)


def _optional_sort(item: ClassifiedItem):
    return (
        0 if item.tier == "smart" else 1,
        _due_sort(item),
        -item.overdue_periods,
        -(item.points or 0),
        item.key,
    )


def _promotion_candidate(
    item: ClassifiedItem,
    *,
    as_of: datetime,
    target_date: date,
    timezone_name: str,
    seen_assignments: dict[str, SeenAssignment],
) -> bool:
    if item.source != "canvas" or item.tier == "must" or item.submission_status != "unsubmitted":
        return False
    if item.due_at is None:
        return False
    timezone_local = ZoneInfo(timezone_name)
    due = item.due_at.astimezone(timezone_local)
    last_allowed = datetime.combine(target_date + timedelta(days=7), time(23, 59, 59), timezone_local)
    if not (as_of.astimezone(timezone_local) <= due <= last_allowed):
        return False
    seen = seen_assignments.get(item.key)
    if seen is None:
        return False
    first_local = seen.first_seen.astimezone(timezone_local).date()
    return (target_date - first_local).days >= 3


def _apply_promotion(
    items: list[ClassifiedItem],
    *,
    existing: Promotion | None,
    target_date: date,
    as_of: datetime,
    timezone_name: str,
    seen_assignments: dict[str, SeenAssignment],
) -> tuple[list[ClassifiedItem], Promotion | None]:
    eligible = {
        item.key: item
        for item in items
        if _promotion_candidate(
            item,
            as_of=as_of,
            target_date=target_date,
            timezone_name=timezone_name,
            seen_assignments=seen_assignments,
        )
    }
    selected_key: str | None = None
    if existing is not None and existing.date == target_date:
        if existing.key in eligible:
            selected_key = existing.key
            promotion = existing
        else:
            promotion = None
    elif eligible:
        selected_key = min(
            eligible,
            key=lambda key: (
                seen_assignments[key].first_seen,
                key,
            ),
        )
        promotion = Promotion(
            date=target_date,
            key=selected_key,
            reason="Seen for at least three local days and due within the next seven days",
        )
    else:
        promotion = None
    if selected_key is None:
        return items, promotion
    promoted_items = []
    for item in items:
        if item.key == selected_key:
            new_tier: Literal["must", "smart"] = "must" if item.tier == "smart" else "smart"
            item = item.model_copy(update={"tier": new_tier, "promoted": True})
        promoted_items.append(item)
    return promoted_items, promotion


def _available_hours(
    capacity_value: str,
    *,
    target_date: date,
    as_of: datetime,
    free_windows: list[FreeWindow] | None,
    calendar_target_date: date | None,
) -> tuple[float, list[str]]:
    nominal = HOUR_CAPS[capacity_value]
    if free_windows is None or calendar_target_date != target_date:
        return nominal, ["capacity_based_on_nominal_hours"]
    total = 0.0
    for window in free_windows:
        if window.end <= as_of:
            continue
        start = max(window.start, as_of)
        if window.end > start:
            total += (window.end - start).total_seconds() / 3600
    return min(nominal, total), []


def classify(
    canvas_items: Iterable[CanvasAssignment],
    notion_items: Iterable[NotionWorkItem],
    *,
    target_date: date,
    as_of: datetime,
    timezone_name: str,
    effort_overrides: dict[str, str] | None = None,
    seen_assignments: dict[str, SeenAssignment] | None = None,
    capacity: Capacity | None = None,
    existing_promotion: Promotion | None = None,
    free_windows: list[FreeWindow] | None = None,
    calendar_target_date: date | None = None,
) -> ClassificationOutput:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    overrides = effort_overrides or {}
    seen = seen_assignments or {}
    classified: list[ClassifiedItem] = []
    verify: list[CanvasAssignment] = []
    verify_keys: set[str] = set()
    for raw in canvas_items:
        item, urgent_verify = _classify_canvas(raw, as_of, overrides)
        classified.append(item)
        if urgent_verify:
            verify.append(raw)
            verify_keys.add(raw.key)
    classified.extend(
        _classify_notion(raw, target_date, as_of, timezone_name) for raw in notion_items
    )
    classified, promotion = _apply_promotion(
        classified,
        existing=existing_promotion,
        target_date=target_date,
        as_of=as_of,
        timezone_name=timezone_name,
        seen_assignments=seen,
    )
    capacity_value = capacity.value if capacity is not None and capacity.set_for == target_date else "normal"
    task_cap = TASK_CAPS[capacity_value]
    available_hours, warnings = _available_hours(
        capacity_value,
        target_date=target_date,
        as_of=as_of,
        free_windows=free_windows,
        calendar_target_date=calendar_target_date,
    )

    candidates = [item for item in classified if item.key not in verify_keys]
    must = sorted((item for item in candidates if item.tier == "must"), key=_must_sort)
    promoted_item = next((item for item in candidates if item.promoted), None)
    required: list[ClassifiedItem] = []
    required_keys: set[str] = set()
    for item in [*must, *([promoted_item] if promoted_item else [])]:
        if item.key not in required_keys:
            required.append(item)
            required_keys.add(item.key)

    selected = list(required)
    selected_keys = set(required_keys)
    remaining_hours = available_hours
    unscheduled_required_count = 0
    required_effort = 0.0
    for item in required:
        required_effort += item.effort_hours
        covered = min(item.effort_hours, remaining_hours)
        remaining_hours -= covered
        if covered < item.effort_hours:
            unscheduled_required_count += 1

    momentum_candidates = sorted(
        (
            item
            for item in candidates
            if item.key not in selected_keys and item.source == "notion" and item.overdue_periods > 0
        ),
        key=lambda item: (-item.overdue_periods, _due_sort(item), item.key),
    )
    momentum_deferred = None
    momentum_considered_key: str | None = None
    if momentum_candidates:
        momentum = momentum_candidates[0].model_copy(update={"momentum": True})
        momentum_considered_key = momentum.key
        if len(selected) < task_cap and momentum.effort_hours <= remaining_hours:
            selected.append(momentum)
            selected_keys.add(momentum.key)
            remaining_hours -= momentum.effort_hours
        else:
            momentum_deferred = momentum

    optional = sorted(
        (
            item
            for item in candidates
            if item.key not in selected_keys and item.key != momentum_considered_key
        ),
        key=_optional_sort,
    )
    for item in optional:
        if len(selected) < task_cap and item.effort_hours <= remaining_hours:
            selected.append(item)
            selected_keys.add(item.key)
            remaining_hours -= item.effort_hours

    selected_effort = sum(item.effort_hours for item in selected)
    overloaded = (
        len(required) > task_cap
        or required_effort > available_hours
        or unscheduled_required_count > 0
    )
    selected_must = sorted((item for item in selected if item.tier == "must"), key=_must_sort)
    selected_smart = sorted((item for item in selected if item.tier == "smart"), key=_optional_sort)
    selected_may = sorted((item for item in selected if item.tier == "may"), key=_optional_sort)
    return ClassificationOutput(
        target_date=target_date,
        as_of=as_of,
        must=selected_must,
        smart=selected_smart,
        may=selected_may,
        verify=sorted(verify, key=lambda item: (item.due_at is None, item.due_at, item.key)),
        promoted=promotion,
        momentum_deferred=momentum_deferred,
        overloaded=overloaded,
        capacity_exceeded_by_tasks=max(0, len(selected) - task_cap),
        capacity_exceeded_by_hours=max(0.0, selected_effort - available_hours),
        unscheduled_required_count=unscheduled_required_count,
        selected_effort_hours=selected_effort,
        available_hours=available_hours,
        dropped_count=len([item for item in candidates if item.key not in selected_keys]),
        warnings=warnings,
    )
