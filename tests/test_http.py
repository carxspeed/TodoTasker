from dataclasses import dataclass, field

import pytest
import requests

from daily_brief.http import HttpClient, HttpFailure


@dataclass
class FakeResponse:
    status_code: int
    body: object
    headers: dict[str, str] = field(default_factory=dict)

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_idempotent_read_retries_connection_and_5xx() -> None:
    session = FakeSession(
        [requests.ConnectionError("offline"), FakeResponse(503, {}), FakeResponse(200, {"ok": True})]
    )
    sleeps = []
    result = HttpClient(session, sleep=sleeps.append, jitter=lambda: 0).request_json(
        "GET", "https://example.test/data", source="calendar"
    )
    assert result.data == {"ok": True}
    assert sleeps == [1, 2]
    assert len(session.calls) == 3


def test_mutation_is_not_implicitly_retried() -> None:
    session = FakeSession([FakeResponse(503, {})])
    with pytest.raises(HttpFailure) as caught:
        HttpClient(session, sleep=lambda _: None).request_json(
            "POST", "https://example.test/items", source="notion"
        )
    assert caught.value.attempt == 1


def test_retry_after_over_sixty_seconds_fails_visibly() -> None:
    session = FakeSession([FakeResponse(429, {}, {"Retry-After": "61"})])
    with pytest.raises(HttpFailure, match="retry_after_too_long"):
        HttpClient(session, sleep=lambda _: None).request_json(
            "GET", "https://example.test/items", source="notion"
        )


def test_invalid_json_is_not_exposed_in_error() -> None:
    session = FakeSession([FakeResponse(200, ValueError("secret body"))])
    with pytest.raises(HttpFailure) as caught:
        HttpClient(session).request_json("GET", "https://example.test", source="canvas")
    assert "secret body" not in str(caught.value)

