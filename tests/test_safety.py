from pathlib import Path


def test_sensitive_paths_are_ignored() -> None:
    rules = {
        line.strip()
        for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {".env", "profile/", "state/", "fixtures/private/"} <= rules


def test_example_environment_contains_no_credentials() -> None:
    entries = {}
    for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            entries[key] = value
    for key in (
        "NOTION_TOKEN",
        "NOTION_PARENT_PAGE_ID",
        "NOTION_WORK_DB_ID",
        "NOTION_SCHOOL_DB_ID",
        "NOTION_CONNECTIONS_DB_ID",
        "NOTION_MISC_DB_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "ICAL_URL",
        "ANTHROPIC_API_KEY",
    ):
        assert entries[key] == ""
