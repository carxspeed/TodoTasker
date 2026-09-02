import json
from datetime import date
from pathlib import Path

import pytest

from daily_brief.notion import BriefPageResult, NotionClient


class FakeBriefClient(NotionClient):
    def __init__(self):
        self.parent_page_id = "parent"
        self.blocks = [
            {
                "id": "old-1",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "Old complete brief"}]},
            }
        ]
        self.archived = []
        self.next_id = 1
        self.fail_append = False
        self.fail_cleanup_once = False

    def list_block_children(self, block_id):
        return [block for block in self.blocks if block["id"] not in self.archived]

    def find_brief_page(self, day):
        return BriefPageResult("page", "https://notion.test/page")

    def _create_brief_page(self, day):
        return BriefPageResult("page", "https://notion.test/page")

    def _append_blocks(self, page_id, blocks):
        if self.fail_append:
            raise RuntimeError("injected append failure")
        ids = []
        for block in blocks:
            value = {**block, "id": f"new-{self.next_id}"}
            self.next_id += 1
            rich = value[value["type"]]["rich_text"]
            for part in rich:
                if "plain_text" not in part:
                    part["plain_text"] = part.get("text", {}).get("content", "")
            self.blocks.append(value)
            ids.append(value["id"])
        return ids

    def _archive_block(self, block_id):
        if self.fail_cleanup_once and block_id.startswith("old"):
            self.fail_cleanup_once = False
            raise RuntimeError("injected cleanup failure")
        self.archived.append(block_id)


def test_append_failure_never_archives_old_complete_brief(tmp_path: Path) -> None:
    client = FakeBriefClient()
    client.fail_append = True
    with pytest.raises(RuntimeError, match="append"):
        client.upsert_brief_page(
            date(2026, 9, 2),
            "# New brief\n- Task",
            journal_dir=tmp_path,
            stored_page_id="page",
        )
    assert "old-1" not in client.archived
    assert client.list_block_children("page")[0]["id"] == "old-1"


def test_cleanup_failure_resumes_from_complete_generation(tmp_path: Path) -> None:
    client = FakeBriefClient()
    client.fail_cleanup_once = True
    with pytest.raises(RuntimeError, match="cleanup"):
        client.upsert_brief_page(
            date(2026, 9, 2),
            "# New brief\n- Task",
            journal_dir=tmp_path,
            stored_page_id="page",
        )
    assert any("Generation" in client._block_text(block) and "end" in client._block_text(block) for block in client.blocks)
    result = client.upsert_brief_page(
        date(2026, 9, 2),
        "# New brief\n- Task",
        journal_dir=tmp_path,
        stored_page_id="page",
    )
    assert result.page_id == "page"
    assert "old-1" in client.archived
    journal = json.loads((tmp_path / "2026-09-02.json").read_text(encoding="utf-8"))
    assert journal["phase"] == "complete"


def test_markdown_rich_text_chunks_do_not_exceed_notion_limit() -> None:
    blocks = NotionClient._markdown_blocks("- " + "x" * 4500)
    assert len(blocks) == 3
    assert all(len(block[block["type"]]["rich_text"][0]["text"]["content"]) <= 2000 for block in blocks)


def test_find_brief_reuses_oldest_and_warns_on_duplicates(monkeypatch) -> None:
    client = object.__new__(NotionClient)
    client.parent_page_id = "parent"
    children = [
        {"id": "new", "type": "child_page", "created_time": "2026-09-02", "child_page": {"title": "Brief — 2026-09-02"}},
        {"id": "old", "type": "child_page", "created_time": "2026-09-01", "child_page": {"title": "Brief — 2026-09-02"}},
    ]
    monkeypatch.setattr(client, "list_block_children", lambda _: children)
    result = client.find_brief_page(date(2026, 9, 2))
    assert result is not None and result.page_id == "old"
    assert result.warnings

