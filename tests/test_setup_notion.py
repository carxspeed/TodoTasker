from pathlib import Path

from daily_brief.config import load_settings
from daily_brief.envfile import persist_env_value
from setup_notion_db import configure_task_databases, persist_database_id, persist_work_db_id


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


def test_persist_database_id_uses_the_requested_table_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("NOTION_TOKEN=secret\n", encoding="utf-8")
    persist_database_id(env, "NOTION_SCHOOL_DB_ID", "01234567-89ab-cdef-0123-456789abcdef")
    assert env.read_text(encoding="utf-8").splitlines() == [
        "NOTION_TOKEN=secret",
        "NOTION_SCHOOL_DB_ID=0123456789abcdef0123456789abcdef",
    ]


def test_partial_setup_reuses_work_and_creates_each_missing_table_once(tmp_path: Path) -> None:
    existing_id = "0123456789abcdef0123456789abcdef"
    env = tmp_path / ".env"
    env.write_text(
        f"NOTION_TOKEN=secret\nNOTION_PARENT_PAGE_ID={existing_id}\n"
        f"NOTION_WORK_DB_ID={existing_id}\n",
        encoding="utf-8",
    )
    created = []

    class FakeClient:
        def __init__(self, token, database_id, parent_page_id):
            self.database_id = database_id

        def retrieve_parent_page(self):
            return {"id": existing_id}

        def retrieve_database(self):
            return {"id": self.database_id}

        def create_work_database(self, title):
            created.append(title)
            return {"id": {"School": "a", "Connections": "b", "Misc": "c"}[title] * 32}

    messages = configure_task_databases(
        load_settings(env), env_path=env, client_factory=FakeClient
    )
    assert created == ["School", "Connections", "Misc"]
    assert messages[0].startswith("Validated Work database")
    saved = load_settings(env)
    assert saved.notion_databases_configured
    assert saved.notion_school_db_id == "a" * 32
    assert saved.notion_connections_db_id == "b" * 32
    assert saved.notion_misc_db_id == "c" * 32
