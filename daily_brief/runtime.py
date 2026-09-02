"""Shared lock, source cache, hashing, and incident helpers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import ctypes
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from .atomic import atomic_write_json, atomic_write_text
from .timeutils import parse_external_timestamp, utc_now


class DeferredHealthyLock(RuntimeError):
    exit_code = 75


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class HeartbeatLock:
    def __init__(
        self,
        path: str | Path = "state/run.lock",
        *,
        stale_after: timedelta = timedelta(minutes=30),
        max_wait: float = 30 * 60,
        poll_interval: float = 2,
        heartbeat_interval: float = 60,
        pid_alive=_pid_alive,
    ) -> None:
        self.path = Path(path)
        self.stale_after = stale_after
        self.max_wait = max_wait
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.pid_alive = pid_alive
        self.token = uuid4().hex
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _payload(self) -> dict[str, Any]:
        now = utc_now().isoformat()
        return {"pid": os.getpid(), "token": self.token, "started_at": now, "heartbeat_at": now}

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _healthy(self, owner: dict[str, Any]) -> bool:
        try:
            heartbeat = parse_external_timestamp(owner["heartbeat_at"])
            recent = utc_now() - heartbeat <= self.stale_after
            return recent or self.pid_alive(int(owner.get("pid", 0)))
        except (KeyError, TypeError, ValueError):
            return False

    def acquire(self) -> "HeartbeatLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.max_wait
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._payload(), handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                self._thread.start()
                return self
            except FileExistsError:
                owner = self._read()
                if not self._healthy(owner):
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise DeferredHealthyLock(
                        f"DEFERRED_HEALTHY_LOCK owner_pid={owner.get('pid', 'unknown')}"
                    )
                time.sleep(self.poll_interval)

    def heartbeat(self) -> None:
        owner = self._read()
        if owner.get("token") != self.token:
            return
        owner["heartbeat_at"] = utc_now().isoformat()
        atomic_write_json(self.path, owner)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            self.heartbeat()

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        owner = self._read()
        if owner.get("token") == self.token:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> "HeartbeatLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


T = TypeVar("T", bound=BaseModel)


class SourceCache:
    def __init__(self, directory: str | Path = "state/cache") -> None:
        self.directory = Path(directory)

    def save(self, source: str, model: BaseModel, *, target_date: date | None = None) -> None:
        atomic_write_json(
            self.directory / f"{source}.json",
            {
                "schema_version": 1,
                "source": source,
                "cached_at": utc_now().isoformat(),
                "target_date": target_date.isoformat() if target_date else None,
                "data": model.model_dump(mode="json"),
            },
        )

    def load(
        self,
        source: str,
        model_type: type[T],
        *,
        target_date: date | None = None,
        require_target_match: bool = False,
    ) -> tuple[T, datetime] | None:
        path = self.directory / f"{source}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cached_target = raw.get("target_date")
            if require_target_match and cached_target != (target_date.isoformat() if target_date else None):
                return None
            return model_type.model_validate(raw["data"]), parse_external_timestamp(raw["cached_at"])
        except (OSError, ValueError, KeyError):
            return None


def normalized_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_incident_dir(override: Path | None) -> Path:
    if override is not None:
        resolved = override.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
    desktop = None
    if os.name == "nt":
        # FOLDERID_Desktop; this respects redirected/localized Desktop locations.
        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        folder_id = GUID.from_buffer_copy(
            uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641").bytes_le
        )
        output = ctypes.c_wchar_p()
        try:
            result = ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id), 0, None, ctypes.byref(output)
            )
            if result == 0 and output.value:
                desktop = Path(output.value)
        except (AttributeError, OSError):
            desktop = None
        finally:
            if output.value:
                try:
                    ctypes.windll.ole32.CoTaskMemFree(output)
                except (AttributeError, OSError):
                    pass
    if desktop is None:
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    return desktop


def alert_incident(directory: Path, text: str) -> tuple[bool, bool]:
    file_ok = False
    message_ok = False
    try:
        atomic_write_text(directory / "BRIEF-DELIVERY-BROKEN.txt", text.rstrip() + "\n")
        file_ok = True
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["msg.exe", "*", "Daily Brief delivery needs attention"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        message_ok = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return file_ok, message_ok
