"""Compact iMessage delivery through an Inkbox identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

from .models import DailyNotification, NotificationTask


IMESSAGE_LIMIT = 1200


@dataclass(frozen=True)
class IMessageSummary:
    text: str


@dataclass(frozen=True)
class IMessageResult:
    success: bool
    message_id: str | None = None
    conversation_id: str | None = None
    error: str | None = None
    uncertain: bool = False


@dataclass(frozen=True)
class InkboxSetupStatus:
    router_number: str
    connect_command: str
    conversation_ids: tuple[str, ...]


def _short(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _format_effort(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") + "h"


def _format_due(task: NotificationTask, target_date: date) -> str:
    if task.due_at is None:
        return ""
    due = task.due_at
    clock = f"{due.strftime('%I').lstrip('0')}:{due.strftime('%M %p')}"
    if due.date() < target_date:
        return f"Overdue since {due.strftime('%b')} {due.day}"
    if due.date() == target_date:
        return f"Due today {clock}"
    if (due.date() - target_date).days == 1:
        return f"Due tomorrow {clock}"
    return f"Due {due.strftime('%a %b')} {due.day}, {clock}"


def _task_title(task: NotificationTask, limit: int) -> str:
    title = _short(task.name, limit)
    if task.locked_for_user:
        title = f"Locked: {title}"
    if task.course:
        title = f"{_short(task.course, 30)} — {title}"
    return title


def render_imessage(
    notification: DailyNotification,
    notion_url: str | None = None,
) -> IMessageSummary:
    """Render a calm phone-first message with only the current focus."""

    lines = [notification.target_date.strftime("%A"), ""]
    primary = notification.primary
    if primary is None:
        lines.append("Nothing needs your attention right now.")
    else:
        lines.extend(
            [
                "Start here",
                _task_title(primary, 82),
                f"Next: {_short(primary.next_step, 150)}",
            ]
        )
        details = [
            value
            for value in (
                _format_due(primary, notification.target_date),
                _format_effort(primary.effort_hours),
            )
            if value
        ]
        if details:
            lines.append(" · ".join(details))
    if notification.followups:
        lines.extend(["", "Then"])
        for index, task in enumerate(notification.followups, start=2):
            lines.append(
                f"{index}. {_task_title(task, 70)} · {_format_effort(task.effort_hours)}"
            )
    if notification.reminders:
        lines.extend(["", "Reminder"])
        for reminder in notification.reminders:
            title = reminder.title
            if reminder.course:
                title = f"{reminder.course} — {title}"
            lines.append(_short(title, 100))
    footer: list[str] = []
    if notification.backlog_count:
        footer.append(
            f"{notification.backlog_count} other task"
            f"{'s' if notification.backlog_count != 1 else ''} are in Notion."
        )
    if notification.verify_count:
        footer.append(
            f"{notification.verify_count} Canvas task"
            f"{'s' if notification.verify_count != 1 else ''} need a status check."
        )
    if footer:
        lines.extend(["", *footer])
    if notification.notice:
        lines.extend(["", f"Note: {_short(notification.notice, 150)}"])
    if primary and primary.url:
        lines.extend(["", f"Open task: {primary.url}"])
    if notion_url and (not primary or notion_url != primary.url):
        lines.append(f"Open Today: {notion_url}")
    text = "\n".join(lines)
    if len(text) > IMESSAGE_LIMIT:
        raise ValueError("compact iMessage notification exceeds its limit")
    return IMessageSummary(text)


def render_imessage_preview(
    notification: DailyNotification,
    notion_url: str | None = None,
) -> str:
    return render_imessage(notification, notion_url).text


class InkboxIMessageClient:
    """Narrow REST client for one identity-scoped Inkbox API key."""

    def __init__(
        self,
        api_key: str,
        conversation_id: str = "",
        *,
        base_url: str = "https://inkbox.ai",
        session=None,
    ) -> None:
        self.api_root = f"{base_url.rstrip('/')}/api/v1/imessage"
        self.api_key = api_key
        self.conversation_id = conversation_id.strip()
        self.session = session or requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key, "Accept": "application/json"}

    @staticmethod
    def _error_code(status_code: int) -> str:
        return {
            401: "INKBOX_UNAUTHORIZED",
            403: "INKBOX_FORBIDDEN",
            404: "INKBOX_NOT_CONNECTED",
            409: "INKBOX_CONFLICT",
            429: "INKBOX_RATE_LIMITED",
        }.get(status_code, f"INKBOX_HTTP_{status_code}")

    @staticmethod
    def _records(body: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(body, list):
            return [item for item in body if isinstance(item, dict)]
        if not isinstance(body, dict):
            return []
        value = body.get(key, body.get("data", []))
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    def _get_json(self, path: str) -> tuple[Any | None, str | None]:
        try:
            response = self.session.get(
                f"{self.api_root}{path}",
                headers=self._headers,
                timeout=(10, 30),
            )
        except requests.RequestException:
            return None, "INKBOX_TRANSPORT_FAILED"
        if response.status_code >= 400:
            return None, self._error_code(response.status_code)
        try:
            return response.json(), None
        except ValueError:
            return None, "INKBOX_INVALID_RESPONSE"

    def setup_status(self) -> InkboxSetupStatus:
        triage, error = self._get_json("/triage-number")
        if error or not isinstance(triage, dict):
            raise RuntimeError(error or "INKBOX_INVALID_RESPONSE")
        conversations, conversation_error = self._get_json(
            "/conversations?limit=50&offset=0"
        )
        if conversation_error:
            raise RuntimeError(conversation_error)
        ids = self._active_conversation_ids(conversations)
        return InkboxSetupStatus(
            router_number=str(triage.get("number") or ""),
            connect_command=str(triage.get("connect_command") or ""),
            conversation_ids=tuple(ids),
        )

    def _active_conversation_ids(self, body: Any) -> list[str]:
        ids: list[str] = []
        for item in self._records(body, "conversations"):
            identifier = item.get("id") or item.get("conversation_id")
            status = item.get("assignment_status")
            if not identifier or item.get("is_group"):
                continue
            if status not in (None, "active"):
                continue
            ids.append(str(identifier))
        return ids

    def resolve_conversation_id(self) -> tuple[str | None, str | None]:
        if self.conversation_id:
            return self.conversation_id, None
        body, error = self._get_json("/conversations?limit=50&offset=0")
        if error:
            return None, error
        ids = self._active_conversation_ids(body)
        if not ids:
            return None, "INKBOX_NOT_CONNECTED"
        if len(ids) > 1:
            return None, "INKBOX_CONVERSATION_REQUIRED"
        return ids[0], None

    def send_notification(
        self,
        notification: DailyNotification,
        notion_url: str | None,
        *,
        idempotency_key: str,
    ) -> tuple[IMessageSummary, IMessageResult]:
        summary = render_imessage(notification, notion_url)
        result = self.send_text(summary.text, idempotency_key=idempotency_key)
        return summary, result

    def send_text(self, text: str, *, idempotency_key: str) -> IMessageResult:
        """Send one already-sanitized message without exposing the API key."""

        conversation_id, error = self.resolve_conversation_id()
        if error or conversation_id is None:
            return IMessageResult(False, error=error)
        headers = {**self._headers, "Idempotency-Key": idempotency_key}
        try:
            response = self.session.post(
                f"{self.api_root}/messages",
                headers=headers,
                json={"conversation_id": conversation_id, "text": text},
                timeout=(10, 30),
            )
        except requests.RequestException:
            return IMessageResult(
                False,
                conversation_id=conversation_id,
                error="INKBOX_TRANSPORT_FAILED",
                uncertain=True,
            )
        if response.status_code >= 400:
            return IMessageResult(
                False,
                conversation_id=conversation_id,
                error=self._error_code(response.status_code),
            )
        try:
            body = response.json()
        except ValueError:
            return IMessageResult(
                False,
                conversation_id=conversation_id,
                error="INKBOX_INVALID_RESPONSE",
            )
        message = body.get("message", body) if isinstance(body, dict) else {}
        message_id = message.get("id") if isinstance(message, dict) else None
        if message_id is None:
            return IMessageResult(
                False,
                conversation_id=conversation_id,
                error="INKBOX_INVALID_RESPONSE",
            )
        delivery_status = str(message.get("status") or "").casefold()
        if delivery_status in {"declined", "error"}:
            return IMessageResult(
                False,
                message_id=str(message_id),
                conversation_id=conversation_id,
                error=f"INKBOX_DELIVERY_{delivery_status.upper()}",
            )
        return IMessageResult(
            True,
            message_id=str(message_id),
            conversation_id=conversation_id,
        )
