# Interview reconciliation utility

This project reconciles a Google Sheet interview tracker with candidate activity in
Outlook mail and calendars. It is designed for Ubuntu under WSL and keeps all
reconciliation work local; it does not write changes back to Google Sheets.

## Requirements

- Ubuntu/Debian under WSL (Ubuntu 22.04 is known to work)
- Access to the NVIDIA internal network or VPN
- Python 3.10 or newer
- Python's built-in `sqlite3` module
- `outlook-cli`, `calendar-cli`, and `gdrive-cli` from the NVIDIA
  `ai-pim-utils` bundle
- `libpcsclite1`, required by the Linux CLI binaries
- `wslu` and `xdg-utils`, recommended so authentication URLs open in the Windows
  browser from WSL

The Python utilities have no third-party Python dependencies. No virtual environment
or `pip install` step is required. The standalone `sqlite3` terminal command is useful
for inspection but is not required by the programs.

## WSL installation

Install the Ubuntu packages:

```bash
sudo apt update
sudo apt install -y python3 sqlite3 libpcsclite1 wslu xdg-utils
```

Install the NVIDIA CLI bundle in binaries-only mode:

```bash
curl -fsSL https://outlook-cli-80d21a.gitlab-master-pages.nvidia.com/install.sh \
  | bash -s -- --binaries-only
```

The installer places the binaries in `~/.local/bin`. Add that directory to Bash's
PATH if it is not already present:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Verify the expected commands and libraries:

```bash
python3 --version
python3 -c 'import sqlite3; print(sqlite3.sqlite_version)'
outlook-cli --version
calendar-cli --version
gdrive-cli --version
```

If a CLI reports `libpcsclite.so.1: cannot open shared object file`, install the
missing package with:

```bash
sudo apt install -y libpcsclite1
```

## Authentication

`outlook-cli` and `calendar-cli` share Microsoft Graph authentication. Authenticate
once with:

```bash
outlook-cli auth login
```

Google Drive authentication is separate:

```bash
gdrive-cli auth login
```

If WSL cannot complete the browser callback automatically, use the two-step flow:

```bash
gdrive-cli auth init
# Open the displayed URL, sign in, and copy the complete localhost callback URL.
gdrive-cli auth complete '<complete-callback-url>'
```

Confirm both sessions before running an import:

```bash
outlook-cli auth status
gdrive-cli auth status
```

Authentication state is stored under `~/.ai-pim-utils/`. Treat that directory as
sensitive and do not copy it into this repository.

## Usage

Imports calendar events, inbox messages, and sent messages from 14 days before
today through 14 days after today whose subject contains
`chat about agents with nvidia` into a local SQLite database.

The Microsoft CLIs perform the initial subject and date search. The utility then
requires the exact phrase to occur as a case-insensitive substring of each returned
subject, removing false positives before anything is saved.

```bash
python3 import_outlook.py
```

The default database is `outlook_objects.sqlite3`. Every row contains the individual
object's complete JSON in `raw_json`, and `object_type` identifies it as a `message`
or `event`. Frequently queried fields are also extracted into columns, including
`object_id`, `conversation_id`, `internet_message_id`, `ical_uid`, sender/organizer,
`candidate_name`, timestamps, attachment status, and web link. Rerunning the command
updates existing rows by Microsoft object ID instead of creating duplicates.

Options:

```bash
python3 import_outlook.py --phrase "another phrase" --days 7 --db ./results.sqlite3
```

Upgrade and backfill an existing database without querying Microsoft again:

```bash
python3 import_outlook.py --migrate-only
```

Pull the configured candidate-tracking Google Sheet through `gdrive-cli`:

```bash
python3 pull_gsheet.py
```

This replaces the `interviews` table in `outlook_objects.sqlite3` with the 109 rows
from the sheet's `Interviews` section. `linkedin_url` is the primary key, and each
row also retains normalized source data in `raw_json`. The lossless source snapshot
is saved to `gsheet_ai_agents_interview_candidates.json` and ignored by Git.

Build the local Outlook-to-sheet reconciliation and hydrate any missing full email
bodies:

```bash
python3 reconcile.py --hydrate
```

Subsequent deterministic runs can reuse cached message bodies:

```bash
python3 reconcile.py
```

The command never writes to Google Sheets. It builds Outlook engagements, ranked
sheet matches, state evidence, current scheduling/incentive estimates,
reconciliation findings, and an agent-review queue in SQLite. A human-readable
`reconciliation_report.csv` is also generated and ignored by Git.

Pending ambiguous identity matches and sheet/Outlook conflicts are exported to
`agent_review_queue.json`. Each packet constrains the allowed decisions and includes
only the relevant ranked candidates or bounded email-thread evidence. Both the CSV
and JSON outputs are local review artifacts; neither changes the source sheet.

Export the complete reconciled `Interviews` grid as a paste-ready CSV:

```bash
python3 export_sheet_csv.py
```

This writes `google_sheet_reconciled.csv` with exactly columns A through O and only
includes rows where reconciliation changes at least one cell. It
preserves the manually managed A:G fields, preserves human-only fields without
Outlook evidence, and fills or replaces supported scheduling and incentive fields
from deterministic evidence. Rows with unresolved identity matches remain unchanged.
For confidently matched rows, column O (`Agent Notes`) is populated from an
agent-authored summary stored in `engagement_agent_summaries`. The deterministic
pipeline assembles and preserves the evidence; an LLM writes the casual-reader
summary separately so its provenance and source-thread hash remain auditable.
The default output includes the header for replacing the complete grid. To paste
under an existing header instead:

```bash
python3 export_sheet_csv.py --no-header
```

Use `--all-rows` only when a complete replacement of the Interviews grid is needed.

Inspect the imported rows with Python:

```bash
python3 - <<'PY'
import sqlite3

with sqlite3.connect("outlook_objects.sqlite3") as db:
    for row in db.execute(
        "SELECT source, subject, event_or_sent_at FROM objects ORDER BY source, event_or_sent_at"
    ):
        print(row)
PY
```
