"""Windows user-scoped encrypted storage for unattended service credentials."""

from __future__ import annotations

import ctypes
import getpass
import json
import os
import subprocess
from ctypes import wintypes
from pathlib import Path
from typing import Callable


VAULT_MAGIC = b"TODO-TASKER-DPAPI\x01\n"
MAX_VAULT_BYTES = 1024 * 1024
SECRET_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "CANVAS_ACCESS_TOKEN",
        "ICAL_URL",
        "NOTION_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    }
)


class SecretVaultError(RuntimeError):
    """Raised when the encrypted vault cannot be safely accessed."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))),
        buffer,
    )


def _windows_crypto():
    if os.name != "nt":
        raise SecretVaultError("the secure vault requires Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def dpapi_protect(plaintext: bytes) -> bytes:
    """Encrypt bytes so only the current Windows user can decrypt them."""
    crypt32, kernel32 = _windows_crypto()
    source, source_buffer = _blob(plaintext)
    destination = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "TodoTasker secret vault",
        None,
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(destination),
    ):
        raise SecretVaultError(
            f"Windows could not encrypt the secret vault (error {ctypes.get_last_error()})"
        )
    del source_buffer
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


def dpapi_unprotect(ciphertext: bytes) -> bytes:
    """Decrypt bytes protected for the current Windows user."""
    crypt32, kernel32 = _windows_crypto()
    source, source_buffer = _blob(ciphertext)
    destination = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(destination),
    ):
        raise SecretVaultError(
            f"Windows could not decrypt the secret vault (error {ctypes.get_last_error()})"
        )
    del source_buffer
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


def default_vault_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise SecretVaultError("LOCALAPPDATA is unavailable")
    return Path(local_app_data).resolve() / "TodoTasker" / "secrets.dpapi"


def _current_windows_principal() -> str:
    username = os.environ.get("USERNAME", "").strip() or getpass.getuser()
    domain = os.environ.get("USERDOMAIN", "").strip()
    return f"{domain}\\{username}" if domain else username


def restrict_windows_acl(path: Path) -> None:
    """Remove inherited access and allow only this user and LocalSystem."""
    if os.name != "nt":
        raise SecretVaultError("vault ACL hardening requires Windows")
    is_directory = path.is_dir()
    inheritance = "(OI)(CI)F" if is_directory else "F"
    command = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{_current_windows_principal()}:{inheritance}",
        f"*S-1-5-18:{inheritance}",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode:
        raise SecretVaultError("Windows could not restrict access to the secret vault")


class SecretVault:
    """Small JSON secret store encrypted as one authenticated DPAPI payload."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
        harden: Callable[[Path], None] | None = None,
    ) -> None:
        self.path = Path(path).resolve() if path else default_vault_path()
        self._protect = protect or dpapi_protect
        self._unprotect = unprotect or dpapi_unprotect
        self._harden = harden or restrict_windows_acl

    @staticmethod
    def _name(name: str) -> str:
        normalized = name.strip().upper()
        if normalized not in SECRET_NAMES:
            raise SecretVaultError(f"unsupported secret name: {normalized or '<empty>'}")
        return normalized

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        raw = self.path.read_bytes()
        if len(raw) > MAX_VAULT_BYTES or not raw.startswith(VAULT_MAGIC):
            raise SecretVaultError("the secret vault is invalid")
        try:
            plaintext = bytearray(self._unprotect(raw[len(VAULT_MAGIC) :]))
            decoded = json.loads(plaintext.decode("utf-8"))
        except SecretVaultError:
            raise
        except Exception as exc:
            raise SecretVaultError("the secret vault is invalid") from exc
        finally:
            if "plaintext" in locals():
                plaintext[:] = b"\x00" * len(plaintext)
        if not isinstance(decoded, dict) or any(
            key not in SECRET_NAMES or not isinstance(value, str)
            for key, value in decoded.items()
        ):
            raise SecretVaultError("the secret vault is invalid")
        return decoded

    def _write(self, values: dict[str, str]) -> None:
        plaintext = bytearray(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        try:
            encrypted = self._protect(bytes(plaintext))
        finally:
            plaintext[:] = b"\x00" * len(plaintext)
        payload = VAULT_MAGIC + encrypted
        if len(payload) > MAX_VAULT_BYTES:
            raise SecretVaultError("the secret vault is unexpectedly large")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._harden(self.path.parent)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_bytes(payload)
            self._harden(temporary)
            os.replace(temporary, self.path)
            self._harden(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, name: str) -> str:
        return self._read().get(self._name(name), "")

    def set(self, name: str, value: str) -> None:
        normalized = self._name(name)
        if not value:
            raise SecretVaultError("secret values cannot be empty")
        values = self._read()
        values[normalized] = value
        self._write(values)

    def delete(self, name: str) -> bool:
        normalized = self._name(name)
        values = self._read()
        if normalized not in values:
            return False
        del values[normalized]
        self._write(values)
        return True

    def configured(self) -> tuple[str, ...]:
        return tuple(sorted(self._read()))
