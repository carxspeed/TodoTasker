"""Canvas login and read-only normalized fetch CLI."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_brief.canvas import CanvasError, fetch_live, load_fixture, verify_session
from daily_brief.config import ConfigurationError, load_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login")
    login.add_argument("--profile", type=Path, default=Path("profile"))
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--profile", type=Path, default=Path("profile"))
    fetch.add_argument("--fixture", type=Path)
    fetch.add_argument("--target-date", type=date.fromisoformat)
    return parser.parse_args()


def _profile_error(exc: Exception) -> CanvasError:
    message = str(exc).lower()
    if "processsingleton" in message or "profile" in message and "in use" in message:
        return CanvasError("PROFILE_IN_USE", "close the Canvas login browser and retry")
    return CanvasError("CANVAS_BROWSER_ERROR", "could not start the Canvas browser")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        settings = load_settings()
        target_date = args.target_date if hasattr(args, "target_date") else None
        if args.command == "fetch" and args.fixture:
            print(load_fixture(args.fixture).model_dump_json())
            return 0
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(args.profile.resolve()), headless=args.command == "fetch"
                )
            except Exception as exc:
                raise _profile_error(exc) from exc
            try:
                if args.command == "login":
                    context.pages[0].goto(str(settings.canvas_base))
                    print("Log in via Microsoft, then press Enter here")
                    input()
                    verify_session(context.request, str(settings.canvas_base))
                    print("Canvas session verified. Close this command before scheduled fetches run.")
                else:
                    effective_date = target_date or datetime.now(
                        ZoneInfo(settings.timezone)
                    ).date()
                    envelope = fetch_live(
                        context.request,
                        str(settings.canvas_base),
                        effective_date,
                        settings.timezone,
                    )
                    print(envelope.model_dump_json())
            finally:
                context.close()
        return 0
    except (ConfigurationError, CanvasError) as exc:
        print(exc)
        return exc.exit_code if isinstance(exc, CanvasError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
