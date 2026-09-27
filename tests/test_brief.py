from types import SimpleNamespace
from datetime import date, datetime, timezone

from brief import _canvas_is_authoritative, _runtime_status_lines
from daily_brief.models import (
    ClassificationOutput,
    GuidanceDiagnostics,
    PreparedArtifact,
    PreparedSources,
)


def _canvas_status(*, planner="ok", missing="ok", courses="ok"):
    return SimpleNamespace(
        source_status=SimpleNamespace(
            planner_items=planner,
            missing_submissions=missing,
            courses=courses,
        )
    )


def test_canvas_is_authoritative_when_all_assignment_sources_succeed() -> None:
    assert _canvas_is_authoritative(_canvas_status()) is True


def test_canvas_is_not_authoritative_when_any_assignment_source_fails() -> None:
    assert _canvas_is_authoritative(_canvas_status(planner="failed")) is False
    assert _canvas_is_authoritative(_canvas_status(missing="failed")) is False
    assert _canvas_is_authoritative(_canvas_status(courses="failed")) is False


def test_runtime_status_reports_ai_and_sources_without_task_text(tmp_path) -> None:
    target = date(2026, 9, 26)
    artifact = PreparedArtifact(
        target_date=target,
        classification_as_of=datetime(2026, 9, 26, 6, 30, tzinfo=timezone.utc),
        prepared_at=datetime(2026, 9, 26, 6, 31, tzinfo=timezone.utc),
        rendered_brief="private assignment title",
        guidance_diagnostics=GuidanceDiagnostics(
            source="local",
            model="qwen36:latest",
            status="success",
            failure_code="anthropic_unavailable",
            attempts=2,
        ),
        classification=ClassificationOutput(
            target_date=target,
            as_of=datetime(2026, 9, 26, 6, 30, tzinfo=timezone.utc),
        ),
        sources=PreparedSources(
            statuses={"canvas": "live", "notion": "live", "calendar": "cached"}
        ),
        classification_input_hash="a" * 64,
    )
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    (prepared / "2026-09-26.json").write_text(
        artifact.model_dump_json(), encoding="utf-8"
    )

    text = "\n".join(_runtime_status_lines(target, tmp_path))

    assert "ai_source=local" in text
    assert "ai_model=qwen36:latest" in text
    assert "ai_failure=anthropic_unavailable" in text
    assert "private assignment title" not in text


def test_runtime_status_handles_missing_plan(tmp_path) -> None:
    assert _runtime_status_lines(date(2026, 9, 26), tmp_path) == [
        "target=2026-09-26",
        "prepared=false",
    ]
