"""Local-only evening note extraction, conservative matching, and replayable apply."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

import jsonschema
import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from rapidfuzz import fuzz

from .models import (
    Capacity,
    CheckinJournalEntry,
    DailyBriefState,
    JournalOperation,
    NotionWorkItem,
    SeenAssignment,
)
from .timeutils import utc_now


AREAS = Literal["MindSpark", "Biosensor", "GoDaddy", "AI Club", "College Apps", "Personal"]


class ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewItem(ExtractionModel):
    name: str = Field(min_length=1)
    area: AREAS
    deadline: str | None


class NextStep(ExtractionModel):
    project: str = Field(min_length=1)
    step: str = Field(min_length=1)


class EffortCorrection(ExtractionModel):
    task: str = Field(min_length=1)
    effort: Literal["S", "M", "L"]


class CheckinExtraction(ExtractionModel):
    capacity: Literal["low", "normal", "high"] | None
    done: list[str]
    new_items: list[NewItem]
    next_steps: list[NextStep]
    unknown_next_step: list[str]
    effort_corrections: list[EffortCorrection]

    @model_validator(mode="after")
    def no_cross_array_collisions(self) -> "CheckinExtraction":
        groups = [
            {normalize_name(value) for value in self.done},
            {normalize_name(value.project) for value in self.next_steps},
            {normalize_name(value) for value in self.unknown_next_step},
        ]
        if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
            raise ValueError("a target appears in conflicting update arrays")
        for item in self.new_items:
            if item.deadline is not None:
                parsed = date.fromisoformat(item.deadline)
                if parsed.isoformat() != item.deadline:
                    raise ValueError("deadline must be exact YYYY-MM-DD")
        return self


EXTRACTION_PROMPT = """Extract from the user's evening notes. Reply with ONLY valid JSON, no other text:
{"capacity":null,"done":["..."],"new_items":[{"name":"...","area":"Personal","deadline":null}],"next_steps":[{"project":"...","step":"..."}],"unknown_next_step":["..."],"effort_corrections":[{"task":"...","effort":"S|M|L"}]}
Each normalized task/project name may appear in only one of done, next_steps, or unknown_next_step. If the user is uncertain about a next step, use only unknown_next_step. If the user says a task took much more or less time than expected, record it in effort_corrections using non-overlapping ranges: S = under 45 minutes, M = 45 minutes to under 2 hours, L = 2 hours or more. If bandwidth is not stated or clearly implied, use null. For a new item whose area is not stated, use Personal. Resolve relative deadline words against local_today in the supplied timezone; if the date remains ambiguous, use null. Deadlines must be YYYY-MM-DD or null."""


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(" " if unicodedata.category(char).startswith("P") else char for char in normalized)
    return re.sub(r"\s+", " ", normalized).strip()


@dataclass(frozen=True)
class MatchResult:
    status: Literal["exact", "fuzzy", "ambiguous", "unmatched"]
    key: str | None = None
    score: float = 0


def conservative_match(query: str, candidates: dict[str, str]) -> MatchResult:
    normalized_query = normalize_name(query)
    normalized = {key: normalize_name(title) for key, title in candidates.items()}
    exact = [key for key, title in normalized.items() if title == normalized_query]
    if len(exact) == 1:
        return MatchResult("exact", exact[0], 100)
    if len(exact) > 1:
        return MatchResult("ambiguous", score=100)
    scored: list[tuple[float, str]] = []
    for key, title in normalized.items():
        if not title or not normalized_query:
            continue
        length_ratio = min(len(title), len(normalized_query)) / max(len(title), len(normalized_query))
        if length_ratio < 0.80:
            continue
        scored.append((float(fuzz.ratio(normalized_query, title)), key))
    scored.sort(key=lambda value: (-value[0], value[1]))
    if not scored or scored[0][0] < 90:
        return MatchResult("unmatched", score=scored[0][0] if scored else 0)
    runner_up = scored[1][0] if len(scored) > 1 else 0
    if scored[0][0] - runner_up < 10:
        return MatchResult("ambiguous", score=scored[0][0])
    return MatchResult("fuzzy", scored[0][1], scored[0][0])


def extract_checkin_local(
    notes: str,
    *,
    local_today: date,
    checkin_sent_for: date,
    timezone_name: str,
    model: str = "qwen36:latest",
    ollama_base_url: str = "http://localhost:11434",
    session=None,
) -> CheckinExtraction | None:
    client = session or requests.Session()
    schema = CheckinExtraction.model_json_schema()
    user = {
        "local_today": local_today.isoformat(),
        "checkin_sent_for": checkin_sent_for.isoformat(),
        "timezone": timezone_name,
        "notes": notes,
        "schema": schema,
    }
    try:
        tags = client.get(f"{ollama_base_url.rstrip('/')}/api/tags", timeout=3)
        if tags.status_code >= 400:
            return None
        response = client.post(
            f"{ollama_base_url.rstrip('/')}/api/chat",
            json={
                "model": model,
                "stream": False,
                "think": False,
                "format": schema,
                "messages": [
                    {"role": "system", "content": EXTRACTION_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
                    },
                ],
            },
            timeout=1800,
        )
        response.raise_for_status()
        text = response.json()["message"]["content"]
        parsed = json.loads(text)
        jsonschema.validate(parsed, schema)
        return CheckinExtraction.model_validate(parsed)
    except (
        requests.RequestException,
        KeyError,
        TypeError,
        ValueError,
        ValidationError,
        jsonschema.ValidationError,
    ):
        return None


@dataclass
class FrozenReplies:
    update_ids: list[int]
    notes: str
    max_update_id: int | None


def freeze_replies(
    updates: list[dict[str, Any]], *, chat_id: str, sent_at: datetime
) -> FrozenReplies:
    relevant: list[tuple[int, str]] = []
    for update in updates:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id")) != str(chat_id) or not isinstance(message.get("text"), str):
            continue
        if datetime.fromtimestamp(int(message.get("date", 0)), tz=sent_at.tzinfo) < sent_at:
            continue
        relevant.append((int(update["update_id"]), message["text"]))
    relevant.sort()
    notes = "\n".join(f"[update_id={update_id}] {text}" for update_id, text in relevant)
    return FrozenReplies(
        update_ids=[update_id for update_id, _ in relevant],
        notes=notes,
        max_update_id=max((int(value["update_id"]) for value in updates), default=None),
    )


@dataclass
class OperationPlan:
    operations: list[JournalOperation] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def build_operation_plan(
    extraction: CheckinExtraction,
    active_work: list[NotionWorkItem],
    seen_assignments: dict[str, SeenAssignment],
    *,
    apply_date: date,
    now: datetime,
    capacity_for: date,
) -> OperationPlan:
    plan = OperationPlan()
    work_titles = {item.page_id: item.name for item in active_work}

    def work_operation(kind: str, query: str, payload: dict[str, Any]) -> None:
        match = conservative_match(query, work_titles)
        if match.key:
            plan.operations.append(JournalOperation(kind=kind, target=match.key, payload=payload))
        else:
            plan.operations.append(
                JournalOperation(
                    kind=kind,
                    target=query,
                    payload=payload,
                    status="skipped_needs_user",
                )
            )
            plan.messages.append(f"Please resend the exact full Notion title for “{query}”.")

    for value in extraction.done:
        work_operation("done", value, {"Status": "Done", "Last touched": apply_date.isoformat()})
    for value in extraction.next_steps:
        work_operation(
            "next_step",
            value.project,
            {"Next step": value.step, "Last touched": apply_date.isoformat()},
        )
    for value in extraction.unknown_next_step:
        work_operation(
            "unknown_next_step",
            value,
            {"Next step": "UNKNOWN — needs 10 min scoping", "Last touched": apply_date.isoformat()},
        )
    for value in extraction.new_items:
        duplicate = conservative_match(value.name, work_titles)
        if duplicate.key:
            continue
        plan.operations.append(
            JournalOperation(
                kind="new_item",
                target=value.name,
                payload={
                    "Name": value.name,
                    "Area": value.area,
                    "Type": "Task",
                    "Cadence": "None",
                    "Last touched": apply_date.isoformat(),
                    "Status": "Active",
                    "Deadline": value.deadline,
                },
            )
        )
    recent_titles = {
        key: value.name
        for key, value in seen_assignments.items()
        if value.last_seen >= now - timedelta(days=7)
    }
    for value in extraction.effort_corrections:
        match = conservative_match(value.task, recent_titles)
        if match.key:
            plan.operations.append(
                JournalOperation(kind="effort", target=match.key, payload={"effort": value.effort})
            )
        else:
            plan.operations.append(
                JournalOperation(
                    kind="effort",
                    target=value.task,
                    payload={"effort": value.effort},
                    status="skipped_needs_user",
                )
            )
            plan.messages.append(f"Please resend the exact full Canvas title for “{value.task}”.")
    if extraction.capacity is not None:
        plan.operations.append(
            JournalOperation(
                kind="capacity",
                target=capacity_for.isoformat(),
                payload={"value": extraction.capacity, "set_for": capacity_for.isoformat()},
            )
        )
    return plan


def apply_operation_plan(
    state: DailyBriefState,
    plan: OperationPlan,
    *,
    update_ids: list[int],
    apply_date: date,
    notion_client,
    persist,
) -> str:
    payload = json.dumps(
        [
            {
                "kind": operation.kind,
                "target": operation.target,
                "payload": operation.payload,
            }
            for operation in plan.operations
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    journal_key = hashlib.sha256(
        ",".join(str(value) for value in sorted(update_ids)).encode("ascii")
    ).hexdigest()
    journal = state.checkin_journal.get(journal_key)
    if journal is None:
        journal = CheckinJournalEntry(
            update_ids=sorted(update_ids),
            payload_hash=payload_hash,
            apply_date=apply_date,
            operations=plan.operations,
        )
        state.checkin_journal[journal_key] = journal
        persist(state)
    elif journal.payload_hash != payload_hash:
        raise ValueError("journal payload changed for the same Telegram updates")

    for operation in journal.operations:
        if operation.status != "pending":
            continue
        if operation.kind in {"done", "next_step", "unknown_next_step"}:
            notion_client.update_work_item(operation.target, operation.payload)
        elif operation.kind == "new_item":
            current = notion_client.get_active_work().items
            duplicate = next(
                (
                    item
                    for item in current
                    if normalize_name(item.name) == normalize_name(operation.payload["Name"])
                    and item.last_touched == apply_date
                ),
                None,
            )
            if duplicate is None:
                notion_client.create_work_item(operation.payload)
        elif operation.kind == "effort":
            state.effort_overrides[operation.target] = operation.payload["effort"]
        elif operation.kind == "capacity":
            state.capacity = Capacity.model_validate(operation.payload)
        else:
            raise ValueError(f"unknown journal operation {operation.kind}")
        operation.status = "applied"
        persist(state)
    journal.completed_at = utc_now()
    persist(state)
    return journal_key
