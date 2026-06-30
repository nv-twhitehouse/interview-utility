import json
import sqlite3
import unittest
from datetime import date

from import_outlook import (
    SCHEMA,
    date_window,
    ensure_schema,
    extract_candidate_name,
    upsert_items,
)


class ImportOutlookTests(unittest.TestCase):
    def test_date_window_includes_both_fourteenth_days(self):
        self.assertEqual(date_window(date(2026, 6, 30), 14), ("2026-06-16", "2026-07-15"))

    def test_only_exact_subject_substrings_are_inserted(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(SCHEMA)
        items = [
            {
                "id": "exact",
                "conversationId": "conversation-1",
                "subject": "Re: Chat About Agents With NVIDIA notes",
                "from": {"emailAddress": {"name": "Example", "address": "e@example.com"}},
            },
            {"id": "false-positive", "subject": "chat about NVIDIA agents"},
        ]

        count = upsert_items(
            connection,
            "inbox",
            "message",
            items,
            "chat about agents with nvidia",
        )

        self.assertEqual(count, 1)
        row = connection.execute(
            "SELECT object_type, object_id, conversation_id, candidate_name, "
            "sender_address, raw_json FROM objects"
        ).fetchone()
        self.assertEqual(
            row[:5],
            ("message", "exact", "conversation-1", None, "e@example.com"),
        )
        self.assertEqual(json.loads(row[5]), items[0])

    def test_candidate_name_subject_formats(self):
        phrase = "chat about agents with nvidia"
        cases = {
            "New booking: Alex Rector for Chat about agents with NVIDIA": "Alex Rector",
            "RE: Chat about agents with NVIDIA - Alex Rector": "Alex Rector",
            "FW: Chat about agents with NVIDIA - anuhya s": "anuhya s",
            (
                "Accepted: Chat about agents with NVIDIA - Alex Rector @ "
                "Mon Jun 29, 2026 12pm"
            ): "Alex Rector",
            "Chat about NVIDIA agents - Not A Match": None,
        }
        for subject, expected in cases.items():
            with self.subTest(subject=subject):
                self.assertEqual(extract_candidate_name(subject, phrase), expected)

    def test_legacy_database_is_migrated_and_backfilled(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE objects (
                source TEXT NOT NULL, object_type TEXT NOT NULL, remote_id TEXT NOT NULL,
                subject TEXT, event_or_sent_at TEXT, raw_json TEXT NOT NULL,
                imported_at TEXT NOT NULL, PRIMARY KEY (source, remote_id)
            );
            """
        )
        raw = json.dumps(
            {"id": "old-id", "conversationId": "old-conversation", "subject": "Subject"}
        )
        connection.execute(
            "INSERT INTO objects VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("inbox", "message", "old-id", "Subject", None, raw, "now"),
        )

        ensure_schema(connection)

        columns = {row[1] for row in connection.execute("PRAGMA table_info(objects)")}
        self.assertIn("object_id", columns)
        self.assertNotIn("remote_id", columns)
        row = connection.execute(
            "SELECT object_id, conversation_id, candidate_name FROM objects"
        ).fetchone()
        self.assertEqual(row, ("old-id", "old-conversation", None))


if __name__ == "__main__":
    unittest.main()
