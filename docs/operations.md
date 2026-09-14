# Operations

Live setup is intentionally deferred until fixture tests pass. Never paste secrets
into source files, chat, or terminal commands that will be logged.

## Local setup

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
venv\Scripts\python.exe -m pytest
```

Keep only non-secret settings in `.env`. Store secrets using hidden prompts:

```powershell
venv\Scripts\python.exe manage_secrets.py set TELEGRAM_BOT_TOKEN
venv\Scripts\python.exe manage_secrets.py set NOTION_TOKEN
venv\Scripts\python.exe manage_secrets.py set ICAL_URL
```

Windows DPAPI encrypts the vault for the current Windows user under
`%LOCALAPPDATA%\TodoTasker`, outside the repository. Runtime state, caches, generated
briefs, incident journals, private captures, and quarantined check-in replies are
ignored by Git.

When a terminal cannot accept hidden input, run `manage_secrets.py web-setup` and
open its one-time `127.0.0.1` URL. The loopback-only, no-cache server shuts down
after one save or ten minutes.

## Live-service prerequisites

1. Share the Notion “To Do List” parent page with the “Todo Agent” integration, place its 32-character page id in `.env`, store its integration secret with `manage_secrets.py set NOTION_TOKEN`, then run `setup_notion_db.py` to create or validate the Work, School, Connections, and Misc databases.
2. Create a Telegram bot, send it one message, then use `getUpdates` once to determine the chat id.
3. Store the Google Calendar secret iCal address with `manage_secrets.py set ICAL_URL`.
4. Prefer a Canvas personal access token stored with `manage_secrets.py set CANVAS_ACCESS_TOKEN`. If no token is available, run `canvas.py login` once to create the encrypted fallback session.

The Canvas token is restricted to the configured Canvas HTTPS origin. The fallback
browser state is DPAPI-encrypted under `%LOCALAPPDATA%\TodoTasker` and decrypted only
in memory; no persistent Chromium profile or plaintext storage state is retained.
Run `canvas.py auth-check` to verify unattended access.

No live mutation should be attempted until the corresponding fixture and transport-mock tests pass.

## Commands

```powershell
# Safe fixture-only preview: no state, cache, log, Notion, or Telegram writes
venv\Scripts\python.exe brief.py prepare --fixture fixtures\sample_todo.json --target-date 2026-09-02 --dry-run

# Safe status checks; neither command prints credential values
venv\Scripts\python.exe manage_secrets.py status
venv\Scripts\python.exe manage_secrets.py audit
venv\Scripts\python.exe canvas.py auth-check

# Refresh the encrypted Canvas fallback session; the user completes Microsoft SSO
venv\Scripts\python.exe canvas.py login

# After TELEGRAM_BOT_TOKEN is in the vault and you have messaged the bot once
venv\Scripts\python.exe setup_telegram.py

# Live commands after `.env` is complete and each connection is verified
venv\Scripts\python.exe checkin.py send --force
venv\Scripts\python.exe checkin.py process --force
venv\Scripts\python.exe brief.py prepare
venv\Scripts\python.exe brief.py deliver
venv\Scripts\python.exe brief.py watchdog
```

## Windows Task Scheduler

Six proposed tasks use the full project-local Python path and current repository path:

| Task | Trigger | Command |
|---|---:|---|
| Daily Brief - Canvas Auth Check | 20:30 | `venv\Scripts\python.exe canvas.py auth-check --notify` |
| Daily Brief - Evening Check-in | 21:00 | `venv\Scripts\python.exe checkin.py send` |
| Daily Brief - Process Check-in | 21:30 | `venv\Scripts\python.exe checkin.py process` |
| Daily Brief - Prepare | 21:50 | `venv\Scripts\python.exe brief.py prepare` |
| Daily Brief - Deliver | 06:30 | `venv\Scripts\python.exe brief.py deliver` |
| Daily Brief - Watchdog | 07:30 and logon | `venv\Scripts\python.exe brief.py watchdog` |

Every task is interactive-user only, starts as soon as possible after a missed trigger, wakes the computer, and retries a nonzero exit three times at ten-minute intervals. The commands enforce their own catch-up windows before locking, so a morning wake cannot send an old evening prompt. Exit code 75 means a healthy owner still holds the shared lock and is retryable; `skipped_stale` exits zero.

Preview the exact local task definitions without changing Task Scheduler:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-scheduled-tasks.ps1
```

Only after reviewing and approving those definitions, register them with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-scheduled-tasks.ps1 -Apply
```

No scheduled tasks are created by repository setup or tests.

## Recovery and removal

- Generated Markdown briefs remain in `state\briefs` even when both remote deliveries fail.
- A corrupt primary state restores `state\state.json.bak`; when both fail, the app rebuilds defaults and preserves the configured Notion database id.
- Two definite Telegram failures or a missing delivery at watchdog time create `BRIEF-DELIVERY-BROKEN.txt` in the configured incident directory (or Desktop) and attempt a Windows message.
- Remove all tasks with `Unregister-ScheduledTask -TaskName 'Daily Brief - *' -Confirm:$false` only after listing and verifying the exact matching task names.
- To disable the system without deleting state, disable the six exact tasks in Task Scheduler.
