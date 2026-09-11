"""Atomic updates for individual ignored `.env` keys."""

from __future__ import annotations

import re
from pathlib import Path

from .atomic import atomic_write_text


ENV_ASSIGNMENT_RE = re.compile(
    r"^(?P<indent>\s*)(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*="
)


def persist_env_value(path: str | Path, key: str, value: str) -> None:
    destination = Path(path)
    lines = destination.read_text(encoding="utf-8").splitlines() if destination.exists() else []
    replacement = f"{key}={value}"
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}="):
            updated.append(replacement)
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(replacement)
    atomic_write_text(destination, "\n".join(updated).rstrip() + "\n")


def blank_env_values(path: str | Path, keys: set[str] | frozenset[str]) -> None:
    """Blank every assignment for selected keys in a single atomic rewrite."""
    destination = Path(path)
    if not destination.exists():
        return
    updated: list[str] = []
    for line in destination.read_text(encoding="utf-8").splitlines():
        match = ENV_ASSIGNMENT_RE.match(line)
        if match and match.group("key") in keys:
            updated.append(f"{match.group('indent')}{match.group('key')}=")
        else:
            updated.append(line)
    atomic_write_text(destination, "\n".join(updated).rstrip() + "\n")
