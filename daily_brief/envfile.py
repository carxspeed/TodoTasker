"""Atomic updates for individual ignored `.env` keys."""

from __future__ import annotations

from pathlib import Path

from .atomic import atomic_write_text


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

