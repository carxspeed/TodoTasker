# Daily Brief Tasker

Daily Brief is a local-first Windows tasker that combines:

- assignments collected from Canvas;
- one unified Notion Tasks database with phone-friendly Work, School, Connections, Misc, and Today views;
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

Ollama normally serves its local API at `http://localhost:11434`. TodoTasker starts the locally installed Ollama service automatically when a run finds it stopped, then waits briefly before preparing guidance. Evening check-in interpretation always stays local. If the configured model is unavailable, the brief still uses deterministic fallback guidance.

See the [official Ollama Windows documentation](https://docs.ollama.com/windows) for installation and service details.

## 4. Create the configuration file and encrypted secret vault

Copy the example file and open the copy:

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` contains only non-secret settings and blank placeholders. TodoTasker refuses to
start if a secret value is left there. Store each secret through a hidden prompt in
the Windows user-scoped encrypted vault instead:

```powershell
venv\Scripts\python.exe manage_secrets.py set TELEGRAM_BOT_TOKEN
venv\Scripts\python.exe manage_secrets.py set NOTION_TOKEN
venv\Scripts\python.exe manage_secrets.py set ICAL_URL
```

The prompt does not echo the value. Windows DPAPI encrypts the vault so it can be
decrypted only by this Windows user, and its permissions allow only this user and
LocalSystem. The ciphertext is stored under the repository's Git-ignored
`.private` directory so interactive commands and Windows Task Scheduler use the same
persistent file view. Never paste a secret into chat or place it directly on a
command line.

If your terminal cannot accept hidden input, start the one-time local setup page:

```powershell
venv\Scripts\python.exe manage_secrets.py web-setup
```

Open the printed `127.0.0.1` URL. The server binds only to loopback, uses an
unguessable one-time path, disables caching and logging, and shuts down after saving
or ten minutes. Fields are masked, and blank fields keep their existing values.

The main settings are:

| Setting | Purpose | Needed |
|---|---|---|
| `NOTION_TOKEN` | Secret for the Notion internal integration; store in the encrypted vault | Yes |
| `NOTION_PARENT_PAGE_ID` | ID of the Notion page that contains the task dashboard | Yes |
| `NOTION_SCHOOL_PAGE_ID` | School page ID, created automatically by `setup_notion_db.py` | Later |
| `NOTION_WORK_DB_ID` | Work database ID, created automatically by `setup_notion_db.py` | Later |
| `NOTION_SCHOOL_DB_ID` | General School table ID, created automatically | Later |
| `NOTION_CONNECTIONS_DB_ID` | Connections database ID, created automatically | Later |
| `NOTION_MISC_DB_ID` | Misc database ID, created automatically | Later |
| `TELEGRAM_BOT_TOKEN` | Token issued by BotFather; store in the encrypted vault | Yes |
| `TELEGRAM_CHAT_ID` | Your numeric Telegram conversation ID | Created automatically |
| `ICAL_URL` | Google Calendar secret iCal URL; store in the encrypted vault | Yes |
| `CANVAS_BASE` | Canvas base URL, without a trailing slash | Yes |
| `MICROSOFT_EMAIL` | School Microsoft email; stored only in the encrypted vault | Yes for unattended Canvas renewal |
| `MICROSOFT_PASSWORD` | School Microsoft password; stored only in the encrypted vault | Yes for unattended Canvas renewal |
| `CANVAS_ACCESS_TOKEN` | Optional Canvas credential, if the school permits it | No |
| `TIMEZONE` | IANA time zone used for scheduling | Yes |
| `OLLAMA_MODEL` | Exact local model name reported by `ollama list` | Yes |
| `OLLAMA_BASE_URL` | Ollama API address | Yes |
| `ANTHROPIC_API_KEY` | Optional remote model key; store in the encrypted vault | No |
| `ANTHROPIC_MODEL` | Optional Anthropic model name | No |

Keep the school-hours and pattern settings from `.env.example` unless you intentionally want to customize scheduling or calendar classification.

## 5. Create and connect the Telegram bot

Telegram bots cannot start a conversation with a user, so you must message the bot once before the tasker can discover your chat ID.

1. In Telegram, open the verified [@BotFather](https://t.me/BotFather) account.
2. Send `/newbot`.
3. Choose a display name.
4. Choose a unique username that ends in `bot`.
5. Store the token BotFather returns through the hidden local prompt:

   ```powershell
   venv\Scripts\python.exe manage_secrets.py set TELEGRAM_BOT_TOKEN
   ```

6. Open your new bot, press **Start**, and send it a message such as `hello`.
7. Let the setup helper discover and store the non-secret chat ID in `.env`:

   ```powershell
   venv\Scripts\python.exe setup_telegram.py
   ```

`TELEGRAM_CHAT_ID` is the numeric ID of the private conversation where the bot sends prompts and briefs. The script reads your latest bot update, checks for a conflicting webhook, and writes the ID to `.env`; you do not need to guess it.

If a bot token is ever exposed, revoke it with BotFather, generate a replacement,
and rerun the `manage_secrets.py set TELEGRAM_BOT_TOKEN` command. The [official
Telegram bot tutorial](https://core.telegram.org/bots/tutorial) explains token
creation and bot setup.

## 6. Create and connect the Notion integration

1. Follow Notion's [internal integration quickstart](https://developers.notion.com/guides/get-started/quick-start) to create an integration named `Todo Agent` in your workspace.
2. Store its internal integration secret with `venv\Scripts\python.exe manage_secrets.py set NOTION_TOKEN`.
3. In Notion, create or open a page named **To Do List**.
4. Open that page's connection/integration menu and add `Todo Agent`. Creating an integration does not automatically give it access to your pages.
5. Copy the page URL. Its page ID is the 32-character hexadecimal value in the URL; hyphens are accepted. Put it in `.env` as `NOTION_PARENT_PAGE_ID`.
6. Leave `NOTION_SCHOOL_PAGE_ID` and all four `NOTION_*_DB_ID` settings empty, save `.env`, and run:

   ```powershell
   venv\Scripts\python.exe setup_notion_db.py
   ```

The helper creates or validates the legacy **Work**, **Connections**, and **Misc** databases below the parent page. It also creates a **School** page containing a **General** table. These databases are safe migration inputs for existing installations.

Work, Connections, Misc, and School / General use this schema:

- `Name`;
- `Type`;
- `Cadence`;
- `Last touched`;
- `Status`;
- `Next step`;
- `Deadline`;
- `Effort`.

After setup, preview the consolidation into the unified **Tasks** database:

```powershell
venv\Scripts\python.exe brief.py migrate-notion --dry-run
```

Review the Canvas, Notion, unique-row, and duplicate counts. If `duplicates=none`, apply it:

```powershell
venv\Scripts\python.exe brief.py migrate-notion --apply
```

The migration is idempotent and leaves all legacy databases unchanged as a rollback copy. It creates one canonical row per task and preserves completion state and `Notes / progress` where available. After **Tasks** exists, both scheduled and manual runs use it as the source of truth.

The Tasks database contains the task name, area, course, due time, priority, effort, next step, a user-editable `Notes / progress` field, a short instruction summary, and the source link. Full Canvas descriptions and extracted attachment text stay in the run's bounded internal context instead of flooding the visible Notion page. Source IDs and sync hashes remain available as bookkeeping fields in **All Tasks**. Repeated runs update matching rows instead of duplicating them and never overwrite `Notes / progress`. Check `Done` for an in-person submission; future briefs will omit that assignment. Notes such as “finished the first half” are supplied to the guidance model on the next run. The excluded DECA course is never synchronized.

Use the database views instead of scanning one huge table:

- **Today** is a compact phone-friendly list of at most three tasks. It shows the primary action first and links each item to its complete task row.
- **School** groups assignments by course on the same page and sorts each course by due date.
- **Work**, **Connections**, and **Misc** show only unfinished tasks in that area, sorted by due date.
- **All Tasks** is the full administrative table, including the normally hidden sync fields.

Telegram uses the same three-task focus as **Today** and reports how many selected tasks remain in the backlog. Assessments found in assignments or class planners—such as quizzes, tests, MCQs, FRQs, and timed writes—receive an explicit study action. Checking `Done` in any view updates the canonical row and removes it from later briefs.

For a manual task, create a row in **Tasks** and choose its `Area`. For school work, also set `Course`; it will automatically appear under that class in the School view. Telegram check-ins route new tasks to the matching area.

Add three to five real active items across the four databases so the first brief has useful data. Keep each row's `Name`, `Status`, and `Next step` current.

If Notion returns 404, check both the page ID and whether the parent page is connected to `Todo Agent`.

## 7. Connect Google Calendar

Use the calendar's private iCalendar address, not its normal browser URL:

1. In Google Calendar on a computer, open **Settings**.
2. Under **Settings for my calendars**, select the calendar.
3. Open **Integrate calendar**.
4. Copy **Secret address in iCal format**.
5. Store it through the hidden local prompt:

   ```powershell
   venv\Scripts\python.exe manage_secrets.py set ICAL_URL
   ```

Google documents these steps under [View your calendar in other applications](https://support.google.com/calendar/answer/37648). Treat the secret address like a password. If it is exposed, reset it in Google Calendar and store the replacement in the encrypted vault.

## 8. Configure unattended Canvas access

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

Assignment collection covers the 14 days before the brief date as well as the
following 14 days. This keeps work due on the preparation evening and recent
on-paper assignments from falling between Canvas endpoints.

For collected assignments, the tasker also loads the full Canvas assignment page.
Small DOCX and text-based PDF instruction files linked from the description are
read locally and supplied to Ollama as bounded guidance context. Files larger than
5 MB, unsupported formats, and scanned PDFs are skipped with a visible warning.

An overdue assignment whose submission mode is `on_paper` is shown under
**VERIFY** instead of being asserted as unfinished. Canvas often leaves offline
work marked `unsubmitted` even after it was handed in.

Briefs display the Canvas course beneath each assignment and render assignment
deadlines in the configured local timezone, including its timezone abbreviation.

This school blocks Canvas access tokens, so TodoTasker uses Microsoft sign-in. Store
your school email and password through the hidden vault prompts:

```powershell
venv\Scripts\python.exe manage_secrets.py set MICROSOFT_EMAIL
venv\Scripts\python.exe manage_secrets.py set MICROSOFT_PASSWORD
```

Then create the initial encrypted browser session with one interactive login:

```powershell
venv\Scripts\python.exe canvas.py login
```

Complete Microsoft SSO in the opened Chromium window, return to PowerShell, and
press Enter. TodoTasker verifies Canvas before saving the session.

The browser session and Microsoft credentials are encrypted with Windows DPAPI and stored in
`.private\canvas-session.dpapi` and `.private\secrets.dpapi`. They are decrypted only in memory; TodoTasker does not
retain a Chromium profile or plaintext `storage-state.json`. When Canvas expires the
saved session, TodoTasker verifies and renews it in headless Chromium without
opening a window on your desktop. It submits the encrypted credentials,
accepts Microsoft's recognized “Stay signed in?” prompt, re-encrypts the renewed
cookies, and closes the background browser without user input. The encrypted session is an
automatic DPAPI-protected cookie cache, not a recurring manual-login step. Credentials
are typed only into Microsoft's approved HTTPS login domains, never into Canvas or a
third-party page. The scheduled authentication check warns you in Telegram only if
renewal fails (for example, after a password change or a new Microsoft challenge).

## 9. Verify each read-only connection

These commands verify security and fetch or summarize data without creating Notion
pages or sending Telegram messages:

```powershell
venv\Scripts\python.exe manage_secrets.py audit
venv\Scripts\python.exe canvas.py auth-check
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

The following commands are intentionally live. `prepare` updates local state. `deliver` synchronizes the canonical Tasks database, refreshes the three-item Today focus, and sends or edits the brief in Telegram.

```powershell
$Today = Get-Date -Format 'yyyy-MM-dd'
venv\Scripts\python.exe brief.py prepare --target-date $Today
venv\Scripts\python.exe brief.py deliver --target-date $Today
```

Verify that:

- a generated Markdown brief exists under `state\briefs`;
- the School view has one group per Canvas class and its assignments are present;
- Today contains no more than three focused tasks and is readable on a phone;
- one morning message arrived in Telegram;
- the Telegram button opens the To Do List dashboard.

Run the same two commands once more. The system should update matching Tasks rows and reuse the same Telegram message rather than create duplicates or repeat presentation text in `Next step`.

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
| Daily Brief - Canvas Auth Check | 8:30 PM | `canvas.py auth-check --notify` |
| Daily Brief - Evening Check-in | 9:00 PM | `checkin.py send` |
| Daily Brief - Process Check-in | 9:30 PM | `checkin.py process` |
| Daily Brief - Prepare | 9:50 PM | `brief.py prepare` |
| Daily Brief - Deliver | 6:30 AM | `brief.py deliver` |
| Daily Brief - Watchdog | 7:30 AM and logon | `brief.py watchdog` |

After reviewing the full paths and triggers printed by the preview, register the tasks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-scheduled-tasks.ps1 -Apply
```

The tasks run only for the logged-on user, wake the computer, start after a missed
trigger when possible, continue on battery power, and retry a nonzero exit three
times at ten-minute intervals. This lets a laptop that was asleep catch up after it
wakes without Windows immediately terminating the run. Repository setup and tests
never create scheduled tasks automatically.

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

Local Ollama is the default. To use Anthropic for bounded morning guidance, store
the API key with `venv\Scripts\python.exe manage_secrets.py set ANTHROPIC_API_KEY`,
then set only the non-secret fields in `.env`:

```dotenv
MODEL_PROVIDER=anthropic
ANTHROPIC_MODEL=replace_with_a_model_available_to_your_account
```

Evening check-ins remain local regardless of this setting. If the remote model is unavailable, morning preparation uses deterministic fallback guidance.

## Common commands

```powershell
# Run tests
venv\Scripts\python.exe -m pytest

# Check the vault, permissions, and Canvas authentication
venv\Scripts\python.exe manage_secrets.py audit
venv\Scripts\python.exe manage_secrets.py status
venv\Scripts\python.exe canvas.py auth-check

# Create the initial encrypted Canvas session (automatic renewal handles later expiry)
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

Run `venv\Scripts\python.exe canvas.py auth-check`. TodoTasker automatically renews
the encrypted Canvas session with its encrypted Microsoft credentials. If that fails, confirm
your password has not changed and that Microsoft has not introduced a new sign-in
challenge; then run `canvas.py login` once to establish a fresh browser session.

### Telegram setup finds no chat

Open the bot in Telegram, press **Start**, send a new message, and rerun `setup_telegram.py`. If a webhook is configured on the same bot, remove the conflicting webhook or use a dedicated bot because polling and webhooks cannot consume updates simultaneously.

### Telegram token or calendar URL was exposed

Revoke and replace the bot token through BotFather. Reset the secret iCal address in
Google Calendar. Store each replacement with the matching `manage_secrets.py set`
command.

### Notion returns 404

Verify `NOTION_PARENT_PAGE_ID`, `NOTION_SCHOOL_PAGE_ID`, and all four `NOTION_*_DB_ID` settings, then confirm the parent page is shared with the internal integration.

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
- Master Tasks rows use stable source IDs and sync hashes so retries do not duplicate assignments; Telegram delivery remains idempotent.
- Check-in events are recorded append-first before downstream mutation.
- Ambiguous or malformed check-in replies go to local quarantine instead of updating the wrong item.
- Raw check-in text is not sent to the morning remote-model path.
- `.env` contains no secret values; live credentials are held in a Windows user-scoped DPAPI vault under the ACL-locked, Git-ignored `.private` directory.
- The Canvas fallback session is also DPAPI-encrypted under `.private`; no persistent browser profile or plaintext storage state is retained.
- `manage_secrets.py audit` detects plaintext `.env` secrets, unsafe vault/session permissions, legacy browser state, and copies of configured secrets in repository files without printing their values.
- Runtime state, source caches, generated briefs, private captures, incidents, and quarantined replies are ignored by Git.
- `--dry-run` performs no writes or deliveries.

## Disable or remove scheduling

To pause the system without deleting local state, disable the six **Daily Brief** tasks in Windows Task Scheduler.

Before removing anything, list the exact matching tasks:

```powershell
Get-ScheduledTask |
    Where-Object TaskName -Like 'Daily Brief - *' |
    Select-Object TaskName, State
```

After verifying the names, remove those exact tasks in Task Scheduler or with
`Unregister-ScheduledTask` one at a time. The repository state, encrypted vault, and
encrypted Canvas session are not deleted when scheduled tasks are disabled or
removed.

## Development

```powershell
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest
```

See [`docs/operations.md`](docs/operations.md) for the shorter operator reference.
