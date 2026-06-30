import sqlite3
import tempfile
import unittest
from pathlib import Path

from pull_gsheet import parse_interviews, replace_interviews


class PullGsheetTests(unittest.TestCase):
    def test_parses_only_interviews_and_pads_trailing_fields(self):
        content = [
            "Overview",
            "ignored,overview,row",
            "Interviews",
            "Total: 1",
            (
                "Search,LinkedIn,Name,Recruiter,Date Contacted,Date Replied,"
                "Response,Date Scheduled,Teams Link,Interviewer,Date Completed,"
                "Incentive Status,Requested Incentive,Gdoc Notes,Agent Notes,"
            ),
            "Search 1,https://linkedin.example/alex,Alex Example,Tyler,6/5/2026,Yes",
            "shirts",
            "ignored,shirt,row",
        ]

        records = parse_interviews(content)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["candidate_name"], "Alex Example")
        self.assertEqual(records[0]["date_replied"], "")
        self.assertEqual(records[0]["response"], "Yes")
        self.assertEqual(records[0]["agent_notes"], "")

    def test_restores_omitted_blank_date_replied_cell(self):
        content = [
            "Interviews",
            (
                "Search,LinkedIn,Name,Recruiter,Date Contacted,Date Replied,"
                "Response,Date Scheduled,Teams Link,Interviewer,Date Completed,"
                "Incentive Status,Requested Incentive,Gdoc Notes,Agent Notes,"
            ),
            (
                "Search 2,https://linkedin.example/alex,,Tyler,6/25/2026,Yes,"
                "6/29/2026,https://teams.example,Tyler,,Replied,DLI,"
                "https://docs.example,"
            ),
            "shirts",
        ]

        record = parse_interviews(content)[0]

        self.assertEqual(record["date_replied"], "")
        self.assertEqual(record["response"], "Yes")
        self.assertEqual(record["date_scheduled"], "6/29/2026")
        self.assertEqual(record["teams_link"], "https://teams.example")
        self.assertEqual(record["interviewer"], "Tyler")
        self.assertEqual(record["incentive_status"], "Replied")
        self.assertEqual(record["requested_incentive"], "DLI")
        self.assertEqual(record["gdoc_notes"], "https://docs.example")

    def test_replaces_existing_table(self):
        records = parse_interviews(
            [
                "Interviews",
                "Search,LinkedIn,Name",
                "Search 1,https://linkedin.example/alex,Alex Example",
                "shirts",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite3"
            replace_interviews(db_path, records, "https://sheet", "pulled-at")
            replace_interviews(db_path, records, "https://sheet", "pulled-at")
            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT candidate_name, json_valid(raw_json) FROM interviews"
                ).fetchone()
                count = connection.execute("SELECT COUNT(*) FROM interviews").fetchone()[0]
        self.assertEqual(row, ("Alex Example", 1))
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
