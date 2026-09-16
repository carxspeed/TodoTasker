"""One-time loopback form for entering secrets without a terminal prompt."""

from __future__ import annotations

import html
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import parse_qs

from .secret_vault import SECRET_NAMES, SecretVaultError


MAX_FORM_BYTES = 64 * 1024
FIELD_LABELS = {
    "TELEGRAM_BOT_TOKEN": "Telegram bot token",
    "NOTION_TOKEN": "Notion integration secret",
    "ICAL_URL": "Google Calendar secret iCal URL",
    "CANVAS_ACCESS_TOKEN": "Canvas access token (optional)",
    "MICROSOFT_EMAIL": "Microsoft school email (used only to renew Canvas)",
    "MICROSOFT_PASSWORD": "Microsoft school password (used only to renew Canvas)",
    "ANTHROPIC_API_KEY": "Anthropic API key (optional)",
}


class VaultWriter(Protocol):
    def configured(self) -> tuple[str, ...]: ...

    def set_many(self, updates: dict[str, str]) -> None: ...


def _security_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'",
    )


def render_setup_page(path: str, configured: set[str]) -> bytes:
    fields: list[str] = []
    for name, label in FIELD_LABELS.items():
        status = " — already configured; leave blank to keep it" if name in configured else ""
        fields.append(
            f'<label for="{name}">{html.escape(label + status)}</label>'
            f'<input id="{name}" name="{name}" type="password" '
            'autocomplete="new-password" autocapitalize="off" spellcheck="false">'
        )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TodoTasker secure setup</title>
<style>
body {{ background:#111827; color:#f9fafb; font:16px system-ui; margin:0; }}
main {{ max-width:680px; margin:40px auto; padding:28px; background:#1f2937; border-radius:16px; }}
h1 {{ margin-top:0; }}
p {{ color:#d1d5db; line-height:1.5; }}
label {{ display:block; margin-top:18px; margin-bottom:6px; }}
input {{ box-sizing:border-box; width:100%; padding:12px; border:1px solid #6b7280; border-radius:8px; background:#111827; color:#fff; }}
button {{ margin-top:24px; padding:12px 18px; border:0; border-radius:8px; background:#22c55e; color:#052e16; font-weight:700; cursor:pointer; }}
</style>
</head>
<body><main>
<h1>TodoTasker secure setup</h1>
<p>This one-time page is available only on this computer. Values are sent to a
loopback-only process, encrypted with Windows DPAPI, and never printed. Blank fields
do not change an existing value.</p>
<form method="post" action="{html.escape(path)}" autocomplete="off">
{''.join(fields)}
<button type="submit">Encrypt and save</button>
</form>
</main></body></html>"""
    return document.encode("utf-8")


def _result_page(success: bool) -> bytes:
    heading = "Secrets saved" if success else "Could not save secrets"
    detail = (
        "You can close this tab and return to Codex."
        if success
        else "No values were stored. Close this tab and retry the setup command."
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{heading}</title></head><body><h1>{heading}</h1><p>{detail}</p>"
        "</body></html>"
    ).encode("utf-8")


def create_secret_setup_server(
    vault: VaultWriter,
) -> tuple[ThreadingHTTPServer, str]:
    """Create a loopback-only, nonce-protected, one-save HTTP server."""
    nonce_path = "/" + secrets.token_urlsafe(32)

    class SecretSetupHandler(BaseHTTPRequestHandler):
        server_version = ""
        sys_version = ""

        def log_message(self, _format: str, *_args) -> None:
            return

        def _request_has_expected_host(self) -> bool:
            expected_host = f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host", "") == expected_host

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            _security_headers(self)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path != nonce_path or not self._request_has_expected_host():
                self._send(404, b"Not found")
                return
            self._send(200, render_setup_page(nonce_path, set(vault.configured())))

        def do_POST(self) -> None:  # noqa: N802
            if self.path != nonce_path or not self._request_has_expected_host():
                self._send(404, b"Not found")
                return
            if not self.headers.get("Content-Type", "").startswith(
                "application/x-www-form-urlencoded"
            ):
                self._send(415, b"Unsupported media type")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, b"Invalid request")
                return
            if length <= 0 or length > MAX_FORM_BYTES:
                self._send(413, b"Invalid form size")
                return

            raw = bytearray(self.rfile.read(length))
            fields: dict[str, list[str]] = {}
            updates: dict[str, str] = {}
            try:
                fields = parse_qs(
                    raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True
                )
                for name in SECRET_NAMES:
                    values = fields.get(name, [])
                    if values and values[-1]:
                        updates[name] = values[-1]
                if updates:
                    vault.set_many(updates)
            except (SecretVaultError, UnicodeError, ValueError):
                self._send(500, _result_page(False))
                return
            finally:
                raw[:] = b"\x00" * len(raw)
                fields.clear()
                updates.clear()

            self._send(200, _result_page(True))
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", 0), SecretSetupHandler)
    server.daemon_threads = True
    return server, f"http://127.0.0.1:{server.server_port}{nonce_path}"


def serve_secret_setup(vault: VaultWriter, *, timeout_seconds: int = 600) -> None:
    server, url = create_secret_setup_server(vault)
    timer = threading.Timer(timeout_seconds, server.shutdown)
    timer.daemon = True
    timer.start()
    print(f"setup_url={url}", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        timer.cancel()
        server.server_close()
