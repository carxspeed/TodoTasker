"""Daily Brief prepare, deliver, and watchdog commands."""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_brief.config import ConfigurationError, load_settings
from daily_brief.notion import NotionSchoolBoard, summarize_master_migration
from daily_brief.orchestrator import DailyBriefOrchestrator, LiveSourceProvider
from daily_brief.runtime import DeferredHealthyLock, HeartbeatLock
from daily_brief.telegram import TelegramClient


def _in_window(now_time: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_time <= end
    return now_time >= start or now_time <= end


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "deliver"):
        command = sub.add_parser(name)
        command.add_argument("--target-date", type=date.fromisoformat)
        command.add_argument("--fixture", type=Path)
        command.add_argument("--profile", type=Path)
        command.add_argument("--dry-run", action="store_true")
    watchdog = sub.add_parser("watchdog")
    watchdog.add_argument("--target-date", type=date.fromisoformat)
    migration = sub.add_parser(
        "migrate-notion",
        help="Preview or apply the safe migration into the unified Tasks database",
    )
    migration.add_argument("--target-date", type=date.fromisoformat)
    migration.add_argument("--profile", type=Path)
    mode = migration.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Create/update master Tasks rows; legacy tables remain unchanged",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report the migration plan (the default)",
    )
    return parser.parse_args()


def _run_notion_migration(
    settings,
    notion: NotionSchoolBoard | None,
    *,
    target: date,
    profile: Path | None,
    apply: bool,
) -> None:
    if notion is None:
        raise ConfigurationError(
            "Notion parent and School pages must be configured before migration"
        )
    provider = LiveSourceProvider(settings, profile=profile)
    canvas = provider.fetch_canvas(target)
    notion_snapshot = provider.fetch_notion()
    summary = summarize_master_migration(
        canvas.assignments,
        notion_snapshot.items,
        excluded_course_ids=settings.canvas_excluded_course_ids,
    )
    duplicate_text = ",".join(summary.duplicate_source_ids) or "none"
    print(f"mode={'apply' if apply else 'dry-run'}")
    print(f"canvas_rows={summary.canvas_rows}")
    print(f"notion_rows={summary.notion_rows}")
    print(f"unique_rows={summary.unique_rows}")
    print(f"duplicates={duplicate_text}")
    print("legacy_databases_unchanged=true")
    if summary.duplicate_source_ids:
        raise RuntimeError(
            "migration stopped because duplicate Source IDs would make the copy ambiguous"
        )
    if not apply:
        print("next=rerun with --apply after reviewing these counts")
        return

    legacy_context = notion.get_assignment_context()
    result = notion.sync_master_tasks(
        canvas.assignments,
        notion_snapshot.items,
        user_context_by_key=legacy_context,
        excluded_course_ids=settings.canvas_excluded_course_ids,
    )
    print(f"master_database_id={result.database_id}")
    print(f"database_created={str(result.database_created).lower()}")
    print(f"rows_created={result.rows_created}")
    print(f"rows_updated={result.rows_updated}")
    print(f"rows_unchanged={result.rows_unchanged}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        settings = load_settings()
        timezone = ZoneInfo(settings.timezone)
        now = datetime.now(timezone)
        explicit = getattr(args, "target_date", None)
        if args.command == "prepare":
            if not explicit and not _in_window(now.time(), time(21, 40), time(3, 0)):
                print("skipped_stale")
                return 0
            target = explicit or (now.date() + timedelta(days=1) if now.time() >= time(21, 40) else now.date())
            as_of = datetime.combine(target, time(6, 30), timezone)
        elif args.command == "deliver":
            if not explicit and not _in_window(now.time(), time(5, 30), time(12, 0)):
                print("skipped_stale")
                return 0
            target = explicit or now.date()
            as_of = now
        elif args.command == "migrate-notion":
            target = explicit or now.date()
            as_of = now
        else:
            if not explicit and now.time() < time(7, 30):
                print("skipped_stale")
                return 0
            target = explicit or now.date()
            as_of = now
        notion = None
        if (
            settings.notion_token
            and settings.notion_parent_page_id
            and settings.notion_school_page_id
        ):
            notion = NotionSchoolBoard(
                settings.notion_token,
                settings.notion_parent_page_id,
                settings.notion_school_page_id,
            )
        telegram = None
        if settings.telegram_bot_token and settings.telegram_chat_id:
            telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
        orchestrator = DailyBriefOrchestrator(
            settings, notion_delivery=notion, telegram=telegram
        )
        dry_run = (
            not args.apply
            if args.command == "migrate-notion"
            else getattr(args, "dry_run", False)
        )
        lock = nullcontext() if dry_run else HeartbeatLock()
        with lock:
            if args.command == "migrate-notion":
                _run_notion_migration(
                    settings,
                    notion,
                    target=target,
                    profile=args.profile,
                    apply=args.apply,
                )
            elif args.command == "prepare":
                provider = LiveSourceProvider(
                    settings, fixture=args.fixture, profile=args.profile
                )
                artifact, _ = orchestrator.prepare(
                    provider, target_date=target, as_of=as_of, dry_run=dry_run
                )
                print(artifact.rendered_brief)
            elif args.command == "deliver":
                provider = LiveSourceProvider(
                    settings, fixture=args.fixture, profile=args.profile
                )
                text, status, _ = orchestrator.deliver(
                    provider, target_date=target, as_of=as_of, dry_run=dry_run
                )
                print(text)
                print(f"delivery={status}")
            else:
                print("alerted" if orchestrator.watchdog(target) else "healthy")
        return 0
    except DeferredHealthyLock as exc:
        print(exc)
        return exc.exit_code
    except (ConfigurationError, RuntimeError, ValueError) as exc:
        print(f"BRIEF_ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
