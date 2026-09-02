"""Prepare, deliver, and watchdog orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .atomic import atomic_write_json, atomic_write_text
from .calendar import build_calendar_snapshot, fetch_ical
from .canvas import fetch_live, load_fixture
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
from .notion import NotionClient
from .render import deterministic_guidance, render_brief
from .runtime import SourceCache, alert_incident, normalized_hash, resolve_incident_dir
from .state import StateStore
from .telegram import TelegramClient, build_summary
from .timeutils import utc_now


@dataclass
class SourceBundle:
    canvas: CanvasEnvelope | None
    notion: NotionSnapshot | None
    calendar: CalendarSnapshot | None
    statuses: dict[str, str]
    warnings: list[str]


class LiveSourceProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        fixture: Path | None = None,
        profile: Path = Path("profile"),
    ) -> None:
        self.settings = settings
        self.fixture = fixture
        self.profile = profile

    def fetch_canvas(self, target_date: date) -> CanvasEnvelope:
        if self.fixture:
            return load_fixture(self.fixture)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile.resolve()), headless=True
            )
            try:
                return fetch_live(
                    context.request,
                    str(self.settings.canvas_base),
                    target_date,
                    self.settings.timezone,
                )
            finally:
                context.close()

    def fetch_notion(self) -> NotionSnapshot:
        if not self.settings.notion_token or not self.settings.notion_work_db_id:
            raise RuntimeError("Notion is not configured")
        work = NotionClient(
            self.settings.notion_token,
            self.settings.notion_work_db_id,
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
        notion_delivery: NotionClient | None = None,
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
        canvas = None
        notion = None
        calendar = None
        try:
            canvas = provider.fetch_canvas(target_date)
            statuses["canvas"] = "live"
            if write_cache:
                self.cache.save("canvas", canvas, target_date=target_date)
        except Exception:
            cached = self.cache.load(
                "canvas", CanvasEnvelope, target_date=target_date, require_target_match=True
            )
            if cached:
                canvas, cached_at = cached
                statuses["canvas"] = "cached"
                warnings.append(f"Canvas is cached from {cached_at.isoformat()}")
            else:
                statuses["canvas"] = "unavailable"
                warnings.append("Canvas is unavailable; assignments were not treated as an empty success")
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
                warnings.append(f"Notion is cached from {cached_at.isoformat()}")
            else:
                statuses["notion"] = "unavailable"
                warnings.append("Notion is unavailable; Work was not treated as an empty success")
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
                warnings.append(f"Calendar is cached from {cached_at.isoformat()}")
            else:
                statuses["calendar"] = "unavailable"
                warnings.append("Calendar is unavailable; nominal capacity is being used")
        if canvas:
            warnings.extend(canvas.data_warnings)
        if notion:
            warnings.extend(notion.warnings)
        if calendar:
            warnings.extend(calendar.warnings)
        return SourceBundle(canvas, notion, calendar, statuses, warnings)

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
            item.key: model_guidance.get(item.key, deterministic_guidance(item)) for item in selected
        }
        artifact = PreparedArtifact(
            target_date=target_date,
            classification_as_of=as_of,
            prepared_at=now,
            rendered_brief=rendered,
            guidance=all_guidance,
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
        if self.notion_delivery is not None:
            try:
                notion_result = self.notion_delivery.upsert_brief_page(
                    target_date,
                    text,
                    journal_dir=self.state_dir / "notion_updates",
                    stored_page_id=delivery.notion_page_id,
                    stored_url=delivery.notion_url or "",
                )
                delivery.notion_page_id = notion_result.page_id
                delivery.notion_url = notion_result.url
                notion_url = notion_result.url
                state.last_notion_ok = now
                self.state_store.save(state)
            except Exception:
                notion_url = None
        summary = build_summary(text, notion_url, local_path=brief_path)
        payload_hash = normalized_hash(
            {"text": summary.text, "notion_url": notion_url, "keyboard": bool(notion_url)}
        )
        status = "failed"
        success = False
        if self.telegram is not None:
            if delivery.telegram_message_id and delivery.telegram_payload_hash == payload_hash:
                status = "skipped"
                success = True
            elif delivery.telegram_message_id:
                _, result = self.telegram.edit_brief(
                    delivery.telegram_message_id,
                    text,
                    notion_url,
                    local_path=brief_path,
                )
                status = "edited" if result.success else "failed"
                success = result.success
            else:
                _, result = self.telegram.send_brief(
                    text, notion_url, local_path=brief_path
                )
                status = "sent" if result.success else "failed"
                success = result.success
            if success:
                if status != "skipped" and result.message_id is not None:
                    delivery.telegram_message_id = result.message_id
                delivery.telegram_payload_hash = payload_hash
        if success:
            state.last_delivered = now
            state.consecutive_telegram_failures = 0
            state.warnings = []
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
            f"telegram={status} diff={'unchanged' if unchanged else 'changed'}\n"
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

