"""Create the task tables, including one School page with per-class tables."""

from __future__ import annotations

import sys
from pathlib import Path

from daily_brief.config import ConfigurationError, Settings, load_settings
from daily_brief.envfile import persist_env_value
from daily_brief.notion import NotionClient, NotionError


DATABASE_ENV_KEYS = {
    "Work": "NOTION_WORK_DB_ID",
    "School": "NOTION_SCHOOL_DB_ID",
    "Connections": "NOTION_CONNECTIONS_DB_ID",
    "Misc": "NOTION_MISC_DB_ID",
}
SCHOOL_PAGE_ENV_KEY = "NOTION_SCHOOL_PAGE_ID"
GENERAL_SCHOOL_TABLE = "General"


def persist_database_id(path: Path, env_key: str, database_id: str) -> None:
    persist_env_value(path, env_key, database_id.replace("-", ""))


def persist_work_db_id(path: Path, database_id: str) -> None:
    """Backward-compatible wrapper used by existing installations and tests."""
    persist_database_id(path, "NOTION_WORK_DB_ID", database_id)


def configure_task_databases(
    settings: Settings,
    *,
    env_path: Path = Path(".env"),
    client_factory=NotionClient,
) -> list[str]:
    messages: list[str] = []
    parent_client = client_factory(
        settings.notion_token,
        "",
        settings.notion_parent_page_id,
    )
    parent_client.retrieve_parent_page()
    configured = settings.notion_database_ids
    for title in ("Work", "Connections", "Misc"):
        database_id = configured[title]
        env_key = DATABASE_ENV_KEYS[title]
        if database_id:
            database = client_factory(
                settings.notion_token,
                database_id,
                settings.notion_parent_page_id,
            ).retrieve_database()
            messages.append(f"Validated {title} database: {database.get('id', database_id)}")
            continue
        database = parent_client.create_work_database(title)
        persist_database_id(env_path, env_key, database["id"])
        messages.append(f"Created {title} database and saved {env_key}")

    children = parent_client.list_block_children(settings.notion_parent_page_id)
    school_page_id = settings.notion_school_page_id
    if school_page_id:
        parent_client.retrieve_page(school_page_id)
        messages.append(f"Validated School page: {school_page_id}")
    else:
        matching_pages = [
            child
            for child in children
            if child.get("type") == "child_page"
            and (child.get("child_page") or {}).get("title") == "School"
        ]
        page = (
            matching_pages[0]
            if matching_pages
            else parent_client.create_child_page("School")
        )
        school_page_id = str(page["id"]).replace("-", "")
        persist_database_id(env_path, SCHOOL_PAGE_ENV_KEY, school_page_id)
        messages.append("Created School page and saved NOTION_SCHOOL_PAGE_ID")

    school_children = parent_client.list_block_children(school_page_id)
    general_matches = [
        child
        for child in school_children
        if child.get("type") == "child_database"
        and (child.get("child_database") or {}).get("title") == GENERAL_SCHOOL_TABLE
    ]
    current_school_id = configured["School"]
    current_database = None
    if current_school_id:
        current_database = client_factory(
            settings.notion_token,
            current_school_id,
            school_page_id,
        ).retrieve_database()
        current_parent = str(
            (current_database.get("parent") or {}).get("page_id") or ""
        ).replace("-", "")
        if current_parent == school_page_id:
            messages.append(f"Validated School General database: {current_school_id}")
            return messages

    if general_matches:
        general_id = str(general_matches[0]["id"]).replace("-", "")
    else:
        general = parent_client.create_database(
            GENERAL_SCHOOL_TABLE,
            parent_page_id=school_page_id,
            is_inline=True,
        )
        general_id = str(general["id"]).replace("-", "")
        messages.append("Created General table on the School page")

    if current_school_id:
        source = client_factory(
            settings.notion_token,
            current_school_id,
            settings.notion_parent_page_id,
        )
        destination = client_factory(
            settings.notion_token,
            general_id,
            school_page_id,
        )
        existing_names = {
            item.name.casefold() for item in destination.get_active_work().items
        }
        for item in source.get_active_work().items:
            if item.name.casefold() in existing_names:
                continue
            destination.create_work_item(
                {
                    "Name": item.name,
                    "Type": item.type or "Task",
                    "Cadence": item.cadence,
                    "Last touched": item.last_touched,
                    "Status": "Active",
                    "Next step": item.next_step,
                    "Deadline": item.deadline,
                    "Effort": item.effort,
                }
            )
            existing_names.add(item.name.casefold())
        persist_database_id(env_path, "NOTION_SCHOOL_DB_ID", general_id)
        source.archive_database(current_school_id)
        messages.append("Moved existing School rows into School / General and archived the old table")
    else:
        persist_database_id(env_path, "NOTION_SCHOOL_DB_ID", general_id)
        messages.append("Saved School General database as NOTION_SCHOOL_DB_ID")
    return messages


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        settings = load_settings(required=("NOTION_TOKEN", "NOTION_PARENT_PAGE_ID"))
        for message in configure_task_databases(settings):
            print(message)
        return 0
    except (ConfigurationError, NotionError, KeyError) as exc:
        print(f"NOTION_SETUP_ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
