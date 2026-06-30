import json
import sqlite3
import unittest

from reconcile import (
    RULES,
    SCHEMA,
    build_engagements,
    build_matches,
    format_sheet_date,
    match_score,
    newest_message_text,
    normalize_name,
)


class ReconcileTests(unittest.TestCase):
    def test_name_normalization_and_middle_initial_slug_match(self):
        self.assertEqual(normalize_name("Mélissa McHugh"), "melissamchugh")
        score, method = match_score(
            "Melissa McHugh", None, "https://www.linkedin.com/in/melissasmchugh"
        )
        self.assertGreaterEqual(score, 0.88)
        self.assertIn(method, {"linkedin_slug_fuzzy", "linkedin_first_last"})

    def test_newest_message_excludes_quoted_history(self):
        body = "Replacement code sent today.\n\nFrom: Candidate\nThe old code expired."
        self.assertEqual(newest_message_text(body), "Replacement code sent today.")

    def test_incentive_rules_tolerate_nonbreaking_spaces(self):
        choice = next(rule for rule in RULES if rule.rule_id == "incentive_choice")
        sent = next(rule for rule in RULES if rule.rule_id == "incentive_sent")
        choice_text = "I will\u00a0go for the t-shirt option."
        sent_text = "Enter\u00a0this\u00a0code: ABC123"
        self.assertIsNotNone(choice.pattern.search(choice_text.replace("\u00a0", " ")))
        self.assertIsNotNone(sent.pattern.search(sent_text))

    def test_graph_fractional_timestamp_formats_as_sheet_date(self):
        self.assertEqual(
            format_sheet_date("2026-06-29T14:30:00.0000000"), "6/29/2026"
        )

    def test_rerun_preserves_confirmed_identity_mapping(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE objects (
                source TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                candidate_name TEXT,
                event_or_sent_at TEXT,
                subject TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (source, object_id)
            );
            CREATE TABLE interviews (
                linkedin_url TEXT PRIMARY KEY,
                candidate_name TEXT
            );
            """
        )
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO objects VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "calendar",
                "event",
                "event-1",
                "Alex Example",
                "2026-06-30T12:00:00Z",
                "Chat - Alex Example",
                json.dumps({"id": "event-1"}),
            ),
        )
        connection.execute(
            "INSERT INTO interviews VALUES (?, ?)",
            ("https://linkedin.example/alex-example", "Alex Example"),
        )

        build_engagements(connection)
        build_matches(connection)
        connection.execute(
            "UPDATE engagement_sheet_matches SET match_status = 'confirmed'"
        )
        build_engagements(connection)
        build_matches(connection)

        row = connection.execute(
            "SELECT match_status, linkedin_url FROM engagement_sheet_matches"
        ).fetchone()
        self.assertEqual(
            tuple(row), ("confirmed", "https://linkedin.example/alex-example")
        )


if __name__ == "__main__":
    unittest.main()
