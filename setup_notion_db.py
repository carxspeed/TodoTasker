"""Validate or create the single Notion Work database."""

from __future__ import annotations

from daily_brief.config import ConfigurationError, load_settings
from daily_brief.notion import NotionClient, NotionError
import sys
from pathlib import Path


def persist_work_db_id(path: Path, database_id: str) -> None:
    compact = database_id.replace("-", "")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"NOTION_WORK_DB_ID={compact}"
    updated = []
    replaced = False
    for line in lines:
        if line.startswith("NOTION_WORK_DB_ID="):
            updated.append(replacement)
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(replacement)
    from daily_brief.atomic import atomic_write_text

    atomic_write_text(path, "\n".join(updated).rstrip() + "\n")


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
            persist_work_db_id(Path(".env"), database["id"])
            print(f"Created Work database and saved NOTION_WORK_DB_ID={database['id'].replace('-', '')}")
        return 0
    except (ConfigurationError, NotionError, KeyError) as exc:
        print(f"NOTION_SETUP_ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
