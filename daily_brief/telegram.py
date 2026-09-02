"""Plain-text Telegram delivery and polling primitives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


SUMMARY_LIMIT = 3900


@dataclass(frozen=True)
class TelegramSummary:
    text: str
    omitted_items: int = 0
    omitted_sections: int = 0


@dataclass(frozen=True)
class TelegramResult:
    success: bool
    message_id: int | None = None
    error: str | None = None
    uncertain: bool = False


def _cap_task_title(line: str) -> str:
    if not line.startswith("- "):
        return line
    content = line[2:]
    if len(content) <= 120:
        return line
    return "- " + content[:117].rstrip() + "..."


def _blocks(full_text: str) -> list[str]:
    """Split at section/item boundaries without cutting a task line."""

    lines = full_text.strip().splitlines()
    blocks: list[str] = []
    current: list[str] = []
    for raw in lines:
        line = _cap_task_title(raw)
        if not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if line.startswith("- "):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif line.startswith("  ") and current and current[0].startswith("- "):
            current.append(line)
        else:
            if current and current[0].startswith("- "):
                blocks.append("\n".join(current))
                current = []
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def build_summary(
    full_text: str,
    notion_url: str | None,
    *,
    local_path: str | Path = "state/briefs/latest.md",
) -> TelegramSummary:
    blocks = _blocks(full_text)
    footer_reserve = 220
    kept: list[str] = []
    omitted_items = 0
    omitted_sections = 0
    used = 0
    skipping = False
    for block in blocks:
        separator = 2 if kept else 0
        if not skipping and used + separator + len(block) <= SUMMARY_LIMIT - footer_reserve:
            kept.append(block)
            used += separator + len(block)
        else:
            skipping = True
            if block.startswith("- "):
                omitted_items += 1
            else:
                omitted_sections += 1
    if omitted_items or omitted_sections:
        counts = []
        if omitted_items:
            counts.append(f"+{omitted_items} additional item{'s' if omitted_items != 1 else ''}")
        if omitted_sections:
            counts.append(f"+{omitted_sections} additional section{'s' if omitted_sections != 1 else ''}")
        if notion_url:
            footer = " and ".join(counts) + " — open the full brief"
        else:
            footer = (
                " and ".join(counts)
                + f" — Notion is unavailable; full brief saved at {Path(local_path)}"
            )
        kept.append(footer)
    result = "\n\n".join(kept)
    if len(result) > SUMMARY_LIMIT:
        raise ValueError("boundary-safe Telegram summary exceeds its limit")
    return TelegramSummary(result, omitted_items, omitted_sections)


class TelegramClient:
    def __init__(self, token: str, chat_id: str, *, session=None) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.session = session or requests.Session()

    @staticmethod
    def _keyboard(notion_url: str | None) -> dict[str, Any] | None:
        if not notion_url:
            return None
        return {
            "inline_keyboard": [[{"text": "Open full brief", "url": notion_url}]]
        }

    def _post(self, method: str, payload: dict[str, Any]) -> TelegramResult:
        try:
            response = self.session.post(
                f"{self.base_url}/{method}", json=payload, timeout=(10, 30)
            )
        except requests.RequestException:
            return TelegramResult(False, error="telegram transport failed", uncertain=True)
        try:
            body = response.json()
        except ValueError:
            return TelegramResult(False, error=f"telegram returned HTTP {response.status_code}")
        if response.status_code >= 400 or body.get("ok") is not True:
            description = str(body.get("description") or f"HTTP {response.status_code}")
            if method == "editMessageText" and "message is not modified" in description.casefold():
                return TelegramResult(True, message_id=payload.get("message_id"))
            return TelegramResult(False, error=f"telegram API error: {description}")
        result = body.get("result") or {}
        message_id = result.get("message_id") or payload.get("message_id")
        return TelegramResult(True, message_id=int(message_id) if message_id is not None else None)

    def send_brief(
        self, full_text: str, notion_url: str | None, *, local_path: str | Path
    ) -> tuple[TelegramSummary, TelegramResult]:
        summary = build_summary(full_text, notion_url, local_path=local_path)
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": summary.text}
        keyboard = self._keyboard(notion_url)
        if keyboard:
            payload["reply_markup"] = keyboard
        return summary, self._post("sendMessage", payload)

    def edit_brief(
        self,
        message_id: int,
        full_text: str,
        notion_url: str | None,
        *,
        local_path: str | Path,
    ) -> tuple[TelegramSummary, TelegramResult]:
        summary = build_summary(full_text, notion_url, local_path=local_path)
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": summary.text,
        }
        keyboard = self._keyboard(notion_url)
        if keyboard:
            payload["reply_markup"] = keyboard
        return summary, self._post("editMessageText", payload)

    def send_plain(self, text: str) -> TelegramResult:
        return self._post("sendMessage", {"chat_id": self.chat_id, "text": text})

    def get_webhook_info(self) -> dict[str, Any]:
        try:
            response = self.session.post(
                f"{self.base_url}/getWebhookInfo", json={}, timeout=(10, 30)
            )
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError("Telegram webhook check failed") from exc
        if response.status_code >= 400 or body.get("ok") is not True:
            raise RuntimeError(str(body.get("description") or "Telegram webhook check failed"))
        return body.get("result") or {}

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        current = offset
        while True:
            payload: dict[str, Any] = {"limit": 100, "timeout": 0}
            if current is not None:
                payload["offset"] = current
            try:
                response = self.session.post(
                    f"{self.base_url}/getUpdates", json=payload, timeout=(10, 30)
                )
                body = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise RuntimeError("Telegram update polling failed") from exc
            if response.status_code >= 400 or body.get("ok") is not True:
                raise RuntimeError(str(body.get("description") or "Telegram update polling failed"))
            batch = body.get("result") or []
            if not batch:
                return updates
            updates.extend(batch)
            current = max(int(item["update_id"]) for item in batch) + 1
