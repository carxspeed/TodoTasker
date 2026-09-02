"""Read-only Notion Work adapter CLI."""

from __future__ import annotations

import json

from daily_brief.config import ConfigurationError, load_settings
from daily_brief.notion import NotionClient, NotionError


def main() -> int:
    try:
        settings = load_settings(required=("NOTION_TOKEN", "NOTION_WORK_DB_ID"))
        snapshot = NotionClient(
            settings.notion_token,
            settings.notion_work_db_id,
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

