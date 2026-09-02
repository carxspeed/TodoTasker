import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from daily_brief.models import CalendarSnapshot
from daily_brief.runtime import DeferredHealthyLock, HeartbeatLock, SourceCache, resolve_incident_dir


def test_healthy_lock_defers_second_owner(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    first = HeartbeatLock(path, max_wait=0).acquire()
    try:
        with pytest.raises(DeferredHealthyLock):
            HeartbeatLock(path, max_wait=0).acquire()
    finally:
        first.release()
    assert not path.exists()


def test_stale_dead_lock_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "token": "old",
                "started_at": "2020-01-01T00:00:00Z",
                "heartbeat_at": "2020-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    lock = HeartbeatLock(path, max_wait=0, pid_alive=lambda _: False).acquire()
    try:
        assert json.loads(path.read_text(encoding="utf-8"))["token"] == lock.token
    finally:
        lock.release()


def test_target_specific_cache_rejects_yesterday(tmp_path: Path) -> None:
    cache = SourceCache(tmp_path)
    snapshot = CalendarSnapshot(
        target_date=date(2026, 9, 1),
        fetched_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    cache.save("calendar", snapshot, target_date=date(2026, 9, 1))
    assert cache.load(
        "calendar",
        CalendarSnapshot,
        target_date=date(2026, 9, 2),
        require_target_match=True,
    ) is None


def test_incident_override_is_resolved_and_created(tmp_path: Path) -> None:
    target = tmp_path / "incidents"
    assert resolve_incident_dir(target) == target.resolve()
    assert target.is_dir()

