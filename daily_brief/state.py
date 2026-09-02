"""Versioned state persistence with backup recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .atomic import atomic_write, atomic_write_json
from .models import DailyBriefState, StateWarning
from .timeutils import utc_now


class StateStore:
    def __init__(self, state_dir: str | Path = "state", *, fallback_work_db_id: str = "") -> None:
        self.state_dir = Path(state_dir)
        self.primary = self.state_dir / "state.json"
        self.backup = self.state_dir / "state.json.bak"
        self.fallback_work_db_id = fallback_work_db_id.replace("-", "") or None

    @staticmethod
    def _decode(path: Path) -> DailyBriefState:
        return DailyBriefState.model_validate_json(path.read_text(encoding="utf-8"))

    def load(self) -> tuple[DailyBriefState, LiteralStatus]:
        if self.primary.exists():
            try:
                return self._decode(self.primary), "primary"
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
                quarantine = self.state_dir / f"state.json.corrupt.{uuid4().hex}"
                self.state_dir.mkdir(parents=True, exist_ok=True)
                os.replace(self.primary, quarantine)

        if self.backup.exists():
            try:
                restored = self._decode(self.backup)
                atomic_write_json(self.primary, restored.model_dump(mode="json"))
                return restored, "backup"
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
                pass

        state = DailyBriefState(work_db_id=self.fallback_work_db_id)
        state.warnings.append(
            StateWarning(
                id=f"state-recovery-{uuid4().hex}",
                created_at=utc_now(),
                text=(
                    "Runtime state was rebuilt because both state copies were unavailable; "
                    "Telegram will establish a fresh update baseline before processing replies."
                ),
            )
        )
        return state, "defaults"

    def save(self, state: DailyBriefState) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        validated = DailyBriefState.model_validate(state.model_dump(mode="json"))
        if self.primary.exists():
            try:
                previous = self._decode(self.primary)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
                previous = None
            if previous is not None:
                atomic_write_json(self.backup, previous.model_dump(mode="json"))
        atomic_write_json(self.primary, validated.model_dump(mode="json"))


LiteralStatus = str

