from datetime import date

import pytest

from daily_brief.http import JsonResponse
from daily_brief.models import NotionWorkItem
from daily_brief.notion import (
    TASK_DATABASES,
    NotionClient,
    NotionTaskStore,
    WorkSnapshot,
    database_schema,
    date_property,
    rich_text_property,
    select_property,
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


def test_named_task_database_creation_and_archive_payloads() -> None:
    http = FakeHttp([{"id": "db"}, {"id": "page"}])
    client = NotionClient("token", "db", "parent", http=http)
    result = client.create_work_database("School")
    assert result == {"id": "db"}
    assert http.calls[0][0:2] == ("POST", "https://api.notion.com/v1/databases")
    assert http.calls[0][2]["json"]["title"][0]["text"]["content"] == "School"
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
