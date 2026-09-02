"""Canvas REST adapter using a Playwright persistent browser context."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import html2text
from bs4 import BeautifulSoup

from .models import (
    Announcement,
    CanvasAssignment,
    CanvasEnvelope,
    CanvasEvent,
    CanvasPlanner,
    CanvasReminder,
    CanvasSourceStatus,
    PlannerEvent,
    PlannerObservation,
)
from .timeutils import parse_external_timestamp, utc_now


TASK_TYPES = {"assignment", "quiz", "discussion_topic", "sub_assignment"}
NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.IGNORECASE)
DATE_RE = re.compile(r"(?<![\d/])(?P<m>\d{1,2})/(?P<d>\d{1,2})(?![\d/])")
WEEKDAY = r"(?:M|T|W|Th|F|Sa|Su|Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
WEEKDAY_RE = re.compile(rf"\b{WEEKDAY}\b", re.IGNORECASE)
REJECT_DATE_CONTEXT = re.compile(r"score|points|pts|out of|fraction|read|chapter|problem", re.I)
PLANNER_TITLE_RE = re.compile(r"planner|week at a glance|agenda|schedule|calendar", re.I)
ASSESSMENT_RE = re.compile(r"\b(?:quiz|test|exam|assessment)\b", re.I)


class CanvasError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int = 1):
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def _response_json(response, *, expected: type, session_check: bool = False) -> Any:
    status = int(response.status)
    url = str(getattr(response, "url", ""))
    headers = {str(k).lower(): str(v) for k, v in dict(response.headers).items()}
    content_type = headers.get("content-type", "").lower()
    if status in (401, 403) or "login" in url.lower():
        raise CanvasError("SESSION_EXPIRED", "run canvas.py login", exit_code=2)
    if status == 429:
        raise CanvasError("RATE_LIMITED", "Canvas rate limit reached")
    if status >= 500:
        raise CanvasError("CANVAS_TEMPORARY_FAILURE", f"Canvas returned {status}")
    if status >= 400:
        raise CanvasError("CANVAS_API_ERROR", f"Canvas returned {status}")
    if "html" in content_type:
        raise CanvasError("SESSION_EXPIRED", "run canvas.py login", exit_code=2)
    try:
        body = response.json()
    except Exception as exc:
        if status == 200:
            raise CanvasError("SESSION_EXPIRED", "run canvas.py login", exit_code=2) from exc
        raise CanvasError("CANVAS_API_ERROR", "Canvas returned invalid JSON") from exc
    if not isinstance(body, expected):
        if session_check:
            raise CanvasError("CANVAS_API_ERROR", "user endpoint returned a non-user response")
        raise CanvasError("CANVAS_API_ERROR", f"expected {expected.__name__} response")
    if session_check and not body.get("id"):
        raise CanvasError("CANVAS_API_ERROR", "user endpoint returned a non-user response")
    return body


def verify_session(request, base_url: str) -> dict[str, Any]:
    response = request.get(f"{base_url.rstrip('/')}/api/v1/users/self", timeout=30_000)
    return _response_json(response, expected=dict, session_check=True)


def paginate(
    get: Callable[..., Any],
    url: str,
    params: dict[str, Any] | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Fetch every Canvas list page and follow the next URL verbatim."""

    initial_params = {**(params or {}), "per_page": 100}
    next_url: str | None = url
    first = True
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    while next_url:
        if next_url in seen:
            raise CanvasError("CANVAS_API_ERROR", "pagination repeated the same next URL")
        seen.add(next_url)
        response = None
        for attempt in range(1, 4):
            try:
                response = get(
                    next_url,
                    params=initial_params if first else None,
                    timeout=30_000,
                )
                page = _response_json(response, expected=list)
                break
            except CanvasError as exc:
                if exc.code not in {"RATE_LIMITED", "CANVAS_TEMPORARY_FAILURE"} or attempt == 3:
                    raise
                sleep(2 ** (attempt - 1))
            except Exception as exc:
                if attempt == 3:
                    raise CanvasError("CANVAS_TEMPORARY_FAILURE", "Canvas connection failed") from exc
                sleep(2 ** (attempt - 1))
        assert response is not None
        results.extend(item for item in page if isinstance(item, dict))
        link = dict(response.headers).get("link") or dict(response.headers).get("Link") or ""
        match = NEXT_LINK_RE.search(str(link))
        next_url = match.group(1) if match else None
        first = False
    return results


def load_fixture(path: str | Path) -> CanvasEnvelope:
    envelope = CanvasEnvelope.model_validate_json(Path(path).read_text(encoding="utf-8"))
    return envelope


def strip_html(value: str, limit: int) -> str:
    converter = html2text.HTML2Text()
    converter.ignore_links = True
    converter.ignore_images = True
    converter.body_width = 0
    text = re.sub(r"\s+", " ", converter.handle(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def stable_identity(item: dict[str, Any]) -> tuple[str, str, int | str, int | None]:
    kind = str(item.get("plannable_type") or "assignment")
    plannable = item.get("plannable") or item.get("assignment") or item
    object_id = item.get("plannable_id") or plannable.get("id")
    if object_id is None:
        raise ValueError("Canvas item has no object id")
    source_key = f"{kind}:{object_id}"
    if kind == "assignment":
        assignment_id = int(plannable.get("id") or object_id)
    else:
        backing = plannable.get("assignment_id")
        assignment_id = int(backing) if backing is not None else None
    key = f"assignment:{assignment_id}" if assignment_id is not None else source_key
    return key, source_key, object_id, assignment_id


def planner_item_is_complete(item: dict[str, Any]) -> bool:
    submissions = item.get("submissions")
    if not isinstance(submissions, dict):
        return False
    if submissions.get("submitted") is True:
        return True
    if "submitted" not in submissions and submissions.get("missing") is not True:
        return any(
            submissions.get(name) is True
            for name in ("graded", "with_feedback", "needs_grading", "excused")
        )
    return False


def todo_submission_complete(submission: dict[str, Any]) -> bool:
    return submission.get("excused") is True or submission.get("workflow_state") in {
        "submitted",
        "graded",
        "pending_review",
    }


def _aware_or_none(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_external_timestamp(str(value))
    except ValueError:
        return None


def normalize_task(
    item: dict[str, Any],
    *,
    submission_status: str = "unsubmitted",
    needs_confirmation: bool = False,
) -> CanvasAssignment:
    kind = str(item.get("plannable_type") or "assignment")
    plannable = item.get("plannable") or item.get("assignment") or item
    key, source_key, object_id, assignment_id = stable_identity(item)
    course_id = item.get("course_id") or plannable.get("course_id")
    if course_id is None:
        raise ValueError("Canvas item has no course id")
    name = plannable.get("title") or plannable.get("name") or item.get("name") or "Untitled assignment"
    return CanvasAssignment(
        key=key,
        source_key=source_key,
        object_id=object_id,
        assignment_id=assignment_id,
        course_id=int(course_id),
        name=str(name)[:500],
        course=str(item.get("context_name") or plannable.get("course_name") or ""),
        kind=kind,
        due_at=_aware_or_none(plannable.get("due_at") or item.get("due_at")),
        points=plannable.get("points_possible"),
        url=str(item.get("html_url") or plannable.get("html_url") or ""),
        description=strip_html(str(plannable.get("description") or ""), 400),
        submission_status=submission_status,
        needs_confirmation=needs_confirmation,
    )


@dataclass
class NormalizedAssignments:
    assignments: list[CanvasAssignment]
    events: list[CanvasEvent]
    reminders: list[CanvasReminder]
    warnings: list[str]


def normalize_assignment_sources(
    planner_items: list[dict[str, Any]],
    missing_items: list[dict[str, Any]],
    *,
    active_courses: dict[int, str],
    todo_items: list[dict[str, Any]] | None = None,
    todo_submissions: dict[int, dict[str, Any] | None] | None = None,
    timezone_name: str = "America/Los_Angeles",
) -> NormalizedAssignments:
    assignments: dict[str, CanvasAssignment] = {}
    events: list[CanvasEvent] = []
    reminders: list[CanvasReminder] = []
    warnings: list[str] = []
    timezone = ZoneInfo(timezone_name)

    for item in planner_items:
        kind = item.get("plannable_type")
        plannable = item.get("plannable") or {}
        if kind in TASK_TYPES:
            if planner_item_is_complete(item):
                continue
            try:
                normalized = normalize_task(item)
                assignments[normalized.key] = normalized
            except (ValueError, TypeError):
                warnings.append("Skipped malformed Canvas planner task")
        elif kind == "calendar_event" and plannable.get("start_at"):
            start = _aware_or_none(plannable.get("start_at"))
            if start is not None:
                events.append(
                    CanvasEvent(
                        title=str(plannable.get("title") or "Untitled Canvas event"),
                        start=start,
                        end=_aware_or_none(plannable.get("end_at")),
                        all_day=bool(plannable.get("all_day")),
                        busy=True,
                    )
                )
        elif kind == "planner_note":
            raw_date = plannable.get("todo_date") or item.get("plannable_date")
            try:
                note_date = date.fromisoformat(str(raw_date)[:10])
            except ValueError:
                continue
            reminders.append(
                CanvasReminder(
                    title=str(plannable.get("title") or "Canvas reminder"),
                    date=note_date,
                    text=strip_html(str(plannable.get("details") or ""), 400),
                )
            )

    for item in todo_items or []:
        raw_assignment = item.get("assignment") or item
        assignment_id = raw_assignment.get("id")
        submission = (todo_submissions or {}).get(int(assignment_id)) if assignment_id else None
        if isinstance(submission, dict) and todo_submission_complete(submission):
            continue
        unknown = not isinstance(submission, dict)
        try:
            normalized = normalize_task(
                item,
                submission_status="unknown" if unknown else "unsubmitted",
                needs_confirmation=unknown,
            )
            assignments[normalized.key] = normalized
            if unknown:
                warnings.append(f"Verify submission status for {normalized.name}")
        except (ValueError, TypeError):
            warnings.append("Skipped malformed Canvas /todo task")

    for raw in missing_items:
        course_id = raw.get("course_id")
        verified = course_id in active_courses and raw.get("locked_for_user") is not True
        wrapped = {
            "plannable_type": "assignment",
            "plannable_id": raw.get("id"),
            "plannable": raw,
            "course_id": course_id,
            "context_name": active_courses.get(course_id, ""),
            "html_url": raw.get("html_url", ""),
        }
        try:
            normalized = normalize_task(
                wrapped,
                submission_status="unsubmitted" if verified else "unknown",
                needs_confirmation=not verified,
            )
            existing = assignments.get(normalized.key)
            if existing is None or (existing.needs_confirmation and verified):
                assignments[normalized.key] = normalized
            if not verified:
                warnings.append(f"Verify old Canvas item {normalized.name}")
        except (ValueError, TypeError):
            warnings.append("Skipped malformed Canvas missing-submission task")

    return NormalizedAssignments(
        assignments=sorted(assignments.values(), key=lambda item: (item.due_at is None, item.due_at, item.key)),
        events=events,
        reminders=reminders,
        warnings=warnings,
    )


def _infer_date(month: int, day: int, target: date) -> date | None:
    candidates: list[date] = []
    for year in (target.year - 1, target.year, target.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            pass
    if not candidates:
        return None
    same_year = next((value for value in candidates if value.year == target.year), None)
    if same_year is not None and abs(month - target.month) <= 6:
        return same_year
    return min(candidates, key=lambda value: abs((value - target).days))


def _eligible_dates(text: str, *, target: date, leading: bool, header_date: bool) -> list[date]:
    results: list[date] = []
    for match in DATE_RE.finditer(text):
        context = text[max(0, match.start() - 12) : min(len(text), match.end() + 12)]
        if REJECT_DATE_CONTEXT.search(context):
            continue
        nearby = text[max(0, match.start() - 12) : min(len(text), match.end() + 12)]
        weekday_near = WEEKDAY_RE.search(nearby) is not None
        begins = leading and not text[: match.start()].strip()
        if not (begins or header_date or weekday_near):
            continue
        parsed = _infer_date(int(match.group("m")), int(match.group("d")), target)
        if parsed is not None and parsed not in results:
            results.append(parsed)
    return results


@dataclass
class PlannerParseResult:
    planner: CanvasPlanner | None
    events: list[PlannerEvent]
    observation: PlannerObservation


def window_planner_html(
    body: str,
    *,
    course_id: int,
    course: str,
    title: str,
    url: str,
    target_date: date,
    title_matched: bool,
) -> PlannerParseResult:
    soup = BeautifulSoup(body or "", "html.parser")
    units: list[tuple[str, list[date]]] = []
    total_dates = 0
    tables = soup.find_all("table")
    if tables:
        for table in tables:
            header_names: list[str] = []
            header_row = table.find("tr")
            if header_row:
                header_names = [re.sub(r"\s+", " ", cell.get_text(" ", strip=True)) for cell in header_row.find_all(["th", "td"])]
            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if not cells:
                    continue
                row_dates: list[date] = []
                for index, cell in enumerate(cells):
                    text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
                    header = header_names[index] if index < len(header_names) else ""
                    values = _eligible_dates(
                        text,
                        target=target_date,
                        leading=index < 2,
                        header_date=bool(re.search(r"date|day|mon|tue|wed|thu|fri|sat|sun", header, re.I)),
                    )
                    row_dates.extend(value for value in values if value not in row_dates)
                total_dates += len(row_dates)
                row_text = re.sub(r"\s+", " ", row.get_text(" ", strip=True))
                units.append((row_text, row_dates))
    else:
        blocks = soup.find_all(["p", "li", "div"])
        for block in blocks:
            if block.find_parent(["p", "li", "div"]) is not None:
                continue
            text = re.sub(r"\s+", " ", block.get_text(" ", strip=True))
            dates = _eligible_dates(text, target=target_date, leading=True, header_date=False)
            total_dates += len(dates)
            units.append((text, dates))

    start = target_date - timedelta(days=1)
    end = target_date + timedelta(days=7)
    kept = [(text, dates) for text, dates in units if any(start <= value <= end for value in dates)]
    all_dates: list[date] = []
    event_rows: list[PlannerEvent] = []
    for text, dates in kept:
        all_dates.extend(value for value in dates if value not in all_dates)
        if ASSESSMENT_RE.search(text):
            for value in dates:
                event_rows.append(
                    PlannerEvent(course=course, title=text[:200], date=value, text=text, url=url)
                )
    if kept:
        joined = "\n".join(text for text, _ in kept)
        if len(joined) > 1200:
            joined = joined[:1197].rstrip() + "..."
        planner = CanvasPlanner(
            course=course,
            title=title,
            url=url,
            dates=sorted(all_dates),
            text=joined,
        )
        status = "windowed"
    elif total_dates:
        planner = None
        status = "empty"
    elif title_matched:
        diagnostic = strip_html(body, 1000)
        planner = CanvasPlanner(course=course, title=title, url=url, dates=[], text=diagnostic) if diagnostic else None
        status = "unparsed"
    else:
        planner = None
        status = "unparsed"
    return PlannerParseResult(
        planner=planner,
        events=event_rows,
        observation=PlannerObservation(
            course_id=course_id,
            course=course,
            url=url,
            target_date=target_date,
            status=status,
        ),
    )


def _get_object(request, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = request.get(url, params=params, timeout=30_000)
    return _response_json(response, expected=dict)


def fetch_live(request, base_url: str, target_date: date, timezone_name: str) -> CanvasEnvelope:
    base = base_url.rstrip("/")
    verify_session(request, base)
    warnings: list[str] = []
    try:
        planner_items = paginate(
            request.get,
            f"{base}/api/v1/planner/items",
            {
                "start_date": target_date.isoformat(),
                "end_date": (target_date + timedelta(days=14)).isoformat(),
                "filter": "incomplete_items",
            },
        )
        planner_ok = True
        todo_items = None
        source = "planner_items+missing"
    except CanvasError:
        planner_ok = False
        planner_items = []
        try:
            todo_items = paginate(request.get, f"{base}/api/v1/users/self/todo")
        except CanvasError:
            todo_items = []
            warnings.append("Canvas /todo fallback also failed")
        source = "todo_fallback+missing"
        warnings.append("Canvas planner failed; /todo fallback was used")
    try:
        missing_items = paginate(request.get, f"{base}/api/v1/users/self/missing_submissions")
        missing_ok = True
    except CanvasError:
        missing_items = []
        missing_ok = False
        warnings.append("Canvas missing-submissions component failed")
    if not planner_ok and not todo_items and not missing_ok:
        raise CanvasError("CANVAS_TEMPORARY_FAILURE", "every assignment source failed")

    try:
        courses_raw = paginate(
            request.get,
            f"{base}/api/v1/courses",
            {"enrollment_state": "active", "enrollment_type": "student"},
        )
        active_courses = {
            int(course["id"]): str(course.get("name") or course.get("course_code") or "")
            for course in courses_raw
            if course.get("id") is not None
        }
        courses_ok = True
    except CanvasError:
        courses_ok = False
        observed = planner_items if planner_ok else (todo_items or [])
        active_courses = {
            int(item["course_id"]): str(item.get("context_name") or "")
            for item in observed
            if item.get("course_id") is not None
        }
        warnings.append("Active-course discovery failed; planner-only courses may be missing")

    todo_submissions: dict[int, dict[str, Any] | None] = {}
    for item in todo_items or []:
        assignment = item.get("assignment") or item
        assignment_id = assignment.get("id")
        course_id = item.get("course_id") or assignment.get("course_id")
        if assignment_id is None or course_id is None:
            continue
        try:
            todo_submissions[int(assignment_id)] = _get_object(
                request,
                f"{base}/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            )
        except CanvasError:
            todo_submissions[int(assignment_id)] = None

    normalized = normalize_assignment_sources(
        planner_items,
        missing_items,
        active_courses=active_courses,
        todo_items=todo_items,
        todo_submissions=todo_submissions,
        timezone_name=timezone_name,
    )
    warnings.extend(normalized.warnings)

    planners: list[CanvasPlanner] = []
    planner_events: list[PlannerEvent] = []
    observations: list[PlannerObservation] = []
    for course_id, course_name in active_courses.items():
        candidates: list[tuple[str, str, str, bool]] = []
        try:
            front = _get_object(request, f"{base}/api/v1/courses/{course_id}/front_page")
            candidates.append(
                (
                    str(front.get("title") or "Front page"),
                    str(front.get("body") or ""),
                    str(front.get("html_url") or f"{base}/courses/{course_id}"),
                    False,
                )
            )
        except CanvasError:
            pass
        try:
            modules = paginate(
                request.get,
                f"{base}/api/v1/courses/{course_id}/modules",
                {"include[]": "items"},
            )
            module_items: list[dict[str, Any]] = []
            for module in modules:
                inline = module.get("items")
                if isinstance(inline, list):
                    module_items.extend(inline)
                elif module.get("id") is not None:
                    module_items.extend(
                        paginate(
                            request.get,
                            f"{base}/api/v1/courses/{course_id}/modules/{module['id']}/items",
                        )
                    )
            for item in module_items:
                if item.get("type") != "Page" or not PLANNER_TITLE_RE.search(str(item.get("title", ""))):
                    continue
                slug = item.get("page_url")
                if not slug:
                    continue
                page = _get_object(request, f"{base}/api/v1/courses/{course_id}/pages/{slug}")
                candidates.append(
                    (
                        str(page.get("title") or item.get("title")),
                        str(page.get("body") or ""),
                        str(page.get("html_url") or item.get("html_url") or ""),
                        True,
                    )
                )
        except CanvasError:
            warnings.append(f"Planner discovery failed for {course_name}")
        for title, body, url, matched in candidates:
            parsed = window_planner_html(
                body,
                course_id=course_id,
                course=course_name,
                title=title,
                url=url,
                target_date=target_date,
                title_matched=matched,
            )
            observations.append(parsed.observation)
            if parsed.planner is not None and (parsed.planner.dates or matched):
                planners.append(parsed.planner)
            planner_events.extend(parsed.events)

    context_codes = [f"course_{course_id}" for course_id in active_courses]
    announcements: list[dict[str, Any]] = []
    announcement_status = "ok"
    if context_codes:
        try:
            announcements = paginate(
                request.get,
                f"{base}/api/v1/announcements",
                {
                    "context_codes[]": context_codes,
                    "start_date": (target_date - timedelta(days=7)).isoformat(),
                    "end_date": target_date.isoformat(),
                },
            )
        except CanvasError:
            announcement_status = "partial"
            for course_id in active_courses:
                try:
                    announcements.extend(
                        paginate(
                            request.get,
                            f"{base}/api/v1/announcements",
                            {
                                "context_codes[]": [f"course_{course_id}"],
                                "start_date": (target_date - timedelta(days=7)).isoformat(),
                                "end_date": target_date.isoformat(),
                            },
                        )
                    )
                except CanvasError:
                    pass
    normalized_announcements: list[Announcement] = []
    for raw in announcements:
        posted = _aware_or_none(raw.get("posted_at"))
        if posted is None:
            continue
        context = str(raw.get("context_code") or "")
        course_id = int(context.removeprefix("course_")) if context.removeprefix("course_").isdigit() else -1
        normalized_announcements.append(
            Announcement(
                course=active_courses.get(course_id, context),
                title=str(raw.get("title") or "Announcement"),
                text=strip_html(str(raw.get("message") or ""), 250),
                posted_at=posted,
            )
        )
    return CanvasEnvelope(
        fetched_at=utc_now(),
        source=source,
        assignments=normalized.assignments,
        canvas_events=normalized.events,
        canvas_reminders=normalized.reminders,
        planners=planners,
        planner_events=planner_events,
        planner_observations=observations,
        announcements=normalized_announcements,
        data_warnings=warnings,
        source_status=CanvasSourceStatus(
            planner_items="ok" if planner_ok else "failed",
            missing_submissions="ok" if missing_ok else "failed",
            courses="ok" if courses_ok else "failed",
            announcements=announcement_status,
        ),
    )
