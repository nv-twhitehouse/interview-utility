#!/usr/bin/env python3
"""Import matching Outlook messages and calendar events into SQLite."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_PHRASE = "chat about agents with nvidia"
DEFAULT_DB = Path(__file__).with_name("outlook_objects.sqlite3")
DEFAULT_DAYS = 14


QUERIES = (
    (
        "calendar",
        "event",
        "calendar-cli",
        (
            "find",
            "--subject",
            "{phrase}",
            "--after",
            "{after}",
            "--before",
            "{before}",
            "--json",
            "--utc",
        ),
    ),
    (
        "inbox",
        "message",
        "outlook-cli",
        (
            "message",
            "find",
            "--folder",
            "inbox",
            "--subject",
            "{phrase}",
            "--after",
            "{after}",
            "--before",
            "{before}",
            "--json",
            "--utc",
        ),
    ),
    (
        "sent",
        "message",
        "outlook-cli",
        (
            "message",
            "find",
            "--folder",
            "sent",
            "--subject",
            "{phrase}",
            "--after",
            "{after}",
            "--before",
            "{before}",
            "--json",
            "--utc",
        ),
    ),
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    source          TEXT NOT NULL CHECK (source IN ('calendar', 'inbox', 'sent')),
    object_type     TEXT NOT NULL CHECK (object_type IN ('event', 'message')),
    object_id       TEXT NOT NULL,
    conversation_id TEXT,
    internet_message_id TEXT,
    ical_uid        TEXT,
    subject         TEXT,
    candidate_name  TEXT,
    sender_name     TEXT,
    sender_address  TEXT,
    organizer_name  TEXT,
    organizer_address TEXT,
    start_at        TEXT,
    end_at          TEXT,
    sent_at         TEXT,
    received_at     TEXT,
    has_attachments INTEGER,
    web_link        TEXT,
    event_or_sent_at TEXT,
    raw_json        TEXT NOT NULL CHECK (json_valid(raw_json)),
    imported_at     TEXT NOT NULL,
    PRIMARY KEY (source, object_id)
);

CREATE INDEX IF NOT EXISTS objects_subject_idx ON objects(subject);
CREATE INDEX IF NOT EXISTS objects_conversation_idx ON objects(conversation_id);
CREATE INDEX IF NOT EXISTS objects_candidate_name_idx ON objects(candidate_name);
"""


EXTRACTED_COLUMNS = {
    "conversation_id": "TEXT",
    "internet_message_id": "TEXT",
    "ical_uid": "TEXT",
    "candidate_name": "TEXT",
    "sender_name": "TEXT",
    "sender_address": "TEXT",
    "organizer_name": "TEXT",
    "organizer_address": "TEXT",
    "start_at": "TEXT",
    "end_at": "TEXT",
    "sent_at": "TEXT",
    "received_at": "TEXT",
    "has_attachments": "INTEGER",
    "web_link": "TEXT",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find matching calendar events, inbox messages, and sent messages "
            "and upsert them into a local SQLite database."
        )
    )
    parser.add_argument("--phrase", default=DEFAULT_PHRASE, help="subject phrase")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="days before and after today to search (default: 14)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--migrate-only",
        action="store_true",
        help="upgrade/backfill the database schema without querying Microsoft",
    )
    return parser.parse_args()


def require_cli(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Required command is not on PATH: {name}")


def extract_items(payload: Any, command: list[str]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected JSON from {' '.join(command)}: expected object")
    if payload.get("success") is False:
        error = payload.get("error") or payload.get("message") or payload
        raise RuntimeError(f"CLI reported an error for {' '.join(command)}: {error}")

    data = payload.get("data", [])
    # Current find commands return data[], but tolerate a nested items/results array.
    if isinstance(data, dict):
        data = data.get("items", data.get("results", data.get("value", [])))
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise RuntimeError(f"Unexpected data field from {' '.join(command)}")
    return data


def date_window(today: date, days: int) -> tuple[str, str]:
    """Return an inclusive date window using the CLIs' exclusive --before bound."""
    return (
        (today - timedelta(days=days)).isoformat(),
        (today + timedelta(days=days + 1)).isoformat(),
    )


def run_query(
    executable: str,
    arguments: tuple[str, ...],
    phrase: str,
    after: str,
    before: str,
) -> list[dict[str, Any]]:
    values = {"phrase": phrase, "after": after, "before": before}
    command = [executable, *(arg.format(**values) for arg in arguments)]
    print(f"Searching {executable}: {after} through {before} (exclusive)...", flush=True)
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no error details"
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{' '.join(command)}\n{detail}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid JSON from {' '.join(command)}: {error}\n{result.stdout[:500]}"
        ) from error
    return extract_items(payload, command)


def nested_string(item: dict[str, Any], *path: str) -> str | None:
    value: Any = item
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, str) else None


def graph_datetime(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if isinstance(value, dict):
        return value.get("dateTime")
    return value if isinstance(value, str) else None


def extract_candidate_name(subject: str | None, phrase: str) -> str | None:
    if not subject:
        return None
    escaped_phrase = re.escape(phrase)
    patterns = (
        rf"^New booking:\s*(?P<name>.+?)\s+for\s+{escaped_phrase}(?:\s|$)",
        rf"{escaped_phrase}\s+-\s+(?P<name>.+?)(?:\s+@\s+|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, subject, flags=re.IGNORECASE)
        if match:
            name = match.group("name").strip()
            return name or None
    return None


def extracted_values(
    item: dict[str, Any], object_type: str, phrase: str = DEFAULT_PHRASE
) -> tuple[Any, ...]:
    has_attachments = item.get("hasAttachments")
    if isinstance(has_attachments, bool):
        has_attachments = int(has_attachments)
    else:
        has_attachments = None
    event_or_sent_at = (
        graph_datetime(item, "sentDateTime") or graph_datetime(item, "receivedDateTime")
        if object_type == "message"
        else graph_datetime(item, "start")
    )
    return (
        item.get("conversationId"),
        item.get("internetMessageId"),
        item.get("iCalUId"),
        item.get("subject"),
        extract_candidate_name(item.get("subject"), phrase),
        nested_string(item, "from", "emailAddress", "name"),
        nested_string(item, "from", "emailAddress", "address"),
        nested_string(item, "organizer", "emailAddress", "name"),
        nested_string(item, "organizer", "emailAddress", "address"),
        graph_datetime(item, "start"),
        graph_datetime(item, "end"),
        graph_datetime(item, "sentDateTime"),
        graph_datetime(item, "receivedDateTime"),
        has_attachments,
        item.get("webLink"),
        event_or_sent_at,
    )


def ensure_schema(connection: sqlite3.Connection, phrase: str = DEFAULT_PHRASE) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'objects'"
    ).fetchone()
    if not exists:
        connection.executescript(SCHEMA)
        return

    columns = {row[1] for row in connection.execute("PRAGMA table_info(objects)")}
    if "remote_id" in columns and "object_id" not in columns:
        connection.execute("ALTER TABLE objects RENAME COLUMN remote_id TO object_id")
        columns.remove("remote_id")
        columns.add("object_id")
    for name, sql_type in EXTRACTED_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE objects ADD COLUMN {name} {sql_type}")

    # Populate extracted fields for rows created by earlier versions of this utility.
    rows = connection.execute("SELECT source, object_id, object_type, raw_json FROM objects")
    for source, object_id, object_type, raw_json in rows.fetchall():
        item = json.loads(raw_json)
        values = extracted_values(item, object_type, phrase)
        connection.execute(
            """
            UPDATE objects SET
                conversation_id = ?, internet_message_id = ?, ical_uid = ?, subject = ?,
                candidate_name = ?,
                sender_name = ?, sender_address = ?, organizer_name = ?, organizer_address = ?,
                start_at = ?, end_at = ?, sent_at = ?, received_at = ?,
                has_attachments = ?, web_link = ?, event_or_sent_at = ?
            WHERE source = ? AND object_id = ?
            """,
            (*values, source, object_id),
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS objects_subject_idx ON objects(subject)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS objects_conversation_idx ON objects(conversation_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS objects_candidate_name_idx ON objects(candidate_name)"
    )


def upsert_items(
    connection: sqlite3.Connection,
    source: str,
    object_type: str,
    items: list[dict[str, Any]],
    phrase: str,
) -> int:
    imported_at = datetime.now(timezone.utc).isoformat()
    matching_items = [
        item
        for item in items
        if phrase.casefold() in str(item.get("subject", "")).casefold()
    ]
    for item in matching_items:
        object_id = item.get("id")
        if not object_id:
            raise RuntimeError(f"{source} object has no id: {item}")
        values = extracted_values(item, object_type, phrase)
        connection.execute(
            """
            INSERT INTO objects (
                source, object_type, object_id,
                conversation_id, internet_message_id, ical_uid, subject, candidate_name,
                sender_name, sender_address, organizer_name, organizer_address,
                start_at, end_at, sent_at, received_at, has_attachments, web_link,
                event_or_sent_at, raw_json, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, object_id) DO UPDATE SET
                object_type = excluded.object_type,
                conversation_id = excluded.conversation_id,
                internet_message_id = excluded.internet_message_id,
                ical_uid = excluded.ical_uid,
                subject = excluded.subject,
                candidate_name = excluded.candidate_name,
                sender_name = excluded.sender_name,
                sender_address = excluded.sender_address,
                organizer_name = excluded.organizer_name,
                organizer_address = excluded.organizer_address,
                start_at = excluded.start_at,
                end_at = excluded.end_at,
                sent_at = excluded.sent_at,
                received_at = excluded.received_at,
                has_attachments = excluded.has_attachments,
                web_link = excluded.web_link,
                event_or_sent_at = excluded.event_or_sent_at,
                raw_json = excluded.raw_json,
                imported_at = excluded.imported_at
            """,
            (
                source,
                object_type,
                str(object_id),
                *values,
                json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                imported_at,
            ),
        )
    return len(matching_items)


def main() -> int:
    args = parse_args()
    if args.migrate_only:
        if not args.db.exists():
            print(f"error: database does not exist: {args.db}", file=sys.stderr)
            return 1
        try:
            with sqlite3.connect(args.db) as connection:
                ensure_schema(connection, args.phrase)
                count = connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        except (OSError, sqlite3.Error, RuntimeError, json.JSONDecodeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"Migrated and backfilled {count} rows in {args.db.resolve()}.")
        return 0

    if not args.phrase.strip():
        print("error: --phrase cannot be empty", file=sys.stderr)
        return 2
    if args.days < 0:
        print("error: --days cannot be negative", file=sys.stderr)
        return 2

    try:
        require_cli("outlook-cli")
        require_cli("calendar-cli")
        after, before = date_window(date.today(), args.days)

        results = []
        for source, object_type, executable, arguments in QUERIES:
            items = run_query(executable, arguments, args.phrase, after, before)
            results.append((source, object_type, items))

        args.db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(args.db) as connection:
            ensure_schema(connection, args.phrase)
            counts = {
                source: upsert_items(connection, source, object_type, items, args.phrase)
                for source, object_type, items in results
            }
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    total = sum(counts.values())
    print(f"Database: {args.db.resolve()}")
    print(f"Window: {after} through {before} (exclusive upper bound)")
    print(
        f"Imported {total} matching objects "
        f"(calendar={counts['calendar']}, inbox={counts['inbox']}, sent={counts['sent']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
