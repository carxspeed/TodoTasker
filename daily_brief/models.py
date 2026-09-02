"""Versioned data contracts shared by all adapters and jobs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class CanvasAssignment(Contract):
    key: str
    source_key: str
    object_id: int | str
    assignment_id: int | None = None
    course_id: int
    name: str = Field(min_length=1, max_length=500)
    course: str
    kind: Literal["assignment", "quiz", "discussion_topic", "sub_assignment"]
    due_at: AwareDatetime | None = None
    points: float | None = None
    url: str = ""
    description: str = Field(default="", max_length=400)
    submission_status: Literal["unsubmitted", "unknown"]
    needs_confirmation: bool = False


class CanvasEvent(Contract):
    title: str
    start: AwareDatetime
    end: AwareDatetime | None = None
    all_day: bool = False
    busy: bool = True
    kind: Literal["calendar_event"] = "calendar_event"


class CanvasReminder(Contract):
    title: str
    date: date
    text: str = ""
    kind: Literal["planner_note"] = "planner_note"


class CanvasPlanner(Contract):
    course: str
    title: str
    url: str
    dates: list[date] = Field(default_factory=list)
    text: str = Field(max_length=1200)


class PlannerEvent(Contract):
    course: str
    title: str
    date: date
    text: str
    url: str


class PlannerObservation(Contract):
    course_id: int
    course: str
    url: str
    target_date: date
    status: Literal["windowed", "empty", "unparsed"]


class Announcement(Contract):
    course: str
    title: str
    text: str = Field(max_length=250)
    posted_at: AwareDatetime


class CanvasSourceStatus(Contract):
    planner_items: Literal["ok", "failed"]
    missing_submissions: Literal["ok", "failed"]
    courses: Literal["ok", "failed"]
    announcements: Literal["ok", "partial", "failed"]


class CanvasEnvelope(Contract):
    schema_version: Literal[1] = 1
    fetched_at: AwareDatetime
    source: Literal["planner_items+missing", "todo_fallback+missing", "fixture"]
    assignments: list[CanvasAssignment] = Field(default_factory=list)
    canvas_events: list[CanvasEvent] = Field(default_factory=list)
    canvas_reminders: list[CanvasReminder] = Field(default_factory=list)
    planners: list[CanvasPlanner] = Field(default_factory=list)
    planner_events: list[PlannerEvent] = Field(default_factory=list)
    planner_observations: list[PlannerObservation] = Field(default_factory=list)
    announcements: list[Announcement] = Field(default_factory=list)
    data_warnings: list[str] = Field(default_factory=list)
    source_status: CanvasSourceStatus


class NotionWorkItem(Contract):
    key: str
    page_id: str
    url: str
    name: str = Field(min_length=1, max_length=200)
    area: str | None = None
    type: str | None = None
    cadence: str | None = None
    last_touched: date | None = None
    next_step: str = Field(default="", max_length=1000)
    deadline: date | None = None
    effort: Literal["S", "M", "L"] | None = None


class CalendarEvent(Contract):
    title: str
    start: AwareDatetime
    end: AwareDatetime
    source: Literal["ical", "canvas"]
    all_day: bool
    busy: bool

    @model_validator(mode="after")
    def chronological(self) -> "CalendarEvent":
        if self.end < self.start:
            raise ValueError("calendar event ends before it starts")
        return self


class FreeWindow(Contract):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def chronological(self) -> "FreeWindow":
        if self.end <= self.start:
            raise ValueError("free window must have positive duration")
        return self


class CalendarSnapshot(Contract):
    schema_version: Literal[1] = 1
    target_date: date
    fetched_at: AwareDatetime
    source_status: Literal["ok"] = "ok"
    events: list[CalendarEvent] = Field(default_factory=list)
    free_windows: list[FreeWindow] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ClassifiedItem(Contract):
    key: str
    source: Literal["canvas", "notion"]
    name: str
    tier: Literal["must", "smart", "may"]
    effort: Literal["S", "M", "L"]
    effort_hours: float = Field(gt=0)
    effort_source: Literal["points", "override", "notion"]
    kind: str = ""
    due_at: AwareDatetime | None = None
    deadline: date | None = None
    course: str = ""
    description: str = ""
    next_step: str = ""
    url: str = ""
    points: float | None = None
    overdue_periods: float = Field(default=0, ge=0)
    promoted: bool = False
    momentum: bool = False
    submission_status: Literal["unsubmitted", "unknown"] | None = None
    needs_confirmation: bool = False


class Promotion(Contract):
    date: date
    key: str
    reason: str


class ClassificationOutput(Contract):
    schema_version: Literal[1] = 1
    target_date: date
    as_of: AwareDatetime
    must: list[ClassifiedItem] = Field(default_factory=list)
    smart: list[ClassifiedItem] = Field(default_factory=list)
    may: list[ClassifiedItem] = Field(default_factory=list)
    verify: list[CanvasAssignment] = Field(default_factory=list)
    promoted: Promotion | None = None
    momentum_deferred: ClassifiedItem | None = None
    overloaded: bool = False
    capacity_exceeded_by_tasks: int = Field(default=0, ge=0)
    capacity_exceeded_by_hours: float = Field(default=0, ge=0)
    unscheduled_required_count: int = Field(default=0, ge=0)
    selected_effort_hours: float = Field(default=0, ge=0)
    available_hours: float = Field(default=0, ge=0)
    dropped_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class GuidanceItem(Contract):
    key: str
    guidance: str = Field(min_length=1, max_length=160)


class GuidanceResult(Contract):
    overview: str = Field(default="", max_length=300)
    task_guidance: list[GuidanceItem] = Field(default_factory=list)


class PreparedSources(Contract):
    canvas: CanvasEnvelope | None = None
    notion: list[NotionWorkItem] | None = None
    calendar: CalendarSnapshot | None = None
    statuses: dict[str, Literal["live", "cached", "unavailable"]]


class PreparedArtifact(Contract):
    schema_version: Literal[1] = 1
    target_date: date
    classification_as_of: AwareDatetime
    prepared_at: AwareDatetime
    rendered_brief: str
    guidance: dict[str, str] = Field(default_factory=dict)
    classification: ClassificationOutput
    sources: PreparedSources
    warnings: list[str] = Field(default_factory=list)
    classification_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Capacity(Contract):
    value: Literal["low", "normal", "high"]
    set_for: date


class SeenAssignment(Contract):
    first_seen: AwareDatetime
    last_seen: AwareDatetime
    course_id: int
    name: str
    due_at: AwareDatetime | None = None
    kind: str
    assignment_id: int | None = None
    source_key: str


class PlannerEmptyStreak(Contract):
    count: int = Field(ge=0)
    last_target_date: date


class StateWarning(Contract):
    id: str
    created_at: AwareDatetime
    text: str


class DeliveryRecord(Contract):
    brief_hash: str | None = None
    telegram_payload_hash: str | None = None
    telegram_message_id: int | None = None
    notion_page_id: str | None = None
    notion_url: str | None = None


class FailedCheckinBatch(Contract):
    update_ids: list[int]
    max_update_id: int
    attempts: int = Field(ge=1)
    last_error_hash: str
    alerted_at: AwareDatetime | None = None


class JournalOperation(Contract):
    kind: str
    target: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "applied", "skipped_needs_user"] = "pending"


class CheckinJournalEntry(Contract):
    update_ids: list[int]
    payload_hash: str
    apply_date: date
    operations: list[JournalOperation]
    completed_at: AwareDatetime | None = None


class SourceLastOk(Contract):
    canvas: AwareDatetime | None = None
    notion: AwareDatetime | None = None
    calendar: AwareDatetime | None = None


class DailyBriefState(Contract):
    schema_version: Literal[2] = 2
    last_generated: AwareDatetime | None = None
    last_delivered: AwareDatetime | None = None
    last_notion_ok: AwareDatetime | None = None
    consecutive_telegram_failures: int = Field(default=0, ge=0)
    capacity: Capacity | None = None
    update_id_offset: int | None = Field(default=None, ge=0)
    checkin_sent_for: date | None = None
    checkin_sent_at: AwareDatetime | None = None
    checkin_prompt_message_id: int | None = None
    last_checkin_processed_for: date | None = None
    seen_assignments: dict[str, SeenAssignment] = Field(default_factory=dict)
    assignment_aliases: dict[str, str] = Field(default_factory=dict)
    effort_overrides: dict[str, Literal["S", "M", "L"]] = Field(default_factory=dict)
    promoted: Promotion | None = None
    planner_empty_streaks: dict[str, PlannerEmptyStreak] = Field(default_factory=dict)
    warnings: list[StateWarning] = Field(default_factory=list)
    work_db_id: str | None = None
    source_last_ok: SourceLastOk = Field(default_factory=SourceLastOk)
    checkin_journal: dict[str, CheckinJournalEntry] = Field(default_factory=dict)
    failed_checkin_batches: dict[str, FailedCheckinBatch] = Field(default_factory=dict)
    deliveries: dict[str, DeliveryRecord] = Field(default_factory=dict)

    @field_validator("work_db_id")
    @classmethod
    def normalize_work_db_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        compact = value.replace("-", "")
        if len(compact) != 32 or any(ch not in "0123456789abcdefABCDEF" for ch in compact):
            raise ValueError("work_db_id must be a Notion id")
        return compact
