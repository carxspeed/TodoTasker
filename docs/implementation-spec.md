# Daily Brief implementation specification

This repository implements the daily-brief design supplied with the project. The source document is treated as a product specification; repository location and implementation workflow are controlled by the user’s live requests.

## Invariants

- Python performs collection, deterministic classification, rendering, delivery, and recovery.
- Nightly preparation makes at most one single-shot guidance call; morning delivery makes none.
- An evening reply batch makes at most one single-shot extraction call; an empty drain makes none.
- Model requests have no tools or agent loop.
- Evening check-in content is always processed by local Ollama. Morning guidance defaults to Ollama and may be configured for Anthropic.
- External timestamps become aware UTC datetimes at their input boundary; local date arithmetic uses the configured IANA timezone.
- State, caches, artifacts, and journals use atomic replacement and schema validation.
- Dry runs do not write files, mutate services, deliver messages, or append logs.
- Live credentials and captured personal data never enter Git.

## Sources and outputs

The system reads incomplete Canvas planner items plus missing submissions, enriches collected assignments from their detail pages and small linked DOCX/text-based PDF instructions, reads active rows from the Notion Work, School, Connections, and Misc databases, and reads a bounded iCalendar window. It normalizes those sources, assigns stable identities, and deterministically classifies work into Must, Smart, May, and Verify views. A bounded language-model request may add one-sentence guidance but cannot select, omit, reorder, rename, or re-estimate tasks.

The rendered brief is saved locally before delivery, then upserted into a system-owned Notion page and sent as a boundary-safe Telegram summary. The evening check-in polls Telegram, applies conservatively matched updates through a replayable journal, and records next-day capacity only when the user states it.

## Operational commands

- `canvas.py login|fetch`
- `setup_notion_db.py`
- `brief.py prepare|deliver|watchdog`
- `checkin.py send|process`
- `calendar_feed.py`
- `classify.py`

Full setup, live verification, recovery, and Windows Task Scheduler details are maintained in `docs/operations.md` as those commands are implemented.
