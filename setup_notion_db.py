"""Validate or create the single Notion Work database."""

from __future__ import annotations

import sys
from pathlib import Path

from daily_brief.config import ConfigurationError, Settings, load_settings
from daily_brief.envfile import persist_env_value
from daily_brief.notion import TASK_DATABASES, NotionClient, NotionError


DATABASE_ENV_KEYS = {
    "Work": "NOTION_WORK_DB_ID",
    "School": "NOTION_SCHOOL_DB_ID",
    "Connections": "NOTION_CONNECTIONS_DB_ID",
    "Misc": "NOTION_MISC_DB_ID",
}


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
    for title in TASK_DATABASES:
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
