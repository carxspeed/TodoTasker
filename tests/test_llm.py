import json
from datetime import date, datetime, timezone

import pytest

from daily_brief.guidance import (
    CANVAS_INSTRUCTION_LIMIT,
    PROMPT_LIMIT,
    build_guidance_request,
    generate_guidance,
    validate_guidance_text,
)
from daily_brief.models import ClassifiedItem, FreeWindow


def task(index: int, *, description="", next_step="") -> ClassifiedItem:
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
            "task_guidance": [{"key": key, "guidance": "Open the instructions and begin."} for key in keys],
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
            "task_guidance": [
                {"key": "assignment:1", "guidance": "Next step unknown — scope it."}
            ],
        }
    )
    with pytest.raises(ValueError, match="Canvas guidance"):
        validate_guidance_text(response, request)


def test_local_generation_makes_exactly_one_chat_call() -> None:
    request = build_guidance_request([task(1)], [], TOTALS, date(2026, 9, 2))
    session = Session(response_for(request.keys))
    result = generate_guidance([task(1)], [], TOTALS, date(2026, 9, 2), session=session)
    assert result is not None
    assert session.get_calls == 1
    assert session.post_calls == 1
    assert session.payload["think"] is False
    assert "tools" not in session.payload


def test_malformed_response_gets_no_repair_call() -> None:
    session = Session("not json")
    result = generate_guidance([task(1)], [], TOTALS, date(2026, 9, 2), session=session)
    assert result is None
    assert session.post_calls == 1


def test_zero_guidance_items_is_valid_and_still_one_call() -> None:
    session = Session('{"overview":"","task_guidance":[]}')
    result = generate_guidance([], [], {**TOTALS, "selected_count": 0}, date(2026, 9, 2), session=session)
    assert result is not None and result.task_guidance == []
    assert session.post_calls == 1


def test_free_window_boundaries_survive_budgeting() -> None:
    windows = [
        FreeWindow(start=datetime(2026, 9, 2, 15, tzinfo=timezone.utc), end=datetime(2026, 9, 2, 16, tzinfo=timezone.utc))
    ]
    request = build_guidance_request([task(1, description="x" * 400)], windows, TOTALS, date(2026, 9, 2))
    assert request.user["DATA"]["free_windows"][0]["start"] == "2026-09-02T15:00:00Z"
