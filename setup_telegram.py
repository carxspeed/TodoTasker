"""Capture the Telegram chat id after the user sends the bot a message."""

from __future__ import annotations

import sys
from pathlib import Path

from daily_brief.config import ConfigurationError, load_settings
from daily_brief.envfile import persist_env_value
from daily_brief.telegram import TelegramClient


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        settings = load_settings(required=("TELEGRAM_BOT_TOKEN",))
        client = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
        webhook = client.get_webhook_info()
        if webhook.get("url"):
            print("TELEGRAM_SETUP_ERROR: a webhook is configured; this project requires polling")
            return 1
        updates = client.get_updates(None)
        candidates = []
        for update in updates:
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if isinstance(message.get("text"), str) and chat.get("id") is not None:
                candidates.append((int(update["update_id"]), str(chat["id"])))
        if not candidates:
            print("TELEGRAM_SETUP_ERROR: send the bot one text message, then run this command again")
            return 1
        _, chat_id = max(candidates)
        persist_env_value(Path(".env"), "TELEGRAM_CHAT_ID", chat_id)
        print(f"Saved TELEGRAM_CHAT_ID={chat_id} to .env")
        return 0
    except (ConfigurationError, RuntimeError) as exc:
        print(f"TELEGRAM_SETUP_ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

