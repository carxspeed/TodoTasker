# Daily Brief Tasker

Daily Brief is a local-first Windows tasker that combines:

- assignments collected from Canvas;
- tasks kept in four Notion databases: Work, School, Connections, and Misc;
- events from a Google Calendar iCalendar feed;
- an evening check-in and morning brief delivered through Telegram.

Python handles collection, classification, rendering, delivery, retries, and recovery. Ollama handles the private evening-note extraction locally. The system stores its runtime state on your computer and is designed to avoid duplicate pages or messages when a command is retried.

This guide starts from a fresh Windows installation and ends with the optional scheduled tasks.

## 1. Requirements

Install or create the following before starting:

- Windows 11 and PowerShell;
- [Git for Windows](https://git-scm.com/download/win);
- Python 3.11 or newer;
- a Telegram account;
- a Notion account with permission to create an internal integration;
- a Google Calendar whose secret iCal address you can access;
- access to the Canvas site configured for this project;
- [Ollama for Windows](https://ollama.com/download/windows).

The examples below use PowerShell from the directory where you want to keep the project.

## 2. Download and install the project

Clone the repository and enter it:

```powershell
git clone https://github.com/carxspeed/TodoTasker.git
Set-Location TodoTasker
```

Create the project-local virtual environment:

```powershell
py -3.11 -m venv venv
```

If the Python launcher is unavailable, confirm that `python --version` reports 3.11 or newer, then run `python -m venv venv` instead.

Install the project and Playwright browser:

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m playwright install chromium
```

Always use `venv\Scripts\python.exe` for this project. This prevents commands from accidentally using a different global Python installation.

Run the complete offline test suite:

```powershell
venv\Scripts\python.exe -m pytest
```

All tests should pass before connecting live accounts. Tests and fixture previews do not contact live services.

## 3. Set up Ollama

Install Ollama, start it, and check which models are available:

```powershell
ollama list
```

The included `.env.example` uses `qwen3:4b`, a relatively small model suitable for broad hardware compatibility. Download it with `ollama pull qwen3:4b`, or choose another official model and set `OLLAMA_MODEL` in `.env` to the exact name shown by `ollama list`.

Ollama normally serves its local API at `http://localhost:11434`. Evening check-in interpretation always stays local. If the morning guidance model is unavailable, the brief still uses deterministic fallback guidance.

See the [official Ollama Windows documentation](https://docs.ollama.com/windows) for installation and service details.

## 4. Create the private configuration file

Copy the example file and open the copy:

```powershell
Copy-Item .env.example .env
notepad .env
```

Never commit, paste, screenshot, or share `.env`. It contains account credentials and private feed URLs. The repository already ignores it.

The main settings are:

| Setting | Purpose | Needed |
|---|---|---|
| `NOTION_TOKEN` | Secret for the Notion internal integration | Yes |
| `NOTION_PARENT_PAGE_ID` | ID of the Notion page that will contain the four task databases | Yes |
| `NOTION_WORK_DB_ID` | Work database ID, created automatically by `setup_notion_db.py` | Later |
| `NOTION_SCHOOL_DB_ID` | School database ID, created automatically | Later |
| `NOTION_CONNECTIONS_DB_ID` | Connections database ID, created automatically | Later |
| `NOTION_MISC_DB_ID` | Misc database ID, created automatically | Later |
| `TELEGRAM_BOT_TOKEN` | Token issued by BotFather | Yes |
| `TELEGRAM_CHAT_ID` | Your numeric Telegram conversation ID | Created automatically |
| `ICAL_URL` | Google Calendar secret iCal URL | Yes |
| `CANVAS_BASE` | Canvas base URL, without a trailing slash | Yes |
| `TIMEZONE` | IANA time zone used for scheduling | Yes |
| `OLLAMA_MODEL` | Exact local model name reported by `ollama list` | Yes |
| `OLLAMA_BASE_URL` | Ollama API address | Yes |
| `ANTHROPIC_API_KEY` | Optional remote model key for morning guidance | No |
| `ANTHROPIC_MODEL` | Optional Anthropic model name | No |

Keep the school-hours and pattern settings from `.env.example` unless you intentionally want to customize scheduling or calendar classification.

## 5. Create and connect the Telegram bot

Telegram bots cannot start a conversation with a user, so you must message the bot once before the tasker can discover your chat ID.

1. In Telegram, open the verified [@BotFather](https://t.me/BotFather) account.
2. Send `/newbot`.
3. Choose a display name.
4. Choose a unique username that ends in `bot`.
5. Copy the token BotFather returns into `.env`:

   ```dotenv
   TELEGRAM_BOT_TOKEN=replace_with_your_token
   TELEGRAM_CHAT_ID=
   ```

6. Open your new bot, press **Start**, and send it a message such as `hello`.
7. Save `.env`, then let the setup helper discover and store the chat ID:

   ```powershell
   venv\Scripts\python.exe setup_telegram.py
   ```

`TELEGRAM_CHAT_ID` is the numeric ID of the private conversation where the bot sends prompts and briefs. The script reads your latest bot update, checks for a conflicting webhook, and writes the ID to `.env`; you do not need to guess it.

If a bot token is ever exposed, revoke it with BotFather, generate a replacement, and update `.env`. The [official Telegram bot tutorial](https://core.telegram.org/bots/tutorial) explains token creation and bot setup.

## 6. Create and connect the Notion integration

1. Follow Notion's [internal integration quickstart](https://developers.notion.com/guides/get-started/quick-start) to create an integration named `Todo Agent` in your workspace.
2. Copy its internal integration secret into `.env` as `NOTION_TOKEN`.
3. In Notion, create or open a page named **To Do List**.
4. Open that page's connection/integration menu and add `Todo Agent`. Creating an integration does not automatically give it access to your pages.
5. Copy the page URL. Its page ID is the 32-character hexadecimal value in the URL; hyphens are accepted. Put it in `.env` as `NOTION_PARENT_PAGE_ID`.
6. Leave all four `NOTION_*_DB_ID` settings empty, save `.env`, and run:

   ```powershell
   venv\Scripts\python.exe setup_notion_db.py
   ```

The helper creates or validates four separate databases named **Work**, **School**, **Connections**, and **Misc** below the parent page, then writes each ID to its matching `.env` setting. Every database uses the same schema:

- `Name`;
- `Type`;
- `Cadence`;
- `Last touched`;
- `Status`;
- `Next step`;
- `Deadline`;
- `Effort`.

The table containing a task is its Area. Telegram check-ins route new tasks to the matching database and the daily brief reads active rows from all four. Canvas assignments are collected automatically, so only add school tasks to **School** when they are not represented in Canvas.

Add three to five real active items across the four databases so the first brief has useful data. Keep each row's `Name`, `Status`, and `Next step` current.

If Notion returns 404, check both the page ID and whether the parent page is connected to `Todo Agent`.

## 7. Connect Google Calendar

Use the calendar's private iCalendar address, not its normal browser URL:

1. In Google Calendar on a computer, open **Settings**.
2. Under **Settings for my calendars**, select the calendar.
3. Open **Integrate calendar**.
4. Copy **Secret address in iCal format**.
5. Paste it into `.env`:

   ```dotenv
   ICAL_URL=https://calendar.google.com/calendar/ical/your_private_feed/basic.ics
   ```

Google documents these steps under [View your calendar in other applications](https://support.google.com/calendar/answer/37648). Treat the secret address like a password. If it is exposed, reset it in Google Calendar and update `.env`.

## 8. Sign in to Canvas

Confirm the Canvas site in `.env`. This repository defaults to:

```dotenv
CANVAS_BASE=https://issaquah.instructure.com
```

To exclude assignments from a specific Canvas course, add its numeric course ID.
For example, a course URL ending in `/courses/46844` uses:

```dotenv
CANVAS_EXCLUDED_COURSE_IDS_JSON=[46844]
```

This filters assignments only; calendar events, planners, and announcements from
the course remain available.

Start the one-time interactive login:

```powershell
venv\Scripts\python.exe canvas.py login
```

Complete Microsoft SSO in the opened Chromium window. Return to PowerShell and press Enter when prompted, allow the script to finish saving the local profile, and close the browser cleanly.

The saved Playwright browser profile and `profile\storage-state.json` contain sensitive session data and are ignored by Git. Protect them like `.env`. If Canvas later reports an expired session, run the login command again. If login reports that the profile is already in use, close all Chromium windows opened by the tasker and retry.

## 9. Verify each read-only connection

These commands fetch and summarize data but do not create Notion pages or send Telegram messages:

```powershell
venv\Scripts\python.exe canvas.py fetch
venv\Scripts\python.exe notion_api.py
venv\Scripts\python.exe calendar_feed.py --target-date (Get-Date -Format 'yyyy-MM-dd')
```

Review the results for the expected Canvas assignments, Notion rows, and calendar events. Their output may contain personal information, so do not commit captures or logs.

## 10. Generate a zero-write fixture preview

Before the first live run, render tomorrow's brief from the included sample data:

```powershell
$TargetDate = (Get-Date).AddDays(1).ToString('yyyy-MM-dd')
venv\Scripts\python.exe brief.py prepare `
    --fixture fixtures\sample_todo.json `
    --target-date $TargetDate `
    --dry-run
```

`--fixture` avoids live source collection and `--dry-run` prevents state, cache, log, Notion, and Telegram writes.

## 11. Run the first live morning cycle

The following commands are intentionally live. `prepare` updates local state and creates or updates the day's Notion brief. `deliver` sends it through Telegram.

```powershell
$Today = Get-Date -Format 'yyyy-MM-dd'
venv\Scripts\python.exe brief.py prepare --target-date $Today
venv\Scripts\python.exe brief.py deliver --target-date $Today
```

Verify that:

- a generated Markdown brief exists under `state\briefs`;
- exactly one brief page exists in Notion for the date;
- one morning message arrived in Telegram;
- the Telegram button opens the expected Notion page.

Run the same two commands once more. The system should reuse the same logical brief rather than create duplicate pages or messages.

Test the evening check-in separately:

```powershell
venv\Scripts\python.exe checkin.py send --force
```

Reply to the Telegram prompt. For the first test, use the exact task title when referring to an item. Then process the reply:

```powershell
venv\Scripts\python.exe checkin.py process --force
```

Confirm the intended Notion row was updated. `--force` bypasses the normal catch-up window; it does not turn the command into a dry run.

## 12. Install the optional Windows scheduled tasks

Preview the proposed tasks first. This changes nothing:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-scheduled-tasks.ps1
```

The installer defines:

| Task | Trigger | Command |
|---|---:|---|
| Daily Brief - Evening Check-in | 9:00 PM | `checkin.py send` |
| Daily Brief - Process Check-in | 9:30 PM | `checkin.py process` |
| Daily Brief - Prepare | 9:50 PM | `brief.py prepare` |
| Daily Brief - Deliver | 6:30 AM | `brief.py deliver` |
| Daily Brief - Watchdog | 7:30 AM and logon | `brief.py watchdog` |

After reviewing the full paths and triggers printed by the preview, register the tasks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-scheduled-tasks.ps1 -Apply
```

The tasks run only for the logged-on user, wake the computer, start after a missed trigger when possible, and retry a nonzero exit three times at ten-minute intervals. Repository setup and tests never create scheduled tasks automatically.

The commands also enforce these catch-up windows:

| Command | Allowed local time |
|---|---|
| Evening prompt | 8:30 PM–10:30 PM |
| Check-in processing | 9:20 PM–2:00 AM |
| Brief preparation | 9:40 PM–3:00 AM |
| Brief delivery | 5:30 AM–12:00 PM |
| Watchdog | After 7:30 AM |

Watch the first three to five days of runs. Confirm that the evening prompt, reply processing, morning brief, and delivery each happen once.

## 13. Optional Anthropic morning guidance

Local Ollama is the default. To use Anthropic for bounded morning guidance, set:

```dotenv
MODEL_PROVIDER=anthropic
ANTHROPIC_API_KEY=replace_with_your_key
ANTHROPIC_MODEL=replace_with_a_model_available_to_your_account
```

Evening check-ins remain local regardless of this setting. If the remote model is unavailable, morning preparation uses deterministic fallback guidance.

## Common commands

```powershell
# Run tests
venv\Scripts\python.exe -m pytest

# Refresh the Canvas login
venv\Scripts\python.exe canvas.py login

# Fetch sources without producing a brief
venv\Scripts\python.exe canvas.py fetch
venv\Scripts\python.exe notion_api.py
venv\Scripts\python.exe calendar_feed.py

# Manual live workflow
venv\Scripts\python.exe checkin.py send --force
venv\Scripts\python.exe checkin.py process --force
venv\Scripts\python.exe brief.py prepare
venv\Scripts\python.exe brief.py deliver
venv\Scripts\python.exe brief.py watchdog
```

## Troubleshooting

### Canvas session expired

Run `venv\Scripts\python.exe canvas.py login` and complete SSO again. If the browser profile is in use, close the tasker's Chromium window before retrying.

### Telegram setup finds no chat

Open the bot in Telegram, press **Start**, send a new message, and rerun `setup_telegram.py`. If a webhook is configured on the same bot, remove the conflicting webhook or use a dedicated bot because polling and webhooks cannot consume updates simultaneously.

### Telegram token or calendar URL was exposed

Revoke and replace the bot token through BotFather. Reset the secret iCal address in Google Calendar. Update `.env` after rotating either credential.

### Notion returns 404

Verify `NOTION_PARENT_PAGE_ID` and all four `NOTION_*_DB_ID` settings, then confirm the relevant page/database is shared with the internal integration.

### Ollama is unavailable

Check that Ollama is running and that the configured model name exists:

```powershell
ollama list
Invoke-RestMethod http://localhost:11434/api/tags
```

Update `OLLAMA_MODEL` if the installed model has a different exact name.

### No morning delivery

Run `venv\Scripts\python.exe brief.py watchdog`, inspect the command output, and check `state\briefs` first. After two definite Telegram failures or a missing delivery at watchdog time, the app creates `BRIEF-DELIVERY-BROKEN.txt` in `INCIDENT_DIR` or on the Desktop and attempts a Windows notification.

## Recovery, safety, and privacy

- Generated Markdown briefs remain in `state\briefs` even if both remote deliveries fail.
- A corrupt `state\state.json` can restore from `state\state.json.bak`; if both are unusable, the app rebuilds safe defaults while preserving compatible configuration.
- Writes use target-specific idempotency so retries do not normally duplicate a Notion page or Telegram delivery.
- Check-in events are recorded append-first before downstream mutation.
- Ambiguous or malformed check-in replies go to local quarantine instead of updating the wrong item.
- Raw check-in text is not sent to the morning remote-model path.
- `.env`, runtime state, browser profiles, source caches, generated briefs, private captures, incidents, and quarantined replies are ignored by Git.
- `--dry-run` performs no writes or deliveries.

## Disable or remove scheduling

To pause the system without deleting local state, disable the five **Daily Brief** tasks in Windows Task Scheduler.

Before removing anything, list the exact matching tasks:

```powershell
Get-ScheduledTask |
    Where-Object TaskName -Like 'Daily Brief - *' |
    Select-Object TaskName, State
```

After verifying the names, remove those exact tasks in Task Scheduler or with `Unregister-ScheduledTask` one at a time. The repository's state and account credentials are not deleted when scheduled tasks are disabled or removed.

## Development

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest
```

See [`docs/operations.md`](docs/operations.md) for the shorter operator reference.
