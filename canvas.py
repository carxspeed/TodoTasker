"""Canvas login and read-only normalized fetch CLI."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_brief.canvas import (
    CanvasError,
    CanvasTokenRequest,
    ensure_canvas_session,
    exclude_course_assignments,
    fetch_live,
    load_fixture,
    migrate_legacy_canvas_session,
    open_saved_canvas_context,
    save_canvas_session,
    verify_session,
)
from daily_brief.config import ConfigurationError, load_settings


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
        if args.command == "fetch" and settings.canvas_access_token:
            with CanvasTokenRequest(
                str(settings.canvas_base), settings.canvas_access_token
            ) as request:
                effective_date = target_date or datetime.now(
                    ZoneInfo(settings.timezone)
                ).date()
                envelope = fetch_live(
                    request,
                    str(settings.canvas_base),
                    effective_date,
                    settings.timezone,
                    excluded_course_ids=settings.canvas_excluded_course_ids,
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
                else:
                    with open_saved_canvas_context(playwright, args.profile) as context:
                        ensure_canvas_session(context, str(settings.canvas_base))
                        save_canvas_session(context, args.profile)
                        effective_date = target_date or datetime.now(
                            ZoneInfo(settings.timezone)
                        ).date()
                        envelope = fetch_live(
                            context.request,
                            str(settings.canvas_base),
                            effective_date,
                            settings.timezone,
                            excluded_course_ids=settings.canvas_excluded_course_ids,
                        )
                        envelope = exclude_course_assignments(
                            envelope, settings.canvas_excluded_course_ids
                        )
                        print(envelope.model_dump_json())
        return 0
    except (ConfigurationError, CanvasError) as exc:
        print(exc)
        return exc.exit_code if isinstance(exc, CanvasError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
