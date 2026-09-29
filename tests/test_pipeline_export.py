"""Pipeline export (CSV + Excel of the currently-filtered leads).

Covers the three layers the feature added:
  * tools/export_leads.py  — the byte builders (columns, BOM, person layout).
  * lead_db.export_leads    — the unpaginated, workspace-scoped filter query.
  * GET /pipeline/export     — the download route (headers, filter, tenancy).

Run (from the repo root):
    python -m unittest tests.test_pipeline_export -v
"""
from __future__ import annotations

import csv
import io
import unittest

from fastapi.testclient import TestClient
from openpyxl import load_workbook

import web.app as webapp

try:
    from tests.tenancy import TenancyTestMixin, signup_owner, signup_tenant
except ImportError:
    from tenancy import TenancyTestMixin, signup_owner, signup_tenant

import export_leads  # noqa: E402  (tools/ is on sys.path after web.app import)
from save_to_excel import COLUMNS, LIST_COLUMNS  # noqa: E402

lead_db = webapp.lead_db

OWNER_WS = 1
COL_HEADERS = [label for label, _key, _width in COLUMNS]
COL_KEYS = [key for _label, key, _width in COLUMNS]
LIST_HEADERS = [label for label, _key, _width in LIST_COLUMNS]


def seed(workspace_id: int, leads: list[dict], status: str = "new") -> None:
    lead_db.upsert_leads(workspace_id, leads, status=status)


class ExportFormatsTestCase(unittest.TestCase):
    """The pure byte builders — no DB, just rendering leads → CSV/XLSX."""

    BUSINESS = [
        {"name": "Alpha Dental", "phone": "555-1000",
         "email": "alpha@dental.example", "website": "alpha.example",
         "area": "Downtown", "rating": 4.7, "reviews_count": 120,
         "pain_points": ["slow site", "no booking"],
         "email_subject": "Hi", "email_body": "Body", "score": 5},
        {"name": "Bravo Plumbing", "score": 3, "pain_points": []},
    ]

    def _csv_rows(self, leads: list[dict]) -> list[list[str]]:
        text = export_leads.to_csv_bytes(leads).decode("utf-8-sig")
        return list(csv.reader(text.splitlines()))

    def test_csv_header_matches_deliverable_columns(self):
        self.assertEqual(self._csv_rows(self.BUSINESS)[0], COL_HEADERS)

    def test_csv_has_utf8_bom_so_excel_opens_it_right(self):
        self.assertTrue(
            export_leads.to_csv_bytes(self.BUSINESS).startswith(b"\xef\xbb\xbf"))

    def test_csv_sorts_by_score_desc_and_joins_pain_points(self):
        rows = self._csv_rows(self.BUSINESS)
        self.assertEqual(rows[1][0], "Alpha Dental")   # score 5 first
        self.assertEqual(rows[2][0], "Bravo Plumbing")  # score 3
        pain = rows[1][COL_KEYS.index("pain_points")]
        self.assertEqual(pain, "slow site; no booking")

    def test_csv_adds_person_columns_when_a_person_is_present(self):
        people = [{"name": "Dana Smith", "entity_type": "person",
                   "title": "Owner", "company": "Acme",
                   "linkedin_url": "linkedin.com/in/dana", "score": 4}]
        header = self._csv_rows(people)[0]
        for col in ("Name", "Title", "Company", "LinkedIn"):
            self.assertIn(col, header)

    def test_empty_export_is_header_only(self):
        self.assertEqual(len(self._csv_rows([])), 1)

    def test_xlsx_bytes_load_back_with_the_right_header(self):
        data = export_leads.to_xlsx_bytes(self.BUSINESS)
        self.assertTrue(data.startswith(b"PK"))     # xlsx is a zip
        ws = load_workbook(io.BytesIO(data))["Leads"]
        self.assertEqual([c.value for c in ws[1]], COL_HEADERS)
        self.assertEqual(ws.cell(row=2, column=1).value, "Alpha Dental")


class ListLayoutTestCase(unittest.TestCase):
    """The free "just the leads, no emails" deliverable — eight columns
    (socials included since the actor returns them pre-enrichment), none of
    them AI-written, produced from what Apify returns before enrichment."""

    LEADS = [
        {"name": "Alpha Dental", "phone": "555-1000", "website": "alpha.example",
         "area": "Downtown", "rating": 4.7, "reviews_count": 120,
         # These AI fields exist on the dict but must NOT reach the list file.
         "email": "alpha@dental.example", "pain_points": ["slow site"],
         "email_subject": "Hi", "email_body": "Body", "score": 5},
        {"name": "Bravo Plumbing", "phone": "555-2000", "website": "bravo.example",
         "area": "Eastside", "rating": 4.1, "reviews_count": 30},
    ]

    def _csv_rows(self, leads, layout):
        text = export_leads.to_csv_bytes(leads, layout=layout).decode("utf-8-sig")
        return list(csv.reader(text.splitlines()))

    def test_list_layout_has_exactly_the_list_columns(self):
        self.assertEqual(self._csv_rows(self.LEADS, "list")[0], LIST_HEADERS)
        self.assertEqual(len(LIST_HEADERS), 8)

    def test_list_layout_omits_every_ai_column(self):
        header = self._csv_rows(self.LEADS, "list")[0]
        for ai_col in ("Email", "Pain Points", "Email Subject",
                       "Email Body", "Score"):
            self.assertNotIn(ai_col, header)

    def test_list_xlsx_header_is_the_list_columns(self):
        data = export_leads.to_xlsx_bytes(self.LEADS, layout="list")
        ws = load_workbook(io.BytesIO(data))["Leads"]
        self.assertEqual([c.value for c in ws[1]], LIST_HEADERS)

    def test_auto_layout_is_unchanged_by_the_new_param(self):
        # Default path must still produce the full 11-column business layout.
        self.assertEqual(self._csv_rows(self.LEADS, "auto")[0], COL_HEADERS)

    def test_preview_rows_match_the_file(self):
        headers, rows = export_leads.preview_rows(self.LEADS)
        self.assertEqual(headers, LIST_HEADERS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "Alpha Dental")
        self.assertEqual(len(rows[0]), len(LIST_HEADERS))

    def test_preview_rows_limit(self):
        many = [dict(self.LEADS[0], name=f"Biz {i}") for i in range(25)]
        _, rows = export_leads.preview_rows(many, limit=10)
        self.assertEqual(len(rows), 10)

    def test_preview_rows_render_area_and_reviews(self):
        _, rows = export_leads.preview_rows(self.LEADS)
        row = rows[0]
        self.assertEqual(row[LIST_HEADERS.index("Area")], "Downtown")
        self.assertEqual(row[LIST_HEADERS.index("Number of Reviews")], 120)


class ExportLeadsQueryTestCase(TenancyTestMixin, unittest.TestCase):
    """lead_db.export_leads — unpaginated, filtered, workspace-scoped."""

    def setUp(self) -> None:
        self.setup_tenancy()
        self.other_ws = lead_db.create_workspace("Other Co")
        seed(OWNER_WS, [
            {"name": "Alpha Dental", "city": "Boise ID", "score": 5},
            {"name": "Bravo Plumbing", "city": "Austin TX", "score": 3},
            {"name": "Gamma Cafe", "city": "Boise ID", "score": 5},
        ])
        seed(OWNER_WS, [{"name": "Delta Won", "score": 5}], status="won")
        seed(self.other_ws, [{"name": "Zulu Spa", "score": 5}])

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def test_returns_all_matching_across_pages(self):
        # query_leads caps a page at 50; export must return everything.
        seed(OWNER_WS, [{"name": f"L{n:03d}", "city": "Boise ID"}
                        for n in range(60)])
        rows = lead_db.export_leads(OWNER_WS)
        self.assertEqual(len(rows), 64)   # 3 + 1 won + 60, all statuses

    def test_min_score_five_returns_only_fives(self):
        names = {r["name"] for r in lead_db.export_leads(OWNER_WS, min_score=5)}
        self.assertEqual(names, {"Alpha Dental", "Gamma Cafe", "Delta Won"})
        self.assertNotIn("Bravo Plumbing", names)   # score 3

    def test_exact_score_excludes_higher(self):
        # `score` is EXACT: score=3 returns only the 3s, not the 4s/5s that
        # min_score=3 (the "N and up" range) would sweep in.
        exact = {r["name"] for r in lead_db.export_leads(OWNER_WS, score=3)}
        self.assertEqual(exact, {"Bravo Plumbing"})
        at_least = {r["name"] for r in lead_db.export_leads(OWNER_WS, min_score=3)}
        self.assertEqual(at_least, {"Alpha Dental", "Bravo Plumbing",
                                    "Gamma Cafe", "Delta Won"})

    def test_workspace_isolation(self):
        self.assertEqual(
            [r["name"] for r in lead_db.export_leads(self.other_ws)],
            ["Zulu Spa"])
        self.assertNotIn(
            "Zulu Spa", {r["name"] for r in lead_db.export_leads(OWNER_WS)})

    def test_limit_is_respected(self):
        self.assertEqual(len(lead_db.export_leads(OWNER_WS, limit=2)), 2)

    def test_requires_workspace_id(self):
        with self.assertRaises(TypeError):
            lead_db.export_leads()


class ExportRouteTestCase(TenancyTestMixin, unittest.TestCase):
    """GET /pipeline/export — download headers, filter application, tenancy."""

    def setUp(self) -> None:
        self.setup_tenancy()
        self.client = TestClient(webapp.app)
        signup_owner(self.client)
        self.tenant = TestClient(webapp.app)
        signup_tenant(self.tenant)
        self.tenant_ws = lead_db.get_user_by_email(
            "tenant@test-co.example")["workspace_id"]
        seed(OWNER_WS, [
            {"name": "Alpha Dental", "city": "Boise ID", "score": 5,
             "email": "alpha@dental.example"},
            {"name": "Bravo Plumbing", "city": "Austin TX", "score": 3},
        ])
        seed(self.tenant_ws, [{"name": "Foreign Spa", "score": 5}])

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def test_xlsx_download_headers(self):
        r = self.client.get("/pipeline/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml.sheet", r.headers["content-type"])
        cd = r.headers["content-disposition"]
        self.assertIn("attachment", cd)
        self.assertTrue(cd.rstrip('"').endswith(".xlsx"))
        self.assertTrue(r.content.startswith(b"PK"))

    def test_csv_download_headers_and_content(self):
        r = self.client.get("/pipeline/export", params={"fmt": "csv"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn(".csv", r.headers["content-disposition"])
        text = r.content.decode("utf-8-sig")
        self.assertIn("Alpha Dental", text)
        self.assertIn("Bravo Plumbing", text)

    def test_min_score_5_filters_the_download(self):
        r = self.client.get("/pipeline/export",
                            params={"fmt": "csv", "min_score": 5})
        text = r.content.decode("utf-8-sig")
        self.assertIn("Alpha Dental", text)       # score 5 stays
        self.assertNotIn("Bravo Plumbing", text)  # score 3 filtered out

    def test_exact_score_filters_the_download(self):
        # The dropdown's exact filter: score=3 keeps the 3, drops the 5.
        text = self.client.get(
            "/pipeline/export",
            params={"fmt": "csv", "score": 3}).content.decode("utf-8-sig")
        self.assertIn("Bravo Plumbing", text)     # score 3 kept
        self.assertNotIn("Alpha Dental", text)    # score 5 excluded

    def test_export_is_workspace_scoped(self):
        own = self.client.get(
            "/pipeline/export", params={"fmt": "csv"}).content.decode("utf-8-sig")
        self.assertNotIn("Foreign Spa", own)      # never another tenant's lead
        theirs = self.tenant.get(
            "/pipeline/export", params={"fmt": "csv"}).content.decode("utf-8-sig")
        self.assertIn("Foreign Spa", theirs)
        self.assertNotIn("Alpha Dental", theirs)

    def test_unknown_fmt_defaults_to_xlsx(self):
        r = self.client.get("/pipeline/export", params={"fmt": "pdf"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml.sheet", r.headers["content-type"])

    def test_export_is_login_gated(self):
        anon = TestClient(webapp.app)
        r = anon.get("/pipeline/export", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login", r.headers["location"])

    def test_export_buttons_render_on_the_pipeline_page(self):
        r = self.client.get("/pipeline")
        self.assertIn('id="export-xlsx"', r.text)
        self.assertIn('id="export-csv"', r.text)
        self.assertIn("/pipeline/export?fmt=xlsx", r.text)   # unfiltered

    def test_export_href_carries_the_current_filter(self):
        r = self.client.get("/pipeline", params={"min_score": 5})
        # The initial (no-JS) href already reflects the filter, so a straight
        # click exports the five-star leads even before any live interaction.
        self.assertIn("/pipeline/export?min_score=5&fmt=xlsx", r.text)
        self.assertIn("/pipeline/export?min_score=5&fmt=csv", r.text)

    def test_export_href_carries_the_exact_score(self):
        r = self.client.get("/pipeline", params={"score": 4})
        self.assertIn("/pipeline/export?score=4&fmt=xlsx", r.text)
        self.assertIn("/pipeline/export?score=4&fmt=csv", r.text)

    def test_score_dropdown_is_exact_not_a_range(self):
        r = self.client.get("/pipeline", params={"score": 4})
        self.assertIn('name="score"', r.text)
        self.assertIn("★ 4 only", r.text)
        self.assertNotIn("★ 4+", r.text)              # the old "and up" is gone
        self.assertNotIn("★ 5+", r.text)
        self.assertIn('value="4" selected', r.text)   # the pick is reflected


if __name__ == "__main__":
    unittest.main()
