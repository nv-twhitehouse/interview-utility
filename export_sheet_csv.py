#!/usr/bin/env python3
"""Export a paste-ready, fully reconciled Interviews CSV (columns A through O)."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping


DEFAULT_DB = Path(__file__).with_name("outlook_objects.sqlite3")
DEFAULT_OUTPUT = Path(__file__).with_name("google_sheet_reconciled.csv")

FIELDS = (
    "search_name",
    "linkedin_url",
    "candidate_name",
    "recruiter",
    "date_contacted",
    "date_replied",
    "response",
    "date_scheduled",
    "teams_link",
    "interviewer",
    "date_completed",
    "incentive_status",
    "requested_incentive",
    "gdoc_notes",
    "agent_notes",
)

HEADERS = (
    "Search",
    "LinkedIn",
    "Name",
    "Recruiter",
    "Date Contacted",
    "Date Replied",
    "Response",
    "Date Scheduled",
    "Teams Link",
    "Interviewer",
    "Date Completed",
    "Incentive Status",
    "Requested Incentive",
    "Gdoc Notes",
    "Agent Notes",
)

INCENTIVE_LABELS = {
    "options_offered": "Asked",
    "requested": "Replied",
    "sent": "Sent",
    "received": "Received",
    "problem": "Problem",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="omit the header when pasting beneath an existing sheet header",
    )
    parser.add_argument(
        "--all-rows",
        action="store_true",
        help="include unchanged rows; the default exports only rows with reconciled changes",
    )
    return parser.parse_args()


def merge_interview(
    interview: Mapping[str, Any], derived: Mapping[str, Any] | None
) -> tuple[dict[str, str], int]:
    merged = {field: str(interview[field] or "") for field in FIELDS}
    if not derived:
        return merged, 0

    replacements = {
        "date_scheduled": derived.get("date_scheduled"),
        "teams_link": derived.get("teams_link"),
        "interviewer": derived.get("interviewer"),
        "date_completed": derived.get("date_completed"),
        "requested_incentive": derived.get("requested_incentive"),
        "agent_notes": derived.get("agent_summary"),
    }
    incentive_status = INCENTIVE_LABELS.get(str(derived.get("incentive_state") or ""))
    if incentive_status:
        replacements["incentive_status"] = incentive_status
    if derived.get("scheduling_state") == "no_show":
        replacements["incentive_status"] = "No show--not applicable"

    changed = 0
    for field, value in replacements.items():
        if value is None or str(value).strip() == "":
            continue
        value = str(value).strip()
        if merged[field] != value:
            merged[field] = value
            changed += 1
    return merged, changed


def load_derived(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS engagement_agent_summaries (
            engagement_id INTEGER PRIMARY KEY,
            summary TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            generated_by TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            FOREIGN KEY (engagement_id) REFERENCES outlook_engagements(engagement_id)
        )
        """
    )
    rows = connection.execute(
        """
        SELECT m.linkedin_url, m.engagement_id,
               s.scheduling_state, s.incentive_state, s.requested_incentive,
               a.summary AS agent_summary
        FROM engagement_sheet_matches AS m
        JOIN engagement_current_state AS s USING (engagement_id)
        LEFT JOIN engagement_agent_summaries AS a USING (engagement_id)
        WHERE m.match_status IN ('automatic', 'confirmed')
          AND m.linkedin_url IS NOT NULL
        """
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        derived = {
            "scheduling_state": row["scheduling_state"],
            "incentive_state": row["incentive_state"],
            "requested_incentive": row["requested_incentive"],
            "agent_summary": row["agent_summary"],
        }
        findings = connection.execute(
            """
            SELECT field_name, observed_value
            FROM reconciliation_findings
            WHERE engagement_id = ? AND observed_value IS NOT NULL
            """,
            (row["engagement_id"],),
        ).fetchall()
        derived.update({finding["field_name"]: finding["observed_value"] for finding in findings})
        result[row["linkedin_url"]] = derived
    return result


def export_csv(
    db_path: Path,
    output: Path,
    include_header: bool = True,
    changed_only: bool = True,
) -> tuple[int, int, int, int]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        interviews = connection.execute(
            "SELECT * FROM interviews ORDER BY rowid"
        ).fetchall()
        derived = load_derived(connection)

    output.parent.mkdir(parents=True, exist_ok=True)
    changed_rows = 0
    changed_cells = 0
    written_rows = 0
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        if include_header:
            writer.writerow(HEADERS)
        for interview in interviews:
            merged, changed = merge_interview(interview, derived.get(interview["linkedin_url"]))
            if changed_only and not changed:
                continue
            writer.writerow([merged[field] for field in FIELDS])
            written_rows += 1
            if changed:
                changed_rows += 1
                changed_cells += changed
    return len(interviews), written_rows, changed_rows, changed_cells


def main() -> int:
    args = parse_args()
    if not args.db.exists():
        print(f"error: database not found: {args.db}", file=sys.stderr)
        return 1
    try:
        total_rows, written_rows, changed_rows, changed_cells = export_csv(
            args.db,
            args.output,
            include_header=not args.no_header,
            changed_only=not args.all_rows,
        )
    except (OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        f"Wrote {written_rows} paste-ready rows from {total_rows} sheet rows "
        f"to {args.output.resolve()}"
    )
    print(f"Reconciled {changed_cells} cells across {changed_rows} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
