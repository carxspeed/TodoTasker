from types import SimpleNamespace

from brief import _canvas_is_authoritative


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
