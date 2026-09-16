from __future__ import annotations

import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from daily_brief.secret_setup import FIELD_LABELS, create_secret_setup_server


class MemoryVault:
    def __init__(self) -> None:
        self.values = {"NOTION_TOKEN": "existing-notion-secret"}

    def configured(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))

    def set_many(self, updates: dict[str, str]) -> None:
        self.values.update(updates)


def test_loopback_setup_page_uses_password_fields_and_no_store_headers() -> None:
    vault = MemoryVault()
    server, url = create_secret_setup_server(vault)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(url, timeout=2) as response:
            body = response.read().decode("utf-8")
            assert response.headers["Cache-Control"] == "no-store, max-age=0"
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert body.count('type="password"') == len(FIELD_LABELS)
        assert "existing-notion-secret" not in body
        assert "already configured" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_loopback_setup_saves_nonempty_values_and_shuts_down() -> None:
    vault = MemoryVault()
    server, url = create_secret_setup_server(vault)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = urlencode(
        {
            "TELEGRAM_BOT_TOKEN": "new-telegram-secret",
            "NOTION_TOKEN": "",
            "ICAL_URL": "https://calendar.test/private.ics",
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            # Chromium can use an opaque origin for an in-app browser page.
            # Loopback binding, exact Host validation, and the random path remain.
            "Origin": "null",
        },
    )

    with urlopen(request, timeout=2) as response:
        assert "Secrets saved" in response.read().decode("utf-8")
    thread.join(timeout=2)
    server.server_close()

    assert not thread.is_alive()
    assert vault.values["TELEGRAM_BOT_TOKEN"] == "new-telegram-secret"
    assert vault.values["NOTION_TOKEN"] == "existing-notion-secret"
    assert vault.values["ICAL_URL"] == "https://calendar.test/private.ics"


def test_loopback_setup_rejects_wrong_nonce() -> None:
    vault = MemoryVault()
    server, url = create_secret_setup_server(vault)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wrong_url = url.rsplit("/", 1)[0] + "/wrong"
    try:
        with pytest.raises(HTTPError) as exc:
            urlopen(wrong_url, timeout=2)
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
