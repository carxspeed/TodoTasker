from datetime import date, datetime, timezone
from pathlib import Path

from daily_brief.checkin_runtime import CheckinRunner
from daily_brief.config import load_settings
from daily_brief.models import DailyBriefState
from daily_brief.state import StateStore


class Telegram:
    def __init__(self, updates):
        self.updates = updates
        self.messages = []

    def get_webhook_info(self):
        return {"url": ""}

    def get_updates(self, offset):
        return [value for value in self.updates if offset is None or value["update_id"] >= offset]

    def send_plain(self, text):
        self.messages.append(text)
        return type("Result", (), {"success": True, "message_id": 9})()


class Notion:
    def get_active_work(self):
        return type("Snapshot", (), {"items": []})()


class Response:
    status_code = 200

    def json(self):
        return {"message": {"content": "not json"}}

    def raise_for_status(self):
        return None


class BadOllama:
    def get(self, *args, **kwargs):
        return Response()

    def post(self, *args, **kwargs):
        return Response()


def test_no_reply_drain_makes_no_model_call_and_advances_ignored(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    state = DailyBriefState(
        checkin_sent_for=date(2026, 9, 2),
        checkin_sent_at=datetime(2026, 9, 1, 21, tzinfo=timezone.utc),
        checkin_prompt_message_id=1,
    )
    store.save(state)
    telegram = Telegram([{"update_id": 4, "message": {"chat": {"id": 999}, "date": 0, "text": "other"}}])
    runner = CheckinRunner(
        load_settings(tmp_path / "missing.env"),
        state_store=store,
        telegram=telegram,
        notion=Notion(),
        state_dir=tmp_path / "state",
        ollama_session=object(),
    )
    assert runner.process(now=datetime(2026, 9, 1, 22, tzinfo=timezone.utc)) == "NO_REPLIES"
    assert store.load()[0].update_id_offset == 5


def test_two_failed_jobs_quarantine_exact_batch_and_advance(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    sent = datetime(2026, 9, 1, 21, tzinfo=timezone.utc)
    store.save(
        DailyBriefState(
            checkin_sent_for=date(2026, 9, 2),
            checkin_sent_at=sent,
            checkin_prompt_message_id=1,
        )
    )
    update = {
        "update_id": 10,
        "message": {"chat": {"id": ""}, "date": int(sent.timestamp()), "text": "private reply"},
    }
    telegram = Telegram([update])
    runner = CheckinRunner(
        load_settings(tmp_path / "missing.env"),
        state_store=store,
        telegram=telegram,
        notion=Notion(),
        state_dir=tmp_path / "state",
        ollama_session=BadOllama(),
    )
    assert runner.process(now=sent) == "EXTRACTION_FAILED"
    assert store.load()[0].update_id_offset is None
    assert runner.process(now=sent) == "QUARANTINED"
    assert store.load()[0].update_id_offset == 11
    files = list((tmp_path / "state" / "failed_checkins").glob("*.txt"))
    assert len(files) == 1 and "private reply" in files[0].read_text(encoding="utf-8")

