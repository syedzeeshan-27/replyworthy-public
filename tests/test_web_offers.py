"""Web-layer tests for the offer-profiles UI (V2 Stage 1, Task 4).

Stdlib unittest + FastAPI TestClient. Each test points the app's lead_db at a
fresh temp DB, so nothing touches data/leads.db. web.app imports lead_db from
the tools/ dir (as the module `lead_db`), so we patch DB_PATH on that exact
module object — `web.app.lead_db` — which the route handlers read at request
time. Data is created through the same object so it lands in the temp DB.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import web.app as webapp

lead_db = webapp.lead_db  # the exact module the app's routes use

_VALID = {
    "name": "Booking widget",
    "product_description": "A booking widget for salon websites that lets "
                           "clients self-book and cuts no-shows.",
    "value_prop": "Fewer no-shows, more booked chairs.",
}


class WebOffersTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {
            "SESSION_SECRET": "test-secret",
            "UNSUBSCRIBE_SECRET": "test-unsub",
        })
        self._env.start()
        os.environ.pop("DATABASE_URL", None)
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db_path = lead_db.DB_PATH
        lead_db.DB_PATH = Path(self._tmp.name) / "leads.db"
        self.client = TestClient(webapp.app)
        # Routes are session-gated since V2 Stage 4 — sign in as the owner.
        self.client.post("/signup", data={
            "email": "owner@example.com", "password": "ownerpass123",
            "workspace_name": "Owner HQ",
        }, follow_redirects=False)

    def tearDown(self) -> None:
        lead_db.DB_PATH = self._orig_db_path
        self._tmp.cleanup()
        self._env.stop()

    # --- /offers listing -------------------------------------------------

    def test_offers_page_lists_seeded_default(self):
        r = self.client.get("/offers")
        self.assertEqual(r.status_code, 200)
        self.assertIn("SMS text-back bot", r.text)
        # default badge context is rendered on the default row
        self.assertIn("default", r.text)

    # --- create ----------------------------------------------------------

    def test_create_offer_appears_in_list(self):
        r = self.client.post("/offers/create", data=_VALID, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        listing = self.client.get("/offers")
        self.assertIn("Booking widget", listing.text)
        self.assertEqual(len(lead_db.list_offers(1)), 2)

    def test_create_blank_name_is_handled_no_500(self):
        bad = {**_VALID, "name": ""}
        r = self.client.post("/offers/create", data=bad, follow_redirects=False)
        self.assertEqual(r.status_code, 400)  # handled, not a 500
        self.assertIn("required", r.text.lower())
        # nothing created — only the seeded default remains
        self.assertEqual(len(lead_db.list_offers(1)), 1)

    # --- edit ------------------------------------------------------------

    def test_edit_updates_field(self):
        oid = lead_db.create_offer(1, dict(_VALID))
        payload = {**_VALID, "name": "Renamed widget"}
        r = self.client.post(f"/offers/{oid}/update", data=payload,
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(lead_db.get_offer(1, oid)["name"], "Renamed widget")

    # --- set default -----------------------------------------------------

    def test_set_default_swaps(self):
        oid = lead_db.create_offer(1, dict(_VALID))
        old_default = lead_db.get_default_offer(1)
        self.assertNotEqual(old_default["id"], oid)
        r = self.client.post(f"/offers/{oid}/default", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(lead_db.get_default_offer(1)["id"], oid)
        self.assertEqual(lead_db.get_offer(1, old_default["id"])["is_default"], 0)

    # --- delete ----------------------------------------------------------

    def test_delete_nondefault_hidden_from_list(self):
        oid = lead_db.create_offer(1, dict(_VALID))
        r = self.client.post(f"/offers/{oid}/delete", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertNotIn("Booking widget", self.client.get("/offers").text)
        # soft-deleted, not gone
        self.assertEqual(lead_db.get_offer(1, oid)["deleted"], 1)

    def test_delete_default_shows_friendly_error_and_stays_listed(self):
        default = lead_db.get_default_offer(1)
        r = self.client.post(f"/offers/{default['id']}/delete",
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("cannot", r.headers["location"].lower())
        # still present and still the (undeleted) default
        self.assertIn(default["name"], self.client.get("/offers").text)
        self.assertEqual(lead_db.get_offer(1, default["id"])["deleted"], 0)

    # --- dashboard generate form ----------------------------------------

    def test_dashboard_has_offer_select_with_default_selected(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('name="offer_id"', r.text)
        self.assertIn("SMS text-back bot", r.text)
        self.assertIn("selected", r.text)

    # --- lead detail -----------------------------------------------------

    def test_lead_detail_shows_soft_deleted_offer_name(self):
        oid = lead_db.create_offer(1, {**_VALID, "name": "Legacy offer"})
        lead_db.upsert_leads(1, [{"name": "Acme Co", "place_id": "p1",
                               "offer_id": oid}])
        lead_db.delete_offer(1, oid)  # soft delete (non-default, allowed)
        lead_id = lead_db.get_leads(1)[0]["id"]
        r = self.client.get(f"/lead/{lead_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Legacy offer", r.text)
        self.assertIn("Scored against", r.text)

    # --- /jobs pass-through (no real pipeline) ---------------------------

    def test_jobs_passes_offer_id_to_runner(self):
        # A real offer in THIS workspace passes through to the runner.
        oid = lead_db.create_offer(1, {**_VALID, "name": "Job offer"})
        with mock.patch.object(webapp, "start_job", return_value="fake123") as m:
            r = self.client.post(
                "/jobs",
                data={"category": "dentist", "city": "Austin TX",
                      "limit": "10", "offer_id": str(oid)},
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 303)
        m.assert_called_once()
        passed = m.call_args.args + tuple(m.call_args.kwargs.values())
        self.assertIn(oid, passed)

    def test_jobs_drops_foreign_offer_id(self):
        # An offer id that isn't in this workspace must not stick (Stage 4).
        with mock.patch.object(webapp, "start_job", return_value="fake123") as m:
            r = self.client.post(
                "/jobs",
                data={"category": "dentist", "city": "Austin TX",
                      "limit": "10", "offer_id": "9999"},
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 303)
        passed = m.call_args.args + tuple(m.call_args.kwargs.values())
        self.assertNotIn(9999, passed)


if __name__ == "__main__":
    unittest.main()
