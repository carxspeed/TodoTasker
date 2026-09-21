from pathlib import Path

import requests

from daily_brief.models import DailyNotification, NotificationReminder, NotificationTask
from daily_brief.telegram import (
    NOTIFICATION_LIMIT,
    SUMMARY_LIMIT,
    TelegramClient,
    build_summary,
    render_notification,
    render_notification_preview,
)


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def json(self):
        return self.body


class Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def test_summary_truncates_only_at_item_boundaries() -> None:
    tasks = "\n".join(f"- Task {index} " + "x" * 110 for index in range(80))
    full = f"Daily Brief — 2026-09-02\n\nMUST\n{tasks}\n\nEvents\n- Meeting"
    summary = build_summary(full, "https://notion.test/brief")
    assert len(summary.text) <= SUMMARY_LIMIT
    assert summary.omitted_items > 0
    assert "open the task tables" in summary.text
    assert not summary.text.endswith("x")


def test_no_notion_footer_names_local_failsafe() -> None:
    full = "Header\n\n" + "\n".join(f"- {'x' * 120}" for _ in range(80))
    summary = build_summary(full, None, local_path="state/briefs/2026-09-02.md")
    assert "Notion is unavailable" in summary.text
    assert "state\\briefs\\2026-09-02.md" in summary.text or "state/briefs/2026-09-02.md" in summary.text


def test_send_uses_json_plain_text_and_validates_api_ok() -> None:
    session = Session(Response({"ok": True, "result": {"message_id": 42}}))
    client = TelegramClient("token", "chat", session=session)
    _, result = client.send_brief("Header\n\nMUST\n- Task", "https://notion.test", local_path="brief.md")
    assert result.success and result.message_id == 42
    payload = session.calls[0][1]["json"]
    assert "parse_mode" not in payload
    assert payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "Open task tables"
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://notion.test"

    failed = TelegramClient(
        "token", "chat", session=Session(Response({"ok": False, "description": "bad chat"}))
    ).send_plain("hello")
    assert not failed.success


def test_uncertain_transport_is_labeled() -> None:
    result = TelegramClient(
        "token", "chat", session=Session(error=requests.ConnectionError("offline"))
    ).send_plain("hello")
    assert not result.success and result.uncertain


def test_edit_not_modified_is_success() -> None:
    result = TelegramClient(
        "token",
        "chat",
        session=Session(Response({"ok": False, "description": "Bad Request: message is not modified"}, 400)),
    ).edit_brief(9, "Header", None, local_path=Path("brief.md"))[1]
    assert result.success and result.message_id == 9


def compact_notification() -> DailyNotification:
    return DailyNotification(
        target_date=__import__("datetime").date(2026, 9, 20),
        primary=NotificationTask(
            key="assignment:1",
            name="Lab: Millions",
            course="AP Physics",
            next_step="Finish the calculations and upload the final page.",
            effort_hours=0.75,
            url="https://canvas.test/assignment/1",
        ),
        followups=[
            NotificationTask(
                key="assignment:2",
                name="Calculus practice",
                course="Calculus",
                next_step="Complete the first problem.",
                effort_hours=0.5,
                url="https://notion.test/calculus-practice",
            )
        ],
        reminders=[
            NotificationReminder(
                title="Quiz 3", course="Calculus", date=__import__("datetime").date(2026, 9, 20)
            )
        ],
        backlog_count=7,
        verify_count=2,
    )


def test_compact_notification_is_phone_sized_and_escapes_html() -> None:
    notification = compact_notification()
    notification.primary.name = "Lab <Millions>"

    rendered = render_notification(notification).text

    assert len(rendered) <= NOTIFICATION_LIMIT
    assert "🎯 <b>Start here</b>" in rendered
    assert "Lab &lt;Millions&gt;" in rendered
    assert "7 other tasks remain in Notion" in rendered
    assert "2 Canvas tasks need status confirmation" in rendered
    assert '<a href="' in rendered
    assert "Capacity" not in rendered


def test_send_notification_uses_html_and_two_useful_buttons() -> None:
    session = Session(Response({"ok": True, "result": {"message_id": 42}}))
    client = TelegramClient("token", "chat", session=session)

    _, result = client.send_notification(
        compact_notification(), "https://notion.test/today"
    )

    assert result.success
    payload = session.calls[0][1]["json"]
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"] == {"is_disabled": True}
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == [
        "Open main task",
        "Today in Notion",
    ]


def test_terminal_preview_removes_telegram_html_without_losing_text() -> None:
    preview = render_notification_preview(compact_notification())

    assert "<b>" not in preview and "<a " not in preview
    assert "Start here" in preview
    assert "Calculus practice" in preview
