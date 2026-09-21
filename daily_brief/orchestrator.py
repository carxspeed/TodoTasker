"""Prepare, deliver, and watchdog orchestration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

from .atomic import atomic_write_json, atomic_write_text
from .calendar import build_calendar_snapshot, fetch_ical
from .canvas import (
    CanvasError,
    CanvasTokenRequest,
    ensure_canvas_session,
    exclude_course_assignments,
    fetch_live,
    load_fixture,
    open_saved_canvas_context,
    verify_session,
)
from .classifier import classify
from .config import Settings
from .guidance import generate_guidance
from .models import (
    CalendarSnapshot,
    CanvasEnvelope,
    CheckinJournalEntry,
    ClassificationOutput,
    DailyBriefState,
    DeliveryRecord,
    GuidanceItem,
    GuidanceResult,
    NotionSnapshot,
    PreparedArtifact,
    PreparedSources,
    SeenAssignment,
)
from .notion import NotionSchoolBoard, NotionTaskStore
from .notification import apply_task_urls, build_daily_notification
from .render import deterministic_guidance, render_brief, resolved_guidance, select_focus_items
from .runtime import SourceCache, alert_incident, normalized_hash, resolve_incident_dir
from .state import StateStore
from .telegram import TelegramClient, render_notification
from .timeutils import utc_now


@dataclass
class SourceBundle:
    canvas: CanvasEnvelope | None
    notion: NotionSnapshot | None
    calendar: CalendarSnapshot | None
    statuses: dict[str, str]
    warnings: list[str]
    errors: dict[str, str] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


def _assessment_focus_aliases(
    classification: ClassificationOutput, canvas: CanvasEnvelope | None
) -> dict[str, str]:
    """Map synthetic planner study prompts onto their matching real assignment row."""
    if canvas is None:
        return {}
    aliases: dict[str, str] = {}
    selected = [*classification.must, *classification.smart, *classification.may]
    for item in selected:
        if item.kind != "planner_assessment":
            continue
        target = re.sub(r"^study\s+for\s+", "", item.name, flags=re.IGNORECASE)
        candidates = [
            assignment
            for assignment in canvas.assignments
            if assignment.course.casefold() == item.course.casefold()
        ]
        scored: list[tuple[float, Any]] = []
        for assignment in candidates:
            score = float(fuzz.token_set_ratio(target, assignment.name))
            if (
                item.due_at is not None
                and assignment.due_at is not None
                and item.due_at.date() == assignment.due_at.date()
            ):
                score += 25.0
            scored.append((score, assignment))
        if not scored:
            continue
        score, match = max(scored, key=lambda value: value[0])
        if score >= 70.0:
            aliases[item.key] = match.key
    return aliases


class LiveSourceProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        fixture: Path | None = None,
        profile: Path | None = None,
    ) -> None:
        self.settings = settings
        self.fixture = fixture
        self.profile = profile

    def _canvas_session_attempt(
        self,
        operation: Callable[[Any], Any],
        *,
        headless: bool,
        renew: bool,
    ) -> Any:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            with open_saved_canvas_context(
                playwright, self.profile, headless=headless
            ) as context:
                if renew:
                    ensure_canvas_session(
                        context,
                        str(self.settings.canvas_base),
                        microsoft_email=self.settings.microsoft_email,
                        microsoft_password=self.settings.microsoft_password,
                    )
                else:
                    verify_session(context.request, str(self.settings.canvas_base))
                return operation(context)

    def _run_canvas_session(self, operation: Callable[[Any], Any]) -> Any:
        retryable = {
            "SESSION_EXPIRED",
            "CANVAS_BROWSER_ERROR",
            "CANVAS_BROWSER_CLOSED",
            "CANVAS_TEMPORARY_FAILURE",
        }
        for attempt in range(2):
            try:
                return self._canvas_session_attempt(
                    operation,
                    headless=True,
                    renew=attempt == 1,
                )
            except CanvasError as exc:
                if attempt or exc.code not in retryable:
                    raise
        raise AssertionError("Canvas session retry loop exited unexpectedly")

    def _fetch_canvas_session(self, target_date: date) -> CanvasEnvelope:
        return self._run_canvas_session(
            lambda context: fetch_live(
                context.request,
                str(self.settings.canvas_base),
                target_date,
                self.settings.timezone,
                excluded_course_ids=self.settings.canvas_excluded_course_ids,
            )
        )

    def fetch_canvas(self, target_date: date) -> CanvasEnvelope:
        if self.fixture:
            return load_fixture(self.fixture)
        if self.settings.canvas_access_token:
            try:
                with CanvasTokenRequest(
                    str(self.settings.canvas_base), self.settings.canvas_access_token
                ) as request:
                    return fetch_live(
                        request,
                        str(self.settings.canvas_base),
                        target_date,
                        self.settings.timezone,
                        excluded_course_ids=self.settings.canvas_excluded_course_ids,
                    )
            except CanvasError as exc:
                if exc.code not in {"CANVAS_TOKEN_INVALID", "SESSION_EXPIRED"}:
                    raise
        return self._fetch_canvas_session(target_date)

    def check_canvas_auth(self) -> str:
        """Return the working auth method without exposing account or credential data."""
        token_failed = False
        if self.settings.canvas_access_token:
            try:
                with CanvasTokenRequest(
                    str(self.settings.canvas_base), self.settings.canvas_access_token
                ) as request:
                    verify_session(request, str(self.settings.canvas_base))
                return "token"
            except CanvasError as exc:
                if exc.code not in {"CANVAS_TOKEN_INVALID", "SESSION_EXPIRED"}:
                    raise
                token_failed = True

        self._run_canvas_session(lambda _context: None)
        return "session_fallback" if token_failed else "session"

    def fetch_notion(self) -> NotionSnapshot:
        if not self.settings.notion_token or not self.settings.notion_databases_configured:
            raise RuntimeError("Notion is not configured")
        work = NotionTaskStore(
            self.settings.notion_token,
            self.settings.notion_database_ids,
            self.settings.notion_parent_page_id,
        ).get_active_work()
        return NotionSnapshot(fetched_at=utc_now(), items=work.items, warnings=work.warnings)

    def fetch_calendar(
        self, target_date: date, canvas_events
    ) -> CalendarSnapshot:
        if not self.settings.ical_url:
            raise RuntimeError("Calendar is not configured")
        return build_calendar_snapshot(
            fetch_ical(self.settings.ical_url),
            target_date,
            timezone_name=self.settings.timezone,
            school_hours=self.settings.school_hours,
            fixed_busy_windows=self.settings.fixed_busy_windows,
            no_school_patterns=self.settings.no_school_patterns,
            informational_patterns=self.settings.informational_all_day_patterns,
            canvas_events=canvas_events,
        )


class DailyBriefOrchestrator:
    def __init__(
        self,
        settings: Settings,
        *,
        state_store: StateStore | None = None,
        cache: SourceCache | None = None,
        state_dir: str | Path = "state",
        guidance_call: Callable[..., GuidanceResult | None] = generate_guidance,
        notion_delivery: NotionSchoolBoard | None = None,
        telegram: TelegramClient | None = None,
    ) -> None:
        self.settings = settings
        self.state_dir = Path(state_dir)
        self.state_store = state_store or StateStore(
            self.state_dir, fallback_work_db_id=settings.notion_work_db_id
        )
        self.cache = cache or SourceCache(self.state_dir / "cache")
        self.guidance_call = guidance_call
        self.notion_delivery = notion_delivery
        self.telegram = telegram

    def fetch_sources(
        self, provider, target_date: date, *, write_cache: bool
    ) -> SourceBundle:
        statuses: dict[str, str] = {}
        warnings: list[str] = []
        errors: dict[str, str] = {}
        diagnostics: list[str] = []
        canvas = None
        notion = None
        calendar = None
        master_layout = False
        if self.notion_delivery is not None:
            try:
                master_layout = self.notion_delivery.master_tasks_enabled()
            except Exception:
                diagnostics.append("MASTER_TASKS_LAYOUT_CHECK_FAILED")
        try:
            canvas = provider.fetch_canvas(target_date)
            statuses["canvas"] = "live"
            assignments_partial = (
                canvas.source_status.planner_items == "failed"
                or canvas.source_status.missing_submissions == "failed"
            )
            partial = (
                assignments_partial
                or canvas.source_status.courses == "failed"
                or canvas.source_status.announcements != "ok"
            )
            prior = self.cache.load(
                "canvas", CanvasEnvelope, target_date=target_date, require_target_match=True
            ) if partial else None
            if prior:
                cached_canvas, cached_at = prior
                updates: dict[str, Any] = {
                    "data_warnings": list(canvas.data_warnings),
                }
                if assignments_partial:
                    assignments = {
                        item.key: item for item in cached_canvas.assignments
                    }
                    assignments.update(
                        {item.key: item for item in canvas.assignments}
                    )
                    updates["assignments"] = sorted(
                        assignments.values(),
                        key=lambda item: (
                            item.due_at is None,
                            item.due_at,
                            item.key,
                        ),
                    )
                diagnostics.append(
                    "CANVAS_PARTIAL_CACHE_MERGE "
                    f"cached_at={cached_at.isoformat()}"
                )
                if canvas.source_status.courses == "failed":
                    updates.update(
                        planners=cached_canvas.planners,
                        planner_events=cached_canvas.planner_events,
                        planner_observations=cached_canvas.planner_observations,
                    )
                if canvas.source_status.announcements != "ok":
                    announcements = {
                        (item.course, item.title, item.posted_at): item
                        for item in cached_canvas.announcements
                    }
                    announcements.update(
                        {
                            (item.course, item.title, item.posted_at): item
                            for item in canvas.announcements
                        }
                    )
                    updates["announcements"] = sorted(
                        announcements.values(),
                        key=lambda item: (item.posted_at, item.course, item.title),
                    )
                canvas = canvas.model_copy(update=updates)
            canvas = exclude_course_assignments(
                canvas, self.settings.canvas_excluded_course_ids
            )
            if write_cache:
                self.cache.save("canvas", canvas, target_date=target_date)
        except Exception as exc:
            error_code = exc.code if isinstance(exc, CanvasError) else "UNEXPECTED_ERROR"
            errors["canvas"] = error_code
            cached = self.cache.load(
                "canvas", CanvasEnvelope, target_date=target_date, require_target_match=True
            )
            cross_date_cache = False
            if cached is None:
                stale = self.cache.load("canvas", CanvasEnvelope)
                if stale is not None:
                    stale_canvas, stale_at = stale
                    cache_age = utc_now() - stale_at
                    if timedelta(0) <= cache_age <= timedelta(hours=72):
                        cached = (stale_canvas, stale_at)
                        cross_date_cache = True
            if cached:
                canvas, cached_at = cached
                canvas = exclude_course_assignments(
                    canvas, self.settings.canvas_excluded_course_ids
                )
                statuses["canvas"] = "stale" if cross_date_cache else "cached"
                if cross_date_cache:
                    diagnostics.append(
                        f"CANVAS_STALE_CACHE error={error_code} cached_at={cached_at.isoformat()}"
                    )
                    warnings.append(
                        "Canvas couldn't refresh; today's plan may be missing recent changes."
                    )
                else:
                    diagnostics.append(
                        f"CANVAS_TARGET_CACHE error={error_code} cached_at={cached_at.isoformat()}"
                    )
                    warnings.append(
                        "Canvas couldn't refresh; saved assignments are being used."
                    )
            else:
                statuses["canvas"] = "unavailable"
                diagnostics.append(f"CANVAS_UNAVAILABLE error={error_code}")
                warnings.append(
                    "Canvas couldn't refresh and no saved assignments are available; "
                    "today's plan may be incomplete."
                )
        if master_layout and self.notion_delivery is not None:
            try:
                master_work = self.notion_delivery.get_master_work()
                notion = NotionSnapshot(
                    fetched_at=utc_now(),
                    items=master_work.items,
                    warnings=master_work.warnings,
                )
                statuses["notion"] = "live"
                if write_cache:
                    self.cache.save("notion", notion)
            except Exception:
                cached = self.cache.load("notion", NotionSnapshot)
                if cached:
                    notion, cached_at = cached
                    statuses["notion"] = "cached"
                    diagnostics.append(
                        f"NOTION_MASTER_CACHE cached_at={cached_at.isoformat()}"
                    )
                    warnings.append("Notion couldn't refresh; saved tasks are being used.")
                else:
                    statuses["notion"] = "unavailable"
                    diagnostics.append("NOTION_MASTER_UNAVAILABLE")
                    warnings.append(
                        "Notion couldn't refresh; personal tasks may be missing from today's plan."
                    )
        else:
            try:
                notion = provider.fetch_notion()
                statuses["notion"] = "live"
                if write_cache:
                    self.cache.save("notion", notion)
            except Exception:
                cached = self.cache.load("notion", NotionSnapshot)
                if cached:
                    notion, cached_at = cached
                    statuses["notion"] = "cached"
                    diagnostics.append(f"NOTION_CACHE cached_at={cached_at.isoformat()}")
                    warnings.append("Notion couldn't refresh; saved tasks are being used.")
                else:
                    statuses["notion"] = "unavailable"
                    diagnostics.append("NOTION_UNAVAILABLE")
                    warnings.append(
                        "Notion couldn't refresh; personal tasks may be missing from today's plan."
                    )
        try:
            calendar = provider.fetch_calendar(target_date, canvas.canvas_events if canvas else [])
            statuses["calendar"] = "live"
            if write_cache:
                self.cache.save("calendar", calendar, target_date=target_date)
        except Exception:
            cached = self.cache.load(
                "calendar", CalendarSnapshot, target_date=target_date, require_target_match=True
            )
            if cached:
                calendar, cached_at = cached
                statuses["calendar"] = "cached"
                diagnostics.append(f"CALENDAR_CACHE cached_at={cached_at.isoformat()}")
                warnings.append(
                    "Calendar couldn't refresh; free-time estimates may be outdated."
                )
            else:
                statuses["calendar"] = "unavailable"
                diagnostics.append("CALENDAR_UNAVAILABLE")
                warnings.append(
                    "Calendar couldn't refresh; a default time estimate is being used."
                )
        if canvas:
            warnings.extend(canvas.data_warnings)
        if notion:
            warnings.extend(notion.warnings)
        if master_layout and self.notion_delivery is not None:
            try:
                master_context = self.notion_delivery.get_master_task_context()
                if canvas is not None:
                    canvas = canvas.model_copy(
                        update={
                            "assignments": [
                                item.model_copy(
                                    update={
                                        "user_notes": master_context.get(item.key, {}).get(
                                            "notes", ""
                                        ),
                                        "manual_status": master_context.get(item.key, {}).get(
                                            "status"
                                        ),
                                    }
                                )
                                for item in canvas.assignments
                                if not master_context.get(item.key, {}).get("done", False)
                                and not master_context.get(item.key, {}).get("archived", False)
                                and master_context.get(item.key, {}).get("status")
                                not in {"Submitted", "Waiting"}
                            ]
                        }
                    )
                if notion is not None:
                    canvas_keys = {
                        item.key for item in canvas.assignments
                    } if canvas is not None else set()
                    notion = notion.model_copy(
                        update={
                            "items": [
                                item
                                for item in notion.items
                                if not master_context.get(item.key, {}).get("done", False)
                                and not master_context.get(item.key, {}).get("archived", False)
                                and master_context.get(item.key, {}).get("status")
                                not in {"Submitted", "Waiting"}
                                and item.key not in canvas_keys
                            ]
                        }
                    )
            except Exception:
                diagnostics.append("MASTER_TASK_CONTEXT_READ_FAILED")
                warnings.append(
                    "Notion progress couldn't refresh; recently completed tasks may still appear."
                )
        elif canvas is not None and self.notion_delivery is not None:
            try:
                school_context = self.notion_delivery.get_assignment_context()
                assignments = []
                for item in canvas.assignments:
                    context = school_context.get(item.key, {})
                    if context.get("status") == "Done":
                        continue
                    assignments.append(
                        item.model_copy(
                            update={"user_notes": context.get("notes", "")}
                        )
                    )
                canvas = canvas.model_copy(update={"assignments": assignments})
            except Exception:
                diagnostics.append("SCHOOL_TASK_CONTEXT_READ_FAILED")
                warnings.append(
                    "School progress couldn't refresh; recently completed tasks may still appear."
                )
        if self.notion_delivery is not None and not master_layout:
            try:
                plan_context = self.notion_delivery.get_daily_plan_context()
                if canvas is not None:
                    canvas = canvas.model_copy(
                        update={
                            "assignments": [
                                item
                                for item in canvas.assignments
                                if plan_context.get(item.key, {}).get("status") != "Done"
                            ]
                        }
                    )
                if notion is not None:
                    notion = notion.model_copy(
                        update={
                            "items": [
                                item
                                for item in notion.items
                                if plan_context.get(item.key, {}).get("status") != "Done"
                            ]
                        }
                    )
            except Exception:
                diagnostics.append("DAILY_PLAN_CONTEXT_READ_FAILED")
                warnings.append(
                    "Today's progress couldn't refresh; completed tasks may still appear."
                )
        if calendar:
            warnings.extend(calendar.warnings)
        return SourceBundle(
            canvas=canvas,
            notion=notion,
            calendar=calendar,
            statuses=statuses,
            warnings=warnings,
            errors=errors,
            diagnostics=diagnostics,
        )

    def _merge_canvas_observations(
        self, state: DailyBriefState, canvas: CanvasEnvelope, observed_at: datetime
    ) -> None:
        for item in canvas.assignments:
            if item.source_key != item.key:
                state.assignment_aliases[item.source_key] = item.key
                old = state.seen_assignments.pop(item.source_key, None)
                if old and item.key not in state.seen_assignments:
                    state.seen_assignments[item.key] = old
                if item.source_key in state.effort_overrides and item.key not in state.effort_overrides:
                    state.effort_overrides[item.key] = state.effort_overrides.pop(item.source_key)
            prior = state.seen_assignments.get(item.key)
            state.seen_assignments[item.key] = SeenAssignment(
                first_seen=prior.first_seen if prior else observed_at,
                last_seen=observed_at,
                course_id=item.course_id,
                name=item.name,
                due_at=item.due_at,
                kind=item.kind,
                assignment_id=item.assignment_id,
                source_key=item.source_key,
            )
        for observation in canvas.planner_observations:
            key = f"{observation.course_id}:{observation.url}"
            prior = state.planner_empty_streaks.get(key)
            if observation.status == "windowed":
                state.planner_empty_streaks.pop(key, None)
            elif prior is None or prior.last_target_date != observation.target_date:
                from .models import PlannerEmptyStreak

                state.planner_empty_streaks[key] = PlannerEmptyStreak(
                    count=(prior.count if prior else 0) + 1,
                    last_target_date=observation.target_date,
                )

    @staticmethod
    def _fingerprint(classification: ClassificationOutput, bundle: SourceBundle) -> str:
        classification_data = classification.model_dump(mode="json")
        classification_data.pop("as_of", None)
        canvas = bundle.canvas.model_dump(mode="json") if bundle.canvas else None
        if canvas:
            canvas.pop("fetched_at", None)
        notion = [item.model_dump(mode="json") for item in bundle.notion.items] if bundle.notion else None
        calendar = bundle.calendar.model_dump(mode="json") if bundle.calendar else None
        if calendar:
            calendar.pop("fetched_at", None)
        return normalized_hash(
            {
                "classification": classification_data,
                "canvas": canvas,
                "notion": notion,
                "calendar": calendar,
                "statuses": bundle.statuses,
                "warnings": bundle.warnings,
            }
        )

    def _classify(
        self,
        state: DailyBriefState,
        bundle: SourceBundle,
        target_date: date,
        as_of: datetime,
    ) -> ClassificationOutput:
        return classify(
            bundle.canvas.assignments if bundle.canvas else [],
            bundle.notion.items if bundle.notion else [],
            target_date=target_date,
            as_of=as_of,
            timezone_name=self.settings.timezone,
            effort_overrides=state.effort_overrides,
            seen_assignments=state.seen_assignments,
            capacity=state.capacity,
            existing_promotion=state.promoted,
            free_windows=bundle.calendar.free_windows if bundle.calendar else None,
            calendar_target_date=bundle.calendar.target_date if bundle.calendar else None,
            planner_events=bundle.canvas.planner_events if bundle.canvas else (),
        )

    @staticmethod
    def _selected(classification: ClassificationOutput):
        return [*classification.must, *classification.smart, *classification.may]

    def prepare(
        self,
        provider,
        *,
        target_date: date,
        as_of: datetime,
        dry_run: bool = False,
    ) -> tuple[PreparedArtifact, DailyBriefState]:
        state, _ = self.state_store.load()
        bundle = self.fetch_sources(provider, target_date, write_cache=not dry_run)
        now = utc_now()
        if bundle.canvas and not dry_run:
            self._merge_canvas_observations(state, bundle.canvas, now)
        classification = self._classify(state, bundle, target_date, as_of)
        selected = self._selected(classification)
        totals = {
            "selected_count": len(selected),
            "selected_effort_hours": classification.selected_effort_hours,
            "available_hours": classification.available_hours,
            "overloaded": classification.overloaded,
            "unscheduled_required_count": classification.unscheduled_required_count,
        }
        result = self.guidance_call(
            selected,
            bundle.calendar.free_windows if bundle.calendar else [],
            totals,
            target_date,
            provider=self.settings.model_provider,
            model=(
                self.settings.ollama_model
                if self.settings.model_provider == "local"
                else self.settings.anthropic_model
            ),
            ollama_base_url=str(self.settings.ollama_base_url),
            anthropic_api_key=self.settings.anthropic_api_key,
        )
        warnings = list(bundle.warnings)
        if result is None:
            warnings.append("Guidance model was unavailable or invalid; deterministic guidance is shown")
        rendered = render_brief(
            classification,
            guidance=result,
            canvas=bundle.canvas,
            calendar=bundle.calendar,
            warnings=warnings,
        )
        model_guidance = {item.key: item.guidance for item in result.task_guidance} if result else {}
        all_guidance = {
            item.key: resolved_guidance(item, model_guidance.get(item.key, ""))
            for item in selected
        }
        artifact = PreparedArtifact(
            target_date=target_date,
            classification_as_of=as_of,
            prepared_at=now,
            rendered_brief=rendered,
            guidance=all_guidance,
            focus=result.focus if result else None,
            classification=classification,
            sources=PreparedSources(
                canvas=bundle.canvas,
                notion=bundle.notion.items if bundle.notion else None,
                calendar=bundle.calendar,
                statuses=bundle.statuses,
            ),
            warnings=warnings,
            classification_input_hash=self._fingerprint(classification, bundle),
        )
        if not dry_run:
            state.promoted = classification.promoted
            state.last_generated = now
            atomic_write_json(
                self.state_dir / "prepared" / f"{target_date.isoformat()}.json",
                artifact.model_dump(mode="json"),
            )
            self.state_store.save(state)
        return artifact, state

    def _load_prepared(self, target_date: date) -> PreparedArtifact | None:
        path = self.state_dir / "prepared" / f"{target_date.isoformat()}.json"
        try:
            artifact = PreparedArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            return artifact if artifact.target_date == target_date else None
        except (OSError, ValueError):
            return None

    def preview_notification(
        self,
        provider,
        *,
        target_date: date,
        as_of: datetime,
    ):
        """Build the Telegram card without cache, state, Notion, or Telegram writes."""

        artifact, _state = self.prepare(
            provider,
            target_date=target_date,
            as_of=as_of,
            dry_run=True,
        )
        guidance = GuidanceResult(
            overview="",
            task_guidance=[
                GuidanceItem(key=key, guidance=value)
                for key, value in artifact.guidance.items()
            ],
            focus=artifact.focus,
        )
        return build_daily_notification(
            artifact.classification,
            guidance=guidance,
            canvas=artifact.sources.canvas,
            warnings=artifact.warnings,
        )

    def deliver(
        self,
        provider,
        *,
        target_date: date,
        as_of: datetime,
        dry_run: bool = False,
    ) -> tuple[str, str, DailyBriefState]:
        state, _ = self.state_store.load()
        prepared = self._load_prepared(target_date)
        bundle = self.fetch_sources(provider, target_date, write_cache=not dry_run)
        now = utc_now()
        if bundle.canvas and not dry_run:
            self._merge_canvas_observations(state, bundle.canvas, now)
        classification = self._classify(state, bundle, target_date, as_of)
        fingerprint = self._fingerprint(classification, bundle)
        unchanged = prepared is not None and prepared.classification_input_hash == fingerprint
        guidance = None
        if unchanged and prepared:
            guidance = GuidanceResult(
                overview="",
                task_guidance=[
                    GuidanceItem(key=item.key, guidance=prepared.guidance[item.key])
                    for item in self._selected(classification)
                    if item.key in prepared.guidance
                ],
                focus=prepared.focus,
            )
        warnings = list(bundle.warnings)
        if state.last_delivered and now - state.last_delivered > timedelta(hours=36):
            warnings.insert(
                0,
                f"No brief has reached you since {state.last_delivered.date().isoformat()} — Telegram delivery may be broken.",
            )
        warnings.extend(value.text for value in state.warnings)
        text = render_brief(
            classification,
            guidance=guidance,
            canvas=bundle.canvas,
            calendar=bundle.calendar,
            warnings=warnings,
            updated=not unchanged,
        )
        if dry_run:
            return text, "dry-run", state

        brief_path = self.state_dir / "briefs" / f"{target_date.isoformat()}.md"
        atomic_write_text(brief_path, text)
        brief_hash = normalized_hash(text)
        delivery = state.deliveries.setdefault(target_date.isoformat(), DeliveryRecord())
        delivery.brief_hash = brief_hash
        notion_url = None
        task_urls: dict[str, str] = {}
        focus_aliases: dict[str, str] = {}
        notification = build_daily_notification(
            classification,
            guidance=guidance,
            canvas=bundle.canvas,
            warnings=warnings,
        )
        if self.notion_delivery is not None:
            try:
                guidance_by_key = (
                    {item.key: item.guidance for item in guidance.task_guidance}
                    if guidance
                    else {}
                )
                selected_items = self._selected(classification)
                guidance_by_key = {
                    item.key: resolved_guidance(
                        item, guidance_by_key.get(item.key, "")
                    )
                    for item in selected_items
                }
                safe_focus, safe_focus_reason = select_focus_items(
                    classification, guidance
                )
                safe_focus_keys = [item.key for item in safe_focus]
                summaries_by_key = (
                    {item.key: item.summary for item in guidance.task_guidance}
                    if guidance
                    else {}
                )
                details_by_key: dict[str, dict[str, str]] = {}
                for priority, items in (
                    ("MUST", classification.must),
                    ("SMART", classification.smart),
                    ("MAY", classification.may),
                ):
                    for item in items:
                        details_by_key[item.key] = {
                            "Priority": priority,
                            "Effort": item.effort,
                            "Next step": guidance_by_key.get(item.key)
                            or deterministic_guidance(item),
                            "Instructions": summaries_by_key.get(item.key, ""),
                        }
                for item in classification.verify:
                    details_by_key.setdefault(
                        item.key,
                        {
                            "Priority": "Verify",
                            "Next step": "Confirm whether this Canvas item is still unfinished.",
                        },
                    )
                master_layout = self.notion_delivery.master_tasks_enabled()
                if master_layout:
                    master_result = self.notion_delivery.sync_master_tasks(
                        bundle.canvas.assignments if bundle.canvas is not None else [],
                        bundle.notion.items if bundle.notion is not None else [],
                        details_by_key=details_by_key,
                        excluded_course_ids=self.settings.canvas_excluded_course_ids,
                        authoritative_canvas=(
                            bundle.statuses.get("canvas") == "live"
                            and bundle.canvas is not None
                            and bundle.canvas.source_status.planner_items == "ok"
                            and bundle.canvas.source_status.missing_submissions == "ok"
                            and bundle.canvas.source_status.courses == "ok"
                        ),
                    )
                    focus_aliases = _assessment_focus_aliases(
                        classification, bundle.canvas
                    )
                    plan_result = self.notion_delivery.sync_master_focus(
                        classification,
                        guidance_by_key=guidance_by_key,
                        focus_keys=safe_focus_keys,
                        focus_reason=safe_focus_reason,
                        source_id_by_focus_key=focus_aliases,
                        target_date=target_date,
                    )
                    notification = apply_task_urls(
                        notification,
                        master_result.task_urls,
                        aliases=focus_aliases,
                    )
                    plan_result = self.notion_delivery.sync_focus_dashboard(
                        notification,
                        full_tasks_url=(
                            "https://www.notion.so/"
                            + master_result.database_id.replace("-", "")
                        ),
                    )
                    notion_result = master_result
                    task_urls = master_result.task_urls
                else:
                    if bundle.canvas is not None:
                        notion_result = self.notion_delivery.sync_canvas_assignments(
                            bundle.canvas.assignments,
                            details_by_key=details_by_key,
                            excluded_course_ids=self.settings.canvas_excluded_course_ids,
                        )
                    else:
                        notion_result = None
                    plan_result = self.notion_delivery.sync_daily_plan(
                        classification,
                        guidance_by_key=guidance_by_key,
                        focus_keys=safe_focus_keys,
                        target_date=target_date,
                    )
                delivery.notion_page_id = plan_result.page_id
                delivery.notion_url = plan_result.url
                notion_url = plan_result.url
                state.last_notion_ok = now
                self.state_store.save(state)
            except Exception:
                notion_url = None
        notification = apply_task_urls(
            notification,
            task_urls,
            aliases=focus_aliases,
        )
        summary = render_notification(notification)
        payload_hash = normalized_hash(
            {
                "text": summary.text,
                "notion_url": notion_url,
                "primary_url": notification.primary.url if notification.primary else None,
            }
        )
        status = "failed"
        success = False
        uncertain = False
        if self.telegram is not None:
            if delivery.telegram_message_id and delivery.telegram_payload_hash == payload_hash:
                status = "skipped"
                success = True
            elif delivery.telegram_message_id:
                _, result = self.telegram.edit_notification(
                    delivery.telegram_message_id,
                    notification,
                    notion_url,
                )
                status = "edited" if result.success else "failed"
                success = result.success
                uncertain = result.uncertain
            else:
                _, result = self.telegram.send_notification(notification, notion_url)
                status = "sent" if result.success else "failed"
                success = result.success
                uncertain = result.uncertain
            if success:
                if status != "skipped" and result.message_id is not None:
                    delivery.telegram_message_id = result.message_id
                delivery.telegram_payload_hash = payload_hash
        if success:
            state.last_delivered = now
            state.consecutive_telegram_failures = 0
            state.warnings = []
        elif uncertain:
            status = "uncertain"
        else:
            state.consecutive_telegram_failures += 1
            if state.consecutive_telegram_failures >= 2:
                directory = resolve_incident_dir(self.settings.incident_dir)
                alert_incident(
                    directory,
                    f"Daily Brief Telegram delivery failed for {target_date.isoformat()}.",
                )
        self.state_store.save(state)
        log_path = self.state_dir / "runs.log"
        prior_log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        line = (
            f"{now.isoformat()} | {'ok' if success else 'partial'} | "
            f"canvas={bundle.statuses['canvas']} notion={bundle.statuses['notion']} "
            f"calendar={bundle.statuses['calendar']} guidance={'prepared' if unchanged else 'deterministic'} "
            f"telegram={status} diff={'unchanged' if unchanged else 'changed'} "
            f"canvas_error={bundle.errors.get('canvas', 'none')} "
            f"diagnostics={','.join(value.split()[0] for value in bundle.diagnostics) or 'none'}\n"
        )
        atomic_write_text(log_path, prior_log + line)
        return text, status, state

    def watchdog(self, target_date: date) -> bool:
        state, _ = self.state_store.load()
        delivery = state.deliveries.get(target_date.isoformat())
        healthy = bool(delivery and delivery.telegram_message_id and state.last_delivered)
        if healthy:
            return False
        directory = resolve_incident_dir(self.settings.incident_dir)
        alert_incident(
            directory,
            f"No Telegram Daily Brief delivery is recorded for {target_date.isoformat()}.",
        )
        return True
