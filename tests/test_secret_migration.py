from pathlib import Path

from daily_brief.secret_vault import SecretVault
from manage_secrets import migrate_env_secrets


def fake_protect(value: bytes) -> bytes:
    return bytes(byte ^ 0x5A for byte in value)


def test_migration_encrypts_then_blanks_every_plaintext_secret(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "NOTION_TOKEN=notion-secret\n"
        "TELEGRAM_BOT_TOKEN=telegram-secret\n"
        "TIMEZONE=America/Los_Angeles\n",
        encoding="utf-8",
    )
    vault_path = tmp_path / "secrets.dpapi"
    vault = SecretVault(
        vault_path,
        protect=fake_protect,
        unprotect=fake_protect,
        harden=lambda _: None,
    )

    migrated = migrate_env_secrets(env, vault)

    assert migrated == ("NOTION_TOKEN", "TELEGRAM_BOT_TOKEN")
    assert env.read_text(encoding="utf-8").splitlines() == [
        "NOTION_TOKEN=",
        "TELEGRAM_BOT_TOKEN=",
        "TIMEZONE=America/Los_Angeles",
    ]
    assert vault.get("NOTION_TOKEN") == "notion-secret"
    assert vault.get("TELEGRAM_BOT_TOKEN") == "telegram-secret"
    assert b"notion-secret" not in vault_path.read_bytes()


def test_migration_with_no_plaintext_secrets_is_a_noop(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NOTION_TOKEN=\nTIMEZONE=America/Los_Angeles\n", encoding="utf-8")
    vault = SecretVault(
        tmp_path / "secrets.dpapi",
        protect=fake_protect,
        unprotect=fake_protect,
        harden=lambda _: None,
    )

    assert migrate_env_secrets(env, vault) == ()
    assert not vault.path.exists()
