from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from daily_brief.canvas import (
    CanvasError,
    assignment_collection_window,
    canvas_storage_state_path,
    enrich_assignment_details,
    exclude_course_assignments,
    extract_document_text,
    load_fixture,
    mark_overdue_on_paper_for_verification,
    normalize_assignment_sources,
    open_saved_canvas_context,
    paginate,
    planner_item_is_complete,
    save_canvas_session,
    stable_identity,
    todo_submission_complete,
    verify_session,
    window_planner_html,
)
from daily_brief.models import CanvasAssignment


class Response:
    def __init__(self, status, body, *, headers=None, url="https://canvas.test/api"):
        self.status = status
        self._body = body
        self.headers = headers or {"content-type": "application/json"}
        self.url = url

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def docx_bytes(text: str) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return output.getvalue()


def test_verified_session_state_is_saved_and_restored_in_a_fresh_context(tmp_path: Path) -> None:
    profile = tmp_path / "profile"

    class LoginContext:
        def storage_state(self, *, path):
            Path(path).write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

    state_path = save_canvas_session(LoginContext(), profile)
    assert state_path == canvas_storage_state_path(profile)
    assert state_path.exists()
    assert not state_path.with_suffix(".tmp").exists()

    class Context:
        closed = False

        def close(self):
            self.closed = True

    context = Context()

    class Browser:
        closed = False

        def new_context(self, **kwargs):
            assert kwargs == {"storage_state": str(state_path)}
            return context

        def close(self):
            self.closed = True

    browser = Browser()

    class Chromium:
        def launch(self, **kwargs):
            assert kwargs == {"headless": True}
            return browser

    playwright = type("Playwright", (), {"chromium": Chromium()})()
    with open_saved_canvas_context(playwright, profile) as restored:
        assert restored is context
    assert context.closed and browser.closed


def test_missing_saved_session_requires_login(tmp_path: Path) -> None:
    with pytest.raises(CanvasError) as missing:
        with open_saved_canvas_context(object(), tmp_path / "profile"):
            pass
    assert missing.value.code == "SESSION_EXPIRED"
    assert missing.value.exit_code == 2


def test_normalized_fixture_round_trips() -> None:
    fixture = load_fixture(Path("fixtures/sample_todo.json"))
    assert fixture.source == "fixture"
    assert fixture.assignments[0].key == "assignment:101"
    assert "secure_params" not in fixture.model_dump_json()


def test_assignment_window_includes_recent_and_future_work() -> None:
    assert assignment_collection_window(date(2026, 9, 4)) == (
        date(2026, 8, 21),
        date(2026, 9, 18),
    )


def test_document_text_extraction_supports_docx_and_pdf(monkeypatch) -> None:
    assert extract_document_text(docx_bytes("Measure one million units."), "lab.docx") == (
        "Measure one million units."
    )

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Reader:
        def __init__(self, *args, **kwargs):
            self.pages = [Page("Read the rubric."), Page("Submit the chart.")]

    monkeypatch.setattr("daily_brief.canvas.PdfReader", Reader)
    assert extract_document_text(b"pdf", "rubric.pdf") == "Read the rubric. Submit the chart."


def test_assignment_details_include_linked_docx_instructions() -> None:
    assignment = CanvasAssignment(
        key="assignment:7",
        source_key="assignment:7",
        object_id=7,
        assignment_id=7,
        course_id=2,
        name="Lab",
        course="Physics",
        kind="assignment",
        due_at="2026-09-05T04:00:00Z",
        submission_status="unsubmitted",
    )
    document = docx_bytes("Graph the measurements and answer the conclusion questions.")

    class BinaryResponse:
        status = 200

        def body(self):
            return document

    class Request:
        def get(self, url, **kwargs):
            if url.endswith("/courses/2/assignments/7"):
                return Response(
                    200,
                    {
                        "id": 7,
                        "name": "Lab: Millions",
                        "due_at": "2026-09-05T04:00:00Z",
                        "points_possible": 20,
                        "html_url": "https://canvas.test/courses/2/assignments/7",
                        "description": '<p>Follow the lab sheet.</p><a href="https://canvas.test/courses/2/files/99?wrap=1">Lab.docx</a>',
                    },
                )
            if url.endswith("/api/v1/files/99"):
                return Response(
                    200,
                    {
                        "id": 99,
                        "display_name": "Lab.docx",
                        "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "size": len(document),
                        "url": "https://download.test/lab.docx",
                    },
                )
            if url == "https://download.test/lab.docx":
                return BinaryResponse()
            raise AssertionError(url)

    enriched, warnings = enrich_assignment_details(Request(), "https://canvas.test", [assignment])
    assert warnings == []
    assert enriched[0].name == "Lab: Millions"
    assert "Follow the lab sheet" in enriched[0].description
    assert "Graph the measurements" in enriched[0].description


def test_pagination_follows_opaque_next_url_and_only_initial_params() -> None:
    calls = []
    responses = iter(
        [
            Response(
                200,
                [{"id": index} for index in range(100)],
                headers={
                    "content-type": "application/json",
                    "link": '<https://canvas.test/api?page=opaque-token>; rel="next"',
                },
            ),
            Response(200, [{"id": 101}]),
        ]
    )

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    result = paginate(get, "https://canvas.test/api", {"filter": "incomplete_items"})
    assert len(result) == 101
    assert calls[0][1]["params"] == {"filter": "incomplete_items", "per_page": 100}
    assert calls[1][0] == "https://canvas.test/api?page=opaque-token"
    assert calls[1][1]["params"] is None


def test_pagination_rejects_repeated_next_url() -> None:
    def get(url, **kwargs):
        return Response(
            200,
            [],
            headers={"content-type": "application/json", "link": f'<{url}>; rel="next"'},
        )

    with pytest.raises(CanvasError, match="repeated"):
        paginate(get, "https://canvas.test/api")


def test_session_classification_is_precise() -> None:
    class Request:
        def __init__(self, response):
            self.response = response

        def get(self, *args, **kwargs):
            return self.response

    with pytest.raises(CanvasError) as expired:
        verify_session(
            Request(Response(200, ValueError(), headers={"content-type": "text/html"})),
            "https://canvas.test",
        )
    assert expired.value.code == "SESSION_EXPIRED"
    assert expired.value.exit_code == 2
    with pytest.raises(CanvasError) as temporary:
        verify_session(Request(Response(500, {})), "https://canvas.test")
    assert temporary.value.code == "CANVAS_TEMPORARY_FAILURE"

    class ClosedRequest:
        def get(self, *args, **kwargs):
            raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(CanvasError) as closed:
        verify_session(ClosedRequest(), "https://canvas.test")
    assert closed.value.code == "CANVAS_BROWSER_CLOSED"
    assert closed.value.exit_code == 2


@pytest.mark.parametrize(
    ("kind", "plannable", "expected"),
    [
        ("assignment", {"id": 10}, "assignment:10"),
        ("quiz", {"id": 20, "assignment_id": 11}, "assignment:11"),
        ("discussion_topic", {"id": 30}, "discussion_topic:30"),
        ("sub_assignment", {"id": 40, "assignment_id": 12}, "assignment:12"),
    ],
)
def test_stable_identity_for_every_task_type(kind, plannable, expected) -> None:
    item = {"plannable_type": kind, "plannable_id": plannable["id"], "plannable": plannable}
    assert stable_identity(item)[0] == expected


def test_submission_filters_do_not_use_course_wide_flag() -> None:
    item = {
        "has_submitted_submissions": True,
        "submissions": {"submitted": False, "missing": True},
    }
    assert planner_item_is_complete(item) is False
    assert planner_item_is_complete({"submissions": {"submitted": True}}) is True
    assert todo_submission_complete({"workflow_state": "pending_review"}) is True
    assert todo_submission_complete({"workflow_state": "pending-review"}) is False


def test_missing_union_keeps_unknown_locked_item_in_verify_path() -> None:
    missing = {
        "id": 99,
        "course_id": 5,
        "name": "Old paper task",
        "due_at": "2026-08-20T23:59:00Z",
        "locked_for_user": True,
    }
    normalized = normalize_assignment_sources([], [missing], active_courses={5: "History"})
    assert len(normalized.assignments) == 1
    assert normalized.assignments[0].submission_status == "unknown"
    assert normalized.assignments[0].needs_confirmation is True


def test_excluded_course_removes_only_its_assignments() -> None:
    envelope = load_fixture("fixtures/sample_todo.json")
    filtered = exclude_course_assignments(envelope, [12])

    assert filtered.assignments == []
    assert filtered.canvas_events == envelope.canvas_events
    assert filtered.planners == envelope.planners
    assert envelope.assignments[0].course_id == 12


def test_only_overdue_on_paper_assignments_require_verification() -> None:
    observed = datetime(2026, 9, 4, tzinfo=timezone.utc)
    base = load_fixture("fixtures/sample_todo.json").assignments[0]
    paper = base.model_copy(
        update={
            "key": "assignment:paper",
            "due_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
            "submission_types": ["on_paper"],
        }
    )
    online = base.model_copy(
        update={
            "key": "assignment:online",
            "due_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
            "submission_types": ["online_upload"],
        }
    )
    future_paper = paper.model_copy(
        update={
            "key": "assignment:future",
            "due_at": datetime(2026, 9, 5, tzinfo=timezone.utc),
        }
    )

    marked = mark_overdue_on_paper_for_verification(
        [paper, online, future_paper], observed
    )
    assert marked[0].submission_status == "unknown"
    assert marked[0].needs_confirmation is True
    assert marked[1].submission_status == "unsubmitted"
    assert marked[2].submission_status == "unsubmitted"


def test_planner_windowing_is_structural_and_creates_quiz_event() -> None:
    body = """
    <table><tr><th>Date</th><th>Work</th></tr>
      <tr><td>9/2 W</td><td>Chapter 4 quiz</td></tr>
      <tr><td>9/10 points</td><td>Not a date</td></tr>
      <tr><td>read 1/2</td><td>Not a date</td></tr>
      <tr><td>13/40</td><td>Invalid</td></tr>
    </table>
    """
    result = window_planner_html(
        body,
        course_id=1,
        course="Calculus",
        title="Planner",
        url="https://canvas.test/planner",
        target_date=date(2026, 9, 2),
        title_matched=True,
    )
    assert result.observation.status == "windowed"
    assert result.planner is not None
    assert result.planner.dates == [date(2026, 9, 2)]
    assert len(result.events) == 1


def test_generic_undated_front_page_is_discarded() -> None:
    result = window_planner_html(
        "<p>Welcome to class. Read the syllabus.</p>",
        course_id=1,
        course="English",
        title="Home",
        url="https://canvas.test/home",
        target_date=date(2026, 9, 2),
        title_matched=False,
    )
    assert result.planner is None
    assert result.observation.status == "unparsed"


def test_title_matched_undated_page_is_diagnostic_only() -> None:
    result = window_planner_html(
        "<p>Schedule will be posted shortly.</p>",
        course_id=1,
        course="English",
        title="Weekly Planner",
        url="https://canvas.test/planner",
        target_date=date(2026, 9, 2),
        title_matched=True,
    )
    assert result.planner is not None
    assert result.planner.dates == []
    assert result.events == []
