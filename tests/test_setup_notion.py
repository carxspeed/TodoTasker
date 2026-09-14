from pathlib import Path

from daily_brief.config import load_settings
from daily_brief.envfile import persist_env_value
from daily_brief.models import NotionWorkItem
from daily_brief.notion import WorkSnapshot
from setup_notion_db import configure_task_databases, persist_database_id, persist_work_db_id


class Vault:
    def get_many(self, names):
        return {"NOTION_TOKEN": "test-token"} if "NOTION_TOKEN" in names else {}


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


def test_partial_setup_reuses_tables_and_validates_school_page(tmp_path: Path) -> None:
    existing_id = "0123456789abcdef0123456789abcdef"
    school_page_id = "b" * 32
    school_db_id = "c" * 32
    env = tmp_path / ".env"
    env.write_text(
        f"NOTION_PARENT_PAGE_ID={existing_id}\n"
        f"NOTION_SCHOOL_PAGE_ID={school_page_id}\n"
        f"NOTION_WORK_DB_ID={existing_id}\n"
        f"NOTION_SCHOOL_DB_ID={school_db_id}\n",
        encoding="utf-8",
    )
    created = []

    class FakeClient:
        def __init__(self, token, database_id, parent_page_id):
            self.database_id = database_id
            self.parent_page_id = parent_page_id

        def retrieve_parent_page(self):
            return {"id": existing_id}

        def retrieve_database(self):
            parent = school_page_id if self.database_id == school_db_id else existing_id
            return {"id": self.database_id, "parent": {"page_id": parent}}

        def retrieve_page(self, page_id):
            return {"id": page_id}

        def list_block_children(self, page_id):
            return []

        def create_work_database(self, title):
            created.append(title)
            return {"id": {"Connections": "d", "Misc": "e"}[title] * 32}

    messages = configure_task_databases(
        load_settings(env, secret_vault=Vault()), env_path=env, client_factory=FakeClient
    )
    assert created == ["Connections", "Misc"]
    assert messages[0].startswith("Validated Work database")
    saved = load_settings(env, secret_vault=Vault())
    assert saved.notion_databases_configured
    assert saved.notion_school_page_id == school_page_id
    assert saved.notion_school_db_id == school_db_id
    assert saved.notion_connections_db_id == "d" * 32
    assert saved.notion_misc_db_id == "e" * 32


def test_school_migration_copies_general_rows_before_archiving_old_table(
    tmp_path: Path,
) -> None:
    parent_id = "a" * 32
    old_school_id = "b" * 32
    school_page_id = "c" * 32
    general_id = "d" * 32
    env = tmp_path / ".env"
    env.write_text(
        f"NOTION_PARENT_PAGE_ID={parent_id}\n"
        f"NOTION_WORK_DB_ID={parent_id}\n"
        f"NOTION_SCHOOL_DB_ID={old_school_id}\n"
        f"NOTION_CONNECTIONS_DB_ID={parent_id}\n"
        f"NOTION_MISC_DB_ID={parent_id}\n",
        encoding="utf-8",
    )
    created_rows = []
    archived = []

    class FakeClient:
        def __init__(self, token, database_id, parent_page_id):
            self.database_id = database_id

        def retrieve_parent_page(self):
            return {"id": parent_id}

        def retrieve_page(self, page_id):
            return {"id": page_id}

        def retrieve_database(self):
            return {
                "id": self.database_id,
                "parent": {"page_id": parent_id},
            }

        def list_block_children(self, page_id):
            return []

        def create_child_page(self, title):
            assert title == "School"
            return {"id": school_page_id}

        def create_database(self, title, **kwargs):
            assert title == "General"
            assert kwargs["parent_page_id"] == school_page_id
            assert kwargs["is_inline"] is True
            return {"id": general_id}

        def get_active_work(self):
            if self.database_id == old_school_id:
                return WorkSnapshot(
                    items=[
                        NotionWorkItem(
                            key="notion:old",
                            page_id="old",
                            url="https://notion.test/old",
                            name="Review notes",
                            type="Task",
                            next_step="Read chapter one.",
                        )
                    ]
                )
            return WorkSnapshot(items=[])

        def create_work_item(self, fields):
            created_rows.append(fields)
            return {"id": "new"}

        def archive_database(self, database_id):
            archived.append(database_id)
            return {"id": database_id, "archived": True}

    configure_task_databases(
        load_settings(env, secret_vault=Vault()),
        env_path=env,
        client_factory=FakeClient,
    )
    saved = load_settings(env, secret_vault=Vault())
    assert saved.notion_school_page_id == school_page_id
    assert saved.notion_school_db_id == general_id
    assert created_rows[0]["Name"] == "Review notes"
    assert archived == [old_school_id]
