"""Notion Work database adapter and property builders."""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable
from urllib.parse import unquote

from .http import HttpClient, HttpFailure
from .models import DailyNotification, NotionWorkItem, NotificationTask


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_VIEW_VERSION = "2026-03-11"
TASK_DATABASES = ("Work", "School", "Connections", "Misc")
TYPES = ["Project", "Task", "Recurring"]
CADENCES = ["Daily", "2x/week", "Weekly", "Biweekly", "None"]
STATUSES = ["Active", "Paused", "Done"]
EFFORTS = ["S", "M", "L"]
SCHOOL_STATUSES = ["To do", "Verify", "Done"]
SCHOOL_PRIORITIES = ["MUST", "SMART", "MAY", "Later", "Verify"]
SCHOOL_KINDS = ["assignment", "quiz", "discussion_topic", "sub_assignment"]
DAILY_PLAN_TITLE = "Today's Focus"
FOCUS_DASHBOARD_TITLE = "Today"
DAILY_PLAN_STATUSES = ["To do", "Done"]
MASTER_TASK_TITLE = "Tasks"
MASTER_AREAS = ["Work", "School", "Connections", "Misc"]
MASTER_SOURCE_TYPES = ["Canvas", "Notion"]
MASTER_STATUSES = [
    "To do",
    "In progress",
    "Needs remake",
    "Submitted",
    "Waiting",
    "Verify",
    "Done",
]
DISPLAY_TYPES = [
    "Test / Quiz",
    "Project",
    "Lab",
    "Essay / Writing",
    "Homework",
    "Event",
    "Personal task",
]
NON_ACTIONABLE_STATUSES = {"Submitted", "Waiting", "Done"}
PROTECTED_STALE_STATUSES = {"Needs remake", "Submitted", "Waiting"}
ASSESSMENT_TYPE_RE = re.compile(
    r"\b(?:quiz(?:zes)?|test|exam(?:ination)?|assessment|mcq|multiple[ -]choice|"
    r"frq|free[ -]response|timed[ -](?:write|writing|essay)|midterm|final)\b",
    re.IGNORECASE,
)


class NotionError(RuntimeError):
    pass


@dataclass
class WorkSnapshot:
    items: list[NotionWorkItem]
    warnings: list[str] = field(default_factory=list)


@dataclass
class SchoolSyncResult:
    page_id: str
    url: str
    databases_created: int = 0
    rows_created: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0


@dataclass
class DailyPlanSyncResult:
    page_id: str
    url: str
    database_created: bool = False
    rows_created: int = 0
    rows_updated: int = 0
    rows_archived: int = 0


@dataclass
class MasterTaskSyncResult:
    database_id: str
    page_id: str
    url: str
    database_created: bool = False
    rows_created: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_archived: int = 0
    task_urls: dict[str, str] = field(default_factory=dict)
    view_urls: dict[str, str] = field(default_factory=dict)


@dataclass
class LegacySchoolMigrationResult:
    rows_scanned: int = 0
    unique_source_ids: int = 0
    rows_created: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0


@dataclass
class LegacyLayoutArchiveResult:
    root_databases: tuple[str, ...] = ()
    school_databases: tuple[str, ...] = ()


@dataclass
class MasterFocusSyncResult:
    page_id: str
    url: str
    rows_focused: int = 0
    rows_cleared: int = 0
    missing_source_ids: tuple[str, ...] = ()


@dataclass
class FocusDashboardSyncResult:
    page_id: str
    url: str
    page_created: bool = False
    blocks_written: int = 0


@dataclass(frozen=True)
class MasterMigrationSummary:
    canvas_rows: int
    notion_rows: int
    unique_rows: int
    duplicate_source_ids: tuple[str, ...]


def summarize_master_migration(
    assignments: Iterable[Any],
    notion_items: Iterable[NotionWorkItem],
    *,
    excluded_course_ids: Iterable[int] = (),
) -> MasterMigrationSummary:
    excluded = set(excluded_course_ids)
    canvas_keys = [item.key for item in assignments if item.course_id not in excluded]
    notion_keys = [item.key for item in notion_items]
    all_keys = [*canvas_keys, *notion_keys]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for key in all_keys:
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return MasterMigrationSummary(
        canvas_rows=len(canvas_keys),
        notion_rows=len(notion_keys),
        unique_rows=len(seen),
        duplicate_source_ids=tuple(sorted(duplicates)),
    )


def title_property(value: str) -> dict[str, Any]:
    return {"title": [{"text": {"content": value}}]}


def rich_text_property(value: str | None) -> dict[str, Any]:
    return {"rich_text": [{"text": {"content": value}}]} if value else {"rich_text": []}


def select_property(value: str | None) -> dict[str, Any]:
    return {"select": {"name": value}} if value else {"select": None}


def date_property(value: date | str | None) -> dict[str, Any]:
    if isinstance(value, date):
        value = value.isoformat()
    return {"date": {"start": value}} if value else {"date": None}


def database_schema() -> dict[str, Any]:
    def options(values: Iterable[str]) -> dict[str, Any]:
        return {"select": {"options": [{"name": value} for value in values]}}

    return {
        "Name": {"title": {}},
        "Type": options(TYPES),
        "Cadence": options(CADENCES),
        "Last touched": {"date": {}},
        "Status": options(STATUSES),
        "Next step": {"rich_text": {}},
        "Deadline": {"date": {}},
        "Effort": options(EFFORTS),
    }


def school_database_schema() -> dict[str, Any]:
    def options(values: Iterable[str]) -> dict[str, Any]:
        return {"select": {"options": [{"name": value} for value in values]}}

    return {
        "Name": {"title": {}},
        "Due": {"date": {}},
        "Status": options(SCHOOL_STATUSES),
        "Priority": options(SCHOOL_PRIORITIES),
        "Effort": options(EFFORTS),
        "Kind": options(SCHOOL_KINDS),
        "Next step": {"rich_text": {}},
        "Notes / progress": {"rich_text": {}},
        "Instructions": {"rich_text": {}},
        "Canvas": {"url": {}},
        "Canvas ID": {"rich_text": {}},
        "Sync hash": {"rich_text": {}},
    }


def daily_plan_schema() -> dict[str, Any]:
    def options(values: Iterable[str]) -> dict[str, Any]:
        return {"select": {"options": [{"name": value} for value in values]}}

    return {
        "Task": {"title": {}},
        "Priority": options(SCHOOL_PRIORITIES[:3]),
        "Course / area": {"rich_text": {}},
        "Time (hours)": {"number": {"format": "number"}},
        "Next step": {"rich_text": {}},
        "Status": options(DAILY_PLAN_STATUSES),
        "Source": {"url": {}},
        "Plan date": {"date": {}},
        "Task ID": {"rich_text": {}},
        "Rank": {"number": {"format": "number"}},
    }


def master_task_schema() -> dict[str, Any]:
    """Schema for the one task source shared by desktop and mobile views."""
    def options(values: Iterable[str]) -> dict[str, Any]:
        return {"select": {"options": [{"name": value} for value in values]}}

    return {
        "Task": {"title": {}},
        "Done": {"checkbox": {}},
        "Status": options(MASTER_STATUSES),
        "Area": options(MASTER_AREAS),
        "Course": {"rich_text": {}},
        "Source type": options(MASTER_SOURCE_TYPES),
        "Source ID": {"rich_text": {}},
        "Source URL": {"url": {}},
        "Due": {"date": {}},
        "Priority": options(SCHOOL_PRIORITIES),
        "Effort": options(EFFORTS),
        "Kind": {"rich_text": {}},
        "Display type": options(DISPLAY_TYPES),
        "Next step": {"rich_text": {}},
        "Notes / progress": {"rich_text": {}},
        "Instructions": {"rich_text": {}},
        "Focus date": {"date": {}},
        "Focus rank": {"number": {"format": "number"}},
        "Focus reason": {"rich_text": {}},
        "Needs verification": {"checkbox": {}},
        "Locked": {"checkbox": {}},
        "Unlock at": {"date": {}},
        "Archived": {"checkbox": {}},
        "Cadence": options(CADENCES),
        "Last touched": {"date": {}},
        "Task type": options(TYPES),
        "Sync hash": {"rich_text": {}},
        "Open": {
            "formula": {
                "expression": 'if(empty(prop("Source URL")), "", link("Open ↗", prop("Source URL")))'
            }
        },
        "Sort order": {
            "formula": {
                "expression": (
                    'ifs(prop("Status") == "In progress", 0, '
                    'prop("Status") == "Needs remake", 1, '
                    'prop("Locked"), 70, '
                    'prop("Status") == "Verify", 60, '
                    'prop("Priority") == "MUST", 10, '
                    'prop("Priority") == "SMART", 20, '
                    'prop("Priority") == "MAY", 30, 40)'
                )
            }
        },
    }


def display_task_type(name: str, kind: str = "", *, source_type: str = "Canvas") -> str:
    """Turn low-level source kinds into labels that help someone plan."""
    if source_type != "Canvas":
        return "Personal task"
    lowered = f"{name} {kind}".casefold()
    if ASSESSMENT_TYPE_RE.search(lowered):
        return "Test / Quiz"
    if "project" in lowered or "presentation" in lowered:
        return "Project"
    if re.search(r"\blab(?:oratory)?\b", lowered):
        return "Lab"
    if re.search(r"\b(?:essay|journal|writing|write-up|response)\b", lowered):
        return "Essay / Writing"
    if kind == "calendar_event":
        return "Event"
    return "Homework"


def _view_filter(property_id: str, kind: str, operator: str, value: Any) -> dict[str, Any]:
    return {"property": property_id, kind: {operator: value}}


def master_view_specs(property_ids: dict[str, str]) -> list[dict[str, Any]]:
    """Return the idempotent human-facing view design for the master task data."""
    required = set(master_task_schema())
    missing = required - set(property_ids)
    if missing:
        raise NotionError(
            "Tasks database is missing view properties: " + ", ".join(sorted(missing))
        )

    def active_filter(*extra: dict[str, Any]) -> dict[str, Any]:
        return {
            "and": [
                _view_filter(property_ids["Archived"], "checkbox", "equals", False),
                *(
                    _view_filter(
                        property_ids["Status"], "select", "does_not_equal", status
                    )
                    for status in NON_ACTIONABLE_STATUSES
                ),
                *extra,
            ]
        }

    table_visible = [
        ("Task", 260, True),
        ("Open", 80, False),
        ("Status", 110, False),
        ("Course", 150, True),
        ("Display type", 115, False),
        ("Due", 145, False),
        ("Priority", 90, False),
        ("Next step", 420, True),
        ("Notes / progress", 320, True),
        ("Instructions", 420, True),
        ("Last touched", 120, False),
    ]

    def properties(
        visible: list[str] | None = None, *, table: bool = False
    ) -> list[dict[str, Any]]:
        visible = visible or []
        configured: list[dict[str, Any]] = []
        emitted: set[str] = set()
        if table:
            for name, width, wrap in table_visible:
                configured.append(
                    {
                        "property_id": property_ids[name],
                        "visible": True,
                        "width": width,
                        "wrap": wrap,
                    }
                )
                emitted.add(name)
        else:
            for name in visible:
                configured.append(
                    {"property_id": property_ids[name], "visible": True}
                )
                emitted.add(name)
        for name in property_ids:
            if name not in emitted:
                configured.append(
                    {"property_id": property_ids[name], "visible": False}
                )
        return configured

    active_sorts = [
        {"property": property_ids["Sort order"], "direction": "ascending"},
        {"property": property_ids["Due"], "direction": "ascending"},
        {"property": property_ids["Course"], "direction": "ascending"},
    ]
    compact = [
        "Task",
        "Open",
        "Status",
        "Due",
        "Course",
        "Display type",
        "Priority",
        "Next step",
    ]
    area_specs = []
    for area in MASTER_AREAS:
        visible = compact if area == "School" else [
            "Task",
            "Open",
            "Status",
            "Due",
            "Priority",
            "Next step",
            "Notes / progress",
            "Last touched",
        ]
        configuration: dict[str, Any] = {
            "type": "list",
            "properties": properties(visible),
        }
        area_specs.append(
            {
                "name": area,
                "type": "list",
                "filter": active_filter(
                    _view_filter(property_ids["Area"], "select", "equals", area)
                ),
                "sorts": active_sorts,
                "quick_filters": {},
                "configuration": configuration,
            }
        )

    return [
        {
            "name": "Active tasks",
            "type": "table",
            "filter": active_filter(),
            "sorts": active_sorts,
            "quick_filters": {},
            "configuration": {
                "type": "table",
                "properties": properties(table=True),
                "wrap_cells": False,
                "frozen_column_index": 1,
                "show_vertical_lines": True,
            },
            "position": {"type": "start"},
        },
        {
            "name": "Today",
            "type": "list",
            "filter": active_filter(
                _view_filter(property_ids["Focus rank"], "number", "is_not_empty", True)
            ),
            "sorts": [
                {"property": property_ids["Focus rank"], "direction": "ascending"}
            ],
            "quick_filters": {},
            "configuration": {
                "type": "list",
                "properties": properties(
                    [
                        "Task",
                        "Open",
                        "Course",
                        "Due",
                        "Priority",
                        "Next step",
                        "Notes / progress",
                        "Focus reason",
                    ]
                ),
            },
        },
        {
            "name": "Due calendar",
            "type": "calendar",
            "filter": active_filter(
                _view_filter(property_ids["Due"], "date", "is_not_empty", True)
            ),
            "sorts": [
                {"property": property_ids["Due"], "direction": "ascending"},
                {"property": property_ids["Course"], "direction": "ascending"},
            ],
            "quick_filters": {},
            "configuration": {
                "type": "calendar",
                "date_property_id": property_ids["Due"],
                "properties": properties(
                    ["Task", "Course", "Status", "Priority", "Display type", "Open"]
                ),
                "view_range": "month",
                "show_weekends": True,
            },
            "position": {"type": "start"},
        },
        {
            "name": "Upcoming",
            "type": "list",
            "filter": active_filter(
                _view_filter(property_ids["Due"], "date", "is_not_empty", True)
            ),
            "sorts": [
                {"property": property_ids["Due"], "direction": "ascending"},
                {"property": property_ids["Sort order"], "direction": "ascending"},
                {"property": property_ids["Course"], "direction": "ascending"},
            ],
            "quick_filters": {},
            "configuration": {
                "type": "list",
                "properties": properties(compact + ["Notes / progress"]),
            },
        },
        {
            "name": "By area",
            "type": "table",
            "filter": active_filter(),
            "sorts": [
                {"property": property_ids["Area"], "direction": "ascending"},
                {"property": property_ids["Sort order"], "direction": "ascending"},
                {"property": property_ids["Due"], "direction": "ascending"},
            ],
            "quick_filters": {},
            "configuration": {
                "type": "table",
                "properties": properties(
                    [
                        "Task",
                        "Open",
                        "Status",
                        "Area",
                        "Course",
                        "Due",
                        "Priority",
                        "Next step",
                        "Notes / progress",
                    ]
                ),
                "group_by": {
                    "type": "select",
                    "property_id": property_ids["Area"],
                    "sort": {"type": "manual"},
                    "hide_empty_groups": True,
                },
                "wrap_cells": False,
                "frozen_column_index": 1,
                "show_vertical_lines": True,
            },
        },
        *area_specs,
        {
            "name": "Needs attention",
            "type": "list",
            "filter": {
                "and": [
                    _view_filter(
                        property_ids["Archived"], "checkbox", "equals", False
                    ),
                    {
                        "or": [
                            _view_filter(
                                property_ids["Status"],
                                "select",
                                "equals",
                                "Needs remake",
                            ),
                            _view_filter(
                                property_ids["Status"], "select", "equals", "Verify"
                            ),
                            _view_filter(
                                property_ids["Needs verification"],
                                "checkbox",
                                "equals",
                                True,
                            ),
                        ]
                    },
                ]
            },
            "sorts": active_sorts,
            "quick_filters": {},
            "configuration": {
                "type": "list",
                "properties": properties(compact + ["Notes / progress"]),
            },
        },
        {
            "name": "Submitted / waiting",
            "type": "list",
            "filter": {
                "and": [
                    _view_filter(
                        property_ids["Archived"], "checkbox", "equals", False
                    ),
                    {
                        "or": [
                            _view_filter(
                                property_ids["Status"], "select", "equals", "Submitted"
                            ),
                            _view_filter(
                                property_ids["Status"], "select", "equals", "Waiting"
                            ),
                        ]
                    },
                ]
            },
            "sorts": [
                {"property": property_ids["Due"], "direction": "descending"}
            ],
            "quick_filters": {},
            "configuration": {
                "type": "list",
                "properties": properties(
                    ["Task", "Open", "Status", "Course", "Due", "Notes / progress"]
                ),
            },
        },
        {
            "name": "History",
            "type": "list",
            "filter": {
                "or": [
                    _view_filter(
                        property_ids["Archived"], "checkbox", "equals", True
                    ),
                    _view_filter(
                        property_ids["Status"], "select", "equals", "Done"
                    ),
                ]
            },
            "sorts": [{"property": property_ids["Due"], "direction": "descending"}],
            "quick_filters": {},
            "configuration": {
                "type": "list",
                "properties": properties(
                    ["Task", "Open", "Status", "Course", "Display type", "Due"]
                ),
            },
        },
        {
            "name": "_System",
            "type": "table",
            "filter": None,
            "sorts": [{"property": property_ids["Task"], "direction": "ascending"}],
            "quick_filters": {},
            "configuration": {
                "type": "table",
                "properties": [
                    {"property_id": property_id, "visible": True}
                    for property_id in property_ids.values()
                ],
                "wrap_cells": False,
                "frozen_column_index": 1,
                "show_vertical_lines": True,
            },
        },
    ]


def _bounded(value: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip()
    return normalized if len(normalized) <= limit else normalized[: limit - 3].rstrip() + "..."


def clean_notion_next_step(value: str) -> str:
    """Return the user-owned action without presentation prefixes from older runs."""
    text = _bounded(value, 1000)
    return re.sub(
        r"^(?:start with this next step:\s*)+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def compact_instruction_summary(value: str, limit: int = 400) -> str:
    """Create a readable fallback when model-generated summary text is unavailable."""
    text = re.sub(r"[*_#`]+", "", value or "")
    text = re.sub(r"\bAttachment\s+[^:]{1,120}:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" :-")
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = " ".join([*summary, sentence])
        if summary and len(candidate) > limit:
            break
        summary.append(sentence)
        if len(summary) == 2 or len(candidate) >= limit:
            break
    return _bounded(" ".join(summary) or text, limit)


def school_assignment_fields(
    assignment,
    details: dict[str, str] | None = None,
) -> dict[str, Any]:
    details = details or {}
    status = (
        "Verify"
        if (
            assignment.needs_confirmation
            or assignment.submission_status == "unknown"
            or details.get("Priority") == "Verify"
        )
        else "To do"
    )
    instructions = _bounded(
        details.get("Instructions")
        or compact_instruction_summary(assignment.description),
        400,
    )
    next_step = _bounded(
        details.get("Next step")
        or (
            "Open the Canvas assignment and follow the listed requirements."
            if instructions
            else "Open the Canvas assignment and review the requirements."
        ),
        1000,
    )
    fields: dict[str, Any] = {
        "Name": _bounded(assignment.name, 500),
        "Due": assignment.due_at.isoformat() if assignment.due_at else None,
        "Status": status,
        "Priority": details.get("Priority")
        or ("Verify" if status == "Verify" else "Later"),
        "Effort": details.get("Effort") or None,
        "Kind": assignment.kind,
        "Next step": next_step,
        "Instructions": instructions,
        "Canvas": assignment.url or None,
        "Canvas ID": assignment.key,
    }
    fingerprint_data = {
        **fields,
        "Source instructions hash": hashlib.sha256(
            assignment.description.encode("utf-8")
        ).hexdigest(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    fields["Sync hash"] = fingerprint
    return fields


def canvas_master_task_fields(
    assignment: Any, details: dict[str, str] | None = None
) -> dict[str, Any]:
    school = school_assignment_fields(assignment, details)
    status = "Verify" if school["Status"] == "Verify" else "To do"
    source_fields = {
        "Task": school["Name"],
        "Status": status,
        "Area": "School",
        "Course": _bounded(assignment.course, 200),
        "Source type": "Canvas",
        "Source ID": assignment.key,
        "Source URL": assignment.url or None,
        "Due": school["Due"],
        "Priority": school["Priority"],
        "Effort": school["Effort"],
        "Kind": assignment.kind,
        "Display type": display_task_type(assignment.name, assignment.kind),
        "Next step": school["Next step"],
        "Instructions": school["Instructions"],
        "Needs verification": school["Status"] == "Verify",
        "Locked": assignment.locked_for_user,
        "Unlock at": assignment.unlock_at.isoformat() if assignment.unlock_at else None,
        "Archived": False,
    }
    source_fields["Sync hash"] = hashlib.sha256(
        json.dumps(source_fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return source_fields


def notion_master_task_fields(
    item: NotionWorkItem, details: dict[str, str] | None = None
) -> dict[str, Any]:
    details = details or {}
    area = item.area if item.area in MASTER_AREAS else "Misc"
    source_fields = {
        "Task": _bounded(item.name, 500),
        "Status": item.status,
        "Area": area,
        "Course": "",
        "Source type": "Notion",
        "Source ID": item.key,
        "Source URL": item.url or None,
        "Due": item.deadline,
        "Priority": details.get("Priority") or "Later",
        "Effort": details.get("Effort") or item.effort,
        "Kind": item.type or "Task",
        "Display type": display_task_type(
            item.name, item.type or "Task", source_type="Notion"
        ),
        # Keep this field user-owned. Generated guidance is presentation data and
        # must not become the input to the next planning run.
        "Next step": clean_notion_next_step(item.next_step),
        "Instructions": "",
        "Needs verification": False,
        "Locked": False,
        "Unlock at": None,
        "Archived": False,
        "Cadence": item.cadence,
        "Last touched": item.last_touched,
        "Task type": item.type,
    }
    source_fields["Sync hash"] = hashlib.sha256(
        json.dumps(source_fields, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return source_fields


def school_properties(fields: dict[str, Any]) -> dict[str, Any]:
    builders = {
        "Name": title_property,
        "Due": date_property,
        "Status": select_property,
        "Priority": select_property,
        "Effort": select_property,
        "Kind": select_property,
        "Next step": rich_text_property,
        "Notes / progress": rich_text_property,
        "Instructions": rich_text_property,
        "Canvas": lambda value: {"url": value or None},
        "Canvas ID": rich_text_property,
        "Sync hash": rich_text_property,
    }
    unknown = set(fields) - set(builders)
    if unknown:
        raise ValueError(f"unknown School properties: {', '.join(sorted(unknown))}")
    return {name: builders[name](value) for name, value in fields.items()}


def daily_plan_properties(fields: dict[str, Any]) -> dict[str, Any]:
    builders = {
        "Task": title_property,
        "Priority": select_property,
        "Course / area": rich_text_property,
        "Time (hours)": lambda value: {"number": value},
        "Next step": rich_text_property,
        "Status": select_property,
        "Source": lambda value: {"url": value or None},
        "Plan date": date_property,
        "Task ID": rich_text_property,
        "Rank": lambda value: {"number": value},
    }
    unknown = set(fields) - set(builders)
    if unknown:
        raise ValueError(f"unknown Today's Plan properties: {', '.join(sorted(unknown))}")
    return {name: builders[name](value) for name, value in fields.items()}


def master_task_properties(fields: dict[str, Any]) -> dict[str, Any]:
    builders = {
        "Task": title_property,
        "Done": lambda value: {"checkbox": bool(value)},
        "Status": select_property,
        "Area": select_property,
        "Course": rich_text_property,
        "Source type": select_property,
        "Source ID": rich_text_property,
        "Source URL": lambda value: {"url": value or None},
        "Due": date_property,
        "Priority": select_property,
        "Effort": select_property,
        "Kind": rich_text_property,
        "Display type": select_property,
        "Next step": rich_text_property,
        "Notes / progress": rich_text_property,
        "Instructions": rich_text_property,
        "Focus date": date_property,
        "Focus rank": lambda value: {"number": value},
        "Focus reason": rich_text_property,
        "Needs verification": lambda value: {"checkbox": bool(value)},
        "Locked": lambda value: {"checkbox": bool(value)},
        "Unlock at": date_property,
        "Archived": lambda value: {"checkbox": bool(value)},
        "Cadence": select_property,
        "Last touched": date_property,
        "Task type": select_property,
        "Sync hash": rich_text_property,
    }
    unknown = set(fields) - set(builders)
    if unknown:
        raise ValueError(f"unknown master Task properties: {', '.join(sorted(unknown))}")
    return {name: builders[name](value) for name, value in fields.items()}


def work_properties(fields: dict[str, Any]) -> dict[str, Any]:
    builders = {
        "Name": title_property,
        "Type": select_property,
        "Cadence": select_property,
        "Last touched": date_property,
        "Status": select_property,
        "Next step": rich_text_property,
        "Deadline": date_property,
        "Effort": select_property,
    }
    unknown = set(fields) - set(builders)
    if unknown:
        raise ValueError(f"unknown Work properties: {', '.join(sorted(unknown))}")
    return {name: builders[name](value) for name, value in fields.items()}


def _plain_text(prop: dict[str, Any], expected_type: str) -> str:
    if prop.get("type") != expected_type or not isinstance(prop.get(expected_type), list):
        raise ValueError(f"expected {expected_type} property")
    return re.sub(
        r"\s+",
        " ",
        "".join(str(part.get("plain_text", "")) for part in prop[expected_type]),
    ).strip()


def _select(prop: dict[str, Any] | None, name: str) -> str | None:
    if prop is None:
        return None
    if prop.get("type") != "select":
        raise ValueError(f"{name} has wrong property type")
    selected = prop.get("select")
    if selected is None:
        return None
    return selected.get("name")


def _date(prop: dict[str, Any] | None, name: str, warnings: list[str], page_id: str) -> date | None:
    if prop is None:
        return None
    if prop.get("type") != "date":
        raise ValueError(f"{name} has wrong property type")
    raw = prop.get("date")
    if raw is None:
        return None
    start = raw.get("start")
    try:
        parsed = date.fromisoformat(start)
        if parsed.isoformat() != start:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        warnings.append(f"Notion row {page_id}: {name} is not a valid date and was ignored")
        return None


class NotionClient:
    def __init__(self, token: str, work_db_id: str, parent_page_id: str = "", *, http=None) -> None:
        self.work_db_id = work_db_id.replace("-", "")
        self.parent_page_id = parent_page_id.replace("-", "")
        self.http = http or HttpClient()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self.view_headers = {
            **self.headers,
            "Notion-Version": NOTION_VIEW_VERSION,
        }

    def _json(
        self,
        method: str,
        path: str,
        *,
        payload=None,
        params=None,
        idempotent=None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = self.http.request_json(
                method,
                f"{NOTION_API}{path}",
                source="notion",
                headers=headers or self.headers,
                json=payload,
                params=params,
                idempotent=idempotent,
            )
        except HttpFailure as exc:
            if exc.status == 404:
                raise NotionError(
                    "Notion returned 404: the id may be invalid/nonexistent or the page/database "
                    "may not be shared with the Todo Agent integration"
                ) from exc
            raise NotionError(str(exc)) from exc
        return response.data

    def master_property_ids(self, database_id: str) -> tuple[str, dict[str, str]]:
        """Resolve the current data source and decoded property IDs for view APIs."""
        compact_id = database_id.replace("-", "")
        database = self._json(
            "GET",
            f"/databases/{compact_id}",
            idempotent=True,
            headers=self.view_headers,
        )
        data_sources = database.get("data_sources") or []
        if len(data_sources) != 1 or not data_sources[0].get("id"):
            raise NotionError("Tasks database must contain exactly one data source")
        data_source_id = str(data_sources[0]["id"])
        data_source = self._json(
            "GET",
            f"/data_sources/{data_source_id}",
            idempotent=True,
            headers=self.view_headers,
        )
        raw_properties = data_source.get("properties") or {}
        property_ids = {
            str(name): unquote(str(value.get("id") or ""))
            for name, value in raw_properties.items()
            if value.get("id")
        }
        return data_source_id, property_ids

    def list_database_views(self, database_id: str) -> list[dict[str, Any]]:
        """Return complete view objects, not the abbreviated list references."""
        params: dict[str, Any] = {
            "database_id": database_id.replace("-", ""),
            "page_size": 100,
        }
        references: list[dict[str, Any]] = []
        while True:
            body = self._json(
                "GET",
                "/views",
                params=params,
                idempotent=True,
                headers=self.view_headers,
            )
            references.extend(body.get("results") or [])
            if not body.get("has_more"):
                break
            cursor = body.get("next_cursor")
            if not cursor:
                raise NotionError("Notion views response has no next_cursor")
            params = {**params, "start_cursor": cursor}
        return [
            self._json(
                "GET",
                f"/views/{reference['id']}",
                idempotent=True,
                headers=self.view_headers,
            )
            for reference in references
            if reference.get("id")
        ]

    def create_view(
        self, database_id: str, data_source_id: str, spec: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "database_id": database_id.replace("-", ""),
            "data_source_id": data_source_id,
            **spec,
        }
        return self._json(
            "POST",
            "/views",
            payload=payload,
            idempotent=False,
            headers=self.view_headers,
        )

    def update_view(self, view_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        payload = {name: value for name, value in spec.items() if name != "type"}
        payload.pop("position", None)
        return self._json(
            "PATCH",
            f"/views/{view_id}",
            payload=payload,
            idempotent=True,
            headers=self.view_headers,
        )

    def ensure_master_task_views(self, database_id: str) -> dict[str, str]:
        """Create or update the clean task views while retaining one hidden data source."""
        data_source_id, property_ids = self.master_property_ids(database_id)
        specs = master_view_specs(property_ids)
        existing = self.list_database_views(database_id)
        by_name = {str(view.get("name") or ""): view for view in existing}
        if "_System" not in by_name and "All Tasks" in by_name:
            by_name["_System"] = by_name.pop("All Tasks")

        urls: dict[str, str] = {}
        for spec in specs:
            current = by_name.get(spec["name"])
            if current is not None:
                if current.get("type") != spec["type"]:
                    raise NotionError(
                        f"Notion view {spec['name']} has type {current.get('type')}; "
                        f"expected {spec['type']}"
                    )
                response = self.update_view(str(current["id"]), spec)
            else:
                response = self.create_view(database_id, data_source_id, spec)
            fallback_url = str(current.get("url") or "") if current else ""
            url = str(response.get("url") or fallback_url)
            if url:
                urls[spec["name"]] = url
        return urls

    def get_active_work(self) -> WorkSnapshot:
        payload: dict[str, Any] = {
            "page_size": 100,
            "filter": {"property": "Status", "select": {"equals": "Active"}},
        }
        pages: list[dict[str, Any]] = []
        while True:
            body = self._json(
                "POST", f"/databases/{self.work_db_id}/query", payload=payload, idempotent=True
            )
            if not isinstance(body.get("results"), list):
                raise NotionError("Notion query response is missing results")
            pages.extend(body["results"])
            if not body.get("has_more"):
                break
            cursor = body.get("next_cursor")
            if not cursor:
                raise NotionError("Notion query says has_more without next_cursor")
            payload = {**payload, "start_cursor": cursor}

        warnings: list[str] = []
        items: list[NotionWorkItem] = []
        for page in pages:
            page_id = str(page.get("id", "")).replace("-", "")
            props = page.get("properties")
            if not page_id or not isinstance(props, dict):
                warnings.append("Skipped malformed Notion row without id/properties")
                continue
            try:
                name = _plain_text(props.get("Name", {}), "title")
                status = _select(props.get("Status"), "Status")
                if not name or status != "Active":
                    warnings.append(f"Skipped Notion row {page_id}: blank Name or non-Active Status")
                    continue
                area = _select(props.get("Area"), "Area")
                item_type = _select(props.get("Type"), "Type")
                cadence = _select(props.get("Cadence"), "Cadence")
                effort = _select(props.get("Effort"), "Effort")
                next_step = _plain_text(props["Next step"], "rich_text") if "Next step" in props else ""
                last_touched = _date(props.get("Last touched"), "Last touched", warnings, page_id)
                deadline = _date(props.get("Deadline"), "Deadline", warnings, page_id)
            except ValueError as exc:
                warnings.append(f"Skipped Notion row {page_id}: {exc}")
                continue
            if effort not in {*EFFORTS, None}:
                warnings.append(f"Notion row {page_id}: unknown Effort was ignored")
                effort = None
            if len(name) > 200:
                name = name[:197] + "..."
                warnings.append(f"Notion row {page_id}: Name was truncated")
            if len(next_step) > 1000:
                next_step = next_step[:997] + "..."
                warnings.append(f"Notion row {page_id}: Next step was truncated")
            items.append(
                NotionWorkItem(
                    key=f"notion:{page_id}",
                    page_id=page_id,
                    url=str(page.get("url", "")),
                    name=name,
                    area=area,
                    type=item_type,
                    cadence=cadence,
                    last_touched=last_touched,
                    next_step=next_step,
                    deadline=deadline,
                    effort=effort,
                )
            )
        return WorkSnapshot(items=items, warnings=warnings)

    def retrieve_parent_page(self) -> dict[str, Any]:
        return self._json("GET", f"/pages/{self.parent_page_id}")

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self._json("GET", f"/pages/{page_id.replace('-', '')}")

    def retrieve_database(self) -> dict[str, Any]:
        return self._json("GET", f"/databases/{self.work_db_id}")

    def ensure_database_properties(
        self, database_id: str, properties: dict[str, Any]
    ) -> bool:
        compact_id = database_id.replace("-", "")
        database = self._json("GET", f"/databases/{compact_id}")
        existing = database.get("properties") or {}
        missing = {name: schema for name, schema in properties.items() if name not in existing}
        if not missing:
            return False
        self._json(
            "PATCH",
            f"/databases/{compact_id}",
            payload={"properties": missing},
            idempotent=True,
        )
        return True

    def query_database_pages(self, database_id: str) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"page_size": 100}
        pages: list[dict[str, Any]] = []
        compact_id = database_id.replace("-", "")
        while True:
            body = self._json(
                "POST",
                f"/databases/{compact_id}/query",
                payload=payload,
                idempotent=True,
            )
            results = body.get("results")
            if not isinstance(results, list):
                raise NotionError("Notion query response is missing results")
            pages.extend(results)
            if not body.get("has_more"):
                return pages
            cursor = body.get("next_cursor")
            if not cursor:
                raise NotionError("Notion query says has_more without next_cursor")
            payload = {**payload, "start_cursor": cursor}

    def create_child_page(
        self, title: str, *, parent_page_id: str | None = None
    ) -> dict[str, Any]:
        parent_id = (parent_page_id or self.parent_page_id).replace("-", "")
        return self._json(
            "POST",
            "/pages",
            payload={
                "parent": {"type": "page_id", "page_id": parent_id},
                "properties": {"title": title_property(_bounded(title, 200))},
            },
            idempotent=False,
        )

    def create_database(
        self,
        title: str,
        *,
        parent_page_id: str | None = None,
        properties: dict[str, Any] | None = None,
        is_inline: bool = False,
    ) -> dict[str, Any]:
        parent_id = (parent_page_id or self.parent_page_id).replace("-", "")
        payload = {
            "parent": {"type": "page_id", "page_id": parent_id},
            "title": [{"type": "text", "text": {"content": _bounded(title, 200)}}],
            "properties": properties or database_schema(),
            "is_inline": is_inline,
        }
        return self._json("POST", "/databases", payload=payload, idempotent=False)

    def create_work_database(self, title: str = "Work") -> dict[str, Any]:
        if title not in TASK_DATABASES:
            raise ValueError(f"unknown task database: {title}")
        return self.create_database(title)

    def update_work_item(self, page_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id}",
            payload={"properties": work_properties(fields)},
            idempotent=False,
        )

    def create_work_item(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._json(
            "POST",
            "/pages",
            payload={
                "parent": {"type": "database_id", "database_id": self.work_db_id},
                "properties": work_properties(fields),
            },
            idempotent=False,
        )

    def archive_work_item(self, page_id: str) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id}",
            payload={"archived": True},
            idempotent=True,
        )

    def archive_page(self, page_id: str) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id.replace('-', '')}",
            payload={"archived": True},
            idempotent=True,
        )

    def archive_database(self, database_id: str) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/databases/{database_id.replace('-', '')}",
            payload={"archived": True},
            idempotent=True,
        )

    def create_school_item(
        self, database_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/pages",
            payload={
                "parent": {
                    "type": "database_id",
                    "database_id": database_id.replace("-", ""),
                },
                "properties": school_properties(fields),
            },
            idempotent=False,
        )

    def update_school_item(
        self, page_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id.replace('-', '')}",
            payload={"properties": school_properties(fields)},
            idempotent=True,
        )

    def create_daily_plan_item(
        self, database_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/pages",
            payload={
                "parent": {
                    "type": "database_id",
                    "database_id": database_id.replace("-", ""),
                },
                "properties": daily_plan_properties(fields),
            },
            idempotent=False,
        )

    def append_block_children(
        self, block_id: str, children: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/blocks/{block_id.replace('-', '')}/children",
            payload={"children": children},
            idempotent=False,
        )

    def archive_block(self, block_id: str) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/blocks/{block_id.replace('-', '')}",
            payload={"archived": True},
            idempotent=True,
        )

    def create_master_task(
        self, database_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/pages",
            payload={
                "parent": {"type": "database_id", "database_id": database_id},
                "properties": master_task_properties(fields),
            },
            idempotent=False,
        )

    def update_master_task(self, page_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id.replace('-', '')}",
            payload={"properties": master_task_properties(fields)},
            idempotent=False,
        )

    def update_daily_plan_item(
        self, page_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id.replace('-', '')}",
            payload={"properties": daily_plan_properties(fields)},
            idempotent=True,
        )

    def list_block_children(self, block_id: str) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            body = self._json(
                "GET",
                f"/blocks/{block_id}/children",
                params=params,
                idempotent=True,
            )
            children.extend(body.get("results") or [])
            if not body.get("has_more"):
                return children
            cursor = body.get("next_cursor")
            if not cursor:
                raise NotionError("Notion children response has no next_cursor")

def _property_rich_text(page: dict[str, Any], name: str) -> str:
    properties = page.get("properties") or {}
    prop = properties.get(name) or {}
    if prop.get("type") != "rich_text":
        return ""
    return "".join(
        str(part.get("plain_text") or (part.get("text") or {}).get("content") or "")
        for part in prop.get("rich_text") or []
    )


def _property_title(page: dict[str, Any], name: str) -> str:
    properties = page.get("properties") or {}
    prop = properties.get(name) or {}
    if prop.get("type") != "title":
        return ""
    return "".join(
        str(part.get("plain_text") or (part.get("text") or {}).get("content") or "")
        for part in prop.get("title") or []
    )


def _property_select(page: dict[str, Any], name: str) -> str:
    properties = page.get("properties") or {}
    prop = properties.get(name) or {}
    selected = prop.get("select") if prop.get("type") == "select" else None
    return str((selected or {}).get("name") or "")


def _property_checkbox(page: dict[str, Any], name: str) -> bool:
    properties = page.get("properties") or {}
    prop = properties.get(name) or {}
    return bool(prop.get("checkbox")) if prop.get("type") == "checkbox" else False


def _property_date_start(page: dict[str, Any], name: str) -> str:
    properties = page.get("properties") or {}
    prop = properties.get(name) or {}
    value = prop.get("date") if prop.get("type") == "date" else None
    return str((value or {}).get("start") or "")


def _property_url(page: dict[str, Any], name: str) -> str:
    properties = page.get("properties") or {}
    prop = properties.get(name) or {}
    return str(prop.get("url") or "") if prop.get("type") == "url" else ""


def _property_number(page: dict[str, Any], name: str) -> float | None:
    properties = page.get("properties") or {}
    prop = properties.get(name) or {}
    value = prop.get("number") if prop.get("type") == "number" else None
    return value if isinstance(value, (int, float)) else None


class NotionSchoolBoard:
    """Synchronize Canvas assignments into one database per class on the School page."""

    def __init__(
        self,
        token: str = "",
        parent_page_id: str = "",
        school_page_id: str = "",
        *,
        client: Any | None = None,
    ) -> None:
        self.parent_page_id = parent_page_id.replace("-", "")
        self.school_page_id = school_page_id.replace("-", "")
        if not self.parent_page_id or not self.school_page_id:
            raise NotionError("Notion parent and School page IDs are required")
        self.client = client or NotionClient(token, "", self.parent_page_id)

    @staticmethod
    def _child_databases(children: Iterable[dict[str, Any]]) -> dict[str, str]:
        databases: dict[str, str] = {}
        for child in children:
            if child.get("type") != "child_database" or not child.get("id"):
                continue
            title = str((child.get("child_database") or {}).get("title") or "").strip()
            if title and title not in databases:
                databases[title] = str(child["id"])
        return databases

    def _master_task_database(self) -> str | None:
        databases = self._child_databases(
            self.client.list_block_children(self.parent_page_id)
        )
        return databases.get(MASTER_TASK_TITLE)

    @staticmethod
    def _child_pages(children: Iterable[dict[str, Any]]) -> dict[str, str]:
        pages: dict[str, str] = {}
        for child in children:
            if child.get("type") != "child_page" or not child.get("id"):
                continue
            title = str((child.get("child_page") or {}).get("title") or "").strip()
            if title and title not in pages:
                pages[title] = str(child["id"])
        return pages

    @staticmethod
    def _focus_task_block(task: NotificationTask, *, rank: int) -> dict[str, Any]:
        icon = "🎯" if rank == 1 else ("2️⃣" if rank == 2 else "3️⃣")
        link = {"url": task.url} if task.url else None
        due = ""
        if task.due_at is not None:
            due = f"Due {task.due_at.strftime('%a %b')} {task.due_at.day}"
        metadata = " · ".join(
            value
            for value in (
                _bounded(task.course, 80),
                due,
                f"{task.effort_hours:g}h",
            )
            if value
        )
        rich_text: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": {"content": _bounded(task.name, 300), "link": link},
                "annotations": {"bold": True},
            }
        ]
        if metadata:
            rich_text.append(
                {"type": "text", "text": {"content": f"\n{metadata}"}}
            )
        rich_text.append(
            {
                "type": "text",
                "text": {"content": f"\n{_bounded(task.next_step, 300)}"},
            }
        )
        return {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": rich_text,
                "icon": {"type": "emoji", "emoji": icon},
                "color": "default",
            },
        }

    def sync_focus_dashboard(
        self,
        notification: DailyNotification,
        *,
        full_tasks_url: str = "",
    ) -> FocusDashboardSyncResult:
        """Replace the generated, phone-first Today page with the current focus."""
        root_children = self.client.list_block_children(self.parent_page_id)
        page_id = self._child_pages(root_children).get(FOCUS_DASHBOARD_TITLE)
        created = False
        if page_id:
            page = self.client.retrieve_page(page_id)
        else:
            page = self.client.create_child_page(
                FOCUS_DASHBOARD_TITLE, parent_page_id=self.parent_page_id
            )
            page_id = str(page["id"])
            created = True

        for child in self.client.list_block_children(page_id):
            block_id = str(child.get("id") or "")
            if block_id:
                self.client.archive_block(block_id)

        tasks = [task for task in [notification.primary, *notification.followups] if task]
        blocks: list[dict[str, Any]] = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": (
                                    f"{notification.target_date.strftime('%A, %B')} "
                                    f"{notification.target_date.day} · "
                                    "Tap a task to mark it done or add progress notes."
                                )
                            },
                            "annotations": {"color": "gray"},
                        }
                    ]
                },
            }
        ]
        if tasks:
            blocks.extend(
                self._focus_task_block(task, rank=rank)
                for rank, task in enumerate(tasks, start=1)
            )
        else:
            blocks.append(
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [
                            {"type": "text", "text": {"content": "No focus tasks today."}}
                        ],
                        "icon": {"type": "emoji", "emoji": "✅"},
                    },
                }
            )

        for reminder in notification.reminders:
            label = " · ".join(
                value for value in (reminder.course, reminder.title) if value
            )
            blocks.append(
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": _bounded(f"Reminder: {label}", 300),
                                    "link": {"url": reminder.url} if reminder.url else None,
                                },
                            }
                        ],
                        "icon": {"type": "emoji", "emoji": "⏰"},
                        "color": "yellow_background",
                    },
                }
            )

        backlog_text = f"{notification.backlog_count} other task(s) are safely kept out of today's view."
        if notification.verify_count:
            backlog_text += f" {notification.verify_count} Canvas item(s) need status confirmation."
        backlog_rich_text: list[dict[str, Any]] = [
            {"type": "text", "text": {"content": backlog_text}}
        ]
        if full_tasks_url:
            backlog_rich_text.extend(
                [
                    {"type": "text", "text": {"content": " "}},
                    {
                        "type": "text",
                        "text": {
                            "content": "Open the full task list only when you need it.",
                            "link": {"url": full_tasks_url},
                        },
                        "annotations": {"bold": True},
                    },
                ]
            )
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": backlog_rich_text},
            }
        )
        self.client.append_block_children(page_id, blocks)
        return FocusDashboardSyncResult(
            page_id=page_id,
            url=str(page.get("url") or f"https://www.notion.so/{page_id.replace('-', '')}"),
            page_created=created,
            blocks_written=len(blocks),
        )

    def master_tasks_enabled(self) -> bool:
        """Return whether the safe migration has created the master database."""
        return self._master_task_database() is not None

    def migrate_legacy_school_rows(self) -> LegacySchoolMigrationResult:
        """Preserve legacy class-table history before those databases are archived."""
        database_id = self._master_task_database()
        if not database_id:
            raise NotionError("master Tasks database does not exist")

        existing: dict[str, tuple[str, dict[str, Any]]] = {}
        for page in self.client.query_database_pages(database_id):
            source_id = _property_rich_text(page, "Source ID")
            page_id = str(page.get("id") or "")
            if source_id and page_id and source_id not in existing:
                existing[source_id] = (page_id, page)

        candidates: dict[str, dict[str, Any]] = {}
        result = LegacySchoolMigrationResult()
        databases = self._child_databases(
            self.client.list_block_children(self.school_page_id)
        )
        status_rank = {"": 0, "To do": 1, "Verify": 2, "Done": 3}
        for course, legacy_database_id in databases.items():
            if course == "General":
                continue
            for page in self.client.query_database_pages(legacy_database_id):
                result.rows_scanned += 1
                source_id = _property_rich_text(page, "Canvas ID")
                name = _property_title(page, "Name").strip()
                if not source_id or not name:
                    continue
                candidate = {
                    "Task": _bounded(name, 500),
                    "Done": _property_select(page, "Status") == "Done",
                    "Status": _property_select(page, "Status") or "To do",
                    "Area": "School",
                    "Course": _bounded(course, 200),
                    "Source type": "Canvas",
                    "Source ID": source_id,
                    "Source URL": _property_url(page, "Canvas") or None,
                    "Due": _property_date_start(page, "Due") or None,
                    "Priority": _property_select(page, "Priority") or "Later",
                    "Effort": _property_select(page, "Effort") or None,
                    "Kind": _property_select(page, "Kind") or "assignment",
                    "Display type": display_task_type(
                        name, _property_select(page, "Kind") or "assignment"
                    ),
                    "Next step": _property_rich_text(page, "Next step"),
                    "Notes / progress": _property_rich_text(page, "Notes / progress"),
                    "Instructions": _property_rich_text(page, "Instructions"),
                    "Needs verification": _property_select(page, "Status") == "Verify",
                    "Locked": False,
                    "Unlock at": None,
                    "Archived": True,
                }
                previous = candidates.get(source_id)
                if previous is None:
                    candidates[source_id] = candidate
                    continue
                if status_rank.get(candidate["Status"], 1) > status_rank.get(
                    previous["Status"], 1
                ):
                    previous["Status"] = candidate["Status"]
                    previous["Done"] = candidate["Done"]
                    previous["Needs verification"] = candidate["Needs verification"]
                for field_name in (
                    "Source URL",
                    "Due",
                    "Effort",
                    "Next step",
                    "Notes / progress",
                    "Instructions",
                ):
                    if not previous.get(field_name) and candidate.get(field_name):
                        previous[field_name] = candidate[field_name]

        result.unique_source_ids = len(candidates)
        for source_id, fields in candidates.items():
            current = existing.get(source_id)
            if current is None:
                fingerprint_fields = {
                    name: value for name, value in fields.items() if name != "Done"
                }
                fields["Sync hash"] = hashlib.sha256(
                    json.dumps(
                        fingerprint_fields,
                        sort_keys=True,
                        default=str,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                self.client.create_master_task(database_id, fields)
                result.rows_created += 1
                continue

            page_id, page = current
            updates: dict[str, Any] = {}
            current_status = _property_select(page, "Status")
            if not current_status and fields["Status"]:
                updates["Status"] = fields["Status"]
                updates["Done"] = fields["Done"]
                updates["Needs verification"] = fields["Needs verification"]
            if (
                not _property_rich_text(page, "Notes / progress")
                and fields["Notes / progress"]
            ):
                updates["Notes / progress"] = fields["Notes / progress"]
            if updates:
                self.client.update_master_task(page_id, updates)
                result.rows_updated += 1
            else:
                result.rows_unchanged += 1
        return result

    def archive_legacy_layout(self) -> LegacyLayoutArchiveResult:
        """Archive superseded databases only after their task rows are preserved.

        The cleanup is deliberately narrow: five known root databases and the
        legacy class databases nested under School.  It refuses to mutate
        anything when a Today or School row cannot be matched to the master
        Tasks database.
        """
        root_databases = self._child_databases(
            self.client.list_block_children(self.parent_page_id)
        )
        master_database_id = root_databases.get(MASTER_TASK_TITLE)
        if not master_database_id:
            raise NotionError("master Tasks database does not exist")

        master_source_ids = {
            source_id
            for page in self.client.query_database_pages(master_database_id)
            if (source_id := _property_rich_text(page, "Source ID"))
        }
        if not master_source_ids:
            raise NotionError("master Tasks database has no preserved source IDs")

        root_titles = (
            "Work",
            "Connections",
            "Misc",
            "Today's Plan",
            "Today's Focus",
        )
        for title in ("Today's Plan", "Today's Focus"):
            database_id = root_databases.get(title)
            if not database_id:
                continue
            for page in self.client.query_database_pages(database_id):
                task_id = _property_rich_text(page, "Task ID")
                if not task_id or task_id not in master_source_ids:
                    raise NotionError(
                        f"cannot archive {title}: row is not preserved in Tasks"
                    )

        school_databases = self._child_databases(
            self.client.list_block_children(self.school_page_id)
        )
        for title, database_id in school_databases.items():
            for page in self.client.query_database_pages(database_id):
                name = _property_title(page, "Name").strip()
                if not name or name.casefold().startswith("example:"):
                    continue
                source_id = _property_rich_text(page, "Canvas ID")
                if not source_id or source_id not in master_source_ids:
                    raise NotionError(
                        f"cannot archive School/{title}: row is not preserved in Tasks"
                    )

        archived_root: list[str] = []
        for title in root_titles:
            database_id = root_databases.get(title)
            if not database_id:
                continue
            self.client.archive_database(database_id)
            archived_root.append(title)

        archived_school: list[str] = []
        for title, database_id in school_databases.items():
            self.client.archive_database(database_id)
            archived_school.append(title)

        return LegacyLayoutArchiveResult(
            root_databases=tuple(archived_root),
            school_databases=tuple(archived_school),
        )

    def sync_master_tasks(
        self,
        assignments: Iterable[Any],
        notion_items: Iterable[NotionWorkItem],
        *,
        details_by_key: dict[str, dict[str, str]] | None = None,
        user_context_by_key: dict[str, dict[str, Any]] | None = None,
        excluded_course_ids: Iterable[int] = (),
        authoritative_canvas: bool = False,
    ) -> MasterTaskSyncResult:
        """Upsert active rows while preserving user status, completion, and notes."""
        root = self.client.retrieve_page(self.parent_page_id)
        dashboard_url = str(root.get("url") or f"https://www.notion.so/{self.parent_page_id}")
        database_id = self._master_task_database()
        created_database = False
        if not database_id:
            database = self.client.create_database(
                MASTER_TASK_TITLE,
                parent_page_id=self.parent_page_id,
                properties=master_task_schema(),
                is_inline=False,
            )
            database_id = str(database["id"])
            created_database = True
        else:
            self.client.ensure_database_properties(database_id, master_task_schema())

        existing: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
        for page in self.client.query_database_pages(database_id):
            source_id = _property_rich_text(page, "Source ID")
            page_id = str(page.get("id") or "")
            source_type = _property_select(page, "Source type")
            if not source_id and page_id and source_type != "Canvas":
                source_id = f"notion:{page_id.replace('-', '')}"
            if source_id and page_id and source_id not in existing:
                existing[source_id] = (
                    page_id,
                    _property_rich_text(page, "Sync hash"),
                    str(page.get("url") or ""),
                    page,
                )

        details = details_by_key or {}
        user_context = user_context_by_key or {}
        excluded = set(excluded_course_ids)
        rows: list[tuple[str, dict[str, Any], bool, str]] = []
        rows.extend(
            (
                item.key,
                canvas_master_task_fields(item, details.get(item.key)),
                user_context.get(item.key, {}).get("status") == "Done",
                _bounded(
                    str(
                        user_context.get(item.key, {}).get("notes")
                        or getattr(item, "user_notes", "")
                    ),
                    1000,
                ),
            )
            for item in assignments
            if item.course_id not in excluded
        )
        rows.extend(
            (item.key, notion_master_task_fields(item, details.get(item.key)), False, "")
            for item in notion_items
        )
        result = MasterTaskSyncResult(
            database_id=database_id,
            page_id=self.parent_page_id,
            url=dashboard_url,
            database_created=created_database,
        )
        current_source_ids = {source_id for source_id, *_rest in rows}
        for source_id, fields, initial_done, initial_notes in rows:
            current = existing.get(source_id)
            if (
                current
                and current[1] == fields["Sync hash"]
                and _property_select(current[3], "Status")
                and not _property_checkbox(current[3], "Archived")
            ):
                result.rows_unchanged += 1
                result.task_urls[source_id] = current[2]
                continue
            if current:
                current_status = _property_select(current[3], "Status")
                if not current_status:
                    current_status = (
                        "Done"
                        if _property_checkbox(current[3], "Done")
                        else "Verify"
                        if _property_checkbox(current[3], "Needs verification")
                        else "To do"
                    )
                update_fields = {name: value for name, value in fields.items() if name != "Status"}
                update_fields["Status"] = current_status
                response = self.client.update_master_task(current[0], update_fields)
                result.rows_updated += 1
                result.task_urls[source_id] = str(response.get("url") or current[2])
            else:
                response = self.client.create_master_task(
                    database_id,
                    {
                        **fields,
                        "Done": initial_done,
                        "Status": "Done" if initial_done else fields["Status"],
                        "Notes / progress": initial_notes,
                    },
                )
                result.rows_created += 1
                result.task_urls[source_id] = str(response.get("url") or "")

        for source_id, (page_id, _sync_hash, _url, page) in existing.items():
            title = _property_title(page, "Task").strip()
            status = _property_select(page, "Status") or (
                "Done" if _property_checkbox(page, "Done") else "To do"
            )
            source_type = _property_select(page, "Source type")
            should_archive = title.casefold().startswith("example:")
            if (
                authoritative_canvas
                and source_type == "Canvas"
                and source_id not in current_source_ids
                and status not in PROTECTED_STALE_STATUSES
            ):
                should_archive = True
            if should_archive and not _property_checkbox(page, "Archived"):
                self.client.update_master_task(page_id, {"Archived": True})
                result.rows_archived += 1
        if hasattr(self.client, "ensure_master_task_views"):
            result.view_urls = self.client.ensure_master_task_views(database_id)
        return result

    def get_master_task_context(self) -> dict[str, dict[str, Any]]:
        database_id = self._master_task_database()
        if not database_id:
            return {}
        context: dict[str, dict[str, Any]] = {}
        for page in self.client.query_database_pages(database_id):
            source_id = _property_rich_text(page, "Source ID")
            if source_id:
                status = _property_select(page, "Status") or (
                    "Done" if _property_checkbox(page, "Done") else "To do"
                )
                context[source_id] = {
                    "done": _property_checkbox(page, "Done") or status == "Done",
                    "status": status,
                    "archived": _property_checkbox(page, "Archived"),
                    "notes": _bounded(
                        _property_rich_text(page, "Notes / progress"), 1000
                    ),
                    "url": str(page.get("url") or ""),
                }
        return context

    def get_master_work(self) -> WorkSnapshot:
        """Read non-Canvas work directly from the migrated source of truth."""
        database_id = self._master_task_database()
        if not database_id:
            raise NotionError("master Tasks database does not exist")
        items: list[NotionWorkItem] = []
        warnings: list[str] = []
        for page in self.client.query_database_pages(database_id):
            source_type = _property_select(page, "Source type")
            status = _property_select(page, "Status") or (
                "Done" if _property_checkbox(page, "Done") else "To do"
            )
            if _property_checkbox(page, "Archived") or status in NON_ACTIONABLE_STATUSES:
                continue
            if source_type == "Canvas" and status != "Needs remake":
                continue
            page_id = str(page.get("id") or "").replace("-", "")
            name = _bounded(_property_title(page, "Task"), 200)
            if not page_id or not name:
                warnings.append("Skipped a master Tasks row without a page id or Task title")
                continue
            source_id = _property_rich_text(page, "Source ID") or f"notion:{page_id}"
            area = _property_select(page, "Area") or "Misc"
            item_type = (
                _property_select(page, "Display type")
                or _property_select(page, "Task type")
                or _property_rich_text(page, "Kind")
            )
            effort = _property_select(page, "Effort") or None
            if effort not in {*EFFORTS, None}:
                warnings.append(f"Master Tasks row {page_id}: unknown Effort was ignored")
                effort = None

            due_start = _property_date_start(page, "Due")
            touched_start = _property_date_start(page, "Last touched")
            try:
                deadline = date.fromisoformat(due_start[:10]) if due_start else None
            except ValueError:
                deadline = None
                warnings.append(f"Master Tasks row {page_id}: invalid Due date was ignored")
            try:
                last_touched = (
                    date.fromisoformat(touched_start[:10]) if touched_start else None
                )
            except ValueError:
                last_touched = None
                warnings.append(
                    f"Master Tasks row {page_id}: invalid Last touched date was ignored"
                )
            items.append(
                NotionWorkItem(
                    key=source_id,
                    page_id=page_id,
                    url=str(page.get("url") or ""),
                    name=name,
                    area=area,
                    type=item_type or None,
                    cadence=_property_select(page, "Cadence") or None,
                    last_touched=last_touched,
                    next_step=clean_notion_next_step(
                        _property_rich_text(page, "Next step")
                    ),
                    deadline=deadline,
                    effort=effort,
                    status=status,
                )
            )
        return WorkSnapshot(items=items, warnings=warnings)

    def sync_master_focus(
        self,
        classification: Any,
        *,
        guidance_by_key: dict[str, str] | None = None,
        focus_keys: list[str] | None = None,
        source_id_by_focus_key: dict[str, str] | None = None,
        focus_reason: str = "",
        target_date: date,
    ) -> MasterFocusSyncResult:
        """Mark only 1–3 master rows for the phone-friendly Today's Focus view."""
        database_id = self._master_task_database()
        if not database_id:
            raise NotionError("master Tasks database does not exist; run migrate-notion --apply")
        root = self.client.retrieve_page(self.parent_page_id)
        dashboard_url = str(root.get("url") or f"https://www.notion.so/{self.parent_page_id}")
        pages = self.client.query_database_pages(database_id)
        existing: dict[str, tuple[str, dict[str, Any]]] = {}
        for page in pages:
            source_id = _property_rich_text(page, "Source ID")
            page_id = str(page.get("id") or "")
            if source_id and page_id and source_id not in existing:
                existing[source_id] = (page_id, page)

        ordered = [*classification.must, *classification.smart, *classification.may]
        items_by_key = {item.key: item for item in ordered}
        if focus_keys:
            requested = focus_keys
        else:
            imminent = next(
                (item for item in ordered if item.kind == "planner_assessment"), None
            )
            requested = (
                [imminent.key]
                + [item.key for item in ordered if item.key != imminent.key][:2]
                if imminent is not None
                else [item.key for item in ordered[:3]]
            )
            if not focus_reason:
                focus_reason = (
                    "An imminent assessment needs study preparation before ordinary overdue work."
                    if imminent is not None
                    else "Start with the nearest required task, then continue only if time remains."
                )
        selected = []
        selected_keys: set[str] = set()
        for key in requested:
            if key in items_by_key and key not in selected_keys and len(selected) < 3:
                selected.append(items_by_key[key])
                selected_keys.add(key)
        if not selected:
            selected = ordered[:3]
            selected_keys = {item.key for item in selected}

        result = MasterFocusSyncResult(self.parent_page_id, dashboard_url)
        guidance = guidance_by_key or {}
        source_aliases = source_id_by_focus_key or {}
        missing: list[str] = []
        selected_source_ids: set[str] = set()
        for rank, item in enumerate(selected, start=1):
            source_id = source_aliases.get(item.key, item.key)
            selected_source_ids.add(source_id)
            current = existing.get(source_id)
            if not current:
                missing.append(item.key)
                continue
            reason = focus_reason if rank == 1 else ""
            fallback_step = (
                "Study the topics listed in the class planner, then do a short practice check."
                if item.kind == "planner_assessment"
                else "Open the task and begin."
            )
            self.client.update_master_task(
                current[0],
                {
                    "Focus date": target_date,
                    "Focus rank": rank,
                    "Focus reason": _bounded(reason, 240),
                    "Priority": item.tier.upper(),
                    "Next step": _bounded(
                        guidance.get(item.key)
                        or item.next_step
                        or fallback_step,
                        1000,
                    ),
                },
            )
            result.rows_focused += 1

        for source_id, (page_id, page) in existing.items():
            if source_id in selected_source_ids:
                continue
            had_focus = bool(
                _property_date_start(page, "Focus date")
                or _property_number(page, "Focus rank") is not None
                or _property_rich_text(page, "Focus reason")
            )
            if not had_focus:
                continue
            self.client.update_master_task(
                page_id,
                {"Focus date": None, "Focus rank": None, "Focus reason": ""},
            )
            result.rows_cleared += 1
        result.missing_source_ids = tuple(missing)
        return result

    def sync_canvas_assignments(
        self,
        assignments: Iterable[Any],
        *,
        details_by_key: dict[str, dict[str, str]] | None = None,
        excluded_course_ids: Iterable[int] = (),
    ) -> SchoolSyncResult:
        excluded = set(excluded_course_ids)
        grouped: dict[tuple[int, str], list[Any]] = {}
        for assignment in assignments:
            if assignment.course_id in excluded:
                continue
            course = _bounded(assignment.course or f"Course {assignment.course_id}", 200)
            grouped.setdefault((assignment.course_id, course), []).append(assignment)

        root = self.client.retrieve_page(self.parent_page_id)
        dashboard_url = str(root.get("url") or f"https://www.notion.so/{self.parent_page_id}")
        databases = self._child_databases(
            self.client.list_block_children(self.school_page_id)
        )
        result = SchoolSyncResult(self.parent_page_id, dashboard_url)
        details = details_by_key or {}

        for (_course_id, course), course_assignments in sorted(
            grouped.items(), key=lambda value: value[0][1].casefold()
        ):
            database_id = databases.get(course)
            if not database_id:
                created = self.client.create_database(
                    course,
                    parent_page_id=self.school_page_id,
                    properties=school_database_schema(),
                    is_inline=True,
                )
                database_id = str(created["id"])
                databases[course] = database_id
                result.databases_created += 1
            else:
                self.client.ensure_database_properties(
                    database_id, {"Notes / progress": {"rich_text": {}}}
                )

            existing_by_source: dict[str, tuple[str, str]] = {}
            for page in self.client.query_database_pages(database_id):
                source_id = _property_rich_text(page, "Canvas ID")
                page_id = str(page.get("id") or "")
                if source_id and page_id and source_id not in existing_by_source:
                    existing_by_source[source_id] = (
                        page_id,
                        _property_rich_text(page, "Sync hash"),
                    )

            for assignment in sorted(
                course_assignments,
                key=lambda item: (item.due_at is None, item.due_at, item.name.casefold()),
            ):
                fields = school_assignment_fields(
                    assignment, details.get(assignment.key)
                )
                current = existing_by_source.get(assignment.key)
                if current and current[1] == fields["Sync hash"]:
                    result.rows_unchanged += 1
                elif current:
                    self.client.update_school_item(current[0], fields)
                    result.rows_updated += 1
                else:
                    self.client.create_school_item(database_id, fields)
                    result.rows_created += 1
        return result

    def get_assignment_context(self) -> dict[str, dict[str, str]]:
        """Read user-owned School fields without treating them as Canvas data."""
        context: dict[str, dict[str, str]] = {}
        databases = self._child_databases(
            self.client.list_block_children(self.school_page_id)
        )
        for title, database_id in databases.items():
            if title == "General":
                continue
            self.client.ensure_database_properties(
                database_id, {"Notes / progress": {"rich_text": {}}}
            )
            for page in self.client.query_database_pages(database_id):
                source_id = _property_rich_text(page, "Canvas ID")
                if not source_id:
                    continue
                context[source_id] = {
                    "status": _property_select(page, "Status"),
                    "notes": _bounded(
                        _property_rich_text(page, "Notes / progress"), 1000
                    ),
                }
        return context

    def _daily_plan_database(self) -> str | None:
        databases = self._child_databases(
            self.client.list_block_children(self.parent_page_id)
        )
        return databases.get(DAILY_PLAN_TITLE)

    def get_daily_plan_context(self) -> dict[str, dict[str, str]]:
        """Read completion state from the generated dashboard without trusting its copy."""
        database_id = self._daily_plan_database()
        if not database_id:
            return {}
        context: dict[str, dict[str, str]] = {}
        for page in self.client.query_database_pages(database_id):
            task_id = _property_rich_text(page, "Task ID")
            if task_id:
                context[task_id] = {"status": _property_select(page, "Status")}
        return context

    def sync_daily_plan(
        self,
        classification: Any,
        *,
        guidance_by_key: dict[str, str] | None = None,
        focus_keys: list[str] | None = None,
        target_date: date,
    ) -> DailyPlanSyncResult:
        """Mirror only the humane 1–3 task focus into its own Notion dashboard."""
        root = self.client.retrieve_page(self.parent_page_id)
        dashboard_url = str(root.get("url") or f"https://www.notion.so/{self.parent_page_id}")
        database_id = self._daily_plan_database()
        created = False
        if not database_id:
            database = self.client.create_database(
                DAILY_PLAN_TITLE,
                parent_page_id=self.parent_page_id,
                properties=daily_plan_schema(),
                is_inline=True,
            )
            database_id = str(database["id"])
            created = True
        else:
            self.client.ensure_database_properties(database_id, daily_plan_schema())

        result = DailyPlanSyncResult(
            self.parent_page_id,
            dashboard_url,
            database_created=created,
        )
        existing: dict[str, tuple[str, str]] = {}
        for page in self.client.query_database_pages(database_id):
            task_id = _property_rich_text(page, "Task ID")
            page_id = str(page.get("id") or "")
            if task_id and page_id:
                existing[task_id] = (page_id, _property_select(page, "Status"))

        guidance = guidance_by_key or {}
        ordered = [*classification.must, *classification.smart, *classification.may]
        items_by_key = {item.key: item for item in ordered}
        requested = focus_keys or [item.key for item in ordered[:3]]
        focus_items = []
        seen_focus: set[str] = set()
        for key in requested:
            if key in items_by_key and key not in seen_focus and len(focus_items) < 3:
                focus_items.append(items_by_key[key])
                seen_focus.add(key)
        if not focus_items:
            focus_items = ordered[:3]

        active_ids: set[str] = set()
        rank = 0
        for item in focus_items:
            rank += 1
            active_ids.add(item.key)
            fields = {
                "Task": _bounded(item.name, 500),
                "Priority": item.tier.upper(),
                "Course / area": _bounded(item.course or item.source.title(), 200),
                "Time (hours)": item.effort_hours,
                "Next step": _bounded(
                    guidance.get(item.key) or item.next_step or "Open the task and begin.",
                    500,
                ),
                "Status": "To do",
                "Source": item.url or None,
                "Plan date": target_date,
                "Task ID": item.key,
                "Rank": rank,
            }
            current = existing.get(item.key)
            if current:
                if current[1] == "Done":
                    fields["Status"] = "Done"
                self.client.update_daily_plan_item(current[0], fields)
                result.rows_updated += 1
            else:
                self.client.create_daily_plan_item(database_id, fields)
                result.rows_created += 1

        for task_id, (page_id, _status) in existing.items():
            if task_id not in active_ids:
                self.client.archive_page(page_id)
                result.rows_archived += 1
        return result


class NotionTaskStore:
    """Treat the four task databases as one logical task collection."""

    def __init__(
        self,
        token: str = "",
        database_ids: dict[str, str] | None = None,
        parent_page_id: str = "",
        *,
        clients: dict[str, Any] | None = None,
    ) -> None:
        if clients is not None:
            self.clients = clients
        else:
            configured = database_ids or {}
            missing = [name for name in TASK_DATABASES if not configured.get(name)]
            if missing:
                raise NotionError(
                    "missing Notion task databases: " + ", ".join(missing)
                )
            self.clients = {
                name: NotionClient(token, configured[name], parent_page_id)
                for name in TASK_DATABASES
            }
        missing_clients = [name for name in TASK_DATABASES if name not in self.clients]
        if missing_clients:
            raise NotionError(
                "missing Notion task database clients: " + ", ".join(missing_clients)
            )

    def get_active_work(self) -> WorkSnapshot:
        items: list[NotionWorkItem] = []
        warnings: list[str] = []
        for name in TASK_DATABASES:
            snapshot = self.clients[name].get_active_work()
            items.extend(item.model_copy(update={"area": name}) for item in snapshot.items)
            warnings.extend(f"{name}: {warning}" for warning in snapshot.warnings)
        return WorkSnapshot(items=items, warnings=warnings)

    def create_work_item(self, fields: dict[str, Any]) -> dict[str, Any]:
        database = str(fields.get("Area") or "Misc")
        if database not in self.clients:
            raise ValueError(f"unknown task database: {database}")
        properties = {name: value for name, value in fields.items() if name != "Area"}
        return self.clients[database].create_work_item(properties)

    def update_work_item(self, page_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if "Area" in fields:
            raise ValueError("moving tasks between Notion databases is not supported")
        return self.clients["Work"].update_work_item(page_id, fields)
