from pathlib import Path

from daily_brief.models import DailyBriefState
from daily_brief.state import StateStore


def test_state_save_preserves_last_valid_backup(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    first = DailyBriefState(consecutive_telegram_failures=1)
    store.save(first)
    second = DailyBriefState(consecutive_telegram_failures=2)
    store.save(second)
    assert DailyBriefState.model_validate_json(store.backup.read_text()).consecutive_telegram_failures == 1
    assert DailyBriefState.model_validate_json(store.primary.read_text()).consecutive_telegram_failures == 2


def test_truncated_primary_restores_backup(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.save(DailyBriefState(consecutive_telegram_failures=1))
    store.save(DailyBriefState(consecutive_telegram_failures=2))
    store.primary.write_text('{"schema_version":', encoding="utf-8")
    recovered, source = store.load()
    assert source == "backup"
    assert recovered.consecutive_telegram_failures == 1
    assert DailyBriefState.model_validate_json(store.primary.read_text()).consecutive_telegram_failures == 1
    assert list(store.state_dir.glob("state.json.corrupt.*"))


def test_both_invalid_rebuilds_with_canonical_work_db_id(tmp_path: Path) -> None:
    work_id = "0123456789abcdef0123456789abcdef"
    store = StateStore(tmp_path / "state", fallback_work_db_id=work_id)
    store.state_dir.mkdir()
    store.primary.write_text("bad", encoding="utf-8")
    store.backup.write_text("bad", encoding="utf-8")
    recovered, source = store.load()
    assert source == "defaults"
    assert recovered.work_db_id == work_id
    assert recovered.update_id_offset is None
    assert recovered.warnings

