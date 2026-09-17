from datetime import date

import pytest

from daily_brief.http import JsonResponse
from daily_brief.models import NotionWorkItem
from daily_brief.canvas import load_fixture
from daily_brief.notion import (
    TASK_DATABASES,
    DAILY_PLAN_TITLE,
    NotionClient,
    NotionSchoolBoard,
    NotionTaskStore,
    WorkSnapshot,
    database_schema,
    daily_plan_properties,
    daily_plan_schema,
    date_property,
    rich_text_property,
    master_task_properties,
    master_task_schema,
    school_assignment_fields,
    school_database_schema,
    school_properties,
    compact_instruction_summary,
    select_property,
    summarize_master_migration,
    title_property,
    work_properties,
)


def text_prop(kind, value):
    return {"type": kind, kind: [{"plain_text": value}]}


def select_prop(value):
    return {"type": "select", "select": {"name": value} if value else None}


def date_prop(value):
    return {"type": "date", "date": {"start": value} if value else None}


class FakeHttp:
    def __init__(self, bodies):
        self.bodies = iter(bodies)
        self.calls = []

    def request_json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return JsonResponse(next(self.bodies), 200, {})


def row(page_id="abc", name="Physics lab", deadline="2026-09-05"):
    return {
        "id": page_id,
        "url": f"https://notion.test/{page_id}",
        "properties": {
            "Name": text_prop("title", name),
            "Type": select_prop("Task"),
            "Cadence": select_prop(None),
            "Last touched": date_prop("2026-09-01"),
            "Status": select_prop("Active"),
            "Next step": text_prop("rich_text", "Draft outline"),
            "Deadline": date_prop(deadline),
            "Effort": select_prop("M"),
        },
    }


def test_property_payload_shapes_are_not_bare_strings() -> None:
    assert title_property("x") == {"title": [{"text": {"content": "x"}}]}
    assert rich_text_property(None) == {"rich_text": []}
    assert select_property(None) == {"select": None}
    assert date_property(date(2026, 9, 1)) == {"date": {"start": "2026-09-01"}}
    assert work_properties({"Status": "Done"}) == {"Status": {"select": {"name": "Done"}}}
    assert database_schema()["Name"] == {"title": {}}
    assert "Area" not in database_schema()
    assert school_database_schema()["Canvas"] == {"url": {}}
    assert school_database_schema()["Canvas ID"] == {"rich_text": {}}
    assert school_database_schema()["Notes / progress"] == {"rich_text": {}}
    assert daily_plan_schema()["Rank"] == {"number": {"format": "number"}}
    assert daily_plan_properties({"Time (hours)": 1.5}) == {"Time (hours)": {"number": 1.5}}
    assert master_task_schema()["Done"] == {"checkbox": {}}
    assert master_task_schema()["Focus rank"] == {"number": {"format": "number"}}
    assert master_task_properties(
        {
            "Task": "Study for Quiz 2",
            "Done": True,
            "Area": "School",
            "Focus date": date(2026, 9, 17),
            "Focus rank": 1,
        }
    ) == {
        "Task": {"title": [{"text": {"content": "Study for Quiz 2"}}]},
        "Done": {"checkbox": True},
        "Area": {"select": {"name": "School"}},
        "Focus date": {"date": {"start": "2026-09-17"}},
        "Focus rank": {"number": 1},
    }


def test_school_assignment_payload_includes_source_id_and_safe_next_step() -> None:
    assignment = load_fixture("fixtures/sample_todo.json").assignments[0]
    fields = school_assignment_fields(
        assignment,
        {"Priority": "MUST", "Effort": "M", "Next step": "Complete part one."},
    )
    assert fields["Canvas ID"] == assignment.key
    assert fields["Priority"] == "MUST"
    assert fields["Next step"] == "Complete part one."
    assert len(fields["Sync hash"]) == 64
    payload = school_properties(fields)
    assert payload["Canvas"]["url"] == assignment.url
    assert payload["Due"]["date"]["start"] == assignment.due_at.isoformat()


def test_school_instructions_use_short_summary_and_keep_source_out_of_view() -> None:
    assignment = load_fixture("fixtures/sample_todo.json").assignments[0]
    assignment.description = "First requirement. Second requirement. " + "Raw detail " * 100

    fields = school_assignment_fields(
        assignment,
        {"Instructions": "Complete the first two requirements and submit the result."},
    )

    assert fields["Instructions"] == "Complete the first two requirements and submit the result."
    assert "Raw detail" not in fields["Instructions"]
    assert compact_instruction_summary(assignment.description) == "First requirement. Second requirement."


class FakeSchoolClient:
    def __init__(self):
        self.children = []
        self.created_databases = []
        self.created_rows = []
        self.ensured_properties = []
        self.updated_rows = []
        self.archived_rows = []

    def retrieve_page(self, page_id):
        return {"id": page_id, "url": "https://notion.test/tasks"}

    def list_block_children(self, page_id):
        return list(self.children)

    def create_database(self, title, **kwargs):
        database_id = f"db-{len(self.created_databases) + 1}"
        self.created_databases.append((title, kwargs))
        return {"id": database_id}

    def query_database_pages(self, database_id):
        return []

    def ensure_database_properties(self, database_id, properties):
        self.ensured_properties.append((database_id, properties))
        return True

    def create_school_item(self, database_id, fields):
        self.created_rows.append((database_id, fields))
        return {"id": f"row-{len(self.created_rows)}"}

    def update_school_item(self, page_id, fields):
        raise AssertionError("new rows should not be updated")

    def create_daily_plan_item(self, database_id, fields):
        self.created_rows.append((database_id, fields))
        return {"id": f"plan-{len(self.created_rows)}"}

    def update_daily_plan_item(self, page_id, fields):
        self.updated_rows.append((page_id, fields))
        return {"id": page_id}

    def create_master_task(self, database_id, fields):
        self.created_rows.append((database_id, fields))
        return {
            "id": f"master-{len(self.created_rows)}",
            "url": f"https://notion.test/master-{len(self.created_rows)}",
        }

    def update_master_task(self, page_id, fields):
        self.updated_rows.append((page_id, fields))
        return {"id": page_id, "url": f"https://notion.test/{page_id}"}

    def archive_page(self, page_id):
        self.archived_rows.append(page_id)
        return {"id": page_id, "archived": True}


def test_school_board_creates_one_table_per_class_and_excludes_course() -> None:
    assignments = load_fixture("fixtures/sample_todo.json").assignments
    fake = FakeSchoolClient()
    board = NotionSchoolBoard("", "parent", "school", client=fake)
    result = board.sync_canvas_assignments(
        assignments,
        details_by_key={assignments[0].key: {"Priority": "MUST", "Effort": "M"}},
        excluded_course_ids={assignments[-1].course_id},
    )
    included = [item for item in assignments if item.course_id != assignments[-1].course_id]
    assert result.databases_created == len({item.course for item in included})
    assert result.rows_created == len(included)
    assert {title for title, _ in fake.created_databases} == {item.course for item in included}
    assert all(options["is_inline"] is True for _, options in fake.created_databases)
    assert all(row[1]["Canvas ID"] != assignments[-1].key for row in fake.created_rows)


def test_master_sync_creates_one_database_and_preserves_user_fields_on_updates() -> None:
    assignment = load_fixture("fixtures/sample_todo.json").assignments[0]
    notion_item = NotionWorkItem(
        key="notion:work",
        page_id="work",
        url="https://notion.test/work",
        name="Prepare weekly update",
        area="Work",
        type="Task",
        next_step="Draft three bullets.",
    )
    fake = FakeSchoolClient()
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    result = board.sync_master_tasks(
        [assignment],
        [notion_item],
        details_by_key={assignment.key: {"Priority": "MUST", "Effort": "M"}},
    )

    assert result.database_created is True
    assert result.rows_created == 2
    assert fake.created_databases[0][0] == "Tasks"
    created_fields = [fields for _, fields in fake.created_rows]
    assert {fields["Source ID"] for fields in created_fields} == {
        assignment.key,
        notion_item.key,
    }
    assert all(fields["Done"] is False for fields in created_fields)
    assert all(fields["Notes / progress"] == "" for fields in created_fields)


def test_master_migration_preserves_legacy_done_and_notes_on_create() -> None:
    assignment = load_fixture("fixtures/sample_todo.json").assignments[0]
    fake = FakeSchoolClient()
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    board.sync_master_tasks(
        [assignment],
        [],
        user_context_by_key={
            assignment.key: {
                "status": "Done",
                "notes": "Submitted to the teacher in person.",
            }
        },
    )

    fields = fake.created_rows[0][1]
    assert fields["Done"] is True
    assert fields["Notes / progress"] == "Submitted to the teacher in person."


def test_master_migration_summary_reports_duplicates_and_exclusions() -> None:
    assignments = load_fixture("fixtures/sample_todo.json").assignments
    included = assignments[0]
    excluded = included.model_copy(
        update={"key": "assignment:excluded", "course_id": 999}
    )
    notion_item = NotionWorkItem(
        key=included.key,
        page_id="duplicate",
        url="https://notion.test/duplicate",
        name="Duplicate source",
    )

    summary = summarize_master_migration(
        [included, excluded],
        [notion_item],
        excluded_course_ids={excluded.course_id},
    )

    assert summary.canvas_rows == 1
    assert summary.notion_rows == 1
    assert summary.unique_rows == 1
    assert summary.duplicate_source_ids == (included.key,)


def test_master_context_reads_done_notes_and_page_url() -> None:
    fake = FakeSchoolClient()
    fake.children = [
        {"type": "child_database", "id": "master", "child_database": {"title": "Tasks"}}
    ]
    fake.query_database_pages = lambda _database_id: [
        {
            "id": "row",
            "url": "https://notion.test/row",
            "properties": {
                "Source ID": text_prop("rich_text", "assignment:1"),
                "Done": {"type": "checkbox", "checkbox": True},
                "Notes / progress": text_prop("rich_text", "Submitted in person."),
            },
        }
    ]
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    assert board.get_master_task_context() == {
        "assignment:1": {
            "done": True,
            "notes": "Submitted in person.",
            "url": "https://notion.test/row",
        }
    }


def test_school_context_reads_notes_and_status_without_canvas_bookkeeping() -> None:
    fake = FakeSchoolClient()
    fake.children = [
        {"type": "child_database", "id": "general", "child_database": {"title": "General"}},
        {"type": "child_database", "id": "physics", "child_database": {"title": "Physics"}},
    ]
    fake.query_database_pages = lambda database_id: [
        {
            "id": "row",
            "properties": {
                "Canvas ID": text_prop("rich_text", "assignment:1"),
                "Status": select_prop("Done"),
                "Notes / progress": text_prop("rich_text", "Submitted in person."),
            },
        }
    ]
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    assert board.get_assignment_context() == {
        "assignment:1": {"status": "Done", "notes": "Submitted in person."}
    }
    assert fake.ensured_properties == [
        ("physics", {"Notes / progress": {"rich_text": {}}})
    ]


def test_daily_plan_uses_internal_rank_and_archives_stale_rows() -> None:
    assignment = load_fixture("fixtures/sample_todo.json").assignments[0]
    from daily_brief.models import ClassificationOutput, ClassifiedItem
    from datetime import datetime, timezone

    item = ClassifiedItem(
        key=assignment.key,
        source="canvas",
        name=assignment.name,
        tier="must",
        effort="M",
        effort_hours=1.5,
        effort_source="points",
        course=assignment.course,
        url=assignment.url,
    )
    classification = ClassificationOutput(
        target_date=date(2026, 9, 15),
        as_of=datetime(2026, 9, 15, tzinfo=timezone.utc),
        must=[item],
    )
    fake = FakeSchoolClient()
    fake.children = [
        {"type": "child_database", "id": "plan-db", "child_database": {"title": DAILY_PLAN_TITLE}}
    ]
    fake.query_database_pages = lambda _database_id: [
        {
            "id": "stale-page",
            "properties": {
                "Task ID": text_prop("rich_text", "assignment:stale"),
                "Status": select_prop("To do"),
            },
        }
    ]
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    result = board.sync_daily_plan(
        classification,
        guidance_by_key={assignment.key: "Complete the first requirement."},
        target_date=date(2026, 9, 15),
    )

    assert result.rows_created == 1
    assert result.rows_archived == 1
    assert fake.created_rows[0][1]["Rank"] == 1
    assert fake.created_rows[0][1]["Next step"] == "Complete the first requirement."
    assert fake.archived_rows == ["stale-page"]


def test_ensure_database_properties_adds_only_missing_properties() -> None:
    http = FakeHttp(
        [
            {"properties": {"Name": {"type": "title"}}},
            {"id": "db"},
            {"properties": {"Notes / progress": {"type": "rich_text"}}},
        ]
    )
    client = NotionClient("token", "", http=http)
    schema = {"Notes / progress": {"rich_text": {}}}

    assert client.ensure_database_properties("db", schema) is True
    assert http.calls[1][0:2] == ("PATCH", "https://api.notion.com/v1/databases/db")
    assert http.calls[1][2]["json"] == {"properties": schema}
    assert client.ensure_database_properties("db", schema) is False


def test_named_task_database_creation_and_archive_payloads() -> None:
    http = FakeHttp([{"id": "db"}, {"id": "page"}])
    client = NotionClient("token", "db", "parent", http=http)
    result = client.create_work_database("School")
    assert result == {"id": "db"}
    assert http.calls[0][0:2] == ("POST", "https://api.notion.com/v1/databases")
    assert http.calls[0][2]["json"]["title"][0]["text"]["content"] == "School"
    assert http.calls[0][2]["json"]["is_inline"] is False
    with pytest.raises(ValueError, match="unknown task database"):
        client.create_work_database("Other")
    assert client.archive_work_item("page") == {"id": "page"}
    assert http.calls[1][0:2] == ("PATCH", "https://api.notion.com/v1/pages/page")
    assert http.calls[1][2]["json"] == {"archived": True}
    assert http.calls[1][2]["idempotent"] is True


def test_query_paginates_and_normalizes_by_page_id() -> None:
    http = FakeHttp(
        [
            {"results": [row("page-one")], "has_more": True, "next_cursor": "cursor"},
            {"results": [row("page-two", "Essay")], "has_more": False, "next_cursor": None},
        ]
    )
    snapshot = NotionClient("token", "db", http=http).get_active_work()
    assert [item.key for item in snapshot.items] == ["notion:pageone", "notion:pagetwo"]
    assert http.calls[1][2]["json"]["start_cursor"] == "cursor"
    assert http.calls[0][2]["idempotent"] is True
    assert http.calls[0][2]["headers"]["Notion-Version"] == "2022-06-28"


def test_bad_optional_date_warns_without_dropping_row() -> None:
    http = FakeHttp([{"results": [row(deadline="tomorrow")], "has_more": False}])
    snapshot = NotionClient("token", "db", http=http).get_active_work()
    assert snapshot.items[0].deadline is None
    assert any("Deadline" in warning for warning in snapshot.warnings)


def test_blank_name_and_wrong_status_type_skip_only_bad_rows() -> None:
    malformed = row("bad", "")
    http = FakeHttp([{"results": [malformed, row("good")], "has_more": False}])
    snapshot = NotionClient("token", "db", http=http).get_active_work()
    assert [item.page_id for item in snapshot.items] == ["good"]
    assert snapshot.warnings


class FakeTaskDatabase:
    def __init__(self, items=None, warnings=None):
        self.items = items or []
        self.warnings = warnings or []
        self.created = []
        self.updated = []

    def get_active_work(self):
        return WorkSnapshot(items=self.items, warnings=self.warnings)

    def create_work_item(self, fields):
        self.created.append(fields)
        return {"id": "created"}

    def update_work_item(self, page_id, fields):
        self.updated.append((page_id, fields))
        return {"id": page_id}


def test_task_store_combines_tables_and_assigns_table_as_area() -> None:
    school_item = NotionWorkItem(
        key="notion:school",
        page_id="school",
        url="https://notion.test/school",
        name="Review notes",
    )
    clients = {name: FakeTaskDatabase() for name in TASK_DATABASES}
    clients["School"] = FakeTaskDatabase([school_item], ["row warning"])
    snapshot = NotionTaskStore(clients=clients).get_active_work()
    assert [(item.name, item.area) for item in snapshot.items] == [("Review notes", "School")]
    assert snapshot.warnings == ["School: row warning"]


def test_task_store_routes_new_items_and_updates_existing_pages() -> None:
    clients = {name: FakeTaskDatabase() for name in TASK_DATABASES}
    store = NotionTaskStore(clients=clients)
    store.create_work_item({"Name": "Follow up", "Area": "Connections", "Status": "Active"})
    assert clients["Connections"].created == [{"Name": "Follow up", "Status": "Active"}]
    store.update_work_item("page", {"Status": "Done"})
    assert clients["Work"].updated == [("page", {"Status": "Done"})]
    with pytest.raises(ValueError, match="unknown task database"):
        store.create_work_item({"Name": "Other", "Area": "Unknown"})
