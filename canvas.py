"""Canvas login and read-only normalized fetch CLI."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_brief.canvas import (
    CanvasError,
    exclude_course_assignments,
    load_fixture,
    migrate_legacy_canvas_session,
    save_canvas_session,
    verify_session,
)
from daily_brief.config import ConfigurationError, load_settings
from daily_brief.orchestrator import LiveSourceProvider
from daily_brief.telegram import TelegramClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login")
    login.add_argument("--profile", type=Path)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--profile", type=Path)
    fetch.add_argument("--fixture", type=Path)
    fetch.add_argument("--target-date", type=date.fromisoformat)
    migrate = sub.add_parser("migrate-session")
    migrate.add_argument("--legacy-profile", type=Path, default=Path("profile"))
    migrate.add_argument("--profile", type=Path)
    auth_check = sub.add_parser("auth-check")
    auth_check.add_argument("--profile", type=Path)
    auth_check.add_argument("--notify", action="store_true")
    return parser.parse_args()


def _profile_error(exc: Exception) -> CanvasError:
    return CanvasError("CANVAS_BROWSER_ERROR", "could not start the Canvas browser")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    try:
        settings = load_settings()
        target_date = args.target_date if hasattr(args, "target_date") else None
        if args.command == "migrate-session":
            destination = migrate_legacy_canvas_session(
                args.legacy_profile, args.profile
            )
            print(f"Encrypted legacy Canvas session at {destination}.")
            return 0
        if args.command == "fetch" and args.fixture:
            envelope = exclude_course_assignments(
                load_fixture(args.fixture), settings.canvas_excluded_course_ids
            )
            print(envelope.model_dump_json())
            return 0
        if args.command == "auth-check":
            method = LiveSourceProvider(settings, profile=args.profile).check_canvas_auth()
            if (
                method == "session_fallback"
                and args.notify
                and settings.telegram_bot_token
                and settings.telegram_chat_id
            ):
                result = TelegramClient(
                    settings.telegram_bot_token, settings.telegram_chat_id
                ).send_plain(
                    "TodoTasker: the Canvas token needs replacement. The encrypted "
                    "browser session is still working, so briefs will continue."
                )
                if not result.success:
                    raise CanvasError(
                        "CANVAS_AUTH_NOTIFICATION_FAILED",
                        "Canvas fallback worked but Telegram notification failed",
                    )
            print(f"canvas_auth={method}")
        elif args.command == "fetch":
            effective_date = target_date or datetime.now(
                ZoneInfo(settings.timezone)
            ).date()
            envelope = LiveSourceProvider(settings, profile=args.profile).fetch_canvas(
                effective_date
            )
            envelope = exclude_course_assignments(
                envelope, settings.canvas_excluded_course_ids
            )
            print(envelope.model_dump_json())
        else:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                if args.command == "login":
                    browser = None
                    try:
                        browser = playwright.chromium.launch(headless=False)
                        context = browser.new_context()
                    except Exception as exc:
                        raise _profile_error(exc) from exc
                    try:
                        page = context.new_page()
                        page.goto(str(settings.canvas_base))
                        print("Log in via Microsoft, then press Enter here")
                        input()
                        verify_session(context.request, str(settings.canvas_base))
                        save_canvas_session(context, args.profile)
                        print("Canvas session verified and saved.")
                    finally:
                        context.close()
                        browser.close()
        return 0
    except (ConfigurationError, CanvasError) as exc:
        if (
            args.command == "auth-check"
            and args.notify
            and "settings" in locals()
            and settings.telegram_bot_token
            and settings.telegram_chat_id
        ):
            result = TelegramClient(
                settings.telegram_bot_token, settings.telegram_chat_id
            ).send_plain(
                "TodoTasker needs attention: Canvas authentication could not be "
                "renewed automatically. Run canvas.py login when convenient."
            )
            if result.success:
                print("canvas_auth=action_required_notified")
                return 0
        print(exc)
        return exc.exit_code if isinstance(exc, CanvasError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
