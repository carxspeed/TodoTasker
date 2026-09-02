"""Notion Work database adapter and property builders."""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .atomic import atomic_write_json
from .http import HttpClient, HttpFailure
from .models import NotionWorkItem


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TASK_DATABASES = ("Work", "School", "Connections", "Misc")
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


@dataclass
class BriefPageResult:
    page_id: str
    url: str
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

    def retrieve_database(self) -> dict[str, Any]:
        return self._json("GET", f"/databases/{self.work_db_id}")

    def create_work_database(self, title: str = "Work") -> dict[str, Any]:
        if title not in TASK_DATABASES:
            raise ValueError(f"unknown task database: {title}")
        payload = {
            "parent": {"type": "page_id", "page_id": self.parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
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

    def archive_work_item(self, page_id: str) -> dict[str, Any]:
        return self._json(
            "PATCH",
            f"/pages/{page_id}",
            payload={"archived": True},
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

    def find_brief_page(self, day: date) -> BriefPageResult | None:
        title = f"Brief — {day.isoformat()}"
        matches = [
            child
            for child in self.list_block_children(self.parent_page_id)
            if child.get("type") == "child_page"
            and (child.get("child_page") or {}).get("title") == title
        ]
        if not matches:
            return None
        matches.sort(key=lambda value: (value.get("created_time") or "", value.get("id") or ""))
        chosen = matches[0]
        warnings = []
        if len(matches) > 1:
            duplicate_ids = ", ".join(str(value.get("id")) for value in matches[1:])
            warnings.append(f"Duplicate exact-title brief pages found and not modified: {duplicate_ids}")
        return BriefPageResult(
            page_id=str(chosen["id"]),
            url=str(chosen.get("url") or f"https://www.notion.so/{chosen['id']}"),
            warnings=warnings,
        )

    def _create_brief_page(self, day: date) -> BriefPageResult:
        body = self._json(
            "POST",
            "/pages",
            payload={
                "parent": {"type": "page_id", "page_id": self.parent_page_id},
                "properties": {
                    "title": {
                        "title": [
                            {"text": {"content": f"Brief — {day.isoformat()}"}}
                        ]
                    }
                },
            },
            idempotent=False,
        )
        return BriefPageResult(page_id=str(body["id"]), url=str(body.get("url") or ""))

    @staticmethod
    def _rich_text(content: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": {"content": content}}]

    @classmethod
    def _markdown_blocks(cls, markdown: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for raw_line in markdown.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            block_type = "paragraph"
            content = line
            if line.startswith("### "):
                block_type, content = "heading_3", line[4:]
            elif line.startswith("## "):
                block_type, content = "heading_2", line[3:]
            elif line.startswith("# "):
                block_type, content = "heading_1", line[2:]
            elif line.startswith("- "):
                block_type, content = "bulleted_list_item", line[2:]
            for start in range(0, max(1, len(content)), 2000):
                fragment = content[start : start + 2000]
                blocks.append(
                    {
                        "object": "block",
                        "type": block_type,
                        block_type: {"rich_text": cls._rich_text(fragment)},
                    }
                )
        return blocks

    @staticmethod
    def _block_text(block: dict[str, Any]) -> str:
        block_type = block.get("type")
        parts = (block.get(block_type) or {}).get("rich_text") or []
        return "".join(
            str(part.get("plain_text") or (part.get("text") or {}).get("content") or "")
            for part in parts
        )

    def _append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> list[str]:
        ids: list[str] = []
        for start in range(0, len(blocks), 100):
            body = self._json(
                "PATCH",
                f"/blocks/{page_id}/children",
                payload={"children": blocks[start : start + 100]},
                idempotent=False,
            )
            ids.extend(str(value["id"]) for value in body.get("results") or [] if value.get("id"))
        return ids

    def _archive_block(self, block_id: str) -> None:
        self._json(
            "PATCH",
            f"/blocks/{block_id}",
            payload={"archived": True},
            idempotent=False,
        )

    def upsert_brief_page(
        self,
        day: date,
        markdown_body: str,
        *,
        journal_dir: str | Path = "state/notion_updates",
        stored_page_id: str | None = None,
        stored_url: str = "",
    ) -> BriefPageResult:
        warnings: list[str] = []
        if stored_page_id:
            page = BriefPageResult(stored_page_id, stored_url)
        else:
            found = self.find_brief_page(day)
            page = found or self._create_brief_page(day)
            warnings.extend(page.warnings)
        payload_hash = hashlib.sha256(markdown_body.encode("utf-8")).hexdigest()
        start_marker = f"Generation {payload_hash} start"
        end_marker = f"Generation {payload_hash} end"
        journal_path = Path(journal_dir) / f"{day.isoformat()}.json"
        existing = self.list_block_children(page.page_id)
        existing_text = [self._block_text(block) for block in existing]
        journal: dict[str, Any] | None = None
        if journal_path.exists():
            try:
                candidate = json.loads(journal_path.read_text(encoding="utf-8"))
                if candidate.get("page_id") == page.page_id and candidate.get("payload_hash") == payload_hash:
                    journal = candidate
            except (OSError, ValueError):
                journal = None

        if end_marker in existing_text:
            old_ids = (journal or {}).get("old_block_ids", [])
            for block_id in old_ids:
                if block_id not in (journal or {}).get("new_block_ids", []):
                    self._archive_block(block_id)
            complete = journal or {
                "page_id": page.page_id,
                "payload_hash": payload_hash,
                "old_block_ids": [],
                "new_block_ids": [],
            }
            complete["phase"] = "complete"
            atomic_write_json(journal_path, complete)
            return BriefPageResult(page.page_id, page.url, warnings)

        if start_marker in existing_text:
            start_index = existing_text.index(start_marker)
            for block in existing[start_index:]:
                try:
                    self._archive_block(str(block["id"]))
                except Exception:
                    pass
            existing = existing[:start_index]

        old_ids = [str(block["id"]) for block in existing if block.get("id")]
        journal = {
            "page_id": page.page_id,
            "payload_hash": payload_hash,
            "phase": "appending",
            "old_block_ids": old_ids,
            "new_block_ids": [],
        }
        atomic_write_json(journal_path, journal)
        new_blocks = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": self._rich_text(start_marker)},
            },
            *self._markdown_blocks(markdown_body),
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": self._rich_text(end_marker)},
            },
        ]
        try:
            for start in range(0, len(new_blocks), 100):
                new_ids = self._append_blocks(page.page_id, new_blocks[start : start + 100])
                journal["new_block_ids"].extend(new_ids)
                atomic_write_json(journal_path, journal)
        except Exception:
            journal["phase"] = "append_failed"
            atomic_write_json(journal_path, journal)
            for block_id in journal["new_block_ids"]:
                try:
                    self._archive_block(block_id)
                except Exception:
                    pass
            raise
        journal["phase"] = "new_complete"
        atomic_write_json(journal_path, journal)
        for block_id in old_ids:
            self._archive_block(block_id)
        journal["phase"] = "complete"
        atomic_write_json(journal_path, journal)
        return BriefPageResult(page.page_id, page.url, warnings)


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
