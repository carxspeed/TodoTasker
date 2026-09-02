import copy
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from daily_brief.checkin import (
    CheckinExtraction,
    apply_operation_plan,
    build_operation_plan,
    conservative_match,
    extract_checkin_local,
    freeze_replies,
    normalize_name,
)
from daily_brief.models import DailyBriefState, NotionWorkItem, SeenAssignment


def test_name_normalization_and_conservative_matching() -> None:
    assert normalize_name("  Physics—Lab! ") == "physics lab"
    candidates = {"a": "Physics lab", "b": "College essay final draft"}
    assert conservative_match("physics lab", candidates).key == "a"
    assert conservative_match("physics lba", candidates).status == "fuzzy"
    assert conservative_match("essay draft", candidates).status == "unmatched"


def test_ambiguous_tie_is_never_selected() -> None:
    result = conservative_match("project alfa", {"a": "Project alpha", "b": "Project alpa"})
    assert result.status == "ambiguous" and result.key is None


class Response:
    status_code = 200

    def __init__(self, content):
        self.content = content

    def json(self):
        return self.content

    def raise_for_status(self):
        return None


class Session:
    def __init__(self, text):
        self.text = text
        self.posts = []

    def get(self, *args, **kwargs):
        return Response({"models": []})

    def post(self, *args, **kwargs):
        self.posts.append(kwargs["json"])
        return Response({"message": {"content": self.text}})


def extraction_json(**changes):
    value = {
        "capacity": None,
        "done": [],
        "new_items": [],
        "next_steps": [],
        "unknown_next_step": [],
        "effort_corrections": [],
    }
    value.update(changes)
    return json.dumps(value)


def test_extraction_is_one_local_structured_call_and_omitted_capacity_stays_null() -> None:
    session = Session(extraction_json(done=["Physics lab"]))
    result = extract_checkin_local(
        "done with the lab",
        local_today=date(2026, 9, 1),
        checkin_sent_for=date(2026, 9, 2),
        timezone_name="America/Los_Angeles",
        session=session,
    )
    assert result is not None and result.capacity is None
    assert len(session.posts) == 1
    assert session.posts[0]["think"] is False
    assert "tools" not in session.posts[0]


def test_invalid_deadline_and_cross_array_collision_reject_whole_extraction() -> None:
    invalid = Session(extraction_json(new_items=[{"name": "x", "area": "Misc", "deadline": "tomorrow"}]))
    assert extract_checkin_local(
        "new x", local_today=date(2026, 9, 1), checkin_sent_for=date(2026, 9, 2), timezone_name="America/Los_Angeles", session=invalid
    ) is None
    collision = Session(extraction_json(done=["Physics"], unknown_next_step=["physics!"]))
    assert extract_checkin_local(
        "conflict", local_today=date(2026, 9, 1), checkin_sent_for=date(2026, 9, 2), timezone_name="America/Los_Angeles", session=collision
    ) is None


def test_new_item_areas_match_the_notion_database() -> None:
    for area in ("Work", "School", "Connections", "Misc"):
        result = CheckinExtraction.model_validate_json(
            extraction_json(new_items=[{"name": "x", "area": area, "deadline": None}])
        )
        assert result.new_items[0].area == area
    with pytest.raises(ValidationError):
        CheckinExtraction.model_validate_json(
            extraction_json(new_items=[{"name": "x", "area": "Personal", "deadline": None}])
        )


def test_freeze_replies_keeps_only_matching_chat_after_prompt() -> None:
    sent = datetime(2026, 9, 1, 21, tzinfo=timezone.utc)
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 7}, "date": int((sent - timedelta(minutes=1)).timestamp()), "text": "old"}},
        {"update_id": 2, "message": {"chat": {"id": 8}, "date": int(sent.timestamp()), "text": "other"}},
        {"update_id": 3, "message": {"chat": {"id": 7}, "date": int((sent + timedelta(minutes=1)).timestamp()), "text": "mine"}},
    ]
    frozen = freeze_replies(updates, chat_id="7", sent_at=sent)
    assert frozen.update_ids == [3]
    assert "mine" in frozen.notes and "old" not in frozen.notes
    assert frozen.max_update_id == 3


def work(page_id="p", name="Physics lab", touched=date(2026, 9, 1)):
    return NotionWorkItem(key=f"notion:{page_id}", page_id=page_id, url="", name=name, last_touched=touched)


def seen(name="Essay"):
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return SeenAssignment(first_seen=now, last_seen=now, course_id=1, name=name, kind="assignment", source_key="assignment:1")


def test_operation_plan_matches_exact_and_sets_capacity_only_when_present() -> None:
    extraction = CheckinExtraction.model_validate_json(
        extraction_json(capacity="low", done=["Physics lab"], effort_corrections=[{"task": "Essay", "effort": "L"}])
    )
    plan = build_operation_plan(
        extraction,
        [work()],
        {"assignment:1": seen()},
        apply_date=date(2026, 9, 1),
        now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        capacity_for=date(2026, 9, 2),
    )
    assert [(op.kind, op.target) for op in plan.operations] == [
        ("done", "p"),
        ("effort", "assignment:1"),
        ("capacity", "2026-09-02"),
    ]


class FakeNotion:
    def __init__(self):
        self.items = []
        self.created = 0
        self.updated = 0

    def get_active_work(self):
        return type("Snapshot", (), {"items": self.items})()

    def create_work_item(self, fields):
        self.created += 1
        self.items.append(work(f"new-{self.created}", fields["Name"], date.fromisoformat(fields["Last touched"])))

    def update_work_item(self, page_id, fields):
        self.updated += 1


def test_new_item_crash_replay_does_not_duplicate() -> None:
    extraction = CheckinExtraction.model_validate_json(
        extraction_json(new_items=[{"name": "New project", "area": "Misc", "deadline": None}])
    )
    plan = build_operation_plan(
        extraction,
        [],
        {},
        apply_date=date(2026, 9, 1),
        now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        capacity_for=date(2026, 9, 2),
    )
    state = DailyBriefState()
    saved = []
    notion = FakeNotion()

    def crash_after_create(value):
        saved.append(copy.deepcopy(value))
        if notion.created:
            raise RuntimeError("crash")

    try:
        apply_operation_plan(
            state,
            plan,
            update_ids=[10],
            apply_date=date(2026, 9, 1),
            notion_client=notion,
            persist=crash_after_create,
        )
    except RuntimeError:
        pass
    assert notion.created == 1
    recovered = saved[0]
    apply_operation_plan(
        recovered,
        plan,
        update_ids=[10],
        apply_date=date(2026, 9, 1),
        notion_client=notion,
        persist=lambda value: None,
    )
    assert notion.created == 1
