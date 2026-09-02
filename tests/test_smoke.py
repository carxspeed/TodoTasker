from importlib import import_module


def test_package_imports() -> None:
    package = import_module("daily_brief")
    assert package.__version__ == "0.1.0"


def test_declared_runtime_dependencies_import() -> None:
    for name in (
        "bs4",
        "html2text",
        "icalendar",
        "jsonschema",
        "playwright",
        "pydantic",
        "dotenv",
        "rapidfuzz",
        "recurring_ical_events",
        "requests",
        "tzdata",
    ):
        import_module(name)

