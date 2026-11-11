from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from daily_brief.imessage import InkboxIMessageClient, render_imessage
from daily_brief.models import DailyNotification, NotificationReminder, NotificationTask


TARGET = date(2026, 9, 2)
TZ = ZoneInfo("America/Los_Angeles")


def notification() -> DailyNotification:
    return DailyNotification(
        target_date=TARGET,
        primary=NotificationTask(
            key="assignment:1",
            name="Lab: Millions",
            course="AP Physics",
            next_step="Open the attached lab document and complete the first section.",
            due_at=datetime(2026, 9, 2, 21, 0, tzinfo=TZ),
            effort_hours=1.5,
            url="https://notion.test/task",
        ),
        followups=[
            NotificationTask(
                key="assignment:2",
                name="Calculus practice",
                course="Calculus",
                next_step="Solve problems 1 through 4.",
                effort_hours=0.5,
            )
        ],
        reminders=[
            NotificationReminder(
                title="Quiz 3",
                course="Calculus",
                date=TARGET,
            )
        ],
        backlog_count=12,
    )


class Response:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, *, conversations=None, post=None):
        self.conversations = conversations or {"conversations": [{"id": "conversation-1"}]}
        self.post_response = post or Response(200, {"message": {"id": "message-1"}})
        self.posts = []

    def get(self, url, **kwargs):
        if url.endswith("/triage-number"):
            return Response(
                200,
                {"number": "+15555550123", "connect_command": "connect @todo"},
            )
        return Response(200, self.conversations)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if isinstance(self.post_response, Exception):
            raise self.post_response
        return self.post_response


def test_phone_message_is_compact_and_actionable() -> None:
    text = render_imessage(notification(), "https://notion.test/today").text

    assert text.startswith("Wednesday\n\nStart here")
    assert "Next: Open the attached lab document" in text
    assert "Then\n2. Calculus" in text
    assert "Reminder\nCalculus — Quiz 3" in text
    assert "12 other tasks are in Notion." in text
    assert "Open task: https://notion.test/task" in text
    assert "Open Today: https://notion.test/today" in text
    assert "<b>" not in text
    assert len(text) < 1200


def test_send_uses_identity_key_conversation_and_idempotency() -> None:
    session = Session()
    client = InkboxIMessageClient("secret", session=session)

    summary, result = client.send_notification(
        notification(),
        "https://notion.test/today",
        idempotency_key="todotasker-daily-2026-09-02",
    )

    assert result.success
    assert result.message_id == "message-1"
    assert result.conversation_id == "conversation-1"
    url, call = session.posts[0]
    assert url.endswith("/api/v1/imessage/messages")
    assert call["headers"]["X-API-Key"] == "secret"
    assert call["headers"]["Idempotency-Key"] == "todotasker-daily-2026-09-02"
    assert call["json"] == {
        "conversation_id": "conversation-1",
        "text": summary.text,
    }


def test_multiple_conversations_require_an_explicit_selection() -> None:
    client = InkboxIMessageClient(
        "secret",
        session=Session(
            conversations={"conversations": [{"id": "one"}, {"id": "two"}]}
        ),
    )

    result = client.send_text("test", idempotency_key="test-1")

    assert not result.success
    assert result.error == "INKBOX_CONVERSATION_REQUIRED"


def test_transport_failure_is_uncertain_and_does_not_expose_details() -> None:
    client = InkboxIMessageClient(
        "secret",
        conversation_id="conversation-1",
        session=Session(post=requests.Timeout("private network detail")),
    )

    result = client.send_text("test", idempotency_key="test-1")

    assert not result.success
    assert result.uncertain
    assert result.error == "INKBOX_TRANSPORT_FAILED"
    assert "private" not in result.error


def test_immediate_provider_delivery_error_is_a_definite_failure() -> None:
    client = InkboxIMessageClient(
        "secret",
        conversation_id="conversation-1",
        session=Session(
            post=Response(
                200,
                {"message": {"id": "message-1", "status": "error"}},
            )
        ),
    )

    result = client.send_text("test", idempotency_key="test-1")

    assert not result.success
    assert not result.uncertain
    assert result.error == "INKBOX_DELIVERY_ERROR"


def test_setup_status_returns_only_connection_instructions() -> None:
    client = InkboxIMessageClient("secret", session=Session())

    result = client.setup_status()

    assert result.router_number == "+15555550123"
    assert result.connect_command == "connect @todo"
    assert result.conversation_ids == ("conversation-1",)
