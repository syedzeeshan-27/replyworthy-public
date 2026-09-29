"""Database backend for the lead DB — SQLite by default, Postgres by URL.

V2 Stage 4 (plans/v2/04 C). `lead_db.py` stays the only module the rest of
the codebase talks to; this module gives it one connection object that works
identically on both engines:

  - No DATABASE_URL in the environment  -> zero-config SQLite (the offline
    test suite, quick local runs).
  - DATABASE_URL=postgres://...         -> psycopg3 (Supabase Postgres).

Postgres connections are remote, so they are reused: `close()` hands the
connection back to a small idle pool instead of closing it, and the migration
check runs once per process, not once per connection.

The wrapper keeps the repo's existing calling style working on both engines:
`con.execute("... ? ...", params)` (placeholders translated to %s for
Postgres), rows support `row["col"]` and `dict(row)`, `con.tx()` replaces
sqlite3's `with con:` transaction idiom (re-entrant: only the outermost
tx() begins/commits).

Portability rules for every SQL string in this repo (enforced by
tests/test_sql_audit.py):
  - No `datetime('now')` — pass `db.utcnow()` from Python instead.
  - `INSERT OR IGNORE` -> `INSERT ... ON CONFLICT DO NOTHING` (both engines).
  - Date-bucket comparisons via `substr(col, 1, 10) = ?` (both engines).
  - All timestamps are TEXT columns holding UTC "YYYY-MM-DD HH:MM:SS".
  - No literal '?' inside SQL string literals (the translator is naive).

Schema lives in migrations/NNNN_name.<dialect>.sql — plain sequentially
numbered SQL files, applied once each and recorded in schema_migrations
(no Alembic, no ORM, per plans/v2/04 C). Statements are split on ';' so
migration SQL must not contain literal semicolons inside strings.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def utcnow() -> str:
    """Canonical timestamp string for every DB write."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utcnow_plus(**delta) -> str:
    """utcnow shifted by a timedelta, e.g. utcnow_plus(days=10, hours=-24)."""
    when = datetime.now(timezone.utc) + timedelta(**delta)
    return when.strftime("%Y-%m-%d %H:%M:%S")


def utctoday(days_offset: int = 0) -> str:
    """UTC date string for substr(col, 1, 10) comparisons."""
    when = datetime.now(timezone.utc) + timedelta(days=days_offset)
    return when.strftime("%Y-%m-%d")


def shift_timestamp(value: str, days: int) -> str:
    """A stored 'YYYY-MM-DD HH:MM:SS' string moved by N days (OOO pushes)."""
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return (dt + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and url.startswith(("postgres://", "postgresql://"))


class DB:
    """Thin uniform wrapper over sqlite3.Connection / psycopg.Connection."""

    def __init__(self, raw, dialect: str, pool_key: str | None = None):
        self.raw = raw
        self.dialect = dialect  # "sqlite" | "postgres"
        self._tx_depth = 0
        self._pool_key = pool_key  # set for pooled Postgres connections
        self._returned = False

    # -- statements ---------------------------------------------------------

    def _translate(self, sql: str) -> str:
        if self.dialect == "postgres":
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params=()):
        return self.raw.execute(self._translate(sql), params)

    def insert_id(self, sql: str, params=()) -> int:
        """Run an INSERT and return the new row's id on either engine."""
        if self.dialect == "postgres":
            cur = self.execute(sql + " RETURNING id", params)
            return cur.fetchone()["id"]
        return self.execute(sql, params).lastrowid

    # -- transactions -------------------------------------------------------

    def tx(self):
        """Re-entrant transaction context (replaces sqlite3's `with con:`)."""
        return _Tx(self)

    def close(self) -> None:
        if self._pool_key is None:
            self.raw.close()
            return
        if self._returned:  # already back in the pool, maybe in use elsewhere
            return
        self._returned = True
        _checkin(self._pool_key, self.raw, clean=self._tx_depth == 0)


class _Tx:
    def __init__(self, db: DB):
        self.db = db

    def __enter__(self):
        if self.db._tx_depth == 0:
            self.db.raw.execute("BEGIN")
        self.db._tx_depth += 1
        return self.db

    def __exit__(self, exc_type, exc, tb):
        self.db._tx_depth -= 1
        if self.db._tx_depth == 0:
            self.db.raw.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


# --- Postgres connection reuse -------------------------------------------------

POOL_MAX_IDLE = 5           # idle connections kept per database URL
POOL_PING_AFTER = 30.0      # seconds idle before a connection is re-checked

_pool_lock = threading.Lock()
_idle: dict[str, list[tuple[object, float]]] = {}
_migrated: set[str] = set()


def _usable(raw) -> bool:
    return not getattr(raw, "closed", False) and not getattr(raw, "broken", False)


def _quiet_close(raw) -> None:
    try:
        raw.close()
    except Exception:
        pass


def _checkout(key: str, opener):
    """An open connection for `key`: a healthy idle one when the pool has it,
    else a new one from `opener()`."""
    while True:
        with _pool_lock:
            bucket = _idle.get(key)
            entry = bucket.pop() if bucket else None
        if entry is None:
            return opener()
        raw, since = entry
        if not _usable(raw):
            _quiet_close(raw)
            continue
        if time.monotonic() - since > POOL_PING_AFTER:
            try:
                raw.execute("SELECT 1")
            except Exception:
                _quiet_close(raw)
                continue
        return raw


def _checkin(key: str, raw, clean: bool = True) -> None:
    """Hand a connection back. Anything not provably idle is closed instead:
    a connection returned mid-transaction must never be reused."""
    if clean and _usable(raw):
        try:
            idle_tx = raw.info.transaction_status.name == "IDLE"
        except Exception:
            idle_tx = False
        if idle_tx:
            with _pool_lock:
                bucket = _idle.setdefault(key, [])
                if len(bucket) < POOL_MAX_IDLE:
                    bucket.append((raw, time.monotonic()))
                    return
    _quiet_close(raw)


def reset_pool() -> None:
    """Close every idle connection and forget which databases were migrated
    (tests that rebuild a database call this)."""
    with _pool_lock:
        entries = [e for bucket in _idle.values() for e in bucket]
        _idle.clear()
        _migrated.clear()
    for raw, _ in entries:
        _quiet_close(raw)


def _open_postgres(url: str):
    import psycopg
    from psycopg.rows import dict_row

    # prepare_threshold=None: no server-side prepared statements, which
    # connection poolers (Supabase's included) cannot carry between clients.
    kwargs = {"autocommit": True, "row_factory": dict_row,
              "prepare_threshold": None}
    if "supabase" in url and "sslmode=" not in url:
        kwargs["sslmode"] = "require"
    return psycopg.connect(url, **kwargs)


def connect(sqlite_path: Path, url: str | None = None) -> DB:
    """Open a migrated connection. `url` defaults to the DATABASE_URL env var;
    anything non-Postgres falls back to SQLite at `sqlite_path`."""
    if url is None:
        url = os.environ.get("DATABASE_URL") or None
    if is_postgres_url(url):
        raw = _checkout(url, lambda: _open_postgres(url))
        if url not in _migrated:
            try:
                run_migrations(DB(raw, "postgres"))
            except BaseException:
                _quiet_close(raw)
                raise
            _migrated.add(url)
        return DB(raw, "postgres", pool_key=url)
    raw = sqlite3.connect(sqlite_path, isolation_level=None)
    raw.row_factory = sqlite3.Row
    db = DB(raw, "sqlite")
    run_migrations(db)
    return db


# --- Migration runner --------------------------------------------------------

# The 0001 baseline shape, one entry per table, used ONLY to patch legacy
# SQLite DBs (created before the migration runner existed) up to the 0001
# shape before that migration is marked as already satisfied. Must stay in
# lockstep with migrations/0001_baseline.sqlite.sql —
# tests/test_migrations.py asserts fresh and legacy DBs end up identical.
_BASELINE_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "leads": (
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("place_id", "TEXT"), ("name", "TEXT NOT NULL"), ("category", "TEXT"),
        ("city", "TEXT"), ("area", "TEXT"), ("website", "TEXT"),
        ("phone", "TEXT"), ("email", "TEXT"), ("email_source", "TEXT"),
        ("email_verified", "TEXT DEFAULT 'unchecked'"), ("rating", "REAL"),
        ("reviews_count", "INTEGER"), ("score", "INTEGER"),
        ("pain_points", "TEXT"), ("email_subject", "TEXT"),
        ("email_body", "TEXT"), ("soft_signals", "TEXT"),
        ("status", "TEXT DEFAULT 'new'"), ("status_reason", "TEXT"),
        ("first_seen_at", "TEXT"), ("updated_at", "TEXT"),
        ("ghl_contact_id", "TEXT"), ("ghl_opportunity_id", "TEXT"),
        ("ghl_synced_at", "TEXT"), ("ghl_synced_status", "TEXT"),
        ("offer_id", "INTEGER"), ("search_spec", "TEXT"),
        ("entity_type", "TEXT"), ("company", "TEXT"), ("title", "TEXT"),
        ("linkedin_url", "TEXT"), ("source", "TEXT"), ("source_id", "TEXT"),
        ("source_raw", "TEXT"),
    ),
    "campaigns": (
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("name", "TEXT NOT NULL"), ("category", "TEXT"), ("city", "TEXT"),
        ("status", "TEXT DEFAULT 'draft'"), ("daily_cap", "INTEGER DEFAULT 30"),
        ("created_at", "TEXT"), ("offer_id", "INTEGER"),
    ),
    "sequence_steps": (
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("campaign_id", "INTEGER NOT NULL"), ("step_number", "INTEGER NOT NULL"),
        ("delay_days", "INTEGER NOT NULL"), ("subject_template", "TEXT"),
        ("body_template", "TEXT"),
    ),
    "sends": (
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("lead_id", "INTEGER NOT NULL"), ("campaign_id", "INTEGER NOT NULL"),
        ("step_number", "INTEGER NOT NULL"), ("mailbox", "TEXT"),
        ("message_id", "TEXT"), ("scheduled_at", "TEXT NOT NULL"),
        ("sent_at", "TEXT"), ("status", "TEXT DEFAULT 'scheduled'"),
        ("error", "TEXT"),
    ),
    "suppression": (
        ("email", "TEXT PRIMARY KEY"), ("reason", "TEXT"), ("added_at", "TEXT"),
    ),
    "replies": (
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("lead_id", "INTEGER"), ("send_id", "INTEGER"),
        ("mailbox", "TEXT NOT NULL"), ("from_email", "TEXT NOT NULL"),
        ("subject", "TEXT"), ("body_text", "TEXT"),
        ("received_at", "TEXT NOT NULL"), ("imap_uid", "TEXT"),
        ("classification", "TEXT"), ("confidence", "TEXT"), ("summary", "TEXT"),
        ("suggested_action", "TEXT"), ("processed_at", "TEXT"),
        ("ghl_pushed_at", "TEXT"),
    ),
    "notes": (
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("lead_id", "INTEGER NOT NULL"), ("body", "TEXT NOT NULL"),
        ("author", "TEXT DEFAULT 'me'"), ("created_at", "TEXT"),
    ),
    "alerts": (
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("lead_id", "INTEGER"), ("kind", "TEXT NOT NULL"),
        ("severity", "TEXT DEFAULT 'info'"), ("title", "TEXT"),
        ("body", "TEXT"), ("channel", "TEXT"), ("created_at", "TEXT"),
    ),
    "offer_profiles": (
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("workspace_id", "INTEGER"), ("name", "TEXT NOT NULL"),
        ("product_description", "TEXT NOT NULL"), ("value_prop", "TEXT NOT NULL"),
        ("target_customer", "TEXT"), ("tone", "TEXT DEFAULT 'direct, friendly'"),
        ("cta_style", "TEXT DEFAULT 'soft ask for a short reply'"),
        ("sender_name", "TEXT"), ("sender_title", "TEXT"),
        ("qualification_hints", "TEXT"), ("disqualifiers", "TEXT"),
        ("is_default", "INTEGER DEFAULT 0"), ("deleted", "INTEGER DEFAULT 0"),
        ("created_at", "TEXT"),
    ),
}


def _split_statements(script: str) -> list[str]:
    """Split a migration file into statements. `--` comment lines are dropped
    first (they may contain ';'); the convention is that migration SQL never
    contains literal ';' inside string values, so a plain split then works."""
    sql_lines = [
        line for line in script.splitlines() if not line.lstrip().startswith("--")
    ]
    return [s.strip() for s in "\n".join(sql_lines).split(";") if s.strip()]


def _table_exists(db: DB, name: str) -> bool:
    if db.dialect == "postgres":
        row = db.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ?",
            (name,),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    return row is not None


def _patch_legacy_schema(db: DB) -> None:
    """Bring a pre-runner SQLite DB up to the 0001 baseline shape: create any
    missing table, add any missing column (NOT NULL relaxed on ALTER — SQLite
    can't add one to a populated table; 0002's rebuild re-imposes the final
    constraints)."""
    for table, cols in _BASELINE_TABLES.items():
        if not _table_exists(db, table):
            defs = ", ".join(f"{c} {d}" for c, d in cols)
            db.raw.execute(f"CREATE TABLE {table} ({defs})")
            continue
        have = {r[1] for r in db.raw.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols:
            if col in have or "PRIMARY KEY" in decl:
                continue
            db.raw.execute(
                f"ALTER TABLE {table} ADD COLUMN {col} "
                + decl.replace(" NOT NULL", "")
            )


def pending_migrations(db: DB) -> list[Path]:
    applied = {
        r["name"] for r in db.execute("SELECT name FROM schema_migrations").fetchall()
    }
    files = sorted(MIGRATIONS_DIR.glob(f"*.{db.dialect}.sql"))
    return [f for f in files if f.name.split(".")[0] not in applied]


def run_migrations(db: DB) -> list[str]:
    """Apply every unapplied migration in order. Returns the applied names."""
    with db.tx():
        if not _table_exists(db, "schema_migrations"):
            db.execute(
                "CREATE TABLE schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        # Legacy DB: tables exist but the runner has never seen them. The
        # 0001 baseline is what those tables already are (plus late columns),
        # so apply the column adds and record 0001 without running its file.
        if db.dialect == "sqlite" and _table_exists(db, "leads"):
            already = db.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?", ("0001_baseline",)
            ).fetchone()
            if not already:
                _patch_legacy_schema(db)
                db.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                    ("0001_baseline", utcnow()),
                )

    applied_now: list[str] = []
    for path in pending_migrations(db):
        name = path.name.split(".")[0]
        script = path.read_text(encoding="utf-8")
        with db.tx():
            for statement in _split_statements(script):
                db.raw.execute(statement)
            db.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (name, utcnow()),
            )
        applied_now.append(name)
    return applied_now
