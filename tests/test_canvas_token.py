import json

import pytest

from daily_brief.canvas import CanvasError, CanvasTokenRequest, paginate


class RawResponse:
    def __init__(self, status=200, *, url="https://canvas.test/api", headers=None, body=None):
        self.status_code = status
        self.url = url
        self.headers = headers or {"content-type": "application/json"}
        self.content = json.dumps(body if body is not None else {}).encode()

    def json(self):
        return json.loads(self.content)


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    def close(self):
        self.closed = True


def test_token_is_sent_only_to_the_exact_canvas_origin() -> None:
    session = Session(
        [
            RawResponse(url="https://canvas.test/api/v1/users/self"),
            RawResponse(url="https://files.example.test/file.pdf"),
        ]
    )
    request = CanvasTokenRequest("https://canvas.test", "secret-token", session=session)

    request.get("https://canvas.test/api/v1/users/self")
    request.get("https://files.example.test/file.pdf")

    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer secret-token"
    assert "Authorization" not in session.calls[1][1]["headers"]


def test_cross_origin_redirect_drops_authorization() -> None:
    session = Session(
        [
            RawResponse(
                302,
                url="https://canvas.test/files/1/download",
                headers={"Location": "https://files.example.test/signed-file"},
            ),
            RawResponse(url="https://files.example.test/signed-file"),
        ]
    )
    request = CanvasTokenRequest("https://canvas.test", "secret-token", session=session)

    response = request.get("https://canvas.test/files/1/download")

    assert response.status == 200
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer secret-token"
    assert "Authorization" not in session.calls[1][1]["headers"]
    assert all(call[1]["allow_redirects"] is False for call in session.calls)


def test_close_discards_token_and_prevents_reuse() -> None:
    session = Session([])
    request = CanvasTokenRequest("https://canvas.test", "secret-token", session=session)

    request.close()

    assert session.closed is True
    assert request._token == ""
    with pytest.raises(CanvasError, match="transport is closed"):
        request.get("https://canvas.test/api")


@pytest.mark.parametrize(
    "url",
    [
        "http://canvas.test/api/v1/users/self",
        "https://user:password@canvas.test/api/v1/users/self",
    ],
)
def test_unsafe_urls_are_rejected_before_any_request(url: str) -> None:
    session = Session([])
    request = CanvasTokenRequest("https://canvas.test", "secret-token", session=session)

    with pytest.raises(CanvasError) as unsafe:
        request.get(url)

    assert unsafe.value.code == "CANVAS_UNSAFE_URL"
    assert session.calls == []


def test_pagination_rejects_cross_origin_next_link() -> None:
    calls = []

    class Response:
        status = 200
        url = "https://canvas.test/api"
        headers = {
            "content-type": "application/json",
            "link": '<https://attacker.test/next>; rel="next"',
        }

        def json(self):
            return []

    def get(url, **kwargs):
        calls.append(url)
        return Response()

    with pytest.raises(CanvasError) as unsafe:
        paginate(get, "https://canvas.test/api")

    assert unsafe.value.code == "CANVAS_UNSAFE_URL"
    assert calls == ["https://canvas.test/api"]
