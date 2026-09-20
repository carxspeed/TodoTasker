import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import daily_brief.orchestrator as orchestrator_module
from daily_brief.canvas import CanvasError, load_fixture
from daily_brief.config import load_settings
from daily_brief.models import (
    CalendarSnapshot,
    DailyBriefState,
    GuidanceItem,
    GuidanceResult,
    NotionSnapshot,
)
from daily_brief.notion import SchoolSyncResult
from daily_brief.orchestrator import DailyBriefOrchestrator, LiveSourceProvider
from daily_brief.orchestrator import _assessment_focus_aliases
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
        self.context = {}
        self.plan_context = {}
        self.plan_calls = 0

    def get_assignment_context(self):
        return self.context

    def master_tasks_enabled(self):
        return False

    def get_daily_plan_context(self):
        return self.plan_context

    def sync_canvas_assignments(self, *args, **kwargs):
        self.calls += 1
        return SchoolSyncResult("page", "https://notion.test/page")

    def sync_daily_plan(self, *args, **kwargs):
        self.plan_calls += 1
        return SchoolSyncResult("page", "https://notion.test/page")


class MasterNotionDelivery(NotionDelivery):
    def __init__(self):
        super().__init__()
        self.master_context = {}
        self.master_items = []
        self.master_sync_calls = 0
        self.master_focus_calls = 0

    def master_tasks_enabled(self):
        return True

    def get_master_task_context(self):
        return self.master_context

    def get_master_work(self):
        from daily_brief.notion import WorkSnapshot

        return WorkSnapshot(items=self.master_items)

    def get_assignment_context(self):
        raise AssertionError("legacy School tables must not be read in master mode")

    def get_daily_plan_context(self):
        raise AssertionError("legacy Today's Focus must not be read in master mode")

    def sync_canvas_assignments(self, *args, **kwargs):
        raise AssertionError("legacy class tables must not be synced in master mode")

    def sync_daily_plan(self, *args, **kwargs):
        raise AssertionError("duplicate focus rows must not be created in master mode")

    def sync_master_tasks(self, *args, **kwargs):
        self.master_sync_calls += 1
        return SchoolSyncResult("page", "https://notion.test/page")

    def sync_master_focus(self, *args, **kwargs):
        self.master_focus_calls += 1
        return SchoolSyncResult("page", "https://notion.test/page")


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


class UncertainTelegram(Telegram):
    def send_brief(self, text, notion_url, *, local_path):
        self.sent += 1
        return build_summary(text, notion_url, local_path=local_path), TelegramResult(
            False, error="transport", uncertain=True
        )


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


def test_live_source_provider_prefers_canvas_token(tmp_path: Path, monkeypatch) -> None:
    class Vault:
        def get_many(self, names):
            return {"CANVAS_ACCESS_TOKEN": "test-canvas-token"}

    settings = load_settings(tmp_path / "missing.env", secret_vault=Vault())
    opened = []

    class TokenRequest:
        def __init__(self, base_url, token):
            opened.append((base_url, token))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    expected = load_fixture("fixtures/sample_todo.json")
    monkeypatch.setattr(orchestrator_module, "CanvasTokenRequest", TokenRequest)
    monkeypatch.setattr(orchestrator_module, "fetch_live", lambda *args, **kwargs: expected)

    result = LiveSourceProvider(settings).fetch_canvas(TARGET)

    assert result is expected
    assert opened == [("https://issaquah.instructure.com/", "test-canvas-token")]


def test_assessment_focus_maps_to_matching_real_assignment() -> None:
    from daily_brief.models import ClassificationOutput, ClassifiedItem

    canvas = load_fixture("fixtures/sample_todo.json")
    quiz = canvas.assignments[0].model_copy(
        update={"key": "assignment:quiz-2", "name": "Quiz 2", "course": "Calculus"}
    )
    canvas = canvas.model_copy(update={"assignments": [quiz]})
    assessment = ClassifiedItem(
        key="planner-assessment:quiz-2",
        source="canvas",
        name="Study for Quiz 2",
        tier="must",
        effort="M",
        effort_hours=1.5,
        effort_source="points",
        kind="planner_assessment",
        due_at=quiz.due_at,
        course="Calculus",
    )
    classification = ClassificationOutput(
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, tzinfo=timezone.utc),
        must=[assessment],
    )

    assert _assessment_focus_aliases(classification, canvas) == {
        assessment.key: quiz.key
    }


def test_live_source_provider_uses_encrypted_session_if_token_expired(
    tmp_path: Path, monkeypatch
) -> None:
    class Vault:
        def get_many(self, names):
            return {"CANVAS_ACCESS_TOKEN": "expired-token"}

    settings = load_settings(tmp_path / "missing.env", secret_vault=Vault())
    provider = LiveSourceProvider(settings)
    expected = load_fixture("fixtures/sample_todo.json")

    class TokenRequest:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(orchestrator_module, "CanvasTokenRequest", TokenRequest)
    monkeypatch.setattr(
        orchestrator_module,
        "fetch_live",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            CanvasError("SESSION_EXPIRED", "expired", exit_code=2)
        ),
    )
    monkeypatch.setattr(provider, "_fetch_canvas_session", lambda _target: expected)

    assert provider.fetch_canvas(TARGET) is expected


def test_canvas_auth_check_reports_working_token(tmp_path: Path, monkeypatch) -> None:
    class Vault:
        def get_many(self, names):
            return {"CANVAS_ACCESS_TOKEN": "test-canvas-token"}

    settings = load_settings(tmp_path / "missing.env", secret_vault=Vault())

    class TokenRequest:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    checked = []
    monkeypatch.setattr(orchestrator_module, "CanvasTokenRequest", TokenRequest)
    monkeypatch.setattr(
        orchestrator_module,
        "verify_session",
        lambda request, base_url: checked.append((request, base_url)),
    )

    assert LiveSourceProvider(settings).check_canvas_auth() == "token"
    assert len(checked) == 1
    assert checked[0][1] == "https://issaquah.instructure.com/"


def test_canvas_session_retries_one_transient_failure(tmp_path: Path, monkeypatch) -> None:
    settings = load_settings(tmp_path / "missing.env")
    provider = LiveSourceProvider(settings)
    attempts = []

    def attempt(operation, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise CanvasError("SESSION_EXPIRED", "transient redirect", exit_code=2)
        return operation(object())

    monkeypatch.setattr(provider, "_canvas_session_attempt", attempt)

    assert provider._run_canvas_session(lambda _context: "ok") == "ok"
    assert attempts == [
        {"headless": True, "renew": False},
        {"headless": True, "renew": True},
    ]


def test_canvas_session_does_not_retry_rejected_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    settings = load_settings(tmp_path / "missing.env")
    provider = LiveSourceProvider(settings)
    attempts = []

    def attempt(_operation, **kwargs):
        attempts.append(kwargs)
        raise CanvasError(
            "MICROSOFT_CREDENTIALS_REJECTED",
            "Microsoft did not accept the stored email or password",
            exit_code=2,
        )

    monkeypatch.setattr(provider, "_canvas_session_attempt", attempt)

    with pytest.raises(CanvasError) as rejected:
        provider._run_canvas_session(lambda _context: "never")

    assert rejected.value.code == "MICROSOFT_CREDENTIALS_REJECTED"
    assert attempts == [{"headless": True, "renew": False}]


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


def test_school_notes_inform_guidance_and_done_rows_are_excluded(tmp_path: Path) -> None:
    notion = NotionDelivery()
    provider = Provider()
    noted = provider.canvas.assignments[0]
    completed = noted.model_copy(
        update={
            "key": "assignment:completed",
            "source_key": "assignment:completed",
            "object_id": "completed",
            "assignment_id": 999,
            "name": "Completed in-person task",
        }
    )
    provider.canvas = provider.canvas.model_copy(
        update={"assignments": [noted, completed]}
    )
    notion.context = {
        noted.key: {"status": "To do", "notes": "Finished the first half."},
        completed.key: {"status": "Done", "notes": "Submitted in person."},
    }
    seen = []

    def guidance(selected, *_args, **_kwargs):
        seen.extend(selected)
        return GuidanceResult(
            overview="",
            task_guidance=[
                GuidanceItem(key=item.key, guidance="Continue from your saved progress.")
                for item in selected
            ],
        )

    orchestrator = make_orchestrator(tmp_path, guidance, notion)
    artifact, _ = orchestrator.prepare(
        provider,
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 6, 30, tzinfo=TZ),
        dry_run=True,
    )

    selected = {item.key: item for item in seen}
    assert selected[noted.key].user_notes == "Finished the first half."
    assert completed.key not in selected
    assert all(item.key != completed.key for item in artifact.sources.canvas.assignments)


def test_master_layout_is_the_source_of_truth_and_routes_delivery_without_duplicates(
    tmp_path: Path,
) -> None:
    notion = MasterNotionDelivery()
    provider = Provider()
    assignment = provider.canvas.assignments[0]
    notion.master_context = {
        assignment.key: {"done": False, "notes": "Half complete."}
    }
    from daily_brief.models import NotionWorkItem

    notion.master_items = [
        NotionWorkItem(
            key="notion:master-work",
            page_id="master-work",
            url="https://notion.test/master-work",
            name="Master-only work item",
            area="Work",
            next_step="Draft the opening paragraph.",
            effort="S",
        )
    ]
    provider.fetch_notion = lambda: (_ for _ in ()).throw(
        AssertionError("legacy Notion databases must not be read in master mode")
    )
    telegram = Telegram()
    orchestrator = make_orchestrator(tmp_path, Guidance(), notion, telegram)

    artifact, _ = orchestrator.prepare(
        provider,
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 6, 30, tzinfo=TZ),
    )
    assert artifact.sources.canvas.assignments[0].user_notes == "Half complete."
    assert [item.name for item in artifact.sources.notion] == ["Master-only work item"]

    _, status, _ = orchestrator.deliver(
        provider,
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 7, 0, tzinfo=TZ),
    )
    assert status == "sent"
    assert notion.master_sync_calls == 1
    assert notion.master_focus_calls == 1
    assert notion.calls == 0
    assert notion.plan_calls == 0


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


def test_partial_canvas_response_retains_compatible_cached_assignments(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    full = Provider()
    orchestrator.fetch_sources(full, TARGET, write_cache=True)
    partial = Provider()
    partial.canvas = partial.canvas.model_copy(
        update={
            "assignments": [],
            "source_status": partial.canvas.source_status.model_copy(
                update={"planner_items": "failed"}
            ),
        }
    )
    bundle = orchestrator.fetch_sources(partial, TARGET, write_cache=False)
    assert [item.key for item in bundle.canvas.assignments] == ["assignment:101"]
    assert any("retained compatible cached" in value for value in bundle.canvas.data_warnings)


def test_canvas_failure_uses_recent_cross_date_cache_with_error_code(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    prior = Provider()
    orchestrator.cache.save(
        "canvas", prior.canvas, target_date=TARGET - timedelta(days=1)
    )

    class FailedCanvasProvider(Provider):
        def fetch_canvas(self, target):
            raise CanvasError(
                "MICROSOFT_CREDENTIALS_REJECTED",
                "Microsoft did not accept the stored email or password",
                exit_code=2,
            )

    bundle = orchestrator.fetch_sources(
        FailedCanvasProvider(), TARGET, write_cache=False
    )

    assert bundle.canvas is not None
    assert bundle.statuses["canvas"] == "stale"
    assert bundle.errors["canvas"] == "MICROSOFT_CREDENTIALS_REJECTED"
    assert any("may omit recent changes" in warning for warning in bundle.warnings)


def test_canvas_failure_rejects_cross_date_cache_older_than_72_hours(
    tmp_path: Path,
) -> None:
    orchestrator = make_orchestrator(tmp_path)
    prior = Provider()
    orchestrator.cache.save(
        "canvas", prior.canvas, target_date=TARGET - timedelta(days=4)
    )
    cache_path = tmp_path / "state" / "cache" / "canvas.json"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["cached_at"] = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    class FailedCanvasProvider(Provider):
        def fetch_canvas(self, target):
            raise CanvasError("SESSION_EXPIRED", "private detail", exit_code=2)

    bundle = orchestrator.fetch_sources(
        FailedCanvasProvider(), TARGET, write_cache=False
    )

    assert bundle.canvas is None
    assert bundle.statuses["canvas"] == "unavailable"


def test_delivery_log_records_sanitized_canvas_error_code(tmp_path: Path) -> None:
    orchestrator = make_orchestrator(tmp_path, Guidance(), None, Telegram())

    class FailedCanvasProvider(Provider):
        def fetch_canvas(self, target):
            raise CanvasError("SESSION_EXPIRED", "private detail", exit_code=2)

    orchestrator.deliver(
        FailedCanvasProvider(),
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 7, tzinfo=TZ),
    )

    log = (tmp_path / "state" / "runs.log").read_text(encoding="utf-8")
    assert "canvas_error=SESSION_EXPIRED" in log
    assert "private detail" not in log


def test_uncertain_telegram_transport_does_not_count_as_definite_failure(tmp_path: Path) -> None:
    telegram = UncertainTelegram()
    orchestrator = make_orchestrator(tmp_path, Guidance(), None, telegram)
    provider = Provider()
    _, status, state = orchestrator.deliver(
        provider,
        target_date=TARGET,
        as_of=datetime(2026, 9, 2, 7, tzinfo=TZ),
    )
    assert status == "uncertain"
    assert state.consecutive_telegram_failures == 0
