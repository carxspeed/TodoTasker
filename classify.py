"""Deterministic classifier CLI for fixture inspection."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_brief.canvas import load_fixture
from daily_brief.classifier import classify
from daily_brief.config import load_settings
from daily_brief.models import NotionWorkItem


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canvas-fixture", type=Path, required=True)
    parser.add_argument("--notion-fixture", type=Path)
    parser.add_argument("--target-date", type=date.fromisoformat, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    args = parser.parse_args()
    settings = load_settings()
    canvas = load_fixture(args.canvas_fixture)
    notion = []
    if args.notion_fixture:
        notion = [
            NotionWorkItem.model_validate(value)
            for value in json.loads(args.notion_fixture.read_text(encoding="utf-8"))
        ]
    as_of = args.as_of or datetime.combine(
        args.target_date, time(6, 30), ZoneInfo(settings.timezone)
    )
    result = classify(
        canvas.assignments,
        notion,
        target_date=args.target_date,
        as_of=as_of,
        timezone_name=settings.timezone,
    )
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

