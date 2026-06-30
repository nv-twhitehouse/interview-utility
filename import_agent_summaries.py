#!/usr/bin/env python3
"""Import agent-authored engagement summaries into the reconciliation database."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB = Path(__file__).with_name("outlook_objects.sqlite3")
DEFAULT_INPUT = Path(__file__).with_name("agent_summaries.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    return parser.parse_args()


def engagement_hash(connection: sqlite3.Connection, engagement_id: int) -> str:
    rows = connection.execute(
        """
        SELECT o.source, o.object_id, o.raw_json, coalesce(b.body_text, '')
        FROM engagement_objects AS eo
        JOIN objects AS o ON o.source = eo.source AND o.object_id = eo.object_id
        LEFT JOIN outlook_message_bodies AS b
          ON b.source = o.source AND b.object_id = o.object_id
        WHERE eo.engagement_id = ?
        ORDER BY o.event_or_sent_at, o.source, o.object_id
        """,
        (engagement_id,),
    ).fetchall()
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        summaries = payload["summaries"]
        generated_by = payload.get("generated_by", "agent")
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"error: invalid summary input: {error}", file=sys.stderr)
        return 1

    try:
        with sqlite3.connect(args.db) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
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
            imported = 0
            for linkedin_url, summary in summaries.items():
                row = connection.execute(
                    """
                    SELECT m.engagement_id
                    FROM engagement_sheet_matches AS m
                    WHERE m.linkedin_url = ?
                      AND m.match_status IN ('automatic', 'confirmed')
                    """,
                    (linkedin_url,),
                ).fetchone()
                if not row:
                    print(f"warning: no confident engagement for {linkedin_url}", file=sys.stderr)
                    continue
                engagement_id = int(row[0])
                connection.execute(
                    """
                    INSERT INTO engagement_agent_summaries (
                        engagement_id, summary, input_hash, generated_by, generated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(engagement_id) DO UPDATE SET
                        summary = excluded.summary,
                        input_hash = excluded.input_hash,
                        generated_by = excluded.generated_by,
                        generated_at = excluded.generated_at
                    """,
                    (
                        engagement_id,
                        str(summary).strip(),
                        engagement_hash(connection, engagement_id),
                        generated_by,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                imported += 1
    except (OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Imported {imported} agent-authored summaries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
