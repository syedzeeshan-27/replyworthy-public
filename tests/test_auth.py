"""Auth tests: signup -> confirm email -> login -> session -> logout, cookie
flags, the password-reset flow, who becomes admin, and the Supabase client's
error mapping. Supabase itself is the in-memory fake (tests/fake_supabase.py):
zero network, zero real mail.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

import web.app as webapp
import web.auth as auth

try:
    from tests import fake_supabase
    from tests.tenancy import TenancyTestMixin, OWNER, login, signup, signup_owner
except ImportError:  # unittest discover -s tests puts tests/ itself on sys.path
    import fake_supabase
    from tenancy import TenancyTestMixin, OWNER, login, signup, signup_owner

import supabase_auth

lead_db = webapp.lead_db
REAL_CALL = fake_supabase.REAL_CALL


class AuthTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()
        self.client = TestClient(webapp.app)

    def tearDown(self) -> None:
        self.teardown_tenancy()

    # --- signup ------------------------------------------------------------

    def test_first_signup_is_admin_and_claims_workspace_1(self):
        r = signup_owner(self.client)
        self.assertEqual(r.status_code, 303)
        user = lead_db.get_user_by_email(OWNER["email"])
        self.assertEqual(user["role"], "admin")
        self.assertEqual(user["workspace_id"], lead_db.OWNER_WORKSPACE_ID)
        self.assertEqual(lead_db.get_workspace(1)["name"], "Owner HQ")

    def test_second_signup_is_member_with_fresh_workspace(self):
        signup_owner(self.client)
        c2 = TestClient(webapp.app)
        signup(c2, "b@test.example", "password123", "Test Co")
        user = lead_db.get_user_by_email("b@test.example")
        self.assertEqual(user["role"], "member")
        self.assertNotEqual(user["workspace_id"], 1)
        self.assertEqual(lead_db.get_workspace(user["workspace_id"])["name"], "Test Co")
        # ...and the new workspace got its own seeded default offer.
        self.assertIsNotNone(lead_db.get_default_offer(user["workspace_id"]))

    def test_no_password_is_stored_locally(self):
        signup_owner(self.client)
        user = lead_db.get_user_by_email(OWNER["email"])
        self.assertEqual(user["password_hash"], "")
        self.assertTrue(user["auth_user_id"])

    def test_signup_rejects_duplicate_short_password_and_bad_email(self):
        signup_owner(self.client)
        dup = signup(TestClient(webapp.app), OWNER["email"], "password123")
        self.assertEqual(dup.status_code, 400)
        short = signup(TestClient(webapp.app), "x@y.example", "abc")
        self.assertEqual(short.status_code, 400)
        bad = signup(TestClient(webapp.app), "not-an-email", "password123")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(lead_db.count_users(), 1)

    # --- login / logout / session -------------------------------------------

    def test_login_logout_cycle(self):
        signup_owner(self.client)
        self.client.post("/logout")
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login", r.headers["location"])

        bad = login(self.client, OWNER["email"], "wrong-password")
        self.assertEqual(bad.status_code, 401)

        good = login(self.client, OWNER["email"], OWNER["password"])
        self.assertEqual(good.status_code, 303)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(lead_db.count_users(), 1)  # login made no new account

    def test_session_cookie_flags(self):
        r = signup_owner(self.client)
        cookie = r.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertNotIn("secure", cookie)  # BASE_URL is plain http locally

    def test_session_cookie_secure_on_https_base_url(self):
        with mock.patch.dict("os.environ", {"BASE_URL": "https://app.example.com"}):
            r = signup(TestClient(webapp.app), "s@x.example", "password123")
        self.assertIn("secure", r.headers["set-cookie"].lower())

    def test_tampered_session_token_is_rejected(self):
        signup_owner(self.client)
        c = TestClient(webapp.app)
        token = auth.make_session_token(1)
        c.cookies.set(auth.SESSION_COOKIE, token[:-2] + "zz")
        r = c.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)  # bounced to /login

    def test_json_endpoints_return_401_not_redirect(self):
        r = self.client.get("/jobs/abc.json")
        self.assertEqual(r.status_code, 401)

    def test_login_next_cannot_leave_the_site(self):
        signup_owner(self.client)
        for bad in ("//evil.example", "/\\evil.example", "https://evil.example",
                    "/\tevil.example"):
            self.client.post("/logout")
            r = self.client.post("/login", data={
                "email": OWNER["email"], "password": OWNER["password"],
                "next": bad}, follow_redirects=False)
            self.assertEqual(r.headers["location"], "/", repr(bad))
        self.client.post("/logout")
        r = self.client.post("/login", data={
            "email": OWNER["email"], "password": OWNER["password"],
            "next": "/pipeline"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/pipeline")

    def test_login_page_says_so_when_supabase_keys_are_missing(self):
        with mock.patch.dict("os.environ", {"SUPABASE_URL": ""}), \
                mock.patch.object(supabase_auth, "_call", REAL_CALL):
            r = login(self.client, OWNER["email"], OWNER["password"])
        self.assertEqual(r.status_code, 503)
        self.assertIn("set up yet", r.text)

    # --- password reset --------------------------------------------------------

    def test_reset_flow_end_to_end_and_single_use(self):
        signup_owner(self.client)
        self.client.post("/logout")

        r = self.client.post("/forgot", data={"email": OWNER["email"]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("on its way", r.text)
        mail = fake_supabase.outbox()[-1]
        self.assertEqual((mail["kind"], mail["email"]), ("recovery", OWNER["email"]))
        link = f"/auth/confirm?token_hash={mail['token_hash']}&type=recovery"

        # The emailed link lands on the new-password form...
        r = self.client.get(link, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/reset"))
        self.assertIn("httponly", r.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/reset").status_code, 200)
        # ...a too-short password is refused, a real one is accepted...
        self.assertEqual(self.client.post(
            "/reset", data={"password": "abc"}).status_code, 400)
        r = self.client.post("/reset", data={"password": "brand-new-pass1"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        # ...the old password is dead, the new one works...
        self.assertEqual(login(self.client, OWNER["email"],
                               OWNER["password"]).status_code, 401)
        self.assertEqual(login(self.client, OWNER["email"],
                               "brand-new-pass1").status_code, 303)
        # ...and the link is single-use.
        self.assertEqual(TestClient(webapp.app).get(link).status_code, 400)

    def test_reset_form_needs_the_emailed_link(self):
        self.assertEqual(self.client.get("/reset").status_code, 400)
        self.assertEqual(self.client.post(
            "/reset", data={"password": "brand-new-pass1"}).status_code, 400)

    def test_forgot_never_reveals_whether_account_exists(self):
        r = self.client.post("/forgot", data={"email": "ghost@nowhere.example"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("on its way", r.text)  # same answer as a real account
        self.assertEqual(fake_supabase.outbox(), [])

    # --- admin switcher gate --------------------------------------------------

    def test_member_cannot_use_admin_switch(self):
        signup_owner(self.client)
        c2 = TestClient(webapp.app)
        signup(c2, "member@test.example", "password123", "Test Co")
        r = c2.post("/admin/switch", data={"workspace_id": 1})
        self.assertEqual(r.status_code, 403)

    def test_admin_switch_changes_acting_workspace(self):
        signup_owner(self.client)
        c2 = TestClient(webapp.app)
        signup(c2, "member@test.example", "password123", "Test Co")
        ws2 = lead_db.get_user_by_email("member@test.example")["workspace_id"]
        lead_db.upsert_leads(ws2, [{"name": "Bravo Salon", "place_id": "b1"}])

        r = self.client.get("/pipeline")
        self.assertNotIn("Bravo Salon", r.text)
        self.assertEqual(self.client.post("/admin/switch",
                                          data={"workspace_id": ws2},
                                          follow_redirects=False).status_code, 303)
        r = self.client.get("/pipeline")
        self.assertIn("Bravo Salon", r.text)
        # and back
        self.client.post("/admin/switch", data={"workspace_id": 1})
        self.assertNotIn("Bravo Salon", self.client.get("/pipeline").text)


class ConfirmEmailTestCase(TenancyTestMixin, unittest.TestCase):
    """The real flow: a Supabase project with "Confirm email" switched on."""

    def setUp(self) -> None:
        self.setup_tenancy()
        fake_supabase.CONFIRM_EMAIL = True
        self.client = TestClient(webapp.app)

    def tearDown(self) -> None:
        fake_supabase.CONFIRM_EMAIL = False
        self.teardown_tenancy()

    def _confirm_link(self) -> str:
        mail = fake_supabase.outbox()[-1]
        self.assertEqual(mail["kind"], "email")
        return f"/auth/confirm?token_hash={mail['token_hash']}&type=email"

    def test_signup_waits_for_the_email_link(self):
        r = signup(self.client, "new@test.example", "password123", "New Co")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Check your inbox", r.text)
        self.assertNotIn("set-cookie", r.headers)
        # Nothing local yet: no account, no workspace, no trial credits.
        self.assertEqual(lead_db.count_users(), 0)
        self.assertEqual(len(lead_db.list_workspaces()), 1)
        # Still locked out of the app, and login says why.
        self.assertEqual(self.client.get(
            "/", follow_redirects=False).status_code, 303)
        r = login(self.client, "new@test.example", "password123")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Confirm your email", r.text)
        self.assertIn("/resend", r.text)

    def test_clicking_the_link_creates_the_account_and_logs_in(self):
        signup(self.client, OWNER["email"], OWNER["password"], "Owner HQ")
        link = self._confirm_link()
        r = self.client.get(link, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.client.get("/").status_code, 200)
        user = lead_db.get_user_by_email(OWNER["email"])
        self.assertEqual((user["role"], user["workspace_id"]), ("admin", 1))
        self.assertEqual(lead_db.get_workspace(1)["name"], "Owner HQ")
        # Single use, and a second login does not create a second account.
        self.assertEqual(TestClient(webapp.app).get(link).status_code, 400)
        self.client.post("/logout")
        self.assertEqual(login(self.client, OWNER["email"],
                               OWNER["password"]).status_code, 303)
        self.assertEqual(lead_db.count_users(), 1)

    def test_bad_or_missing_link_is_rejected(self):
        self.assertEqual(self.client.get(
            "/auth/confirm?token_hash=nope&type=email").status_code, 400)
        self.assertEqual(self.client.get("/auth/confirm").status_code, 400)
        self.assertEqual(lead_db.count_users(), 0)

    def test_a_signup_link_cannot_be_used_as_a_reset_link(self):
        signup(self.client, "new@test.example", "password123")
        token_hash = fake_supabase.outbox()[-1]["token_hash"]
        r = self.client.get(
            f"/auth/confirm?token_hash={token_hash}&type=recovery")
        self.assertEqual(r.status_code, 400)

    def test_resend_sends_a_fresh_link(self):
        signup(self.client, "new@test.example", "password123")
        r = self.client.post("/resend", data={"email": "new@test.example"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(fake_supabase.outbox()), 2)
        self.assertEqual(self.client.get(
            self._confirm_link(), follow_redirects=False).status_code, 303)
        # Unknown address: same answer, nothing sent.
        r = self.client.post("/resend", data={"email": "ghost@x.example"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(fake_supabase.outbox()), 2)

    def test_duplicate_signup_is_refused(self):
        signup(self.client, "new@test.example", "password123")
        r = signup(TestClient(webapp.app), "new@test.example", "password456")
        self.assertEqual(r.status_code, 400)
        self.assertIn("already has an account", r.text)


class OwnerEmailTestCase(TenancyTestMixin, unittest.TestCase):
    """Who gets admin and the unmetered workspace 1."""

    def setUp(self) -> None:
        self.setup_tenancy()

    def tearDown(self) -> None:
        self.teardown_tenancy()

    def test_only_owner_email_becomes_admin(self):
        with mock.patch.dict("os.environ", {"OWNER_EMAIL": "Boss@Test.example"}):
            signup(TestClient(webapp.app), "early@test.example", "password123")
            signup(TestClient(webapp.app), "boss@test.example", "password123")
        early = lead_db.get_user_by_email("early@test.example")
        boss = lead_db.get_user_by_email("boss@test.example")
        self.assertEqual(early["role"], "member")
        self.assertNotEqual(early["workspace_id"], 1)
        self.assertEqual((boss["role"], boss["workspace_id"]), ("admin", 1))

    def test_deployed_server_without_owner_email_gives_nobody_admin(self):
        with mock.patch.dict("os.environ", {"BASE_URL": "https://app.example.com"}):
            signup(TestClient(webapp.app), "first@test.example", "password123")
        first = lead_db.get_user_by_email("first@test.example")
        self.assertEqual(first["role"], "member")
        self.assertNotEqual(first["workspace_id"], 1)


class _Response:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = b"x"

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class SupabaseClientTestCase(unittest.TestCase):
    """tools/supabase_auth.py against canned HTTP answers (no network)."""

    def setUp(self) -> None:
        patcher = mock.patch.object(supabase_auth, "_call", REAL_CALL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _answer(self, status_code: int, body):
        return mock.patch.object(supabase_auth.requests, "request",
                                 return_value=_Response(status_code, body))

    def _code(self, status_code: int, body) -> str:
        with self._answer(status_code, body):
            with self.assertRaises(supabase_auth.AuthError) as ctx:
                supabase_auth.sign_in("a@b.example", "password123")
        return ctx.exception.code

    def test_error_codes_are_mapped(self):
        self.assertEqual(self._code(400, {
            "error_code": "invalid_credentials",
            "msg": "Invalid login credentials"}), "invalid_credentials")
        self.assertEqual(self._code(400, {
            "error": "invalid_grant",
            "error_description": "Invalid login credentials"}), "invalid_credentials")
        self.assertEqual(self._code(400, {
            "error_code": "email_not_confirmed",
            "msg": "Email not confirmed"}), "email_not_confirmed")
        self.assertEqual(self._code(400, {
            "msg": "Email not confirmed"}), "email_not_confirmed")
        self.assertEqual(self._code(429, {}), "rate_limited")
        self.assertEqual(self._code(401, {"message": "Invalid API key"}),
                         "not_configured")
        self.assertEqual(self._code(500, ValueError("not json")), "unknown")

    def test_network_failure_is_unavailable(self):
        with mock.patch.object(
                supabase_auth.requests, "request",
                side_effect=supabase_auth.requests.ConnectionError("down")):
            with self.assertRaises(supabase_auth.AuthError) as ctx:
                supabase_auth.sign_in("a@b.example", "password123")
        self.assertEqual(ctx.exception.code, "unavailable")

    def test_missing_keys_never_reach_the_network(self):
        with mock.patch.dict("os.environ", {"SUPABASE_ANON_KEY": ""}), \
                mock.patch.object(supabase_auth.requests, "request") as request:
            with self.assertRaises(supabase_auth.AuthError) as ctx:
                supabase_auth.send_recovery("a@b.example")
        self.assertEqual(ctx.exception.code, "not_configured")
        request.assert_not_called()

    def test_sign_in_sends_the_key_and_reads_the_session(self):
        body = {"access_token": "tok", "user": {
            "id": "u-1", "email": "A@B.example",
            "email_confirmed_at": "2026-01-01T00:00:00Z",
            "user_metadata": {"workspace_name": "Acme"}}}
        with self._answer(200, body) as request:
            session = supabase_auth.sign_in("a@b.example", "password123")
        self.assertEqual(session["access_token"], "tok")
        self.assertEqual(session["user"], {
            "id": "u-1", "email": "a@b.example",
            "metadata": {"workspace_name": "Acme"}, "confirmed": True})
        args, kwargs = request.call_args
        self.assertEqual(args, (
            "POST", "http://supabase.invalid/auth/v1/token?grant_type=password"))
        self.assertEqual(kwargs["headers"]["apikey"], "test-anon-key")

    def test_signup_for_an_existing_account_is_spotted(self):
        with self._answer(200, {"id": "x", "email": "a@b.example",
                                "identities": []}):
            with self.assertRaises(supabase_auth.AuthError) as ctx:
                supabase_auth.sign_up("a@b.example", "password123")
        self.assertEqual(ctx.exception.code, "already_registered")

    def test_signup_waiting_for_confirmation_has_no_token(self):
        with self._answer(200, {"id": "u-2", "email": "a@b.example",
                                "identities": [{"provider": "email"}]}):
            result = supabase_auth.sign_up("a@b.example", "password123")
        self.assertIsNone(result["access_token"])
        self.assertFalse(result["user"]["confirmed"])


if __name__ == "__main__":
    unittest.main()
