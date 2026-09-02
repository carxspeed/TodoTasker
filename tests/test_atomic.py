from pathlib import Path

import pytest

from daily_brief.atomic import atomic_write, atomic_write_json


def test_atomic_write_replaces_complete_destination(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    destination.write_bytes(b"old")
    atomic_write(destination, b"new")
    assert destination.read_bytes() == b"new"
    assert list(tmp_path.glob("*.tmp")) == []


def test_interrupted_replace_leaves_old_destination(monkeypatch, tmp_path: Path) -> None:
    destination = tmp_path / "prepared.json"
    destination.write_bytes(b"complete-old")

    def fail_replace(source, target):
        raise OSError("simulated interruption")

    monkeypatch.setattr("daily_brief.atomic.os.replace", fail_replace)
    with pytest.raises(OSError, match="interruption"):
        atomic_write(destination, b"partial-new")
    assert destination.read_bytes() == b"complete-old"
    assert not list(tmp_path.glob("*.tmp"))


def test_json_is_normalized_and_newline_terminated(tmp_path: Path) -> None:
    destination = tmp_path / "cache.json"
    atomic_write_json(destination, {"z": 1, "a": 2})
    assert destination.read_text(encoding="utf-8") == '{"a":2,"z":1}\n'

