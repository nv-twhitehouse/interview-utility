#!/usr/bin/env python3
"""Pull a Google Sheet through gdrive-cli and save its structured content."""

from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1PrMBmqH6m6eaOJNzRZATphjl8QKRh0ALZA6RpClmlYs/edit?gid=0#gid=0"
)
DEFAULT_OUTPUT = Path(__file__).with_name("gsheet_ai_agents_interview_candidates.json")
DEFAULT_DB = Path(__file__).with_name("outlook_objects.sqlite3")

INTERVIEW_FIELDS = (
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

INTERVIEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS interviews (
    linkedin_url        TEXT PRIMARY KEY,
    search_name         TEXT,
    candidate_name      TEXT,
    recruiter           TEXT,
    date_contacted      TEXT,
    date_replied        TEXT,
    response            TEXT,
    date_scheduled      TEXT,
    teams_link          TEXT,
    interviewer         TEXT,
    date_completed      TEXT,
    incentive_status    TEXT,
    requested_incentive TEXT,
    gdoc_notes          TEXT,
    agent_notes         TEXT,
    raw_json            TEXT NOT NULL CHECK (json_valid(raw_json)),
    source_url          TEXT NOT NULL,
    source_pulled_at    TEXT NOT NULL,
    imported_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS interviews_candidate_name_idx
    ON interviews(candidate_name);
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Google Sheet URL")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--max-length", type=int, default=1_000_000)
    parser.add_argument(
        "--from-snapshot",
        action="store_true",
        help="import the existing --output snapshot without contacting Google Drive",
    )
    return parser.parse_args()


def extract_content(payload: Any) -> list[str]:
    try:
        results = payload["data"]["results"]
        successful = [result for result in results if result.get("status") == "success"]
        content = successful[0]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("gdrive-cli returned an unexpected response") from error
    if not isinstance(content, list) or not all(isinstance(line, str) for line in content):
        raise RuntimeError("gdrive-cli response did not contain a list of text rows")
    return content


def parse_interviews(content: list[str]) -> list[dict[str, str]]:
    try:
        start = content.index("Interviews") + 1
        end = content.index("shirts", start)
    except ValueError as error:
        raise RuntimeError("Interviews section boundaries were not found") from error

    parsed = [next(csv.reader(io.StringIO(line))) for line in content[start:end]]
    try:
        header_index = next(
            index
            for index, row in enumerate(parsed)
            if row[:2] == ["Search", "LinkedIn"]
        )
    except StopIteration as error:
        raise RuntimeError("Interviews header row was not found") from error

    records: list[dict[str, str]] = []
    for row in parsed[header_index + 1 :]:
        while row and not row[-1].strip():
            row.pop()
        if not row:
            continue
        if len(row) > len(INTERVIEW_FIELDS):
            raise RuntimeError(f"Interview row has too many fields: {len(row)}")
        values = [value.strip() for value in row]
        values.extend([""] * (len(INTERVIEW_FIELDS) - len(values)))
        record = dict(zip(INTERVIEW_FIELDS, values))
        if not record["linkedin_url"]:
            raise RuntimeError("Interview row is missing its LinkedIn URL")
        records.append(record)

    links = [record["linkedin_url"] for record in records]
    if len(links) != len(set(links)):
        raise RuntimeError("Interviews section contains duplicate LinkedIn URLs")
    return records


def replace_interviews(
    db_path: Path,
    records: list[dict[str, str]],
    source_url: str,
    pulled_at: str,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    imported_at = datetime.now(timezone.utc).isoformat()
    columns = ", ".join(INTERVIEW_FIELDS)
    placeholders = ", ".join("?" for _ in INTERVIEW_FIELDS)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(INTERVIEWS_SCHEMA)
        connection.execute("DELETE FROM interviews")
        for record in records:
            values = [record[field] or None for field in INTERVIEW_FIELDS]
            connection.execute(
                f"""
                INSERT INTO interviews (
                    {columns}, raw_json, source_url, source_pulled_at, imported_at
                ) VALUES ({placeholders}, ?, ?, ?, ?)
                """,
                (
                    *values,
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                    source_url,
                    pulled_at,
                    imported_at,
                ),
            )


def main() -> int:
    args = parse_args()
    if args.from_snapshot:
        try:
            saved = json.loads(args.output.read_text(encoding="utf-8"))
            content = saved["content"]
            pulled_at = saved["pulled_at"]
            source_url = saved["source_url"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            print(f"error: invalid snapshot {args.output}: {error}", file=sys.stderr)
            return 1
    else:
        command = [
            "gdrive-cli",
            "file",
            "get",
            "--file-url",
            args.url,
            "--max-length",
            str(args.max_length),
            "--output",
            "json",
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            print(result.stderr.strip() or "gdrive-cli failed", file=sys.stderr)
            return result.returncode or 1
        try:
            payload = json.loads(result.stdout)
            content = extract_content(payload)
        except (json.JSONDecodeError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

        pulled_at = datetime.now(timezone.utc).isoformat()
        source_url = args.url
        saved = {
            "source_url": source_url,
            "pulled_at": pulled_at,
            "content": content,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    try:
        interviews = parse_interviews(content)
        replace_interviews(args.db, interviews, source_url, pulled_at)
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not args.from_snapshot:
        print(f"Saved source snapshot to {args.output.resolve()}")
    print(f"Replaced interviews table with {len(interviews)} rows in {args.db.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
