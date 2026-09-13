"""Read-only checks for accidental credential exposure and unsafe legacy state."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

from .canvas import CANVAS_STATE_MAGIC, canvas_storage_state_path
from .secret_vault import SECRET_NAMES, SecretVault, windows_acl_is_restricted


EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "fixtures/private",
    "htmlcov",
    "profile",
    "state",
    "venv",
}
MAX_SCAN_BYTES = 5 * 1024 * 1024


def _excluded(relative: Path) -> bool:
    text = relative.as_posix()
    return any(text == part or text.startswith(f"{part}/") for part in EXCLUDED_PARTS)


def run_security_audit(
    root: str | Path,
    env_file: str | Path,
    vault: SecretVault,
    *,
    canvas_session: Path | None = None,
    acl_check: Callable[[Path], bool] = windows_acl_is_restricted,
) -> list[str]:
    """Return safe issue descriptions; never include credential values."""
    repository = Path(root).resolve()
    env_path = Path(env_file).resolve()
    issues: list[str] = []

    env_values = dotenv_values(env_path) if env_path.exists() else {}
    for name in sorted(SECRET_NAMES):
        if str(env_values.get(name, "")).strip():
            issues.append(f"plaintext {name} remains in .env")

    if not vault.path.exists():
        issues.append("encrypted secret vault is missing")
        secrets: dict[str, str] = {}
    else:
        secrets = vault.get_many(SECRET_NAMES)
        if not acl_check(vault.path.parent) or not acl_check(vault.path):
            issues.append("encrypted secret vault permissions are too broad")

    session_path = canvas_session or canvas_storage_state_path()
    if not session_path.exists() and not secrets.get("CANVAS_ACCESS_TOKEN"):
        issues.append("encrypted Canvas session is missing")
    elif session_path.exists():
        try:
            header = session_path.read_bytes()[: len(CANVAS_STATE_MAGIC)]
        except OSError:
            header = b""
        if header != CANVAS_STATE_MAGIC:
            issues.append("Canvas session is not in the encrypted format")
        if not acl_check(session_path):
            issues.append("encrypted Canvas session permissions are too broad")

    if (repository / "profile").exists():
        issues.append("legacy Chromium profile still exists")

    encoded = {
        name: value.encode("utf-8")
        for name, value in secrets.items()
        if len(value.encode("utf-8")) >= 8
    }
    for path in repository.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(repository)
        if _excluded(relative) or path.resolve() == env_path:
            continue
        if path.name == "storage-state.json":
            issues.append(f"plaintext Canvas state exists at {relative.as_posix()}")
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            content = path.read_bytes()
        except OSError:
            issues.append(f"could not inspect {relative.as_posix()}")
            continue
        for name, secret in encoded.items():
            if secret in content:
                issues.append(f"{name} appears in {relative.as_posix()}")

    return sorted(set(issues))
