"""Lead database — cross-run source of truth, workspace-scoped (V2 Stage 4).

Excel in outputs/ stays the client-facing deliverable; this DB is what the
outreach stages read and write. SQLite at data/leads.db by default, Postgres
when DATABASE_URL is set (tools/db.py picks by URL scheme; schema lives in
migrations/).

THE LEAKAGE FIREWALL (plans/v2/04 B): every function that reads or writes a
tenant table takes `workspace_id` as a REQUIRED argument — no default, no
"get all" variant. Forgetting the workspace is a TypeError at the call site,
not a cross-tenant data breach. Workers that must span workspaces iterate
`workspace_ids()` explicitly. tests/test_sql_audit.py greps this file to
keep it that way.

Suppression is two-tier (plans/v2/04 B): `suppression` is per-workspace
(not_interested, bounced, manual); `hard_suppression` is global and wins
everywhere (unsubscribes/complaints — a hard unsubscribe blocks every
workspace, on purpose).

Dedupe is per workspace and per entity type: businesses on place_id ->
(website, name); persons on email -> (name, company). Re-running a search
refreshes scraped fields but NEVER touches the status of a lead that already
progressed (contacted/replied/...) — re-contacting the same business twice is
the failure mode this table exists to prevent.

Usage:
    python tools/lead_db.py --counts     # leads per status, per workspace
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config.settings import DATA_DIR  # noqa: E402
import db  # noqa: E402

DB_PATH = DATA_DIR / "leads.db"

# The migration-created workspace that owns all pre-Stage-4 data and every
# CLI run. The first signup ever claims it (web/auth.py).
OWNER_WORKSPACE_ID = 1

# Statuses a lead moves through. Only 'new' and 'excluded' may be overwritten
# by a fresh scrape; everything past that is outreach state and is preserved.
RESETTABLE_STATUSES = ("new", "excluded")

# Lead-dict keys copied straight into same-named columns on insert/update.
_FIELD_KEYS = (
    "place_id", "name", "category", "city", "area", "website", "phone",
    "email", "email_source", "email_verified", "rating", "reviews_count",
    "score", "email_subject", "email_body", "offer_id",
    "entity_type", "company", "title", "linkedin_url", "source", "source_id",
)
_JSON_KEYS = ("pain_points", "soft_signals", "search_spec", "source_raw",
              "social_links")


_offer_seeded: set[str] = set()


def connect() -> db.DB:
    con = db.connect(DB_PATH)
    # Postgres is remote: check the owner's default offer once per process,
    # not on every connection. SQLite (a local file) keeps the per-connect
    # check it always had.
    key = (os.environ.get("DATABASE_URL") or "") if con.dialect == "postgres" else ""
    if not key or key not in _offer_seeded:
        try:
            ensure_default_offer(con, OWNER_WORKSPACE_ID)
        except BaseException:
            con.close()
            raise
        if key:
            _offer_seeded.add(key)
    return con


# --- Workspaces and users (auth tables, not tenant data) ---------------------


def create_workspace(name: str) -> int:
    """Create a workspace, seed its default offer and its one-time trial
    credits (V2 Stage 5). Returns the new id."""
    import billing  # lazy: billing imports lead_db at module level
    con = connect()
    try:
        with con.tx():
            workspace_id = con.insert_id(
                "INSERT INTO workspaces (name, status, plan, created_at) "
                "VALUES (?, 'active', 'trial', ?)",
                (name.strip() or "My workspace", db.utcnow()),
            )
            ensure_default_offer(con, workspace_id)
            billing.init_workspace_billing(con, workspace_id)
        return workspace_id
    finally:
        con.close()


def get_workspace(workspace_id: int) -> dict | None:
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def rename_workspace(workspace_id: int, name: str) -> None:
    if not (name or "").strip():
        return
    con = connect()
    try:
        with con.tx():
            con.execute(
                "UPDATE workspaces SET name = ? WHERE id = ?",
                (name.strip(), workspace_id),
            )
    finally:
        con.close()


def list_workspaces() -> list[dict]:
    """All workspaces (billing/status columns, no tenant data). Used by the
    admin switcher, /admin/billing and workers that iterate workspaces."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, name, status, plan, frozen, credits_balance, "
            "trial_expires_at, created_at FROM workspaces ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def set_workspace_identity(workspace_id: int, postal_address: str,
                           alert_email: str) -> None:
    """The workspace's CAN-SPAM postal address + its own alert email
    (V2 Stage 6 onboarding). Blank values clear the field."""
    con = connect()
    try:
        with con.tx():
            con.execute(
                "UPDATE workspaces SET postal_address = ?, alert_email = ? "
                "WHERE id = ?",
                ((postal_address or "").strip() or None,
                 (alert_email or "").strip().lower() or None, workspace_id),
            )
    finally:
        con.close()


def set_live_sending(workspace_id: int, live: bool,
                     warmup_ack: bool | None = None) -> None:
    """Flip one workspace between practice and live (V2 Stage 6 B). The
    checklist gate lives in the web route; this just records the switch."""
    con = connect()
    try:
        with con.tx():
            if warmup_ack is None:
                con.execute(
                    "UPDATE workspaces SET live_sending = ? WHERE id = ?",
                    (1 if live else 0, workspace_id),
                )
            else:
                con.execute(
                    "UPDATE workspaces SET live_sending = ?, warmup_ack = ? "
                    "WHERE id = ?",
                    (1 if live else 0, 1 if warmup_ack else 0, workspace_id),
                )
    finally:
        con.close()


def set_outreach_paused(workspace_id: int, paused: bool) -> None:
    """Pause/resume ONE workspace's sending (the bounce breaker sets it too;
    the PAUSED file stays the global admin kill switch)."""
    con = connect()
    try:
        with con.tx():
            con.execute(
                "UPDATE workspaces SET outreach_paused = ? WHERE id = ?",
                (1 if paused else 0, workspace_id),
            )
    finally:
        con.close()


def workspace_ids(con: db.DB | None = None) -> list[int]:
    """Every workspace id, for workers that process each tenant in turn
    (plans/v2/04 B: nothing processes cross-workspace rows in one query)."""
    own = con is None
    con = con or connect()
    try:
        return [r["id"] for r in con.execute(
            "SELECT id FROM workspaces ORDER BY id"
        ).fetchall()]
    finally:
        if own:
            con.close()


def create_user(email: str, workspace_id: int, role: str = "member",
                auth_user_id: str | None = None) -> int:
    """A local account row. The password lives in Supabase Auth
    (tools/supabase_auth.py); `auth_user_id` is the Supabase user id."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("a real email address is required")
    if role not in ("member", "admin"):
        raise ValueError(f"unknown role {role!r}")
    con = connect()
    try:
        with con.tx():
            return con.insert_id(
                "INSERT INTO users (email, password_hash, role, workspace_id, "
                "auth_user_id, created_at) VALUES (?, '', ?, ?, ?, ?)",
                (email, role, workspace_id, auth_user_id, db.utcnow()),
            )
    finally:
        con.close()


def get_user(user_id: int) -> dict | None:
    con = connect()
    try:
        row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def get_user_by_email(email: str) -> dict | None:
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM users WHERE email = ?",
            ((email or "").strip().lower(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def count_users() -> int:
    con = connect()
    try:
        return con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    finally:
        con.close()


def get_user_by_auth_id(auth_user_id: str) -> dict | None:
    if not auth_user_id:
        return None
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM users WHERE auth_user_id = ?", (auth_user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def link_auth_user(user_id: int, auth_user_id: str) -> None:
    """Attach a Supabase user id to an existing local account."""
    con = connect()
    try:
        with con.tx():
            con.execute(
                "UPDATE users SET auth_user_id = ? WHERE id = ?",
                (auth_user_id, user_id),
            )
    finally:
        con.close()


def set_user_role(user_id: int, role: str) -> None:
    """Promote/demote one account (web/auth.py: OWNER_EMAIL set late)."""
    if role not in ("member", "admin"):
        raise ValueError(f"unknown role {role!r}")
    con = connect()
    try:
        with con.tx():
            con.execute("UPDATE users SET role = ? WHERE id = ?",
                        (role, user_id))
    finally:
        con.close()


def count_admins() -> int:
    con = connect()
    try:
        return con.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'"
        ).fetchone()["n"]
    finally:
        con.close()


# --- Leads -------------------------------------------------------------------


def _row_values(workspace_id: int, lead: dict) -> dict:
    values = {"workspace_id": workspace_id}
    values.update({k: lead.get(k) for k in _FIELD_KEYS})
    for k in _JSON_KEYS:
        v = lead.get(k)
        values[k] = json.dumps(v, ensure_ascii=False) if v is not None else None
    # An empty social dict means "the scrape found none", never "erase what
    # we know" — store NULL so the non-null-refresh rule protects the value
    # (same promise as email: a scrape that lost a field doesn't blank it).
    if not lead.get("social_links"):
        values["social_links"] = None
    return values


def _find_existing(con: db.DB, workspace_id: int, lead: dict):
    """Dedupe key per entity type (plans/v2/03 A), always inside ONE
    workspace: businesses keep the original key (place_id, then
    website+name); persons match on email first, then (name, company)."""
    if (lead.get("entity_type") or "business") == "person":
        if lead.get("email"):
            row = con.execute(
                "SELECT * FROM leads WHERE workspace_id = ? "
                "AND entity_type = 'person' AND email = ?",
                (workspace_id, lead["email"]),
            ).fetchone()
            if row:
                return row
        if lead.get("name") and lead.get("company"):
            return con.execute(
                "SELECT * FROM leads WHERE workspace_id = ? "
                "AND entity_type = 'person' AND name = ? AND company = ?",
                (workspace_id, lead["name"], lead["company"]),
            ).fetchone()
        return None
    if lead.get("place_id"):
        row = con.execute(
            "SELECT * FROM leads WHERE workspace_id = ? AND place_id = ?",
            (workspace_id, lead["place_id"]),
        ).fetchone()
        if row:
            return row
    if lead.get("website") and lead.get("name"):
        # NULL entity_type = pre-Stage-3 business row; exclude persons so a
        # person at "Acme" can't absorb a business row named "Acme".
        return con.execute(
            "SELECT * FROM leads WHERE workspace_id = ? AND website = ? "
            "AND name = ? AND (entity_type IS NULL OR entity_type != 'person')",
            (workspace_id, lead["website"], lead["name"]),
        ).fetchone()
    return None


def upsert_lead(
    con: db.DB,
    workspace_id: int,
    lead: dict,
    status: str = "new",
    status_reason: str | None = None,
) -> tuple[int, str]:
    """Insert or refresh one lead. Returns (lead_id, "inserted"|"updated").

    On update: scraped/analysis fields are refreshed (non-null values only, so
    a scrape that lost the email doesn't blank a known one). `status` is
    applied only while the lead is still in a resettable state ('new' /
    'excluded'); outreach progress is never overwritten.
    """
    values = _row_values(workspace_id, lead)
    existing = _find_existing(con, workspace_id, lead)

    if existing is None:
        values["status"] = status
        values["status_reason"] = status_reason
        values["first_seen_at"] = values["updated_at"] = db.utcnow()
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        new_id = con.insert_id(
            f"INSERT INTO leads ({cols}) VALUES ({marks})", tuple(values.values())
        )
        return new_id, "inserted"

    updates = {k: v for k, v in values.items() if v is not None}
    updates.pop("workspace_id", None)  # a lead never moves workspace
    if existing["status"] in RESETTABLE_STATUSES and status != existing["status"]:
        updates["status"] = status
        updates["status_reason"] = status_reason
    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        con.execute(
            f"UPDATE leads SET {assignments}, updated_at = ? "
            "WHERE id = ? AND workspace_id = ?",
            (*updates.values(), db.utcnow(), existing["id"], workspace_id),
        )
    return existing["id"], "updated"


def upsert_leads(
    workspace_id: int,
    leads: list[dict],
    status: str = "new",
    status_reason: str | None = None,
) -> dict:
    """Upsert a batch in one transaction. Returns {'inserted': n, 'updated': n}."""
    counts = {"inserted": 0, "updated": 0}
    con = connect()
    try:
        with con.tx():
            for lead in leads:
                if not lead.get("name"):
                    continue
                _, action = upsert_lead(con, workspace_id, lead, status, status_reason)
                counts[action] += 1
    finally:
        con.close()
    return counts


def get_leads(workspace_id: int, status: str | None = None) -> list[dict]:
    con = connect()
    try:
        if status:
            rows = con.execute(
                "SELECT * FROM leads WHERE workspace_id = ? AND status = ?",
                (workspace_id, status),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM leads WHERE workspace_id = ?", (workspace_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_lead(workspace_id: int, lead_id: int) -> dict | None:
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM leads WHERE id = ? AND workspace_id = ?",
            (lead_id, workspace_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def add_note(workspace_id: int, lead_id: int, body: str, author: str = "me") -> None:
    if not (body or "").strip():
        return
    con = connect()
    try:
        with con.tx():
            con.execute(
                "INSERT INTO notes (workspace_id, lead_id, body, author, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (workspace_id, lead_id, body.strip(), author, db.utcnow()),
            )
    finally:
        con.close()


def get_notes(workspace_id: int, lead_id: int) -> list[dict]:
    con = connect()
    try:
        rows = con.execute(
            "SELECT * FROM notes WHERE workspace_id = ? AND lead_id = ? "
            "ORDER BY id DESC",
            (workspace_id, lead_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def set_status(workspace_id: int, lead_id: int, status: str,
               reason: str | None = None) -> None:
    con = connect()
    try:
        with con.tx():
            con.execute(
                "UPDATE leads SET status = ?, status_reason = ?, updated_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (status, reason, db.utcnow(), lead_id, workspace_id),
            )
    finally:
        con.close()


def cancel_pending_sends(workspace_id: int, lead_id: int,
                         con: db.DB | None = None) -> int:
    """Cancel all scheduled sends for a lead (someone replied/unsubscribed).
    Returns how many were cancelled."""
    own = con is None
    con = con or connect()
    try:
        with con.tx():
            cur = con.execute(
                "UPDATE sends SET status = 'cancelled' "
                "WHERE workspace_id = ? AND lead_id = ? AND status = 'scheduled'",
                (workspace_id, lead_id),
            )
        return cur.rowcount
    finally:
        if own:
            con.close()


def list_scheduled_sends(workspace_id: int, limit: int = 25) -> list[dict]:
    """The workspace's outbox: pending emails soonest-first, with the lead
    and campaign names the Sending page shows."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT s.lead_id, s.step_number, s.scheduled_at, "
            "l.name AS lead_name, l.email AS lead_email, c.name AS campaign_name "
            "FROM sends s "
            "JOIN leads l ON l.id = s.lead_id AND l.workspace_id = s.workspace_id "
            "JOIN campaigns c ON c.id = s.campaign_id AND c.workspace_id = s.workspace_id "
            "WHERE s.workspace_id = ? AND s.status = 'scheduled' "
            "ORDER BY s.scheduled_at LIMIT ?",
            (workspace_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# Tables reset_workspace_data() clears before campaigns/leads. sequence_steps
# is handled separately (no workspace_id column; reached via its campaign).
_RESET_CHILD_TABLES = ("replies", "sends", "notes", "alerts")


def count_workspace_data(workspace_id: int,
                         include_suppression: bool = False) -> dict[str, int]:
    """Row counts of exactly what reset_workspace_data() would delete,
    same keys — the CLI's dry-run preview."""
    con = connect()
    try:
        counts: dict[str, int] = {}
        for table in _RESET_CHILD_TABLES:
            counts[table] = con.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()["n"]
        counts["sequence_steps"] = con.execute(
            "SELECT COUNT(*) AS n FROM sequence_steps WHERE campaign_id IN "
            "(SELECT id FROM campaigns WHERE workspace_id = ?)",
            (workspace_id,),
        ).fetchone()["n"]
        for table in ("campaigns", "leads"):
            counts[table] = con.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()["n"]
        if include_suppression:
            counts["suppression"] = con.execute(
                "SELECT COUNT(*) AS n FROM suppression WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()["n"]
        return counts
    finally:
        con.close()


def reset_workspace_data(workspace_id: int,
                         include_suppression: bool = False) -> dict[str, int]:
    """Wipe ONE workspace's pipeline data in a single transaction: replies,
    sends, notes, alerts, sequence steps (via that workspace's campaign ids),
    campaigns and leads — plus the workspace's own suppression rows only when
    include_suppression=True. Never touches hard_suppression (global),
    offer_profiles, mailboxes, users, workspaces or billing tables. Children
    are deleted explicitly because the schema's foreign keys are declared but
    not enforced. Returns rows deleted per table."""
    con = connect()
    try:
        counts: dict[str, int] = {}
        with con.tx():
            for table in _RESET_CHILD_TABLES:
                counts[table] = con.execute(
                    f"DELETE FROM {table} WHERE workspace_id = ?",
                    (workspace_id,),
                ).rowcount
            counts["sequence_steps"] = con.execute(
                "DELETE FROM sequence_steps WHERE campaign_id IN "
                "(SELECT id FROM campaigns WHERE workspace_id = ?)",
                (workspace_id,),
            ).rowcount
            for table in ("campaigns", "leads"):
                counts[table] = con.execute(
                    f"DELETE FROM {table} WHERE workspace_id = ?",
                    (workspace_id,),
                ).rowcount
            if include_suppression:
                counts["suppression"] = con.execute(
                    "DELETE FROM suppression WHERE workspace_id = ?",
                    (workspace_id,),
                ).rowcount
        return counts
    finally:
        con.close()


def count_by_status(workspace_id: int) -> dict[str, int]:
    con = connect()
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) AS n FROM leads WHERE workspace_id = ? "
            "GROUP BY status ORDER BY n DESC",
            (workspace_id,),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
    finally:
        con.close()


# Sort keys the table view may request. User input picks a KEY here — the
# ORDER BY fragment itself never comes from outside this dict.
LEAD_SORTS = {
    "updated_desc": "updated_at DESC, id DESC",
    "updated_asc": "updated_at ASC, id ASC",
    "score_desc": "score DESC, updated_at DESC, id DESC",
    "name_asc": "LOWER(name) ASC, id ASC",
}

# Safety cap on a single export (all filtered leads, unpaginated). Realistic
# workspaces sit far under this; it just bounds memory/time on a pathological
# one. Not user-facing.
EXPORT_MAX_ROWS = 5000


def _like_term(q: str) -> str:
    """%term% with the user's LIKE wildcards escaped (paired with ESCAPE '\\')."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc.lower()}%"


def _lead_where(
    workspace_id: int,
    *,
    statuses: tuple[str, ...] | list[str] | None = None,
    q: str | None = None,
    min_score: int | None = None,
    score: int | None = None,
    city: str | None = None,
    entity_type: str | None = None,
    channel: str | None = None,
) -> tuple[str, list]:
    """Shared WHERE tail + bound params for the leads table view (query_leads)
    and the export (export_leads). `params[0]` is always workspace_id and the
    caller prepends "… WHERE workspace_id = ?", so this returns just the
    " AND <clause>" tail (empty when nothing is filtered) plus the params.
    Every value travels as a placeholder; sort/pagination stay with the
    caller. One builder means the export can never drift from the table."""
    clauses: list[str] = []
    params: list = [workspace_id]
    if statuses:
        marks = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({marks})")
        params.extend(statuses)
    if q and q.strip():
        # The Name COLUMN only — city, score and type each have their own
        # dropdown beside the box, and email/category are not on screen at
        # all. The company/title half mirrors the template: that grey second
        # line renders under a PERSON's name only, so it is searched for
        # people only, and a search never matches something invisible.
        # Case-insensitive on both engines: SQLite LIKE only folds ASCII by
        # default and Postgres LIKE folds nothing, so LOWER both sides.
        like = _like_term(q.strip())
        clauses.append(
            "(LOWER(name) LIKE ? ESCAPE '\\'"
            " OR (entity_type = 'person'"
            "     AND (LOWER(company) LIKE ? ESCAPE '\\'"
            "          OR LOWER(title) LIKE ? ESCAPE '\\')))")
        params.extend([like] * 3)
    if min_score:
        clauses.append("score >= ?")
        params.append(min_score)
    if score:
        # Exact match (the table's score dropdown), distinct from min_score's
        # "N and up" range (used by Home's Hot-leads tile).
        clauses.append("score = ?")
        params.append(score)
    if city:
        clauses.append("LOWER(city) = LOWER(?)")
        params.append(city)
    if entity_type == "person":
        clauses.append("entity_type = 'person'")
    elif entity_type == "business":
        # NULL entity_type = pre-Stage-3 business row (same rule as dedupe).
        clauses.append("(entity_type IS NULL OR entity_type != 'person')")
    # Best-available reach channel: email → social DM → phone. social_links
    # is NULL exactly when no profile is known (upsert stores an empty dict
    # as NULL), so plain NULL checks stay portable across both engines.
    if channel == "email":
        clauses.append("(email IS NOT NULL AND email != '')")
    elif channel == "dm":
        clauses.append(
            "((email IS NULL OR email = '') AND social_links IS NOT NULL)")
    elif channel == "phone":
        clauses.append(
            "((email IS NULL OR email = '') AND social_links IS NULL)")
    return "".join(" AND " + c for c in clauses), params


def query_leads(
    workspace_id: int,
    *,
    statuses: tuple[str, ...] | list[str] | None = None,
    q: str | None = None,
    min_score: int | None = None,
    score: int | None = None,
    city: str | None = None,
    entity_type: str | None = None,
    channel: str | None = None,
    sort: str = "updated_desc",
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[dict], int]:
    """One filtered/sorted/paginated slice of a workspace's leads, for the
    table view. Returns (rows, total-matching). Every filter is optional;
    the SQL is assembled only from fixed fragments plus placeholders —
    values always travel as parameters, and `sort` maps through LEAD_SORTS
    (unknown keys fall back to newest activity). `q` searches the table's
    Name column only (see _lead_where)."""
    tail, params = _lead_where(
        workspace_id, statuses=statuses, q=q, min_score=min_score,
        score=score, city=city, entity_type=entity_type, channel=channel)
    order = LEAD_SORTS.get(sort) or LEAD_SORTS["updated_desc"]
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 50), 100))
    con = connect()
    try:
        total = con.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE workspace_id = ?" + tail,
            tuple(params),
        ).fetchone()["n"]
        rows = con.execute(
            "SELECT * FROM leads WHERE workspace_id = ?" + tail
            + f" ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page),
        ).fetchall()
        return [dict(r) for r in rows], total
    finally:
        con.close()


def export_leads(
    workspace_id: int,
    *,
    statuses: tuple[str, ...] | list[str] | None = None,
    q: str | None = None,
    min_score: int | None = None,
    score: int | None = None,
    city: str | None = None,
    entity_type: str | None = None,
    channel: str | None = None,
    sort: str = "updated_desc",
    limit: int = EXPORT_MAX_ROWS,
) -> list[dict]:
    """Every lead matching the same filters as query_leads, UNPAGINATED — the
    row set behind a pipeline export download. Shares _lead_where so the file
    is exactly what the filtered table shows, across all pages. `limit` is a
    safety bound (default EXPORT_MAX_ROWS); `sort` maps through LEAD_SORTS."""
    tail, params = _lead_where(
        workspace_id, statuses=statuses, q=q, min_score=min_score,
        score=score, city=city, entity_type=entity_type, channel=channel)
    order = LEAD_SORTS.get(sort) or LEAD_SORTS["updated_desc"]
    limit = max(1, min(int(limit or EXPORT_MAX_ROWS), EXPORT_MAX_ROWS))
    con = connect()
    try:
        rows = con.execute(
            "SELECT * FROM leads WHERE workspace_id = ?" + tail
            + f" ORDER BY {order} LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def dm_queue(workspace_id: int, limit: int = 100) -> list[dict]:
    """Business leads reachable ONLY by social DM: still 'new', no email,
    at least one social profile (social_links is NULL exactly when none is
    known). Best scores first so the human DMs the strongest leads. Leads
    leave the queue when a DM is recorded (status moves to 'contacted')."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT * FROM leads WHERE workspace_id = ? AND status = 'new' "
            "AND (email IS NULL OR email = '') AND social_links IS NOT NULL "
            "AND (entity_type IS NULL OR entity_type != 'person') "
            "ORDER BY COALESCE(score, -1) DESC, id DESC LIMIT ?",
            (workspace_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def recent_dm_openings(workspace_id: int, limit: int = 8) -> list[str]:
    """Opening lines of recently sent DMs, newest first — fed back into
    write_dm so the next DM never reuses an opener (repeated identical
    openers get the sender's account flagged as spam).

    DM notes are author='dm' with body "DM sent (<platform>):" on line one
    and the message underneath; the opener is the message's first line."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT body FROM notes WHERE workspace_id = ? AND author = 'dm' "
            "ORDER BY id DESC LIMIT ?",
            (workspace_id, limit),
        ).fetchall()
    finally:
        con.close()
    openings: list[str] = []
    for row in rows:
        lines = [l.strip() for l in (row["body"] or "").splitlines() if l.strip()]
        if len(lines) >= 2:
            openings.append(lines[1])
    return openings


def distinct_cities(workspace_id: int) -> list[str]:
    """The workspace's lead cities, for the table view's filter dropdown."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT city FROM leads WHERE workspace_id = ? "
            "AND city IS NOT NULL AND city != '' ORDER BY city",
            (workspace_id,),
        ).fetchall()
        return [r["city"] for r in rows]
    finally:
        con.close()


# --- Suppression (two-tier, plans/v2/04 B) -----------------------------------


def add_suppression(workspace_id: int, email: str, reason: str) -> None:
    """Add an address to ONE workspace's do-not-contact list (idempotent)."""
    if not email:
        return
    con = connect()
    try:
        with con.tx():
            con.execute(
                "INSERT INTO suppression (workspace_id, email, reason, added_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (workspace_id, email.strip().lower(), reason, db.utcnow()),
            )
    finally:
        con.close()


def add_hard_suppression(email: str, reason: str) -> None:
    """Globally block an address in EVERY workspace (unsubscribes/complaints
    only — a hard unsubscribe blocks everywhere, on purpose)."""
    if not email:
        return
    con = connect()
    try:
        with con.tx():
            con.execute(
                "INSERT INTO hard_suppression (email, reason, added_at) "
                "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (email.strip().lower(), reason, db.utcnow()),
            )
    finally:
        con.close()


def is_suppressed(workspace_id: int, email: str | None,
                  con: db.DB | None = None) -> bool:
    """True if the address is blocked for this workspace — either on its own
    suppression list or on the global hard list (which always wins)."""
    if not email:
        return False
    own = con is None
    con = con or connect()
    try:
        addr = email.strip().lower()
        row = con.execute(
            "SELECT 1 FROM hard_suppression WHERE email = ?", (addr,)
        ).fetchone()
        if row is not None:
            return True
        row = con.execute(
            "SELECT 1 FROM suppression WHERE workspace_id = ? AND email = ?",
            (workspace_id, addr),
        ).fetchone()
        return row is not None
    finally:
        if own:
            con.close()


# --- Offer profiles ----------------------------------------------------------

# Offer-dict keys copied straight into same-named columns on insert/update.
# is_default/deleted are managed only through set_default_offer/delete_offer.
_OFFER_FIELD_KEYS = (
    "name", "product_description", "value_prop",
    "target_customer", "tone", "cta_style", "sender_name", "sender_title",
    "qualification_hints", "disqualifiers",
)


def list_offers(workspace_id: int, include_deleted: bool = False) -> list[dict]:
    """Return one workspace's offer profiles, oldest first. Soft-deleted ones
    are hidden by default; pass include_deleted=True for the full history."""
    con = connect()
    try:
        if include_deleted:
            rows = con.execute(
                "SELECT * FROM offer_profiles WHERE workspace_id = ? ORDER BY id",
                (workspace_id,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM offer_profiles WHERE workspace_id = ? "
                "AND deleted = 0 ORDER BY id",
                (workspace_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_offer(workspace_id: int, offer_id: int) -> dict | None:
    """Fetch one of this workspace's offers by id, deleted or not (callers
    that need a still-active offer should check the 'deleted' flag)."""
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM offer_profiles WHERE id = ? AND workspace_id = ?",
            (offer_id, workspace_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def get_default_offer(workspace_id: int) -> dict | None:
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM offer_profiles WHERE workspace_id = ? "
            "AND is_default = 1 AND deleted = 0",
            (workspace_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def create_offer(workspace_id: int, fields: dict) -> int:
    """Insert a new offer profile. Returns the new offer's id.

    Raises ValueError if name/product_description/value_prop are blank.
    """
    for key in ("name", "product_description", "value_prop"):
        if not (fields.get(key) or "").strip():
            raise ValueError(f"offer '{key}' is required")
    values = {"workspace_id": workspace_id}
    values.update({k: fields.get(k) for k in _OFFER_FIELD_KEYS})
    values["created_at"] = db.utcnow()
    con = connect()
    try:
        with con.tx():
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            return con.insert_id(
                f"INSERT INTO offer_profiles ({cols}) VALUES ({marks})",
                tuple(values.values()),
            )
    finally:
        con.close()


def update_offer(workspace_id: int, offer_id: int, fields: dict) -> None:
    """Update whitelisted columns on one workspace's offer. Unknown keys in
    `fields` are ignored; use set_default_offer/delete_offer for those flags."""
    updates = {k: v for k, v in fields.items() if k in _OFFER_FIELD_KEYS}
    if not updates:
        return
    con = connect()
    try:
        with con.tx():
            assignments = ", ".join(f"{k} = ?" for k in updates)
            con.execute(
                f"UPDATE offer_profiles SET {assignments} "
                "WHERE id = ? AND workspace_id = ?",
                (*updates.values(), offer_id, workspace_id),
            )
    finally:
        con.close()


def delete_offer(workspace_id: int, offer_id: int) -> None:
    """Soft-delete an offer profile (never hard-deleted, so leads scored
    against it can still show the offer name historically).

    Refuses to delete the default offer — set a new default first.
    """
    con = connect()
    try:
        row = con.execute(
            "SELECT is_default FROM offer_profiles "
            "WHERE id = ? AND workspace_id = ?",
            (offer_id, workspace_id),
        ).fetchone()
        if row is None:
            return
        if row["is_default"]:
            raise ValueError("cannot delete the default offer; set another default first")
        with con.tx():
            con.execute(
                "UPDATE offer_profiles SET deleted = 1 "
                "WHERE id = ? AND workspace_id = ?",
                (offer_id, workspace_id),
            )
    finally:
        con.close()


def set_default_offer(workspace_id: int, offer_id: int) -> None:
    """Make one offer this workspace's default, clearing the flag on all its
    others. Refuses to set a soft-deleted offer as default."""
    con = connect()
    try:
        row = con.execute(
            "SELECT deleted FROM offer_profiles WHERE id = ? AND workspace_id = ?",
            (offer_id, workspace_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"no offer with id {offer_id}")
        if row["deleted"]:
            raise ValueError("cannot set a deleted offer as default")
        with con.tx():
            con.execute(
                "UPDATE offer_profiles SET is_default = 0 WHERE workspace_id = ?",
                (workspace_id,),
            )
            con.execute(
                "UPDATE offer_profiles SET is_default = 1 "
                "WHERE id = ? AND workspace_id = ?",
                (offer_id, workspace_id),
            )
    finally:
        con.close()


def ensure_default_offer(con: db.DB, workspace_id: int) -> None:
    """Seed the standard SMS-bot offer if this workspace has no active offer
    yet. Idempotent — called on connect() for the owner workspace and at
    workspace creation, so every workspace always has a usable offer."""
    row = con.execute(
        "SELECT COUNT(*) AS n FROM offer_profiles "
        "WHERE workspace_id = ? AND deleted = 0",
        (workspace_id,),
    ).fetchone()
    if row["n"] > 0:
        return
    with con.tx():
        con.execute(
            """
            INSERT INTO offer_profiles (
                workspace_id, name, product_description, value_prop,
                target_customer, tone, cta_style, qualification_hints,
                disqualifiers, is_default, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                workspace_id,
                "SMS text-back bot",
                "An automated text-back assistant for small local businesses: "
                "when a customer calls and isn't answered or messages after "
                "hours, it texts them back in seconds, answers their common "
                "questions automatically, and sends a booking link so they "
                "can schedule themselves, no extra staff needed.",
                "Catch the calls and messages you're missing and turn them "
                "into booked appointments automatically, without hiring "
                "anyone.",
                "Small local service businesses that rely on phone calls and "
                "appointments (dentists, salons, clinics, repair shops) and "
                "lack online booking or instant text reply.",
                "casual, direct, value-first",
                "one low-pressure soft call-to-action, e.g. offering a "
                "2-minute video",
                "Relies on calls/appointments; no online booking; no live "
                "chat; likely misses after-hours calls; no way to text the "
                "business; phone-only contact.",
                "Already has online booking or live chat; not "
                "appointment-driven; enterprise or chain business.",
                db.utcnow(),
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the lead database.")
    parser.add_argument("--counts", action="store_true", help="print lead counts by status")
    args = parser.parse_args()

    if args.counts:
        print(f"Lead DB: {DB_PATH}")
        for ws in list_workspaces():
            counts = count_by_status(ws["id"])
            total = sum(counts.values())
            print(f"Workspace {ws['id']} — {ws['name']}: {total} lead(s)")
            for status, n in counts.items():
                print(f"  {status:15} {n}")
    else:
        print(f"Lead DB: {DB_PATH} (use --counts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
