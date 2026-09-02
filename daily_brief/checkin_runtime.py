"""Telegram check-in command runtime."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .atomic import atomic_write_text
from .checkin import (
    apply_operation_plan,
    build_operation_plan,
    extract_checkin_local,
    freeze_replies,
)
from .config import Settings
from .models import DailyBriefState, FailedCheckinBatch
from .notion import NotionTaskStore
from .runtime import normalized_hash
from .state import StateStore
from .telegram import TelegramClient
from .timeutils import utc_now


class CheckinRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        state_store: StateStore | None = None,
        telegram: TelegramClient | None = None,
        notion: NotionTaskStore | None = None,
        state_dir: str | Path = "state",
        ollama_session=None,
    ) -> None:
        self.settings = settings
        self.state_dir = Path(state_dir)
        self.state_store = state_store or StateStore(
            self.state_dir, fallback_work_db_id=settings.notion_work_db_id
        )
        self.telegram = telegram or TelegramClient(
            settings.telegram_bot_token, settings.telegram_chat_id
        )
        self.notion = notion or NotionTaskStore(
            settings.notion_token,
            settings.notion_database_ids,
            settings.notion_parent_page_id,
        )
        self.ollama_session = ollama_session

    def cleanup_quarantine(self, now: datetime) -> None:
        directory = self.state_dir / "failed_checkins"
        if not directory.exists():
            return
        cutoff = now.timestamp() - 7 * 86400
        for path in directory.glob("*.txt"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def _verify_polling(self) -> None:
        info = self.telegram.get_webhook_info()
        if info.get("url"):
            raise RuntimeError("Telegram webhook is configured; polling requires removing it")

    def process(self, *, now: datetime | None = None) -> str:
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
        self.cleanup_quarantine(now)
        state, _ = self.state_store.load()
        if not (
            state.checkin_sent_for
            and state.checkin_sent_at
            and state.checkin_prompt_message_id
        ):
            return "NO_ACTIVE_CHECKIN"
        self._verify_polling()
        updates = self.telegram.get_updates(state.update_id_offset)
        active_failed_key = next(iter(state.failed_checkin_batches), None)
        if active_failed_key:
            failed = state.failed_checkin_batches[active_failed_key]
            wanted = set(failed.update_ids)
            frozen_updates = [value for value in updates if int(value["update_id"]) in wanted]
            frozen = freeze_replies(
                frozen_updates,
                chat_id=self.settings.telegram_chat_id,
                sent_at=state.checkin_sent_at,
            )
            max_safe_update = failed.max_update_id
            batch_key = active_failed_key
        else:
            frozen = freeze_replies(
                updates,
                chat_id=self.settings.telegram_chat_id,
                sent_at=state.checkin_sent_at,
            )
            max_safe_update = frozen.max_update_id
            batch_key = normalized_hash(sorted(frozen.update_ids))
        if not frozen.update_ids:
            if max_safe_update is not None:
                state.update_id_offset = max_safe_update + 1
                self.state_store.save(state)
            return "NO_REPLIES"
        extraction = extract_checkin_local(
            frozen.notes,
            local_today=now.date(),
            checkin_sent_for=state.checkin_sent_for,
            timezone_name=self.settings.timezone,
            model=self.settings.ollama_model,
            ollama_base_url=str(self.settings.ollama_base_url),
            session=self.ollama_session,
        )
        if extraction is None:
            failure = state.failed_checkin_batches.get(batch_key)
            if failure is None:
                failure = FailedCheckinBatch(
                    update_ids=frozen.update_ids,
                    max_update_id=max(frozen.update_ids),
                    attempts=1,
                    last_error_hash=normalized_hash("checkin_validation_failed"),
                )
                state.failed_checkin_batches[batch_key] = failure
            else:
                failure.attempts += 1
            if failure.attempts == 1 and failure.alerted_at is None:
                self.telegram.send_plain("I couldn't read tonight's check-in; your updates were not applied.")
                failure.alerted_at = utc_now()
            if failure.attempts >= 2:
                atomic_write_text(
                    self.state_dir / "failed_checkins" / f"{batch_key}.txt",
                    frozen.notes,
                )
                state.update_id_offset = failure.max_update_id + 1
                state.failed_checkin_batches.pop(batch_key, None)
                self.telegram.send_plain(
                    "I still couldn't read that check-in. Please resend it in a new message."
                )
                self.state_store.save(state)
                return "QUARANTINED"
            self.state_store.save(state)
            return "EXTRACTION_FAILED"

        active_work = self.notion.get_active_work().items
        plan = build_operation_plan(
            extraction,
            active_work,
            state.seen_assignments,
            apply_date=now.date(),
            now=now,
            capacity_for=state.checkin_sent_for,
        )
        apply_operation_plan(
            state,
            plan,
            update_ids=frozen.update_ids,
            apply_date=now.date(),
            notion_client=self.notion,
            persist=self.state_store.save,
        )
        for message in plan.messages:
            self.telegram.send_plain(message)
        state.last_checkin_processed_for = state.checkin_sent_for
        state.update_id_offset = max_safe_update + 1 if max_safe_update is not None else state.update_id_offset
        state.failed_checkin_batches.pop(batch_key, None)
        self.state_store.save(state)
        return "PROCESSED"

    def send(self, *, now: datetime | None = None) -> str:
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
        state, _ = self.state_store.load()
        target = now.date() + timedelta(days=1)
        if state.checkin_sent_for == target and state.checkin_prompt_message_id:
            return "ALREADY_SENT"
        if state.checkin_prompt_message_id:
            result = self.process(now=now)
            if result in {"EXTRACTION_FAILED"}:
                return "PRIOR_CHECKIN_FAILED"
            state, _ = self.state_store.load()
        self._verify_polling()
        updates = self.telegram.get_updates(state.update_id_offset)
        baseline = max((int(value["update_id"]) for value in updates), default=-1) + 1
        work = self.notion.get_active_work().items
        questions = []
        for item in work:
            if not item.next_step.strip():
                questions.append(f"What is the next step for {item.name}?")
        cadence_days = {"Daily": 1, "2x/week": 3.5, "Weekly": 7, "Biweekly": 14}
        for item in work:
            if len(questions) >= 3:
                break
            if item.cadence in cadence_days and (
                item.last_touched is None
                or (target - item.last_touched).days >= cadence_days[item.cadence]
            ):
                question = f"Any progress on {item.name}?"
                if question not in questions:
                    questions.append(question)
        prompt = (
            "Evening check-in: what did you get done today? Anything new on your plate? "
            "How's your bandwidth for tomorrow (low/normal/high)?"
        )
        if questions:
            prompt += "\n" + "\n".join(f"- {value}" for value in questions[:3])
        result = self.telegram.send_plain(prompt)
        if not result.success or result.message_id is None:
            from .models import StateWarning

            state.warnings.append(
                StateWarning(
                    id=f"checkin-send-{utc_now().timestamp()}",
                    created_at=utc_now(),
                    text=(
                        "Evening check-in prompt failed; tomorrow's capacity will default to "
                        "normal unless you retry."
                    ),
                )
            )
            self.state_store.save(state)
            return "SEND_FAILED"
        state.update_id_offset = baseline
        state.checkin_sent_for = target
        state.checkin_sent_at = now
        state.checkin_prompt_message_id = result.message_id
        self.state_store.save(state)
        return "SENT"
