"""Evening Telegram check-in commands."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

from daily_brief.checkin_runtime import CheckinRunner
from daily_brief.config import ConfigurationError, load_settings
from daily_brief.runtime import DeferredHealthyLock, HeartbeatLock


RETRYABLE_RESULTS = {"SEND_FAILED", "EXTRACTION_FAILED", "PRIOR_CHECKIN_FAILED"}


def _in_window(value: time, start: time, end: time) -> bool:
    return value >= start or value <= end if start > end else start <= value <= end


def _result_exit_code(result: str) -> int:
    return 1 if result in RETRYABLE_RESULTS else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("send", "process"))
    parser.add_argument("--force", action="store_true", help="bypass the scheduled catch-up window")
    args = parser.parse_args()
    try:
        settings = load_settings(
            required=(
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID",
                "NOTION_TOKEN",
                "NOTION_PARENT_PAGE_ID",
                "NOTION_SCHOOL_PAGE_ID",
            )
        )
        now = datetime.now(ZoneInfo(settings.timezone))
        allowed = (
            _in_window(now.time(), time(20, 30), time(22, 30))
            if args.command == "send"
            else _in_window(now.time(), time(21, 20), time(2, 0))
        )
        if not args.force and not allowed:
            print("skipped_stale")
            return 0
        with HeartbeatLock():
            runner = CheckinRunner(settings)
            result = runner.send(now=now) if args.command == "send" else runner.process(now=now)
            print(result)
        return _result_exit_code(result)
    except DeferredHealthyLock as exc:
        print(exc)
        return exc.exit_code
    except (ConfigurationError, RuntimeError, ValueError) as exc:
        print(f"CHECKIN_ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
