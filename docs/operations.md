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

## Commands

```powershell
# Safe fixture-only preview: no state, cache, log, Notion, or Telegram writes
venv\Scripts\python.exe brief.py prepare --fixture fixtures\sample_todo.json --target-date 2026-09-02 --dry-run

# Manual Canvas authentication; the user completes Microsoft SSO
venv\Scripts\python.exe canvas.py login

# Live commands after `.env` is complete and each connection is verified
venv\Scripts\python.exe checkin.py send --force
venv\Scripts\python.exe checkin.py process --force
venv\Scripts\python.exe brief.py prepare
venv\Scripts\python.exe brief.py deliver
venv\Scripts\python.exe brief.py watchdog
```

## Windows Task Scheduler

Five proposed tasks use the full project-local Python path and current repository path:

| Task | Trigger | Command |
|---|---:|---|
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
- To disable the system without deleting state, disable the five exact tasks in Task Scheduler.
