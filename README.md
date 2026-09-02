# Daily Brief

A local-first daily planning system that combines Canvas assignments, a Notion Work database, and an iCalendar feed. Python owns collection, deterministic classification, rendering, delivery, and recovery. The language model is used only for bounded guidance and local evening-note extraction.

## Safety defaults

- Secrets live only in `.env`.
- Browser profiles, state, cached source data, private fixtures, and check-in quarantine are ignored by Git.
- Evening check-ins always use local Ollama.
- `--dry-run` performs no writes or deliveries.
- Live services are never contacted by tests.

## Development

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest
```

Copy `.env.example` to `.env` only when you are ready to configure live services. See `docs/operations.md` for setup and scheduling.

