"""Run an approved TodoTasker entry point without creating a console window."""

from __future__ import annotations

import os
import runpy
import sys
import traceback
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ENTRYPOINTS = frozenset({"brief.py", "canvas.py", "checkin.py"})
MAX_LOG_BYTES = 1_000_000


def _exit_code(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return 1


def _log_path(entrypoint: str, arguments: list[str]) -> Path:
    command = arguments[0] if arguments else "run"
    safe_command = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in command
    ).strip("-") or "run"
    return PROJECT_ROOT / "state" / "scheduled-logs" / (
        f"{Path(entrypoint).stem}-{safe_command}.log"
    )


def _rotate_log(path: Path) -> None:
    if not path.exists() or path.stat().st_size <= MAX_LOG_BYTES:
        return
    backup = path.with_suffix(path.suffix + ".previous")
    backup.unlink(missing_ok=True)
    path.replace(backup)


def run(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not arguments or arguments[0] not in ALLOWED_ENTRYPOINTS:
        return 2

    entrypoint_name = arguments.pop(0)
    entrypoint = PROJECT_ROOT / entrypoint_name
    if not entrypoint.is_file():
        return 2

    log_path = _log_path(entrypoint_name, arguments)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(log_path)

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    original_argv = sys.argv
    original_path = list(sys.path)
    original_directory = Path.cwd()
    exit_code = 0
    with log_path.open("a", encoding="utf-8", buffering=1) as stream:
        try:
            sys.stdout = stream
            sys.stderr = stream
            sys.argv = [str(entrypoint), *arguments]
            if str(PROJECT_ROOT) not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))
            os.chdir(PROJECT_ROOT)
            print(
                f"{datetime.now().astimezone().isoformat()} | start | "
                f"{entrypoint_name} {' '.join(arguments)}"
            )
            try:
                runpy.run_path(str(entrypoint), run_name="__main__")
            except SystemExit as exc:
                exit_code = _exit_code(exc.code)
            except BaseException:
                traceback.print_exc()
                exit_code = 1
            print(f"{datetime.now().astimezone().isoformat()} | exit | {exit_code}")
        finally:
            os.chdir(original_directory)
            sys.argv = original_argv
            sys.path[:] = original_path
            sys.stdout = original_stdout
            sys.stderr = original_stderr
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
