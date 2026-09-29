"""Offline end-to-end check for V2 Stage 1: the selected offer flows through
enrich -> lead DB -> Excel with a fake analyzer (no network, no API key).

Stdlib unittest only. DB and Excel output are pointed at temp dirs so the
real data/leads.db and outputs/ are never touched.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.lead_db as lead_db
import tools.enrich_leads as enrich_leads  # noqa: F401 — puts tools/ on sys.path
import tools.save_to_excel as save_to_excel

# enrich_leads.py does `from lead_db import ...` as a flat sibling import, so a
# SECOND module object exists under the name "lead_db". Both must point at the
# same temp DB or the enrich path would silently read the real data/leads.db.
import lead_db as flat_lead_db  # noqa: E402


FAKE_ANALYSIS = {
    "pain_points": ["no online booking"],
    "score": 4,
    "email_subject": "quick idea for your site",
    "email_body": "Hi Test Dental team,\n\nA modern site brings in customers.\n\nBest,\nZee",
}


class PipelineOfferFlowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = lead_db.DB_PATH
        self._orig_flat_db_path = flat_lead_db.DB_PATH
        tmp_db = Path(self._tmpdir.name) / "leads.db"
        lead_db.DB_PATH = tmp_db
        flat_lead_db.DB_PATH = tmp_db

    def tearDown(self) -> None:
        lead_db.DB_PATH = self._orig_db_path
        flat_lead_db.DB_PATH = self._orig_flat_db_path
        self._tmpdir.cleanup()

    def test_offer_flows_enrich_to_db_to_excel(self):
        offer_id = lead_db.create_offer(
            1,
            {
                "name": "Test Co web design retainer",
                "product_description": "Monthly website design and maintenance retainer.",
                "value_prop": "A modern site that brings in customers.",
            }
        )

        offer = enrich_leads._resolve_offer(1, offer_id)
        self.assertEqual(offer["id"], offer_id)

        lead = {
            "place_id": "test-place-1",
            "name": "Test Dental",
            "category": "dentist",
            "city": "Boise ID",
            "website": "https://example.com",
        }

        fake_analyze = mock.Mock(return_value=dict(FAKE_ANALYSIS))
        with mock.patch.object(enrich_leads, "analyze_lead", fake_analyze), \
             mock.patch.object(enrich_leads, "scrape_website", return_value="# Test Dental site"), \
             mock.patch.object(enrich_leads, "ensure_email", return_value=("hi@example.com", "regex")), \
             mock.patch.object(enrich_leads, "verify_email", return_value="mx_ok"):
            enriched = enrich_leads.enrich_lead(lead, ["book now"], offer)

        # The fake analyzer was called with THIS offer, and the enriched lead
        # carries its id plus the analysis fields.
        self.assertEqual(fake_analyze.call_args.args[1]["id"], offer_id)
        self.assertEqual(enriched["offer_id"], offer_id)
        self.assertEqual(enriched["score"], 4)
        self.assertEqual(enriched["email_subject"], "quick idea for your site")

        # DB: offer_id lands on the stored lead row.
        lead_db.upsert_leads(1, [enriched])
        rows = lead_db.get_leads(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["offer_id"], offer_id)
        self.assertEqual(rows[0]["name"], "Test Dental")

        # Excel: the deliverable still writes cleanly with the new fields present.
        with mock.patch.object(save_to_excel, "OUTPUTS_DIR", Path(self._tmpdir.name)):
            xlsx_path = save_to_excel.save_to_excel([enriched])
        self.assertTrue(xlsx_path.exists())
        from openpyxl import load_workbook

        ws = load_workbook(xlsx_path).active
        self.assertEqual(ws.max_row, 2)  # header + one lead
        row_text = " ".join(str(c.value) for c in ws[2] if c.value is not None)
        self.assertIn("Test Dental", row_text)


if __name__ == "__main__":
    unittest.main()
