"""Small, concrete task starts that remain useful when model wording is weak."""

from __future__ import annotations

import re

from .models import ClassifiedItem


_GENERIC_GUIDANCE = (
    "open the canvas assignment",
    "open the assignment",
    "open the instructions",
    "review its requirements",
    "review the requirements",
    "follow the listed requirements",
    "complete the first concrete part",
    "complete the first requirement",
    "start working",
    "and begin",
)


def is_generic_guidance(value: str) -> bool:
    """Return true when wording gives no assignment-specific place to start."""
    normalized = " ".join(value.casefold().split())
    return not normalized or any(phrase in normalized for phrase in _GENERIC_GUIDANCE)


def _finish(value: str, limit: int = 160) -> str:
    value = " ".join(value.split()).strip(" -:;,.\t\r\n")
    if not value:
        return ""
    value = value[0].upper() + value[1:]
    if len(value) >= limit:
        value = value[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return value if value.endswith((".", "!", "?")) else value + "."


def _short_title(item: ClassifiedItem) -> str:
    title = re.sub(r"^study for\s+", "", item.name.strip(), flags=re.IGNORECASE)
    return title[:72].rstrip(" -:;") or "this task"


def deterministic_guidance(item: ClassifiedItem) -> str:
    """Build one specific 5–15 minute action from trusted task facts."""
    if item.locked_for_user:
        return "Locked in Canvas—keep it visible and check again when it becomes available."
    if item.kind == "planner_assessment":
        return _finish(
            f"Review the topics for {_short_title(item)} for 15 minutes, then answer five practice questions"
        )
    if item.source == "notion":
        if not item.next_step.strip() or "unknown" in item.next_step.casefold():
            return "Next step unknown — spend 10 minutes scoping it."
        return _finish(item.next_step)
    if item.user_notes.strip():
        return _finish(f"Continue from your saved note: {item.user_notes.strip()}")

    title = _short_title(item)
    context = f"{item.name} {item.description}".casefold()

    if "webassign" in context:
        return "Open WebAssign and complete the first unsolved problem."
    if "ap classroom" in context:
        return "Open AP Classroom and complete the first unanswered question."
    if "spirit points" in context:
        return "Review the Spirit Points menu and choose one activity to complete this week."
    if "weekly work plan" in context or "work plan template" in context:
        return "Open the work-plan template and complete its first unfinished section."
    if any(word in context for word in ("quiz", "test", "mcq", "frq", "timed write")):
        return _finish(
            f"Review the topics for {title} for 15 minutes, then answer five practice questions"
        )
    if "lab" in context:
        return _finish(f"Read the {title} procedure and complete only the first setup step")
    if any(word in context for word in ("essay", "poem", "paragraph", "write ", "writing")):
        return _finish(f"Write one claim for {title}, then list three supporting points")
    if any(word in context for word in ("project", "presentation", "slides", "canva")):
        return _finish(f"Create the {title} file and list the first three required parts")
    if any(word in context for word in ("question", "annotation", "worksheet", "problem set")):
        return _finish(f"Open {title} and answer only the first unfinished question")
    if any(word in context for word in ("read ", "article", "chapter", "guided reading")):
        return _finish(
            f"Read the first section for {title} and write down one key point"
        )
    if any(word in context for word in ("video", "watch ")):
        return _finish(f"Watch the first 10 minutes for {title} and note one key idea")
    return _finish(f"Open {title} and complete only its first listed step")

