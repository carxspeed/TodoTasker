"""Notion Work database adapter and property builders."""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from .http import HttpClient, HttpFailure
from .models import NotionWorkItem


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TASK_DATABASES = ("Work", "School", "Connections", "Misc")
TYPES = ["Project", "Task", "Recurring"]
CADENCES = ["Daily", "2x/week", "Weekly", "Biweekly", "None"]
STATUSES = ["Active", "Paused", "Done"]
EFFORTS = ["S", "M", "L"]
SCHOOL_STATUSES = ["To do", "Verify", "Done"]
SCHOOL_PRIORITIES = ["MUST", "SMART", "MAY", "Later", "Verify"]
SCHOOL_KINDS = ["assignment", "quiz", "discussion_topic", "sub_assignment"]


class NotionError(RuntimeError):
    pass


@dataclass
class WorkSnapshot:
    items: list[NotionWorkItem]
    warnings: list[str] = field(default_factory=list)


@dataclass
class SchoolSyncResult:
    page_id: str
    url: str
    databases_created: int = 0
    rows_created: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0


def title_property(value: str) -> dict[str, Any]:
    return {"title": [{"text": {"content": value}}]}


def rich_text_property(value: str | None) -> dict[str, Any]:
    return {"rich_text": [{"text": {"content": value}}]} if value else {"rich_text": []}


def select_property(value: str | None) -> dict[str, Any]:
    return {"select": {"name": value}} if value else {"select": None}


def date_property(value: date | str | None) -> dict[str, Any]:
    if isinstance(value, date):
        value = value.isoformat()
    return {"date": {"start": value}} if value else {"date": None}


def database_schema() -> dict[str, Any]:
    def options(values: Iterable[str]) -> dict[str, Any]:
        return {"select": {"options": [{"name": value} for value in values]}}

    return {
        "Name": {"title": {}},
        "Type": options(TYPES),
        "Cadence": options(CADENCES),
        "Last touched": {"date": {}},
        "Status": options(STATUSES),
        "Next step": {"rich_text": {}},
        "Deadline": {"date": {}},
        "Effort": options(EFFORTS),
    }


def school_database_schema() -> dict[str, Any]:
    def options(values: Iterable[str]) -> dict[str, Any]:
        return {"select": {"options": [{"name": value} for value in values]}}

    return {
        "Name": {"title": {}},
        "Due": {"date": {}},
        "Status": options(SCHOOL_STATUSES),
        "Priority": options(SCHOOL_PRIORITIES),
        "Effort": options(EFFORTS),
        "Kind": options(SCHOOL_KINDS),
        "Next step": {"rich_text": {}},
        "Notes / progress": {"rich_text": {}},
        "Instructions": {"rich_text": {}},
        "Canvas": {"url": {}},
        "Canvas ID": {"rich_text": {}},
        "Sync hash": {"rich_text": {}},
    }


def _bounded(value: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip()
    return normalized if len(normalized) <= limit else normalized[: limit - 3].rstrip() + "..."


def school_assignment_fields(
    assignment,
    details: dict[str, str] | None = None,
) -> dict[str, Any]:
    details = details or {}
    status = (
        "Verify"
        if assignment.needs_confirmation or assignment.submission_status == "unknown"
        else "To do"
    )
    instructions = _bounded(assignment.description, 1800)
    next_step = _bounded(
        details.get("Next step")
        or (
            "Open the Canvas assignment and follow the listed requirements."
            if instructions
            else "Open the Canvas assignment and review the requirements."
        ),
        1000,
    )
    fields: dict[str, Any] = {
        "Name": _bounded(assignment.name, 500),
        "Due": assignment.due_at.isoformat() if assignment.due_at else None,
        "Status": status,
        "Priority": details.get("Priority")
        or ("Verify" if status == "Verify" else "Later"),
        "Effort": details.get("Effort") or None,
        "Kind": assignment.kind,
        "Next step": next_step,
        "Instructions": instructions,
        "Canvas": assignment.url or None,
        "Canvas ID": assignment.key,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    fields["Sync hash"] = fingerprint
    return fields


def school_properties(fields: dict[str, Any]) -> dict[str, Any]:
    builders = {
        "Name": title_property,
        "Due": date_property,
        "Status": select_property,
        "Priority": select_property,
        "Effort": select_property,
        "Kind": select_property,
        "Next step": rich_text_property,
        "Notes / progress": rich_text_property,
        "Instructions": rich_text_property,
        "Canvas": lambda value: {"url": value or None},
        "Canvas ID": rich_text_property,
        "Sync hash": rich_text_property,
    }
    unknown = set(fields) - set(builders)
    if unknown:
        raise ValueError(f"unknown School properties: {', '.join(sorted(unknown))}")
    return {name: builders[name](value) for name, value in fields.items()}


def work_properties(fields: dict[str, Any]) -> dict[str, Any]:
    builders = {
        "Name": title_property,
        "Type": select_property,
        "Cadence": select_property,
        "Last touched": date_property,
        "Status": select_property,
        "Next step": rich_text_property,
        "Deadline": date_property,
        "Effort": select_property,
    }
    unknown = set(fields) - set(builders)
    if unknown:
        raise ValueError(f"unknown Work properties: {', '.join(sorted(unknown))}")
    return {name: builders[name](value) for name, value in fields.items()}


def _plain_text(prop: dict[str, Any], expected_type: str) -> str:
    if prop.get("type") != expected_type or not isinstance(prop.get(expected_type), list):
        raise ValueError(f"expected {expected_type} property")
    return re.sub(
        r"\s+",
        " ",
        "".join(str(part.get("plain_text", "")) for part in prop[expected_type]),
    ).strip()


def _select(prop: dict[str, Any] | None, name: str) -> str | None:
    if prop is None:
        return None
    if prop.get("type") != "select":
        raise ValueError(f"{name} has wrong property type")
    selected = prop.get("select")
    if selected is None:
        return None
    return selected.get("name")


def _date(prop: dict[str, Any] | None, name: str, warnings: list[str], page_id: str) -> date | None:
    if prop is None:
        return None
    if prop.get("type") != "date":
        raise ValueError(f"{name} has wrong property type")
    raw = prop.get("date")
    if raw is None:
        return None
    start = raw.get("start")
    try:
        parsed = date.fromisoformat(start)
        if parsed.isoformat() != start:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        warnings.append(f"Notion row {page_id}: {name} is not a valid date and was ignored")
        return None


class NotionClient:
    def __init__(self, token: str, work_db_id: str, parent_page_id: str = "", *, http=None) -> None:
        self.work_db_id = work_db_id.replace("-", "")
        self.parent_page_id = parent_page_id.replace("-", "")
        self.http = http or HttpClient()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _json(
        self, method: str, path: str, *, payload=None, params=None, idempotent=None
    ) -> Any:
        try:
            response = self.http.request_json(
                method,
                f"{NOTION_API}{path}",
                source="notion",
                headers=self.headers,
                json=payload,
                params=params,
                idempotent=idempotent,
            )
        except HttpFailure as exc:
            if exc.status == 404:
                raise NotionError(
                    "Notion returned 404: the id may be invalid/nonexistent or the page/database "
                    "may not be shared with the Todo Agent integration"
                ) from exc
            raise NotionError(str(exc)) from exc
        return response.data

    def get_active_work(self) -> WorkSnapshot:
        payload: dict[str, Any] = {
            "page_size": 100,
            "filter": {"property": "Status", "select": {"equals": "Active"}},
        }
        pages: list[dict[str, Any]] = []
        while True:
            body = self._json(
                "POST", f"/databases/{self.work_db_id}/query", payload=payload, idempotent=True
            )
            if not isinstance(body.get("results"), list):
                raise NotionError("Notion query response is missing results")
            pages.extend(body["results"])
            if not body.get("has_more"):
                break
            cursor = body.get("next_cursor")
            if not cursor:
                raise NotionError("Notion query says has_more without next_cursor")
            payload = {**payload, "start_cursor": cursor}

        warnings: list[str] = []
        items: list[NotionWorkItem] = []
        for page in pages:
            page_id = str(page.get("id", "")).replace("-", "")
            props = page.get("properties")
            if not page_id or not isinstance(props, dict):
                warnings.append("Skipped malformed Notion row without id/properties")
                continue
            try:
                name = _plain_text(props.get("Name", {}), "title")
                status = _select(props.get("Status"), "Status")
                if not name or status != "Active":
                    warnings.append(f"Skipped Notion row {page_id}: blank Name or non-Active Status")
                    continue
                area = _select(props.get("Area"), "Area")
                item_type = _select(props.get("Type"), "Type")
                cadence = _select(props.get("Cadence"), "Cadence")
                effort = _select(props.get("Effort"), "Effort")
                next_step = _plain_text(props["Next step"], "rich_text") if "Next step" in props else ""
                last_touched = _date(props.get("Last touched"), "Last touched", warnings, page_id)
                deadline = _date(props.get("Deadline"), "Deadline", warnings, page_id)
            except ValueError as exc:
                warnings.append(f"Skipped Notion row {page_id}: {exc}")
                continue
            if effort not in {*EFFORTS, None}:
                warnings.append(f"Notion row {page_id}: unknown Effort was ignored")
                effort = None
            if len(name) > 200:
                name = name[:197] + "..."
                warnings.append(f"Notion row {page_id}: Name was truncated")
            if len(next_step) > 1000:
                next_step = next_step[:997] + "..."
                warnings.append(f"Notion row {page_id}: Next step was truncated")
            items.append(
                NotionWorkItem(
                    key=f"notion:{page_id}",
                    page_id=page_id,
                    url=str(page.get("url", "")),
                    name=name,
                    area=area,
                    type=item_type,
                    cadence=cadence,
                    last_touched=last_touched,
                    next_step=next_step,
                    deadline=deadline,
                    effort=effort,
                )
            )
        return WorkSnapshot(items=items, warnings=warnings)

    def retrieve_parent_page(self) -> dict[str, Any]:
        return self._json("GET", f"/pages/{self.parent_page_id}")

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self._json("GET", f"/pages/{page_id.replace('-', '')}")

    def retrieve_database(self) -> dict[str, Any]:
        return self._json("GET", f"/databases/{self.work_db_id}")

    def ensure_database_properties(
        self, database_id: str, properties: dict[str, Any]
    ) -> bool:
        compact_id = database_id.replace("-", "")
        database = self._json("GET", f"/databases/{compact_id}")
        existing = database.get("properties") or {}
        missing = {name: schema for name, schema in properties.items() if name not in existing}
        if not missing:
            return False
        self._json(
            "PATCH",
            f"/databases/{compact_id}",
            payload={"properties": missing},
            idempotent=True,
        )
        return True

    def query_database_pages(self, database_id: str) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"page_size": 100}
        pages: list[dict[str, Any]] = []
        compact_id = database_id.replace("-", "")
        while True:
            body = self._json(
                "POST",
                f"/databases/{compact_id}/query",
                payload=payload,
                idempotent=True,
            )
            results = body.get("results")
            if not isinstance(results, list):
                raise NotionError("Notion query response is missing results")
            pages.extend(results)
            if not body.get("has_more"):
                return pages
            cursor = body.get("next_cursor")
            if not cursor:
                raise NotionError("Notion query says has_more without next_cursor")
            payload = {**payload, "start_cursor": cursor}

    def create_child_page(
        self, title: str, *, parent_page_id: str | None = None
    ) -> dict[str, Any]:
        parent_id = (parent_page_id or self.parent_page_id).replace("-", "")
        return self._json(
            "POST",
            "/pages",
            payload={
                "parent": {"type": "page_id", "page_id": parent_id},
                "properties": {"title": title_property(_bounded(title, 200))},
            },
            idempotent=False,
        )

    def create_database(
        self,
        title: str,
        *,
        parent_page_id: str | None = None,
        properties: dict[str, Any] | None = None,
        is_inline: bool = False,
    ) -> dict[str, Any]:
        parent_id = (parent_page_id or self.parent_page_id).replace("-", "")
        payload = {
            "parent": {"type": "page_id", "page_id": parent_id},
            "title": [{"type": "text", "text": {"content": _bounded(title, 200)}}],
            "properties": properties or database_schema(),
            "is_inline": is_inline,
        }
        return self._json("POST", "/databases", payload=payload, idempotent=False)

    def create_work_database(self, title: str = "Work") -> dict[str, Any]:
        if title not in TASK_DATABASES:
            raise ValueError(f"unknown task database: {title}")
        return self.create_database(title)

    def update_work_item(self, page_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id}",
            payload={"properties": work_properties(fields)},
            idempotent=False,
        )

    def create_work_item(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._json(
            "POST",
            "/pages",
            payload={
                "parent": {"type": "database_id", "database_id": self.work_db_id},
                "properties": work_properties(fields),
            },
            idempotent=False,
        )

    def archive_work_item(self, page_id: str) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id}",
            payload={"archived": True},
            idempotent=True,
        )

    def archive_page(self, page_id: str) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id.replace('-', '')}",
            payload={"archived": True},
            idempotent=True,
        )

    def archive_database(self, database_id: str) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/databases/{database_id.replace('-', '')}",
            payload={"archived": True},
            idempotent=True,
        )

    def create_school_item(
        self, database_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            "/pages",
            payload={
                "parent": {
                    "type": "database_id",
                    "database_id": database_id.replace("-", ""),
                },
                "properties": school_properties(fields),
            },
            idempotent=False,
        )

    def update_school_item(
        self, page_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id.replace('-', '')}",
            payload={"properties": school_properties(fields)},
            idempotent=True,
        )

    def list_block_children(self, block_id: str) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            body = self._json(
                "GET",
                f"/blocks/{block_id}/children",
                params=params,
                idempotent=True,
            )
            children.extend(body.get("results") or [])
            if not body.get("has_more"):
                return children
            cursor = body.get("next_cursor")
            if not cursor:
                raise NotionError("Notion children response has no next_cursor")

def _property_rich_text(page: dict[str, Any], name: str) -> str:
    properties = page.get("properties") or {}
    prop = properties.get(name) or {}
    if prop.get("type") != "rich_text":
        return ""
    return "".join(
        str(part.get("plain_text") or (part.get("text") or {}).get("content") or "")
        for part in prop.get("rich_text") or []
    )


def _property_select(page: dict[str, Any], name: str) -> str:
    properties = page.get("properties") or {}
    prop = properties.get(name) or {}
    selected = prop.get("select") if prop.get("type") == "select" else None
    return str((selected or {}).get("name") or "")


class NotionSchoolBoard:
    """Synchronize Canvas assignments into one database per class on the School page."""

    def __init__(
        self,
        token: str = "",
        parent_page_id: str = "",
        school_page_id: str = "",
        *,
        client: Any | None = None,
    ) -> None:
        self.parent_page_id = parent_page_id.replace("-", "")
        self.school_page_id = school_page_id.replace("-", "")
        if not self.parent_page_id or not self.school_page_id:
            raise NotionError("Notion parent and School page IDs are required")
        self.client = client or NotionClient(token, "", self.parent_page_id)

    @staticmethod
    def _child_databases(children: Iterable[dict[str, Any]]) -> dict[str, str]:
        databases: dict[str, str] = {}
        for child in children:
            if child.get("type") != "child_database" or not child.get("id"):
                continue
            title = str((child.get("child_database") or {}).get("title") or "").strip()
            if title and title not in databases:
                databases[title] = str(child["id"])
        return databases

    def sync_canvas_assignments(
        self,
        assignments: Iterable[Any],
        *,
        details_by_key: dict[str, dict[str, str]] | None = None,
        excluded_course_ids: Iterable[int] = (),
    ) -> SchoolSyncResult:
        excluded = set(excluded_course_ids)
        grouped: dict[tuple[int, str], list[Any]] = {}
        for assignment in assignments:
            if assignment.course_id in excluded:
                continue
            course = _bounded(assignment.course or f"Course {assignment.course_id}", 200)
            grouped.setdefault((assignment.course_id, course), []).append(assignment)

        root = self.client.retrieve_page(self.parent_page_id)
        dashboard_url = str(root.get("url") or f"https://www.notion.so/{self.parent_page_id}")
        databases = self._child_databases(
            self.client.list_block_children(self.school_page_id)
        )
        result = SchoolSyncResult(self.parent_page_id, dashboard_url)
        details = details_by_key or {}

        for (_course_id, course), course_assignments in sorted(
            grouped.items(), key=lambda value: value[0][1].casefold()
        ):
            database_id = databases.get(course)
            if not database_id:
                created = self.client.create_database(
                    course,
                    parent_page_id=self.school_page_id,
                    properties=school_database_schema(),
                    is_inline=True,
                )
                database_id = str(created["id"])
                databases[course] = database_id
                result.databases_created += 1
            else:
                self.client.ensure_database_properties(
                    database_id, {"Notes / progress": {"rich_text": {}}}
                )

            existing_by_source: dict[str, tuple[str, str]] = {}
            for page in self.client.query_database_pages(database_id):
                source_id = _property_rich_text(page, "Canvas ID")
                page_id = str(page.get("id") or "")
                if source_id and page_id and source_id not in existing_by_source:
                    existing_by_source[source_id] = (
                        page_id,
                        _property_rich_text(page, "Sync hash"),
                    )

            for assignment in sorted(
                course_assignments,
                key=lambda item: (item.due_at is None, item.due_at, item.name.casefold()),
            ):
                fields = school_assignment_fields(
                    assignment, details.get(assignment.key)
                )
                current = existing_by_source.get(assignment.key)
                if current and current[1] == fields["Sync hash"]:
                    result.rows_unchanged += 1
                elif current:
                    self.client.update_school_item(current[0], fields)
                    result.rows_updated += 1
                else:
                    self.client.create_school_item(database_id, fields)
                    result.rows_created += 1
        return result

    def get_assignment_context(self) -> dict[str, dict[str, str]]:
        """Read user-owned School fields without treating them as Canvas data."""
        context: dict[str, dict[str, str]] = {}
        databases = self._child_databases(
            self.client.list_block_children(self.school_page_id)
        )
        for title, database_id in databases.items():
            if title == "General":
                continue
            self.client.ensure_database_properties(
                database_id, {"Notes / progress": {"rich_text": {}}}
            )
            for page in self.client.query_database_pages(database_id):
                source_id = _property_rich_text(page, "Canvas ID")
                if not source_id:
                    continue
                context[source_id] = {
                    "status": _property_select(page, "Status"),
                    "notes": _bounded(
                        _property_rich_text(page, "Notes / progress"), 1000
                    ),
                }
        return context


class NotionTaskStore:
    """Treat the four task databases as one logical task collection."""

    def __init__(
        self,
        token: str = "",
        database_ids: dict[str, str] | None = None,
        parent_page_id: str = "",
        *,
        clients: dict[str, Any] | None = None,
    ) -> None:
        if clients is not None:
            self.clients = clients
        else:
            configured = database_ids or {}
            missing = [name for name in TASK_DATABASES if not configured.get(name)]
            if missing:
                raise NotionError(
                    "missing Notion task databases: " + ", ".join(missing)
                )
            self.clients = {
                name: NotionClient(token, configured[name], parent_page_id)
                for name in TASK_DATABASES
            }
        missing_clients = [name for name in TASK_DATABASES if name not in self.clients]
        if missing_clients:
            raise NotionError(
                "missing Notion task database clients: " + ", ".join(missing_clients)
            )

    def get_active_work(self) -> WorkSnapshot:
        items: list[NotionWorkItem] = []
        warnings: list[str] = []
        for name in TASK_DATABASES:
            snapshot = self.clients[name].get_active_work()
            items.extend(item.model_copy(update={"area": name}) for item in snapshot.items)
            warnings.extend(f"{name}: {warning}" for warning in snapshot.warnings)
        return WorkSnapshot(items=items, warnings=warnings)

    def create_work_item(self, fields: dict[str, Any]) -> dict[str, Any]:
        database = str(fields.get("Area") or "Misc")
        if database not in self.clients:
            raise ValueError(f"unknown task database: {database}")
        properties = {name: value for name, value in fields.items() if name != "Area"}
        return self.clients[database].create_work_item(properties)

    def update_work_item(self, page_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if "Area" in fields:
            raise ValueError("moving tasks between Notion databases is not supported")
        return self.clients["Work"].update_work_item(page_id, fields)
