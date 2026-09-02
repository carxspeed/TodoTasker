# Operations

Live setup is intentionally deferred until fixture tests pass. Never paste secrets into source files or terminal commands that will be logged.

## Local setup

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
venv\Scripts\python.exe -m pytest
```

The `.env` file, Playwright profile, runtime state, caches, generated briefs, incident journals, private captures, and quarantined check-in replies are ignored by Git.

## Live-service prerequisites

1. Share the Notion “To Do List” parent page with the “Todo Agent” integration and place its 32-character page id and integration token in `.env`.
2. Create a Telegram bot, send it one message, then use `getUpdates` once to determine the chat id.
3. Add the Google Calendar secret iCal address to `.env`.
4. Run `venv\Scripts\python.exe canvas.py login`, complete Microsoft SSO manually, press Enter, and close the browser cleanly.

No live mutation should be attempted until the corresponding fixture and transport-mock tests pass.

