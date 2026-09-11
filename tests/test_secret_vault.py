import os
from pathlib import Path

import pytest

from daily_brief.secret_vault import (
    SECRET_NAMES,
    SecretVault,
    SecretVaultError,
    dpapi_protect,
    dpapi_unprotect,
)


def fake_protect(value: bytes) -> bytes:
    return bytes(byte ^ 0xA5 for byte in value)


def fake_unprotect(value: bytes) -> bytes:
    return fake_protect(value)


def vault(path: Path) -> SecretVault:
    return SecretVault(
        path,
        protect=fake_protect,
        unprotect=fake_unprotect,
        harden=lambda _: None,
    )


def test_round_trip_never_writes_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "secrets.dpapi"
    store = vault(path)
    secret = "not-a-real-secret-value"

    store.set("CANVAS_ACCESS_TOKEN", secret)

    assert store.get("CANVAS_ACCESS_TOKEN") == secret
    assert secret.encode() not in path.read_bytes()
    assert store.configured() == ("CANVAS_ACCESS_TOKEN",)


def test_updates_are_atomic_and_preserve_other_values(tmp_path: Path) -> None:
    store = vault(tmp_path / "secrets.dpapi")
    store.set("CANVAS_ACCESS_TOKEN", "canvas-value")
    store.set("TELEGRAM_BOT_TOKEN", "telegram-value")

    assert store.get("CANVAS_ACCESS_TOKEN") == "canvas-value"
    assert store.get("TELEGRAM_BOT_TOKEN") == "telegram-value"
    assert not list(tmp_path.glob("*.tmp"))


def test_set_many_encrypts_all_values_in_one_write(tmp_path: Path) -> None:
    path = tmp_path / "secrets.dpapi"
    store = vault(path)
    store.set_many(
        {
            "NOTION_TOKEN": "notion-value",
            "TELEGRAM_BOT_TOKEN": "telegram-value",
        }
    )

    assert store.get_many(frozenset({"NOTION_TOKEN", "TELEGRAM_BOT_TOKEN"})) == {
        "NOTION_TOKEN": "notion-value",
        "TELEGRAM_BOT_TOKEN": "telegram-value",
    }
    assert b"notion-value" not in path.read_bytes()
    assert b"telegram-value" not in path.read_bytes()


def test_delete_does_not_reveal_or_damage_other_values(tmp_path: Path) -> None:
    store = vault(tmp_path / "secrets.dpapi")
    store.set("NOTION_TOKEN", "notion-value")
    store.set("ICAL_URL", "https://calendar.test/private")

    assert store.delete("NOTION_TOKEN") is True
    assert store.delete("NOTION_TOKEN") is False
    assert store.get("ICAL_URL") == "https://calendar.test/private"


def test_unknown_secret_names_are_rejected(tmp_path: Path) -> None:
    store = vault(tmp_path / "secrets.dpapi")

    with pytest.raises(SecretVaultError, match="unsupported secret name"):
        store.set("MICROSOFT_PASSWORD", "do-not-store-this")

    assert "MICROSOFT_PASSWORD" not in SECRET_NAMES


def test_corrupt_or_plaintext_files_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "secrets.dpapi"
    path.write_text('{"CANVAS_ACCESS_TOKEN":"plaintext"}', encoding="utf-8")

    with pytest.raises(SecretVaultError, match="vault is invalid"):
        vault(path).configured()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_windows_dpapi_current_user_round_trip() -> None:
    plaintext = b"TodoTasker DPAPI test value"

    ciphertext = dpapi_protect(plaintext)

    assert ciphertext != plaintext
    assert dpapi_unprotect(ciphertext) == plaintext
