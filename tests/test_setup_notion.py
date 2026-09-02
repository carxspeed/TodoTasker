from pathlib import Path

from setup_notion_db import persist_work_db_id
from daily_brief.envfile import persist_env_value


def test_persist_work_db_id_updates_only_target_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NOTION_TOKEN=secret\nNOTION_WORK_DB_ID=old\nMODEL_PROVIDER=local\n", encoding="utf-8")
    persist_work_db_id(env, "01234567-89ab-cdef-0123-456789abcdef")
    assert env.read_text(encoding="utf-8").splitlines() == [
        "NOTION_TOKEN=secret",
        "NOTION_WORK_DB_ID=0123456789abcdef0123456789abcdef",
        "MODEL_PROVIDER=local",
    ]


def test_persist_env_value_appends_missing_key_without_touching_secrets(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
    persist_env_value(env, "TELEGRAM_CHAT_ID", "1234")
    assert env.read_text(encoding="utf-8").splitlines() == [
        "TELEGRAM_BOT_TOKEN=secret",
        "TELEGRAM_CHAT_ID=1234",
    ]
