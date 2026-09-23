from datetime import date

import pytest

from daily_brief.http import JsonResponse
from daily_brief.models import DailyNotification, NotionWorkItem, NotificationTask
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
    master_view_specs,
    notion_master_task_fields,
    school_assignment_fields,
    school_database_schema,
    school_properties,
    compact_instruction_summary,
    display_task_type,
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
    assert master_task_schema()["Status"]["select"]["options"]
    assert master_task_schema()["Open"]["formula"]["expression"].startswith("if(")
    assert master_task_schema()["Sort order"]["formula"]["expression"].startswith("ifs(")
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


def test_display_type_is_human_meaningful_instead_of_raw_canvas_kind() -> None:
    assert display_task_type("Unit 2 MCQ", "assignment") == "Test / Quiz"
    assert display_task_type("Millions Lab", "assignment") == "Lab"
    assert display_task_type("Federalist essay", "assignment") == "Essay / Writing"
    assert display_task_type("Teach Me Project", "assignment") == "Project"
    assert display_task_type("Call advisor", "Task", source_type="Notion") == "Personal task"


def test_master_views_hide_bookkeeping_and_give_actions_real_width() -> None:
    property_ids = {
        name: f"id-{index}" for index, name in enumerate(master_task_schema(), start=1)
    }
    specs = {spec["name"]: spec for spec in master_view_specs(property_ids)}

    assert {
        "Active tasks",
        "Today",
        "Due calendar",
        "School",
        "Work",
        "Connections",
        "Misc",
        "Needs attention",
        "Submitted / waiting",
        "History",
        "_System",
    } == set(specs)
    active = specs["Active tasks"]
    columns = active["configuration"]["properties"]
    by_id = {column["property_id"]: column for column in columns}
    assert by_id[property_ids["Next step"]]["width"] == 420
    assert by_id[property_ids["Instructions"]]["width"] == 420
    assert by_id[property_ids["Focus rank"]]["visible"] is False
    assert by_id[property_ids["Source ID"]]["visible"] is False
    calendar = specs["Due calendar"]
    assert calendar["type"] == "calendar"
    assert calendar["configuration"] == {
        "type": "calendar",
        "date_property_id": property_ids["Due"],
        "properties": calendar["configuration"]["properties"],
        "view_range": "month",
        "show_weekends": True,
    }
    assert calendar["position"] == {"type": "start"}
    assert calendar["filter"]["and"][-1] == {
        "property": property_ids["Due"],
        "date": {"is_not_empty": True},
    }
    assert "group_by" not in specs["School"]["configuration"]


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


def test_classifier_verify_priority_marks_notion_row_for_confirmation() -> None:
    assignment = load_fixture("fixtures/sample_todo.json").assignments[0].model_copy(
        update={"submission_status": "unsubmitted", "needs_confirmation": False}
    )

    fields = school_assignment_fields(assignment, {"Priority": "Verify"})

    assert fields["Status"] == "Verify"
    assert fields["Priority"] == "Verify"


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
        self.created_pages = []
        self.appended_blocks = []
        self.archived_blocks = []

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

    def create_child_page(self, title, **kwargs):
        self.created_pages.append((title, kwargs))
        return {"id": "today-page", "url": "https://notion.test/today"}

    def append_block_children(self, page_id, blocks):
        self.appended_blocks.append((page_id, blocks))
        return {"results": blocks}

    def archive_block(self, block_id):
        self.archived_blocks.append(block_id)
        return {"id": block_id, "archived": True}


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


def test_notion_master_fields_keep_raw_next_step_instead_of_generated_guidance() -> None:
    item = NotionWorkItem(
        key="notion:work",
        page_id="work",
        url="https://notion.test/work",
        name="Prepare weekly update",
        area="Work",
        next_step="Start with this next step: Draft three bullets.",
    )

    fields = notion_master_task_fields(
        item,
        {"Next step": "Start with this next step: Start with this next step: Draft three bullets."},
    )

    assert fields["Next step"] == "Draft three bullets."


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


def test_master_sync_preserves_manual_status_and_archives_examples() -> None:
    assignment = load_fixture("fixtures/sample_todo.json").assignments[0]
    fake = FakeSchoolClient()
    fake.children = [
        {"type": "child_database", "id": "master", "child_database": {"title": "Tasks"}}
    ]
    fake.query_database_pages = lambda _database_id: [
        {
            "id": "current",
            "url": "https://notion.test/current",
            "properties": {
                "Task": text_prop("title", assignment.name),
                "Source ID": text_prop("rich_text", assignment.key),
                "Source type": select_prop("Canvas"),
                "Status": select_prop("Submitted"),
                "Archived": {"type": "checkbox", "checkbox": False},
                "Sync hash": text_prop("rich_text", "old"),
            },
        },
        {
            "id": "example",
            "properties": {
                "Task": text_prop("title", "Example: Review class notes"),
                "Source ID": text_prop("rich_text", "notion:example"),
                "Source type": select_prop("Notion"),
                "Status": select_prop("To do"),
                "Archived": {"type": "checkbox", "checkbox": False},
                "Sync hash": text_prop("rich_text", "example"),
            },
        },
    ]
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    result = board.sync_master_tasks([assignment], [])

    assert fake.updated_rows[0][0] == "current"
    assert fake.updated_rows[0][1]["Status"] == "Submitted"
    assert fake.updated_rows[1] == ("example", {"Archived": True})
    assert result.rows_archived == 1


def test_stale_canvas_rows_retire_only_after_authoritative_refresh() -> None:
    def stale_page(status="To do"):
        return {
            "id": "stale",
            "properties": {
                "Task": text_prop("title", "Old assignment"),
                "Source ID": text_prop("rich_text", "assignment:old"),
                "Source type": select_prop("Canvas"),
                "Status": select_prop(status),
                "Archived": {"type": "checkbox", "checkbox": False},
                "Sync hash": text_prop("rich_text", "old"),
            },
        }

    fake = FakeSchoolClient()
    fake.children = [
        {"type": "child_database", "id": "master", "child_database": {"title": "Tasks"}}
    ]
    fake.query_database_pages = lambda _database_id: [stale_page()]
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    board.sync_master_tasks([], [], authoritative_canvas=False)
    assert fake.updated_rows == []

    result = board.sync_master_tasks([], [], authoritative_canvas=True)
    assert fake.updated_rows == [("stale", {"Archived": True})]
    assert result.rows_archived == 1

    fake.updated_rows.clear()
    fake.query_database_pages = lambda _database_id: [stale_page("Needs remake")]
    board.sync_master_tasks([], [], authoritative_canvas=True)
    assert fake.updated_rows == []


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
            "status": "Done",
            "archived": False,
            "notes": "Submitted in person.",
            "url": "https://notion.test/row",
        }
    }


def test_master_work_uses_the_master_database_and_adopts_manual_rows() -> None:
    fake = FakeSchoolClient()
    fake.children = [
        {"type": "child_database", "id": "master", "child_database": {"title": "Tasks"}}
    ]
    fake.query_database_pages = lambda _database_id: [
        {
            "id": "manual-row",
            "url": "https://notion.test/manual-row",
            "properties": {
                "Task": text_prop("title", "Call the internship coordinator"),
                "Done": {"type": "checkbox", "checkbox": False},
                "Area": select_prop("Connections"),
                "Source type": select_prop(None),
                "Source ID": text_prop("rich_text", ""),
                "Task type": select_prop("Task"),
                "Kind": text_prop("rich_text", ""),
                "Effort": select_prop("S"),
                "Cadence": select_prop(None),
                "Due": date_prop("2026-09-18"),
                "Last touched": date_prop(None),
                "Next step": text_prop(
                    "rich_text",
                    "Start with this next step: Start with this next step: Send a short follow-up.",
                ),
            },
        },
        {
            "id": "canvas-row",
            "properties": {
                "Source type": select_prop("Canvas"),
                "Done": {"type": "checkbox", "checkbox": False},
            },
        },
    ]
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    snapshot = board.get_master_work()

    assert len(snapshot.items) == 1
    item = snapshot.items[0]
    assert item.key == "notion:manualrow"
    assert item.area == "Connections"
    assert item.deadline == date(2026, 9, 18)
    assert item.next_step == "Send a short follow-up."


def test_canvas_row_marked_needs_remake_reenters_the_work_feed() -> None:
    fake = FakeSchoolClient()
    fake.children = [
        {"type": "child_database", "id": "master", "child_database": {"title": "Tasks"}}
    ]
    fake.query_database_pages = lambda _database_id: [
        {
            "id": "retake",
            "url": "https://notion.test/retake",
            "properties": {
                "Task": text_prop("title", "Unit 2 Test retake"),
                "Source type": select_prop("Canvas"),
                "Source ID": text_prop("rich_text", "assignment:retake"),
                "Status": select_prop("Needs remake"),
                "Archived": {"type": "checkbox", "checkbox": False},
                "Area": select_prop("School"),
                "Display type": select_prop("Test / Quiz"),
                "Effort": select_prop("M"),
                "Due": date_prop("2026-09-25"),
                "Last touched": date_prop(None),
                "Next step": text_prop("rich_text", "Ask the teacher to schedule the retake."),
            },
        }
    ]
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    item = board.get_master_work().items[0]

    assert item.key == "assignment:retake"
    assert item.status == "Needs remake"
    assert item.type == "Test / Quiz"


def test_master_focus_marks_three_or_fewer_and_clears_old_focus_without_archiving() -> None:
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
        target_date=date(2026, 9, 17),
        as_of=datetime(2026, 9, 17, tzinfo=timezone.utc),
        must=[item],
    )
    fake = FakeSchoolClient()
    fake.children = [
        {"type": "child_database", "id": "master", "child_database": {"title": "Tasks"}}
    ]
    fake.query_database_pages = lambda _database_id: [
        {
            "id": "current",
            "properties": {
                "Source ID": text_prop("rich_text", assignment.key),
                "Focus date": date_prop(None),
                "Focus rank": {"type": "number", "number": None},
                "Focus reason": text_prop("rich_text", ""),
            },
        },
        {
            "id": "stale",
            "properties": {
                "Source ID": text_prop("rich_text", "assignment:stale"),
                "Focus date": date_prop("2026-09-16"),
                "Focus rank": {"type": "number", "number": 1},
                "Focus reason": text_prop("rich_text", "Old reason"),
            },
        },
    ]
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    result = board.sync_master_focus(
        classification,
        guidance_by_key={assignment.key: "Finish requirement one."},
        focus_reason="It is due first.",
        target_date=date(2026, 9, 17),
    )

    assert result.rows_focused == 1
    assert result.rows_cleared == 1
    assert result.missing_source_ids == ()
    assert fake.updated_rows[0][0] == "current"
    assert fake.updated_rows[0][1]["Focus rank"] == 1
    assert fake.updated_rows[0][1]["Focus reason"] == "It is due first."
    assert fake.updated_rows[1] == (
        "stale",
        {"Focus date": None, "Focus rank": None, "Focus reason": ""},
    )
    assert fake.archived_rows == []


def test_master_focus_prioritizes_assessment_and_targets_its_real_assignment() -> None:
    from daily_brief.models import ClassificationOutput, ClassifiedItem
    from datetime import datetime, timezone

    ordinary = ClassifiedItem(
        key="assignment:ordinary",
        source="canvas",
        name="Old worksheet",
        tier="must",
        effort="S",
        effort_hours=0.5,
        effort_source="points",
    )
    assessment = ordinary.model_copy(
        update={
            "key": "planner-assessment:quiz-2",
            "name": "Study for Quiz 2",
            "kind": "planner_assessment",
            "effort": "M",
            "effort_hours": 1.5,
        }
    )
    classification = ClassificationOutput(
        target_date=date(2026, 9, 17),
        as_of=datetime(2026, 9, 17, tzinfo=timezone.utc),
        must=[ordinary, assessment],
    )
    fake = FakeSchoolClient()
    fake.children = [
        {"type": "child_database", "id": "master", "child_database": {"title": "Tasks"}}
    ]
    fake.query_database_pages = lambda _database_id: [
        {
            "id": "ordinary-row",
            "properties": {
                "Source ID": text_prop("rich_text", ordinary.key),
                "Focus date": date_prop(None),
                "Focus rank": {"type": "number", "number": None},
                "Focus reason": text_prop("rich_text", ""),
            },
        },
        {
            "id": "quiz-row",
            "properties": {
                "Source ID": text_prop("rich_text", "assignment:quiz-2"),
                "Focus date": date_prop(None),
                "Focus rank": {"type": "number", "number": None},
                "Focus reason": text_prop("rich_text", ""),
            },
        },
    ]
    board = NotionSchoolBoard("", "parent", "school", client=fake)

    result = board.sync_master_focus(
        classification,
        source_id_by_focus_key={assessment.key: "assignment:quiz-2"},
        target_date=date(2026, 9, 17),
    )

    assert result.missing_source_ids == ()
    assert fake.updated_rows[0][0] == "quiz-row"
    assert fake.updated_rows[0][1]["Focus rank"] == 1
    assert "imminent assessment" in fake.updated_rows[0][1]["Focus reason"]
    assert fake.updated_rows[0][1]["Next step"].startswith(
        "Study the topics listed in the class planner"
    )


def test_focus_dashboard_is_phone_first_and_links_exact_task_rows() -> None:
    from datetime import datetime, timezone

    fake = FakeSchoolClient()
    board = NotionSchoolBoard("", "parent", "school", client=fake)
    task = NotificationTask(
        key="assignment:1",
        name="Finish the physics lab",
        course="AP Physics",
        next_step="Complete the graph and write the conclusion.",
        due_at=datetime(2026, 9, 21, 21, 0, tzinfo=timezone.utc),
        effort_hours=1.5,
        url="https://notion.test/task-1",
    )
    notification = DailyNotification(
        target_date=date(2026, 9, 21),
        primary=task,
        backlog_count=17,
        verify_count=2,
    )

    result = board.sync_focus_dashboard(
        notification,
        full_tasks_url="https://notion.test/tasks",
    )

    assert result.url == "https://notion.test/today"
    assert result.page_created is True
    assert fake.created_pages == [("Today", {"parent_page_id": "parent"})]
    assert len(fake.appended_blocks) == 1
    blocks = fake.appended_blocks[0][1]
    assert [block["type"] for block in blocks] == ["paragraph", "callout", "paragraph"]
    task_text = blocks[1]["callout"]["rich_text"]
    assert task_text[0]["text"]["link"] == {"url": "https://notion.test/task-1"}
    assert "17 other task(s)" in blocks[-1]["paragraph"]["rich_text"][0]["text"]["content"]
    assert fake.archived_blocks == []


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


def test_view_api_uses_current_version_and_decodes_property_ids() -> None:
    http = FakeHttp(
        [
            {"data_sources": [{"id": "source"}]},
            {
                "properties": {
                    "Task": {"id": "title", "type": "title"},
                    "Due": {"id": "%5EuQv", "type": "date"},
                }
            },
            {"id": "view", "url": "https://notion.test/view"},
            {"id": "view", "url": "https://notion.test/view"},
        ]
    )
    client = NotionClient("token", "", http=http)

    source_id, property_ids = client.master_property_ids("database")
    created = client.create_view(
        "database",
        source_id,
        {
            "name": "Active tasks",
            "type": "table",
            "filter": None,
            "position": {"type": "start"},
        },
    )
    client.update_view(
        "view",
        {
            "name": "Active tasks",
            "type": "table",
            "position": {"type": "start"},
        },
    )

    assert source_id == "source"
    assert property_ids["Due"] == "^uQv"
    assert created["id"] == "view"
    assert all(
        call[2]["headers"]["Notion-Version"] == "2026-03-11"
        for call in http.calls
    )
    assert http.calls[2][2]["json"]["database_id"] == "database"
    assert "type" not in http.calls[3][2]["json"]
    assert "position" not in http.calls[3][2]["json"]


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
