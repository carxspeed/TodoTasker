"""Read-only Notion Work adapter CLI."""

from __future__ import annotations

import json
import sys

from daily_brief.config import ConfigurationError, load_settings
from daily_brief.notion import NotionError, NotionTaskStore


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        settings = load_settings(
            required=(
                "NOTION_TOKEN",
                "NOTION_WORK_DB_ID",
                "NOTION_SCHOOL_DB_ID",
                "NOTION_CONNECTIONS_DB_ID",
                "NOTION_MISC_DB_ID",
            )
        )
        snapshot = NotionTaskStore(
            settings.notion_token,
            settings.notion_database_ids,
            settings.notion_parent_page_id,
        ).get_active_work()
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "items": [item.model_dump(mode="json") for item in snapshot.items],
                    "warnings": snapshot.warnings,
                },
                separators=(",", ":"),
            )
        )
        return 0
    except (ConfigurationError, NotionError) as exc:
        print(f"NOTION_ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
