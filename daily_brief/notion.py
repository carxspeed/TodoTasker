"""Notion Work database adapter and property builders."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from .http import HttpClient, HttpFailure
from .models import NotionWorkItem


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
AREAS = ["MindSpark", "Biosensor", "GoDaddy", "AI Club", "College Apps", "Personal"]
TYPES = ["Project", "Task", "Recurring"]
CADENCES = ["Daily", "2x/week", "Weekly", "Biweekly", "None"]
STATUSES = ["Active", "Paused", "Done"]
EFFORTS = ["S", "M", "L"]


class NotionError(RuntimeError):
    pass


@dataclass
class WorkSnapshot:
    items: list[NotionWorkItem]
    warnings: list[str] = field(default_factory=list)


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
        "Area": options(AREAS),
        "Type": options(TYPES),
        "Cadence": options(CADENCES),
        "Last touched": {"date": {}},
        "Status": options(STATUSES),
        "Next step": {"rich_text": {}},
        "Deadline": {"date": {}},
        "Effort": options(EFFORTS),
    }


def work_properties(fields: dict[str, Any]) -> dict[str, Any]:
    builders = {
        "Name": title_property,
        "Area": select_property,
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

    def _json(self, method: str, path: str, *, payload=None, idempotent=None) -> Any:
        try:
            response = self.http.request_json(
                method,
                f"{NOTION_API}{path}",
                source="notion",
                headers=self.headers,
                json=payload,
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

    def retrieve_database(self) -> dict[str, Any]:
        return self._json("GET", f"/databases/{self.work_db_id}")

    def create_work_database(self) -> dict[str, Any]:
        payload = {
            "parent": {"type": "page_id", "page_id": self.parent_page_id},
            "title": [{"type": "text", "text": {"content": "Work"}}],
            "properties": database_schema(),
        }
        return self._json("POST", "/databases", payload=payload, idempotent=False)

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

