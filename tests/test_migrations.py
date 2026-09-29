"""Migration-runner tests (plans/v2/04 C): fresh DBs and legacy DBs must end
up with the IDENTICAL schema, existing data must land in workspace 1, old
unsubscribes must be promoted to the global hard list, and re-running is a
no-op.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

try:
    from tests.tenancy import TenancyTestMixin  # also puts tools/ on sys.path
except ImportError:
    from tenancy import TenancyTestMixin

import db
import lead_db

# The exact pre-runner schema (as tools/lead_db.py shipped it before Stage 4).
LEGACY_SCHEMA = """
CREATE TABLE leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, place_id TEXT UNIQUE, name TEXT NOT NULL,
  category TEXT, city TEXT, area TEXT, website TEXT, phone TEXT, email TEXT,
  email_source TEXT, email_verified TEXT DEFAULT 'unchecked', rating REAL,
  reviews_count INTEGER, score INTEGER, pain_points TEXT, email_subject TEXT,
  email_body TEXT, soft_signals TEXT, status TEXT DEFAULT 'new', status_reason TEXT,
  first_seen_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
CREATE TABLE campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  category TEXT, city TEXT, status TEXT DEFAULT 'draft', daily_cap INTEGER DEFAULT 30,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE sequence_steps (id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL,
  step_number INTEGER NOT NULL, delay_days INTEGER NOT NULL, subject_template TEXT, body_template TEXT);
CREATE TABLE sends (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL,
  campaign_id INTEGER NOT NULL, step_number INTEGER NOT NULL, mailbox TEXT, message_id TEXT,
  scheduled_at TEXT NOT NULL, sent_at TEXT, status TEXT DEFAULT 'scheduled', error TEXT);
CREATE TABLE suppression (email TEXT PRIMARY KEY, reason TEXT, added_at TEXT DEFAULT (datetime('now')));
CREATE TABLE replies (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, send_id INTEGER,
  mailbox TEXT NOT NULL, from_email TEXT NOT NULL, subject TEXT, body_text TEXT,
  received_at TEXT NOT NULL, imap_uid TEXT, classification TEXT, confidence TEXT,
  summary TEXT, suggested_action TEXT, processed_at TEXT);
CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL,
  body TEXT NOT NULL, author TEXT DEFAULT 'me', created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, kind TEXT NOT NULL,
  severity TEXT DEFAULT 'info', title TEXT, body TEXT, channel TEXT, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE offer_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER,
  name TEXT NOT NULL, product_description TEXT NOT NULL, value_prop TEXT NOT NULL,
  target_customer TEXT, tone TEXT, cta_style TEXT, sender_name TEXT, sender_title TEXT,
  qualification_hints TEXT, disqualifiers TEXT, is_default INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')));
"""


def _columns(con: db.DB, table: str) -> set[str]:
    return {r[1] for r in con.raw.execute(f"PRAGMA table_info({table})")}


def _tables(con: db.DB) -> set[str]:
    return {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()}


class MigrationsTestCase(TenancyTestMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.setup_tenancy()  # env guard + temp DB_PATH
        self._extra_tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._extra_tmp.name)

    def tearDown(self) -> None:
        self._extra_tmp.cleanup()
        self.teardown_tenancy()

    def _make_legacy(self, path: Path) -> None:
        con = sqlite3.connect(path)
        con.executescript(LEGACY_SCHEMA)
        con.executescript("""
        INSERT INTO leads (place_id, name, city, email, status)
          VALUES ('p1', 'Bright Smile', 'Boise ID', 'hi@bs.example', 'contacted');
        INSERT INTO campaigns (name, status) VALUES ('July push', 'active');
        INSERT INTO sends (lead_id, campaign_id, step_number, scheduled_at)
          VALUES (1, 1, 1, '2026-07-01 15:00:00');
        INSERT INTO suppression (email, reason) VALUES
          ('gone@x.example', 'unsubscribed'), ('meh@x.example', 'not_interested');
        INSERT INTO offer_profiles (name, product_description, value_prop, is_default)
          VALUES ('SMS text-back bot', 'x', 'y', 1);
        """)
        con.commit()
        con.close()

    def test_fresh_and_legacy_schemas_end_up_identical(self):
        fresh = db.connect(self.dir / "fresh.db")
        legacy_path = self.dir / "legacy.db"
        self._make_legacy(legacy_path)
        legacy = db.connect(legacy_path)
        try:
            self.assertEqual(_tables(fresh), _tables(legacy))
            for table in _tables(fresh):
                self.assertEqual(_columns(fresh, table), _columns(legacy, table),
                                 f"column drift in {table}")
        finally:
            fresh.close()
            legacy.close()

    def test_legacy_data_lands_in_workspace_1(self):
        path = self.dir / "legacy.db"
        self._make_legacy(path)
        con = db.connect(path)
        try:
            lead = con.execute("SELECT * FROM leads").fetchone()
            self.assertEqual(lead["workspace_id"], 1)
            self.assertEqual(lead["status"], "contacted")  # progress preserved
            send = con.execute("SELECT * FROM sends").fetchone()
            self.assertEqual(send["workspace_id"], 1)
            campaign = con.execute("SELECT * FROM campaigns").fetchone()
            self.assertEqual(campaign["workspace_id"], 1)
            offer = con.execute("SELECT * FROM offer_profiles").fetchone()
            self.assertEqual(offer["workspace_id"], 1)
            self.assertEqual(offer["is_default"], 1)
            ws = con.execute("SELECT * FROM workspaces").fetchall()
            self.assertEqual([w["id"] for w in ws], [1])
        finally:
            con.close()

    def test_legacy_unsubscribes_promoted_to_hard_suppression(self):
        path = self.dir / "legacy.db"
        self._make_legacy(path)
        con = db.connect(path)
        try:
            hard = {r["email"] for r in
                    con.execute("SELECT email FROM hard_suppression").fetchall()}
            self.assertEqual(hard, {"gone@x.example"})  # not the not_interested
            per_ws = con.execute(
                "SELECT workspace_id, email FROM suppression ORDER BY email"
            ).fetchall()
            self.assertEqual([(r["workspace_id"], r["email"]) for r in per_ws],
                             [(1, "gone@x.example"), (1, "meh@x.example")])
        finally:
            con.close()

    def test_migrations_are_recorded_and_idempotent(self):
        path = self.dir / "again.db"
        con = db.connect(path)
        names = [r["name"] for r in
                 con.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()]
        con.close()
        self.assertEqual(names, ["0001_baseline", "0002_tenancy",
                                 "0003_billing", "0004_tenant_email",
                                 "0005_lead_query_indexes",
                                 "0006_social_links",
                                 "0007_supabase_auth"])
        # Re-open: nothing new applied, nothing broken.
        con = db.connect(path)
        try:
            self.assertEqual(len(con.execute(
                "SELECT name FROM schema_migrations").fetchall()), 7)
            self.assertEqual(con.execute(
                "SELECT COUNT(*) AS n FROM workspaces").fetchone()["n"], 1)
        finally:
            con.close()

    def test_dedupe_is_per_workspace_after_migration(self):
        """Two workspaces may hold the same place_id — dedupe never crosses."""
        ws2 = lead_db.create_workspace("Second")
        biz = {"name": "Same Biz", "place_id": "shared-place",
               "website": "https://same.example"}
        self.assertEqual(lead_db.upsert_leads(1, [dict(biz)])["inserted"], 1)
        self.assertEqual(lead_db.upsert_leads(ws2, [dict(biz)])["inserted"], 1)
        # Same workspace again -> update, not duplicate.
        self.assertEqual(lead_db.upsert_leads(1, [dict(biz)])["updated"], 1)
        self.assertEqual(len(lead_db.get_leads(1)), 1)
        self.assertEqual(len(lead_db.get_leads(ws2)), 1)


class SupabaseSqlTestCase(unittest.TestCase):
    """The file the owner pastes into Supabase must match the migrations."""

    def test_schema_file_is_up_to_date(self):
        import build_supabase_sql
        on_disk = build_supabase_sql.OUT_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            on_disk.replace("\r\n", "\n"), build_supabase_sql.build().replace("\r\n", "\n"),
            "supabase/01_schema.sql is stale: run python tools/build_supabase_sql.py")

    def test_every_postgres_table_has_row_level_security(self):
        import re
        sql = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted(db.MIGRATIONS_DIR.glob("*.postgres.sql")))
        created = set(re.findall(r"CREATE TABLE (\w+)", sql))
        locked = set(re.findall(r"ALTER TABLE (\w+) ENABLE ROW LEVEL SECURITY", sql))
        self.assertEqual(created - locked, set(),
                         "Supabase serves tables without RLS to the public API")


class _FakeRaw:
    """Just enough of a psycopg connection for the pool's bookkeeping."""

    class _Status:
        def __init__(self, name):
            self.name = name

    class _Info:
        pass

    def __init__(self, status: str = "IDLE"):
        self.closed = False
        self.broken = False
        self.pings = 0
        self.fail_ping = False
        self.info = self._Info()
        self.info.transaction_status = self._Status(status)

    def execute(self, sql, params=()):
        self.pings += 1
        if self.fail_ping:
            raise RuntimeError("server closed the connection")

    def close(self):
        self.closed = True


class PostgresPoolTestCase(unittest.TestCase):
    """Connection reuse for the remote database (no Postgres needed)."""

    def setUp(self) -> None:
        db.reset_pool()
        self.addCleanup(db.reset_pool)
        self.opened = []

    def _open(self):
        self.opened.append(_FakeRaw())
        return self.opened[-1]

    def test_a_returned_connection_is_reused(self):
        first = db._checkout("url", self._open)
        db._checkin("url", first)
        self.assertIs(db._checkout("url", self._open), first)
        self.assertEqual(len(self.opened), 1)
        self.assertFalse(first.closed)

    def test_close_on_the_wrapper_returns_it_to_the_pool(self):
        con = db.DB(db._checkout("url", self._open), "postgres", pool_key="url")
        con.close()
        con.close()  # a second close must not pool the connection twice
        self.assertFalse(con.raw.closed)
        self.assertIs(db._checkout("url", self._open), con.raw)
        self.assertEqual(len(self.opened), 1)
        db._checkout("url", self._open)
        self.assertEqual(len(self.opened), 2)

    def test_mid_transaction_connection_is_never_reused(self):
        con = db.DB(db._checkout("url", self._open), "postgres", pool_key="url")
        con._tx_depth = 1
        con.close()
        self.assertTrue(con.raw.closed)
        stuck = _FakeRaw(status="INTRANS")
        db._checkin("url", stuck)
        self.assertTrue(stuck.closed)
        self.assertIsNot(db._checkout("url", self._open), stuck)

    def test_dead_connections_are_replaced(self):
        dead = db._checkout("url", self._open)
        db._checkin("url", dead)
        dead.closed = True
        fresh = db._checkout("url", self._open)
        self.assertIsNot(fresh, dead)

    def test_long_idle_connection_is_pinged_before_reuse(self):
        raw = db._checkout("url", self._open)
        db._checkin("url", raw)
        db._idle["url"][0] = (raw, db.time.monotonic() - db.POOL_PING_AFTER - 1)
        self.assertIs(db._checkout("url", self._open), raw)
        self.assertEqual(raw.pings, 1)
        # ...and one that fails the ping is thrown away.
        db._checkin("url", raw)
        db._idle["url"][0] = (raw, db.time.monotonic() - db.POOL_PING_AFTER - 1)
        raw.fail_ping = True
        self.assertIsNot(db._checkout("url", self._open), raw)
        self.assertTrue(raw.closed)

    def test_pool_is_capped_and_per_database(self):
        raws = [db._checkout("url", self._open) for _ in range(db.POOL_MAX_IDLE + 2)]
        for raw in raws:
            db._checkin("url", raw)
        self.assertEqual(len(db._idle["url"]), db.POOL_MAX_IDLE)
        self.assertEqual(sum(r.closed for r in raws), 2)
        self.assertNotIn(db._checkout("other", self._open), raws)


if __name__ == "__main__":
    unittest.main()
