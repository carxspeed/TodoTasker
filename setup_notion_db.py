"""Validate or create the single Notion Work database."""

from __future__ import annotations

from daily_brief.config import ConfigurationError, load_settings
from daily_brief.notion import NotionClient, NotionError
import sys


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        settings = load_settings(required=("NOTION_TOKEN", "NOTION_PARENT_PAGE_ID"))
        client = NotionClient(
            settings.notion_token,
            settings.notion_work_db_id,
            settings.notion_parent_page_id,
        )
        client.retrieve_parent_page()
        if settings.notion_work_db_id:
            database = client.retrieve_database()
            print(f"Validated existing Work database: {database.get('id', settings.notion_work_db_id)}")
        else:
            database = client.create_work_database()
            print(f"Created Work database. Set NOTION_WORK_DB_ID={database['id'].replace('-', '')}")
        return 0
    except (ConfigurationError, NotionError, KeyError) as exc:
        print(f"NOTION_SETUP_ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
