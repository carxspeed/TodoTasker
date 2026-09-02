"""Daily Brief prepare, deliver, and watchdog commands."""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_brief.config import ConfigurationError, load_settings
from daily_brief.notion import NotionClient
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
        command.add_argument("--profile", type=Path, default=Path("profile"))
        command.add_argument("--dry-run", action="store_true")
    watchdog = sub.add_parser("watchdog")
    watchdog.add_argument("--target-date", type=date.fromisoformat)
    return parser.parse_args()


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
        else:
            if not explicit and now.time() < time(7, 30):
                print("skipped_stale")
                return 0
            target = explicit or now.date()
            as_of = now
        notion = None
        if settings.notion_token and settings.notion_work_db_id and settings.notion_parent_page_id:
            notion = NotionClient(
                settings.notion_token,
                settings.notion_work_db_id,
                settings.notion_parent_page_id,
            )
        telegram = None
        if settings.telegram_bot_token and settings.telegram_chat_id:
            telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
        orchestrator = DailyBriefOrchestrator(
            settings, notion_delivery=notion, telegram=telegram
        )
        dry_run = getattr(args, "dry_run", False)
        lock = nullcontext() if dry_run else HeartbeatLock()
        with lock:
            if args.command == "prepare":
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
