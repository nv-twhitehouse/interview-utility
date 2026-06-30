# Outlook subject importer

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
