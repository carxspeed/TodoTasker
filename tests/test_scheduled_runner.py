import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType


def load_runner() -> ModuleType:
    path = Path("scripts/run-scheduled.pyw").resolve()
    module = ModuleType("todo_tasker_scheduled_runner")
    module.__file__ = str(path)
    SourceFileLoader(module.__name__, str(path)).exec_module(module)
    return module


def test_hidden_runner_redirects_output_and_preserves_exit_code(tmp_path: Path) -> None:
    runner = load_runner()
    runner.PROJECT_ROOT = tmp_path
    (tmp_path / "brief.py").write_text(
        (
            "import helper_module\n"
            "print(f'scheduled output: {helper_module.VALUE}')\n"
            "raise SystemExit(7)\n"
        ),
        encoding="utf-8",
    )
    (tmp_path / "helper_module.py").write_text("VALUE = 'imported'\n", encoding="utf-8")
    original_path = list(sys.path)

    assert runner.run(["brief.py", "prepare"]) == 7
    assert sys.path == original_path

    log = tmp_path / "state" / "scheduled-logs" / "brief-prepare.log"
    text = log.read_text(encoding="utf-8")
    assert "brief.py prepare" in text
    assert "scheduled output: imported" in text
    assert "| exit | 7" in text


def test_hidden_runner_rejects_unapproved_entrypoints(tmp_path: Path) -> None:
    runner = load_runner()
    runner.PROJECT_ROOT = tmp_path
    (tmp_path / "other.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    assert runner.run(["other.py"]) == 2
    assert not (tmp_path / "state").exists()
