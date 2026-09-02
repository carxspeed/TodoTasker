from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_brief.canvas import load_fixture
from daily_brief.config import load_settings
from daily_brief.models import (
    CalendarSnapshot,
    DailyBriefState,
    GuidanceItem,
    GuidanceResult,
    NotionSnapshot,
)
from daily_brief.notion import BriefPageResult
from daily_brief.orchestrator import DailyBriefOrchestrator
from daily_brief.state import StateStore
from daily_brief.telegram import TelegramResult, build_summary


TARGET = date(2026, 9, 2)
TZ = ZoneInfo("America/Los_Angeles")


class Provider:
    def __init__(self):
        self.canvas = load_fixture("fixtures/sample_todo.json")
        self.notion = NotionSnapshot(
            fetched_at=datetime(2026, 9, 1, tzinfo=timezone.utc), items=[]
        )
        self.calendar = CalendarSnapshot(
            target_date=TARGET,
            fetched_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

    def fetch_canvas(self, target):
        return self.canvas

    def fetch_notion(self):
        return self.notion

    def fetch_calendar(self, target, canvas_events):
        return self.calendar


class Guidance:
    def __init__(self):
        self.calls = 0

    def __call__(self, selected, windows, totals, target, **kwargs):
        self.calls += 1
        return GuidanceResult(
            overview="Use the first open block.",
            task_guidance=[
                GuidanceItem(key=item.key, guidance="Begin with the first requirement.")
                for item in selected
            ],
        )


class NotionDelivery:
    def __init__(self):
        self.calls = 0

    def upsert_brief_page(self, *args, **kwargs):
        self.calls += 1
        return BriefPageResult("page", "https://notion.test/page")


class Telegram:
    def __init__(self):
        self.sent = 0
        self.edited = 0

    def send_brief(self, text, notion_url, *, local_path):
        self.sent += 1
        return build_summary(text, notion_url, local_path=local_path), TelegramResult(True, 44)

    def edit_brief(self, message_id, text, notion_url, *, local_path):
        self.edited += 1
        return build_summary(text, notion_url, local_path=local_path), TelegramResult(True, message_id)


def make_orchestrator(tmp_path: Path, guidance=None, notion=None, telegram=None):
    settings = load_settings(tmp_path / "missing.env")
    store = StateStore(tmp_path / "state")
    store.save(DailyBriefState())
    return DailyBriefOrchestrator(
        settings,
        state_store=store,
        state_dir=tmp_path / "state",
        guidance_call=guidance or Guidance(),
        notion_delivery=notion,
        telegram=telegram,
    )


def test_prepare_dry_run_makes_one_call_and_writes_nothing(tmp_path: Path) -> None:
    guidance = Guidance()
    orchestrator = make_orchestrator(tmp_path, guidance)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    artifact, _ = orchestrator.prepare(
        Provider(),
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 6, 30, tzinfo=TZ),
        dry_run=True,
    )
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert guidance.calls == 1
    assert "Synthetic physics worksheet" in artifact.rendered_brief
    assert before == after


def test_prepare_writes_valid_artifact_and_cache(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    artifact, state = orchestrator.prepare(
        Provider(),
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 6, 30, tzinfo=TZ),
    )
    assert (tmp_path / "state" / "prepared" / "2026-09-02.json").exists()
    assert (tmp_path / "state" / "cache" / "canvas.json").exists()
    assert state.last_generated is not None
    assert len(artifact.classification_input_hash) == 64


def test_delivery_reuses_prepared_guidance_and_same_payload_skips(tmp_path: Path) -> None:
    guidance = Guidance()
    notion = NotionDelivery()
    telegram = Telegram()
    orchestrator = make_orchestrator(tmp_path, guidance, notion, telegram)
    provider = Provider()
    orchestrator.prepare(
        provider,
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 6, 30, tzinfo=TZ),
    )
    first_text, first_status, _ = orchestrator.deliver(
        provider,
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 7, 0, tzinfo=TZ),
    )
    second_text, second_status, state = orchestrator.deliver(
        provider,
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 7, 5, tzinfo=TZ),
    )
    assert guidance.calls == 1
    assert first_status == "sent"
    assert second_status == "skipped"
    assert telegram.sent == 1 and telegram.edited == 0
    assert first_text == second_text
    assert state.last_delivered is not None


def test_changed_deadline_gets_updated_header_without_guidance_call(tmp_path: Path) -> None:
    guidance = Guidance()
    telegram = Telegram()
    orchestrator = make_orchestrator(tmp_path, guidance, NotionDelivery(), telegram)
    provider = Provider()
    orchestrator.prepare(
        provider,
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 6, 30, tzinfo=TZ),
    )
    provider.canvas.assignments[0].name = "Changed canonical title"
    text, status, _ = orchestrator.deliver(
        provider,
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 7, 0, tzinfo=TZ),
    )
    assert "Updated this morning" in text
    assert "Changed canonical title" in text
    assert guidance.calls == 1

