"""Pipeline-at-volume tests (pipeline redesign): lead_db.query_leads, the
/pipeline table view, its live-search fragment, and the bulk stage move.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

import web.app as webapp

try:
    from tests.tenancy import TenancyTestMixin, signup_owner, signup_tenant
except ImportError:
    from tenancy import TenancyTestMixin, signup_owner, signup_tenant

lead_db = webapp.lead_db

OWNER_WS = 1


def seed(workspace_id: int, leads: list[dict], status: str = "new") -> None:
    lead_db.upsert_leads(workspace_id, leads, status=status)


def ids_by_name(workspace_id: int) -> dict[str, int]:
    return {l["name"]: l["id"] for l in lead_db.get_leads(workspace_id)}


class QueryLeadsTestCase(TenancyTestMixin, unittest.TestCase):
    """The query function itself — filters, search, sort, pages, isolation."""

    def setUp(self) -> None:
        self.setup_tenancy()
        self.other_ws = lead_db.create_workspace("Other Co")
        seed(OWNER_WS, [
            {"name": "Alpha Dental", "city": "Boise ID", "score": 5,
             "email": "alpha@dental.example", "category": "dentist"},
            {"name": "Charlie 100% Off Deals", "city": "Boise ID", "score": 4,
             "category": "retail"},
            {"name": "Dana Smith", "entity_type": "person", "company": "Acme",
             "title": "Owner", "email": "dana@acme.example", "score": 2},
            # A BUSINESS carrying company/title: the table never renders those
            # for a business, so search must never match them either.
            {"name": "Echo Signs", "company": "Hidden Co",
             "title": "Hidden Title", "score": 3},
        ])
        seed(OWNER_WS, [
            {"name": "Bravo Plumbing", "city": "Austin TX", "score": 3,
             "category": "plumber"},
        ], status="contacted")
        seed(self.other_ws, [
            {"name": "Zulu Spa", "city": "Boise ID", "score": 5},
        ])

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def test_no_filters_returns_everything_in_workspace(self):
        rows, total = lead_db.query_leads(OWNER_WS)
        self.assertEqual(total, 5)
        self.assertEqual(len(rows), 5)

    def test_status_filter(self):
        rows, total = lead_db.query_leads(OWNER_WS, statuses=("contacted",))
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["name"], "Bravo Plumbing")

    def test_search_matches_the_name_column_case_insensitively(self):
        rows, _ = lead_db.query_leads(OWNER_WS, q="ALPHA")
        self.assertEqual([r["name"] for r in rows], ["Alpha Dental"])
        # A person's grey second line (company · title) is part of the Name
        # column on screen, so it is searchable.
        rows, _ = lead_db.query_leads(OWNER_WS, q="acme")
        self.assertEqual([r["name"] for r in rows], ["Dana Smith"])
        rows, _ = lead_db.query_leads(OWNER_WS, q="owner")
        self.assertEqual([r["name"] for r in rows], ["Dana Smith"])

    def test_search_ignores_columns_that_have_their_own_dropdown(self):
        """City, score and type are filtered by the dropdowns beside the box;
        email and category are not on screen at all. Matching any of them
        would return rows with no visible reason for being there."""
        for term in ("austin",            # Bravo Plumbing's city
                     "dental.example",    # Alpha Dental's email
                     "dentist"):          # Alpha Dental's category
            _, total = lead_db.query_leads(OWNER_WS, q=term)
            self.assertEqual(total, 0, term)

    def test_search_ignores_company_and_title_on_business_rows(self):
        """Those fields only render under a PERSON's name."""
        for term in ("hidden co", "hidden title"):
            _, total = lead_db.query_leads(OWNER_WS, q=term)
            self.assertEqual(total, 0, term)
        # …and the business itself is still findable by its name.
        rows, _ = lead_db.query_leads(OWNER_WS, q="echo")
        self.assertEqual([r["name"] for r in rows], ["Echo Signs"])

    def test_search_escapes_like_wildcards(self):
        rows, _ = lead_db.query_leads(OWNER_WS, q="0% off")
        self.assertEqual([r["name"] for r in rows], ["Charlie 100% Off Deals"])
        rows, total = lead_db.query_leads(OWNER_WS, q="%%%")
        self.assertEqual(total, 0)

    def test_min_score(self):
        rows, total = lead_db.query_leads(OWNER_WS, min_score=4)
        self.assertEqual(total, 2)
        self.assertEqual({r["name"] for r in rows},
                         {"Alpha Dental", "Charlie 100% Off Deals"})

    def test_exact_score_excludes_higher_scores(self):
        # `score` is an EXACT match, unlike min_score's "N and up": asking for
        # 4 must never return the 5.
        rows, total = lead_db.query_leads(OWNER_WS, score=4)
        self.assertEqual([r["name"] for r in rows], ["Charlie 100% Off Deals"])
        self.assertEqual(total, 1)
        self.assertNotIn("Alpha Dental", [r["name"] for r in rows])

    def test_city_filter_is_case_insensitive(self):
        rows, total = lead_db.query_leads(OWNER_WS, city="boise id")
        self.assertEqual(total, 2)

    def test_entity_type_person_and_business(self):
        rows, _ = lead_db.query_leads(OWNER_WS, entity_type="person")
        self.assertEqual([r["name"] for r in rows], ["Dana Smith"])
        rows, total = lead_db.query_leads(OWNER_WS, entity_type="business")
        self.assertEqual(total, 4)
        self.assertNotIn("Dana Smith", [r["name"] for r in rows])

    def test_sort_score_desc_and_unknown_sort_falls_back(self):
        rows, _ = lead_db.query_leads(OWNER_WS, sort="score_desc")
        self.assertEqual(rows[0]["name"], "Alpha Dental")
        rows_junk, _ = lead_db.query_leads(OWNER_WS, sort="junk; DROP TABLE")
        rows_default, _ = lead_db.query_leads(OWNER_WS, sort="updated_desc")
        self.assertEqual([r["id"] for r in rows_junk],
                         [r["id"] for r in rows_default])

    def test_pagination(self):
        rows1, total = lead_db.query_leads(OWNER_WS, sort="name_asc",
                                           page=1, per_page=3)
        rows2, _ = lead_db.query_leads(OWNER_WS, sort="name_asc",
                                       page=2, per_page=3)
        self.assertEqual(total, 5)
        self.assertEqual(len(rows1), 3)
        self.assertEqual(len(rows2), 2)
        names = [r["name"] for r in rows1] + [r["name"] for r in rows2]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_workspace_isolation(self):
        rows, total = lead_db.query_leads(self.other_ws)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["name"], "Zulu Spa")
        rows, _ = lead_db.query_leads(OWNER_WS, q="zulu")
        self.assertEqual(rows, [])

    def test_distinct_cities(self):
        self.assertEqual(lead_db.distinct_cities(OWNER_WS),
                         ["Austin TX", "Boise ID"])
        self.assertEqual(lead_db.distinct_cities(self.other_ws), ["Boise ID"])


class TableViewTestCase(TenancyTestMixin, unittest.TestCase):
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
        seed(OWNER_WS, [{"name": "Golf Winner", "city": "Boise ID"}],
             status="won")
        seed(self.tenant_ws, [{"name": "Foreign Spa", "city": "Boise ID"}])

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def test_table_lists_own_leads_only(self):
        r = self.client.get("/pipeline")
        self.assertEqual(r.status_code, 200)
        for name in ("Alpha Dental", "Bravo Plumbing", "Golf Winner"):
            self.assertIn(name, r.text)
        self.assertNotIn("Foreign Spa", r.text)

    def test_search_round_trips(self):
        r = self.client.get("/pipeline", params={"q": "alpha"})
        self.assertIn("Alpha Dental", r.text)
        self.assertNotIn("Bravo Plumbing", r.text)
        self.assertIn('value="alpha"', r.text)  # the box keeps the term

    def test_status_filter(self):
        r = self.client.get("/pipeline", params={"status": "won"})
        self.assertIn("Golf Winner", r.text)
        self.assertNotIn("Alpha Dental", r.text)

    def test_suppressed_filter_shows_hidden_statuses(self):
        ids = ids_by_name(OWNER_WS)
        lead_db.set_status(OWNER_WS, ids["Bravo Plumbing"], "unsubscribed")
        r = self.client.get("/pipeline")
        self.assertNotIn("Bravo Plumbing", r.text)   # gone from All active
        r = self.client.get("/pipeline", params={"status": "suppressed"})
        self.assertIn("Bravo Plumbing", r.text)
        self.assertNotIn("Alpha Dental", r.text)

    def test_pagination_splits_pages(self):
        seed(OWNER_WS, [{"name": f"Page Lead {n:03d}", "city": "Boise ID"}
                        for n in range(60)])
        r = self.client.get("/pipeline")
        self.assertEqual(r.text.count('class="rowchk"'), 50)
        self.assertIn("Page 1 of 2", r.text)
        r2 = self.client.get("/pipeline", params={"page": 2})
        self.assertEqual(r2.text.count('class="rowchk"'), 13)
        self.assertIn("Page 2 of 2", r2.text)

    def test_snapshot_badges_filter_and_stay_off_the_home_page(self):
        """The status breakdown lives here now, not on Home, and each badge
        is a filter link into this same table."""
        r = self.client.get("/pipeline")
        self.assertIn('class="snapshot"', r.text)
        self.assertIn('href="/pipeline?status=new"', r.text)
        self.assertNotIn("Pipeline snapshot", self.client.get("/").text)

    def test_snapshot_marks_the_current_status(self):
        r = self.client.get("/pipeline", params={"status": "new"})
        self.assertIn('badge badge-new on"', r.text)

    def test_snapshot_stays_out_of_the_partial(self):
        r = self.client.get("/pipeline", params={"partial": 1})
        self.assertNotIn("snapshot", r.text)

    def test_partial_returns_rows_only(self):
        """What the search box fetches per keystroke: the results fragment,
        no page chrome — swapping a whole document in would nest one page
        inside another."""
        r = self.client.get("/pipeline", params={"q": "alpha", "partial": 1})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Alpha Dental", r.text)
        self.assertNotIn("Bravo Plumbing", r.text)
        for chrome in ("<html", "<body", "Replyworthy", "filterbar"):
            self.assertNotIn(chrome, r.text)

    def test_partial_carries_the_filters_into_the_bulk_form(self):
        """The re-rendered `back` field is what returns a bulk move to the
        view the user was actually looking at."""
        r = self.client.get("/pipeline", params={"q": "alpha", "partial": 1})
        self.assertIn('name="back" value="/pipeline?q=alpha&amp;page=1"', r.text)

    def test_old_table_url_redirects_and_keeps_the_query(self):
        r = self.client.get("/pipeline/table", params={"status": "won"},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        self.assertEqual(r.headers["location"], "/pipeline?status=won")

    def test_partial_is_login_gated(self):
        """A signed-out fragment request redirects like any other page. The
        search JS keys off exactly this: fetch follows the 303, sees a
        redirected response, and sends the user to the login page instead of
        swapping it into the table."""
        anon = TestClient(webapp.app)
        r = anon.get("/pipeline", params={"partial": 1}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login", r.headers["location"])

    def test_board_is_gone(self):
        r = self.client.get("/pipeline")
        for gone in ("kb-board", "kb-card", "Sortable", 'class="seg"'):
            self.assertNotIn(gone, r.text)


class BulkStatusTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self.client = TestClient(webapp.app)
        signup_owner(self.client)
        self.tenant = TestClient(webapp.app)
        signup_tenant(self.tenant)
        self.tenant_ws = lead_db.get_user_by_email(
            "tenant@test-co.example")["workspace_id"]
        seed(OWNER_WS, [
            {"name": "Alpha Dental", "email": "alpha@dental.example"},
            {"name": "Bravo Plumbing", "email": "bravo@plumb.example"},
            {"name": "Charlie NoMail"},
        ])
        seed(self.tenant_ws, [{"name": "Foreign Spa"}])
        self.ids = ids_by_name(OWNER_WS)
        self.foreign_id = ids_by_name(self.tenant_ws)["Foreign Spa"]

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def _bulk(self, ids, status, back="/pipeline?status=new"):
        return self.client.post("/pipeline/bulk-status", data={
            "lead_id": ids, "status": status, "back": back,
        }, follow_redirects=False)

    def test_bulk_move_updates_all_and_reports_count(self):
        r = self._bulk([self.ids["Alpha Dental"], self.ids["Bravo Plumbing"]],
                       "contacted")
        self.assertEqual(r.status_code, 303)
        self.assertIn("Moved%202%20leads", r.headers["location"])
        self.assertIn("/pipeline?status=new", r.headers["location"])
        for name in ("Alpha Dental", "Bravo Plumbing"):
            self.assertEqual(
                lead_db.get_lead(OWNER_WS, self.ids[name])["status"],
                "contacted")
        self.assertEqual(
            lead_db.get_lead(OWNER_WS, self.ids["Charlie NoMail"])["status"],
            "new")

    def test_bulk_not_interested_suppresses_in_workspace_only(self):
        self._bulk([self.ids["Alpha Dental"]], "not_interested")
        self.assertTrue(lead_db.is_suppressed(OWNER_WS, "alpha@dental.example"))
        # NOT hard: the other workspace may still contact this address.
        self.assertFalse(
            lead_db.is_suppressed(self.tenant_ws, "alpha@dental.example"))

    def test_bulk_unsubscribed_goes_hard_everywhere(self):
        self._bulk([self.ids["Bravo Plumbing"]], "unsubscribed")
        self.assertTrue(
            lead_db.is_suppressed(self.tenant_ws, "bravo@plumb.example"))

    def test_bulk_cancels_pending_sends(self):
        con = lead_db.connect()
        try:
            with con.tx():
                con.execute(
                    "INSERT INTO sends (workspace_id, lead_id, campaign_id, "
                    "step_number, scheduled_at, status) "
                    "VALUES (?, ?, 1, 1, '2026-07-20 10:00:00', 'scheduled')",
                    (OWNER_WS, self.ids["Alpha Dental"]),
                )
        finally:
            con.close()
        self._bulk([self.ids["Alpha Dental"]], "won")
        con = lead_db.connect()
        try:
            row = con.execute(
                "SELECT status FROM sends WHERE workspace_id = ? AND lead_id = ?",
                (OWNER_WS, self.ids["Alpha Dental"])).fetchone()
        finally:
            con.close()
        self.assertEqual(row["status"], "cancelled")

    def test_foreign_lead_id_is_skipped_and_unchanged(self):
        r = self._bulk([self.foreign_id, self.ids["Alpha Dental"]], "contacted")
        self.assertEqual(r.status_code, 303)
        self.assertIn("Moved%201%20lead", r.headers["location"])
        self.assertEqual(
            lead_db.get_lead(self.tenant_ws, self.foreign_id)["status"], "new")

    def test_invalid_status_changes_nothing(self):
        r = self._bulk([self.ids["Alpha Dental"]], "hacked_status")
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("msg=", r.headers["location"])
        self.assertEqual(
            lead_db.get_lead(OWNER_WS, self.ids["Alpha Dental"])["status"],
            "new")

    def test_external_back_url_is_rejected(self):
        r = self._bulk([self.ids["Alpha Dental"]], "contacted",
                       back="https://evil.example/phish")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(
            r.headers["location"].startswith("/pipeline"))


if __name__ == "__main__":
    unittest.main()
