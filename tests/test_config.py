import json
from pathlib import Path

import pytest

from daily_brief.config import ConfigurationError, load_settings


def write_env(path: Path, **values: str) -> None:
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")


def test_defaults_are_safe_and_timezone_is_valid(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.env")
    assert settings.model_provider == "local"
    assert settings.timezone == "America/Los_Angeles"
    assert settings.notion_token == ""
    assert settings.school_hours["mon"][0].start == "07:30"


def test_required_values_are_command_specific(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="NOTION_TOKEN"):
        load_settings(tmp_path / "missing.env", required=("NOTION_TOKEN",))


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("SCHOOL_HOURS_JSON", '{"monday":[["07:30","14:30"]]}', "invalid weekday"),
        ("SCHOOL_HOURS_JSON", '{"mon":[["7:30","14:30"]]}', "invalid interval"),
        ("FIXED_BUSY_WINDOWS_JSON", "[]", "must decode to dict"),
        ("NO_SCHOOL_PATTERNS_JSON", '[""]', "non-empty"),
    ],
)
def test_malformed_json_configuration_fails_loudly(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    env = tmp_path / ".env"
    write_env(env, **{key: value})
    with pytest.raises(ConfigurationError, match=message):
        load_settings(env)


def test_notion_ids_are_normalized(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    value = "01234567-89ab-cdef-0123-456789abcdef"
    write_env(
        env,
        NOTION_PARENT_PAGE_ID=value,
        NOTION_WORK_DB_ID=value,
        NOTION_SCHOOL_DB_ID=value,
        NOTION_CONNECTIONS_DB_ID=value,
        NOTION_MISC_DB_ID=value,
    )
    settings = load_settings(env)
    assert settings.notion_parent_page_id == "0123456789abcdef0123456789abcdef"
    assert settings.notion_databases_configured
    assert set(settings.notion_database_ids) == {"Work", "School", "Connections", "Misc"}
