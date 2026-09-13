from pathlib import Path

from daily_brief.canvas import CANVAS_STATE_MAGIC
from daily_brief.secret_vault import SecretVault
from daily_brief.security_audit import run_security_audit


def fake_protect(value: bytes) -> bytes:
    return bytes(byte ^ 0x37 for byte in value)


def make_vault(path: Path) -> SecretVault:
    return SecretVault(
        path,
        protect=fake_protect,
        unprotect=fake_protect,
        harden=lambda _: None,
    )


def test_clean_repository_audit_reports_no_issues(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    env = repository / ".env"
    env.write_text("NOTION_TOKEN=\nTIMEZONE=America/Los_Angeles\n", encoding="utf-8")
    (repository / "app.py").write_text("print('safe')\n", encoding="utf-8")
    vault = make_vault(tmp_path / "appdata" / "secrets.dpapi")
    vault.set("NOTION_TOKEN", "live-notion-secret")
    session = tmp_path / "appdata" / "canvas-session.dpapi"
    session.write_bytes(CANVAS_STATE_MAGIC + b"encrypted")

    issues = run_security_audit(
        repository,
        env,
        vault,
        canvas_session=session,
        acl_check=lambda _: True,
    )

    assert issues == []


def test_canvas_token_does_not_require_browser_fallback_session(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    env = repository / ".env"
    env.write_text("CANVAS_ACCESS_TOKEN=\n", encoding="utf-8")
    vault = make_vault(tmp_path / "appdata" / "secrets.dpapi")
    vault.set("CANVAS_ACCESS_TOKEN", "live-canvas-token")

    issues = run_security_audit(
        repository,
        env,
        vault,
        canvas_session=tmp_path / "missing-session.dpapi",
        acl_check=lambda _: True,
    )

    assert issues == []


def test_audit_names_exposures_without_repeating_secret(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    env = repository / ".env"
    env.write_text("NOTION_TOKEN=plaintext-notion\n", encoding="utf-8")
    (repository / "leak.txt").write_text("live-notion-secret", encoding="utf-8")
    (repository / "profile").mkdir()
    vault = make_vault(tmp_path / "appdata" / "secrets.dpapi")
    vault.set("NOTION_TOKEN", "live-notion-secret")
    session = tmp_path / "appdata" / "canvas-session.dpapi"
    session.write_text("plaintext", encoding="utf-8")

    issues = run_security_audit(
        repository,
        env,
        vault,
        canvas_session=session,
        acl_check=lambda _: False,
    )
    rendered = "\n".join(issues)

    assert "plaintext NOTION_TOKEN remains in .env" in issues
    assert "NOTION_TOKEN appears in leak.txt" in issues
    assert "legacy Chromium profile still exists" in issues
    assert "Canvas session is not in the encrypted format" in issues
    assert "live-notion-secret" not in rendered
    assert "plaintext-notion" not in rendered
