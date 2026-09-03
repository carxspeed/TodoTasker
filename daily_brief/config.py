"""Environment loading and strict configuration validation."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
PAGE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class ConfigurationError(ValueError):
    """Raised when configuration is missing or unsafe to guess."""


class TimeInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def valid_clock(cls, value: str) -> str:
        if not TIME_RE.fullmatch(value):
            raise ValueError("must use HH:MM in 24-hour time")
        return value

    @field_validator("end")
    @classmethod
    def ends_after_start(cls, value: str, info) -> str:
        start = info.data.get("start")
        if start is not None and value <= start:
            raise ValueError("interval end must be after start")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    notion_token: str = ""
    notion_parent_page_id: str = ""
    notion_work_db_id: str = ""
    notion_school_db_id: str = ""
    notion_connections_db_id: str = ""
    notion_misc_db_id: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    ical_url: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    model_provider: Literal["local", "anthropic"] = "local"
    ollama_model: str = "qwen3:4b"
    ollama_base_url: HttpUrl = "http://localhost:11434"
    canvas_base: HttpUrl = "https://issaquah.instructure.com"
    timezone: str = "America/Los_Angeles"
    school_hours: dict[str, list[TimeInterval]] = Field(default_factory=dict)
    fixed_busy_windows: dict[str, list[TimeInterval]] = Field(default_factory=dict)
    no_school_patterns: list[str] = Field(default_factory=list)
    informational_all_day_patterns: list[str] = Field(default_factory=list)
    incident_dir: Path | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @field_validator(
        "notion_parent_page_id",
        "notion_work_db_id",
        "notion_school_db_id",
        "notion_connections_db_id",
        "notion_misc_db_id",
    )
    @classmethod
    def valid_optional_page_id(cls, value: str) -> str:
        compact = value.replace("-", "").strip()
        if compact and not PAGE_ID_RE.fullmatch(compact):
            raise ValueError("must be a 32-character hexadecimal Notion id")
        return compact

    @property
    def notion_database_ids(self) -> dict[str, str]:
        return {
            "Work": self.notion_work_db_id,
            "School": self.notion_school_db_id,
            "Connections": self.notion_connections_db_id,
            "Misc": self.notion_misc_db_id,
        }

    @property
    def notion_databases_configured(self) -> bool:
        return all(self.notion_database_ids.values())

    @field_validator("no_school_patterns", "informational_all_day_patterns")
    @classmethod
    def valid_patterns(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("patterns must be non-empty strings")
        return [item.strip() for item in value]


DEFAULT_SCHOOL_HOURS = {
    day: [["07:30", "14:30"]] for day in ("mon", "tue", "wed", "thu", "fri")
}


def _parse_json(name: str, raw: str, expected: type) -> object:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{name} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, expected):
        raise ConfigurationError(f"{name} must decode to {expected.__name__}")
    return value


def _parse_schedule(name: str, raw: str) -> dict[str, list[TimeInterval]]:
    value = _parse_json(name, raw, dict)
    parsed: dict[str, list[TimeInterval]] = {}
    for day, intervals in value.items():
        if day not in WEEKDAYS:
            raise ConfigurationError(f"{name} has invalid weekday {day!r}")
        if not isinstance(intervals, list):
            raise ConfigurationError(f"{name}.{day} must be a list")
        parsed[day] = []
        for pair in intervals:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ConfigurationError(f"{name}.{day} intervals must be [start,end]")
            try:
                parsed[day].append(TimeInterval(start=pair[0], end=pair[1]))
            except Exception as exc:
                raise ConfigurationError(f"{name}.{day} has an invalid interval: {exc}") from exc
    return parsed


def load_settings(
    env_file: str | Path = ".env", *, required: tuple[str, ...] = ()
) -> Settings:
    """Load settings without mutating process environment.

    ``required`` contains environment key names required by a specific command. This
    lets fixture tests and dry runs start without live credentials.
    """

    file_values = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    values = {**file_values, **os.environ}

    missing = [name for name in required if not str(values.get(name, "")).strip()]
    if missing:
        raise ConfigurationError(f"missing required configuration: {', '.join(sorted(missing))}")

    school_raw = values.get("SCHOOL_HOURS_JSON", json.dumps(DEFAULT_SCHOOL_HOURS))
    fixed_raw = values.get("FIXED_BUSY_WINDOWS_JSON", "{}")
    no_school = _parse_json(
        "NO_SCHOOL_PATTERNS_JSON",
        values.get(
            "NO_SCHOOL_PATTERNS_JSON",
            '["no school","school holiday","winter break","spring break"]',
        ),
        list,
    )
    informational = _parse_json(
        "INFORMATIONAL_ALL_DAY_PATTERNS_JSON",
        values.get("INFORMATIONAL_ALL_DAY_PATTERNS_JSON", '["birthday"]'),
        list,
    )
    if not all(isinstance(item, str) for item in [*no_school, *informational]):
        raise ConfigurationError("pattern JSON values must contain only strings")

    incident_raw = str(values.get("INCIDENT_DIR", "")).strip()
    payload = {
        "notion_token": values.get("NOTION_TOKEN", ""),
        "notion_parent_page_id": values.get("NOTION_PARENT_PAGE_ID", ""),
        "notion_work_db_id": values.get("NOTION_WORK_DB_ID", ""),
        "notion_school_db_id": values.get("NOTION_SCHOOL_DB_ID", ""),
        "notion_connections_db_id": values.get("NOTION_CONNECTIONS_DB_ID", ""),
        "notion_misc_db_id": values.get("NOTION_MISC_DB_ID", ""),
        "telegram_bot_token": values.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": values.get("TELEGRAM_CHAT_ID", ""),
        "ical_url": values.get("ICAL_URL", ""),
        "anthropic_api_key": values.get("ANTHROPIC_API_KEY", ""),
        "anthropic_model": values.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        "model_provider": values.get("MODEL_PROVIDER", "local"),
        "ollama_model": values.get("OLLAMA_MODEL", "qwen3:4b"),
        "ollama_base_url": values.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        "canvas_base": values.get("CANVAS_BASE", "https://issaquah.instructure.com"),
        "timezone": values.get("TIMEZONE", "America/Los_Angeles"),
        "school_hours": _parse_schedule("SCHOOL_HOURS_JSON", school_raw),
        "fixed_busy_windows": _parse_schedule("FIXED_BUSY_WINDOWS_JSON", fixed_raw),
        "no_school_patterns": no_school,
        "informational_all_day_patterns": informational,
        "incident_dir": Path(incident_raw).expanduser().resolve() if incident_raw else None,
    }
    try:
        return Settings.model_validate(payload)
    except Exception as exc:
        raise ConfigurationError(str(exc)) from exc
