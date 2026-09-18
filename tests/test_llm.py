import json
from datetime import date, datetime, timezone

import pytest
import requests

import daily_brief.guidance as guidance
from daily_brief.guidance import (
    CANVAS_INSTRUCTION_LIMIT,
    PROMPT_LIMIT,
    build_guidance_request,
    generate_guidance,
    validate_guidance_text,
)
from daily_brief.models import ClassifiedItem, FocusPlan, FreeWindow, GuidanceResult


def task(index: int, *, description="", next_step="", user_notes="") -> ClassifiedItem:
    return ClassifiedItem(
        key=f"assignment:{index}",
        source="canvas",
        name=f"Task {index}",
        tier="must",
        effort="M",
        effort_hours=1.5,
        effort_source="points",
        description=description,
        next_step=next_step,
        user_notes=user_notes,
    )


TOTALS = {
    "selected_count": 1,
    "selected_effort_hours": 1.5,
    "available_hours": 3.5,
    "overloaded": False,
    "unscheduled_required_count": 0,
}


def response_for(keys):
    return json.dumps(
        {
            "overview": "Use the available window.",
            "focus": (
                {
                    "primary_key": keys[0],
                    "reason": "It is first in the validated priority order.",
                    "today_keys": keys[:3],
                }
                if keys
                else None
            ),
            "task_guidance": [
                {
                    "key": key,
                    "guidance": "Open the instructions and begin.",
                    "summary": "Complete the assigned work using the provided instructions.",
                }
                for key in keys
            ],
        }
    )


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("bad status")

    def json(self):
        return self.body


class Session:
    def __init__(self, content):
        self.content = content
        self.get_calls = 0
        self.post_calls = 0
        self.payload = None

    def get(self, *args, **kwargs):
        self.get_calls += 1
        return Response({"models": []})

    def post(self, *args, **kwargs):
        self.post_calls += 1
        self.payload = kwargs["json"]
        return Response({"message": {"content": self.content}})


def test_request_uses_first_ten_keys_and_exact_totals() -> None:
    selected = [task(index) for index in range(12)]
    request = build_guidance_request(selected, [], {**TOTALS, "selected_count": 12, "ignored": 99}, date(2026, 9, 2))
    assert request.keys == [f"assignment:{index}" for index in range(10)]
    assert request.moved_to_fallback == ["assignment:10", "assignment:11"]
    assert set(request.user["DATA"]["workload_totals"]) == set(TOTALS)


def test_imminent_assessment_is_included_before_ordinary_backlog() -> None:
    selected = [task(index) for index in range(10)]
    assessment = task(99).model_copy(
        update={
            "key": "planner-assessment:quiz",
            "kind": "planner_assessment",
            "due_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
        }
    )
    request = build_guidance_request([*selected, assessment], [], {**TOTALS, "selected_count": 11}, date(2026, 9, 2))
    assert request.keys[0] == "planner-assessment:quiz"
    assert "assignment:9" in request.moved_to_fallback


def test_imminent_assessment_overrides_a_model_focus_on_old_work() -> None:
    assessment = task(99).model_copy(
        update={
            "key": "planner-assessment:quiz",
            "kind": "planner_assessment",
            "due_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
        }
    )
    result = GuidanceResult(
        focus=FocusPlan(
            primary_key="assignment:1",
            reason="Small task.",
            today_keys=["assignment:1"],
        )
    )
    focused = guidance._enforce_assessment_focus(result, [task(1), assessment], date(2026, 9, 2))
    assert focused.focus is not None
    assert focused.focus.primary_key == "planner-assessment:quiz"


def test_same_day_assessment_does_not_override_model_focus() -> None:
    assessment = task(99).model_copy(
        update={
            "key": "planner-assessment:quiz",
            "kind": "planner_assessment",
            "due_at": datetime(2026, 9, 2, 8, tzinfo=timezone.utc),
        }
    )
    result = GuidanceResult(
        focus=FocusPlan(
            primary_key="assignment:1",
            reason="It is the next actionable assignment.",
            today_keys=["assignment:1"],
        )
    )

    focused = guidance._enforce_assessment_focus(
        result, [task(1), assessment], date(2026, 9, 2)
    )

    assert focused.focus.primary_key == "assignment:1"


def test_large_request_is_bounded_without_dropping_retained_fields() -> None:
    selected = [task(index, description="x" * 400, next_step="y" * 1000) for index in range(20)]
    request = build_guidance_request(
        selected, [], {**TOTALS, "selected_count": 20, "selected_effort_hours": 30}, date(2026, 9, 2)
    )
    assert request.prompt_chars <= PROMPT_LIMIT
    for retained in request.user["DATA"]["guidance_input"]:
        assert retained["key"] and retained["name"] and retained["tier"] and retained["effort_hours"]


def test_canvas_instructions_are_bounded_and_not_confused_with_notion_next_step() -> None:
    request = build_guidance_request(
        [task(1, description="Collect objects. " * 200)],
        [],
        TOTALS,
        date(2026, 9, 2),
    )
    payload = request.user["DATA"]["guidance_input"][0]
    assert "next_step" not in payload
    assert payload["canvas_instructions"].startswith("Collect objects.")
    assert len(payload["canvas_instructions"]) == CANVAS_INSTRUCTION_LIMIT


def test_canvas_user_notes_are_included_in_guidance_context() -> None:
    selected = [task(1, user_notes="Finished the first half; continue with questions 6–10.")]
    request = build_guidance_request(selected, [], TOTALS, date(2026, 9, 14))

    payload = request.user["DATA"]["guidance_input"][0]
    assert payload["user_notes"] == "Finished the first half; continue with questions 6–10."


def test_whole_response_validation_rejects_reordered_extra_and_missing_keys() -> None:
    request = build_guidance_request([task(1), task(2)], [], {**TOTALS, "selected_count": 2}, date(2026, 9, 2))
    with pytest.raises(ValueError, match="reordered"):
        validate_guidance_text(response_for(list(reversed(request.keys))), request)
    with pytest.raises(Exception):
        validate_guidance_text("prefix " + response_for(request.keys), request)
    with pytest.raises(Exception):
        validate_guidance_text(response_for(request.keys + ["extra"]), request)


def test_canvas_unknown_guidance_is_rejected() -> None:
    request = build_guidance_request([task(1)], [], TOTALS, date(2026, 9, 2))
    response = json.dumps(
        {
            "overview": "",
            "focus": {
                "primary_key": "assignment:1",
                "reason": "It is the only selected task.",
                "today_keys": ["assignment:1"],
            },
            "task_guidance": [
                {
                    "key": "assignment:1",
                    "guidance": "Next step unknown — scope it.",
                    "summary": "Review the assignment requirements.",
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="Canvas guidance"):
        validate_guidance_text(response, request)


def test_summary_is_backward_compatible_when_model_omits_it() -> None:
    request = build_guidance_request([task(1)], [], TOTALS, date(2026, 9, 2))
    response = json.dumps(
        {
            "overview": "",
            "focus": {
                "primary_key": "assignment:1",
                "reason": "It is the only selected task.",
                "today_keys": ["assignment:1"],
            },
            "task_guidance": [
                {"key": "assignment:1", "guidance": "Open the worksheet and begin."}
            ],
        }
    )

    result = validate_guidance_text(response, request)

    assert result.task_guidance[0].summary == ""


def test_focus_is_limited_to_known_keys_with_the_primary_first() -> None:
    request = build_guidance_request([task(1), task(2)], [], {**TOTALS, "selected_count": 2}, date(2026, 9, 2))
    response = json.loads(response_for(request.keys))
    response["focus"] = {
        "primary_key": "assignment:2",
        "reason": "It is the nearer assessment.",
        "today_keys": ["assignment:2", "assignment:1"],
    }
    result = validate_guidance_text(json.dumps(response), request)
    assert result.focus is not None
    assert result.focus.today_keys == ["assignment:2", "assignment:1"]


def test_focus_primary_must_be_first() -> None:
    request = build_guidance_request([task(1), task(2)], [], {**TOTALS, "selected_count": 2}, date(2026, 9, 2))
    response = json.loads(response_for(request.keys))
    response["focus"] = {
        "primary_key": "assignment:2",
        "reason": "It is the nearer assessment.",
        "today_keys": ["assignment:1", "assignment:2"],
    }
    with pytest.raises(ValueError, match="primary task"):
        validate_guidance_text(json.dumps(response), request)


def test_local_generation_makes_exactly_one_chat_call() -> None:
    request = build_guidance_request([task(1)], [], TOTALS, date(2026, 9, 2))
    session = Session(response_for(request.keys))
    result = generate_guidance([task(1)], [], TOTALS, date(2026, 9, 2), session=session)
    assert result is not None
    assert session.get_calls == 1
    assert session.post_calls == 1
    assert session.payload["think"] is False
    assert "tools" not in session.payload


def test_local_generation_starts_ollama_when_service_is_off(monkeypatch) -> None:
    request = build_guidance_request([task(1)], [], TOTALS, date(2026, 9, 2))

    class StartsThenWorks(Session):
        def get(self, *args, **kwargs):
            self.get_calls += 1
            if self.get_calls == 1:
                raise requests.ConnectionError("Ollama is off")
            return Response({"models": []})

    session = StartsThenWorks(response_for(request.keys))
    started: list[str] = []
    monkeypatch.setattr(
        guidance,
        "_start_ollama_service",
        lambda _session, *, base_url: started.append(base_url) or True,
    )

    result = generate_guidance([task(1)], [], TOTALS, date(2026, 9, 2), session=session)

    assert result is not None
    assert started == ["http://localhost:11434"]
    assert session.post_calls == 1


def test_ollama_autostart_never_targets_a_remote_server() -> None:
    assert guidance._is_local_ollama_url("http://localhost:11434")
    assert guidance._is_local_ollama_url("http://127.0.0.1:11434")
    assert not guidance._is_local_ollama_url("https://example.com")


def test_malformed_local_response_gets_one_focused_repair_call() -> None:
    session = Session("not json")
    result = generate_guidance([task(1)], [], TOTALS, date(2026, 9, 2), session=session)
    assert result is None
    assert session.post_calls == 2
    assert "previous response failed strict validation" in session.payload["messages"][0]["content"]


def test_local_repair_can_recover_a_valid_second_response() -> None:
    request = build_guidance_request([task(1)], [], TOTALS, date(2026, 9, 2))

    class RetrySession(Session):
        def post(self, *args, **kwargs):
            self.post_calls += 1
            self.payload = kwargs["json"]
            content = "not json" if self.post_calls == 1 else response_for(request.keys)
            return Response({"message": {"content": content}})

    session = RetrySession("")
    result = generate_guidance([task(1)], [], TOTALS, date(2026, 9, 2), session=session)

    assert result is not None
    assert session.post_calls == 2
    assert "previous response failed strict validation" in session.payload["messages"][0]["content"]


def test_anthropic_generation_uses_current_api_parameters() -> None:
    class AnthropicSession(Session):
        def post(self, *args, **kwargs):
            self.post_calls += 1
            self.payload = kwargs["json"]
            return Response(
                {
                    "content": [
                        {"type": "thinking", "thinking": ""},
                        {"type": "text", "text": response_for(["assignment:1"])},
                    ]
                }
            )

    session = AnthropicSession("")
    result = generate_guidance(
        [task(1)],
        [],
        TOTALS,
        date(2026, 9, 2),
        provider="anthropic",
        model="claude-sonnet-5",
        anthropic_api_key="test-key",
        session=session,
    )

    assert result is not None
    assert session.post_calls == 1
    assert "temperature" not in session.payload
    assert session.payload["thinking"] == {"type": "disabled"}


def test_anthropic_generation_retries_one_invalid_structured_response() -> None:
    class RetrySession(Session):
        def post(self, *args, **kwargs):
            self.post_calls += 1
            text = "not json" if self.post_calls == 1 else response_for(["assignment:1"])
            return Response({"content": [{"type": "text", "text": text}]})

    session = RetrySession("")
    result = generate_guidance(
        [task(1)],
        [],
        TOTALS,
        date(2026, 9, 2),
        provider="anthropic",
        model="claude-sonnet-5",
        anthropic_api_key="test-key",
        session=session,
    )

    assert result is not None
    assert session.post_calls == 2


def test_zero_guidance_items_is_valid_and_still_one_call() -> None:
    session = Session('{"overview":"","focus":null,"task_guidance":[]}')
    result = generate_guidance([], [], {**TOTALS, "selected_count": 0}, date(2026, 9, 2), session=session)
    assert result is not None and result.task_guidance == []
    assert session.post_calls == 1


def test_free_window_boundaries_survive_budgeting() -> None:
    windows = [
        FreeWindow(start=datetime(2026, 9, 2, 15, tzinfo=timezone.utc), end=datetime(2026, 9, 2, 16, tzinfo=timezone.utc))
    ]
    request = build_guidance_request([task(1, description="x" * 400)], windows, TOTALS, date(2026, 9, 2))
    assert request.user["DATA"]["free_windows"][0]["start"] == "2026-09-02T15:00:00Z"
