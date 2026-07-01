import unittest

from export_sheet_csv import merge_interview


class ExportSheetCsvTests(unittest.TestCase):
    def base_interview(self):
        return {
            "search_name": "Search 2",
            "linkedin_url": "https://linkedin.example/alex",
            "candidate_name": "",
            "recruiter": "Tyler",
            "date_contacted": "6/25/2026",
            "date_replied": "",
            "response": "Yes",
            "date_scheduled": "6/25/2026",
            "teams_link": "old-link",
            "interviewer": "Amy",
            "date_completed": "",
            "incentive_status": "",
            "requested_incentive": "",
            "gdoc_notes": "gdoc-link",
            "agent_notes": "human note",
        }

    def test_preserves_manual_fields_and_reconciles_supported_fields(self):
        merged, changed = merge_interview(
            self.base_interview(),
            {
                "date_scheduled": "7/7/2026",
                "teams_link": "new-link",
                "interviewer": "Amy Malone",
                "date_completed": "7/7/2026",
                "scheduling_state": "completed",
                "incentive_state": "requested",
                "requested_incentive": "DLI",
                "agent_summary": "Interview completed. Alex selected a DLI course.",
            },
        )
        self.assertEqual(changed, 7)
        self.assertEqual(merged["response"], "Yes")
        self.assertEqual(merged["date_scheduled"], "7/7/2026")
        self.assertEqual(merged["incentive_status"], "Replied")
        self.assertEqual(merged["interviewer"], "Amy Malone")
        self.assertEqual(merged["gdoc_notes"], "gdoc-link")
        self.assertEqual(
            merged["agent_notes"], "Interview completed. Alex selected a DLI course."
        )

    def test_no_show_sets_not_applicable_without_clearing_other_fields(self):
        merged, _ = merge_interview(
            self.base_interview(),
            {"scheduling_state": "no_show", "incentive_state": "unknown"},
        )
        self.assertEqual(merged["incentive_status"], "No show--not applicable")
        self.assertEqual(merged["teams_link"], "old-link")

if __name__ == "__main__":
    unittest.main()
