"""One-way push of lead state into GoHighLevel — ours -> GHL, never back.

Stage-4 CRM sync (plans/04_STAGE4_CRM_SYNC.md B). Auth is a Private
Integration token (sub-account Settings -> Private Integrations), NOT OAuth —
OAuth is Stage-6 multi-tenant territory. `.env` needs GHL_API_TOKEN +
GHL_LOCATION_ID, and GHL_SYNC_ENABLED=true to do anything at all: the default
is a hard no-op with zero API calls, because most early clients live in the
built-in mini-CRM (/pipeline) and never need this module.

Per changed lead (status moved since last sync):
  1. upsert contact  -> leads.ghl_contact_id
  2. create/move opportunity per data/ghl_stage_map.json -> ghl_opportunity_id
     (map file missing -> pipelines fetched once into a reference file for the
     owner to copy IDs from; contacts still sync meanwhile)
  3. push each processed reply as a contact note (classification + summary +
     text) so a client living in GHL needs nothing from our UI

Failures never propagate — GHL being down must not stop sending or
classifying. 3 consecutive failures abort the run with an ALERT (Stage 5
turns that into a phone push); everything retries next tick because
ghl_synced_at only advances on success.

Usage:
    python tools/ghl_sync.py --sync
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import lead_db  # noqa: E402
from config.settings import DATA_DIR, require  # noqa: E402

# GHL is the OWNER's CRM with a single global token in .env, so this module
# syncs the owner workspace only (plans/v2/04 B: nothing processes
# cross-workspace rows). Per-tenant CRM sync would need per-tenant creds —
# out of scope until a stage plans it.
SYNC_WORKSPACE_ID = lead_db.OWNER_WORKSPACE_ID

API_BASE = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"  # requests without this header fail oddly
STAGE_MAP_FILE = DATA_DIR / "ghl_stage_map.json"
PIPELINES_REF_FILE = DATA_DIR / "ghl_pipelines_reference.json"
MAX_CONSECUTIVE_FAILURES = 3

# Statuses worth showing a client. 'bounced'/'excluded' stay out - noise.
SYNC_STATUSES = (
    "contacted", "interested", "needs_human", "meeting",
    "won", "lost", "not_interested", "unsubscribed",
)

# Our status -> GHL opportunity status field (stage id comes from the map file).
_OPP_STATUS = {"won": "won", "lost": "lost", "not_interested": "lost", "unsubscribed": "lost"}


def sync_enabled() -> bool:
    return os.environ.get("GHL_SYNC_ENABLED", "false").strip().lower() == "true"


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    """Single HTTP door to GHL — tests replace this function."""
    import requests

    resp = requests.request(
        method,
        API_BASE + path,
        headers={
            "Authorization": f"Bearer {require('GHL_API_TOKEN')}",
            "Version": API_VERSION,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _load_stage_map() -> dict | None:
    """{'pipeline_id': ..., 'stages': {our_status: stage_id}} or None."""
    if not STAGE_MAP_FILE.exists():
        return None
    try:
        m = json.loads(STAGE_MAP_FILE.read_text(encoding="utf-8"))
        return m if m.get("pipeline_id") and m.get("stages") else None
    except (ValueError, OSError):
        print(f"  [ghl] stage map unreadable: {STAGE_MAP_FILE}", file=sys.stderr)
        return None


def _fetch_pipelines_reference(location_id: str) -> None:
    """No stage map yet: dump the account's pipelines so the owner can copy
    pipeline/stage IDs into ghl_stage_map.json. Runs at most once per tick."""
    if PIPELINES_REF_FILE.exists():
        return
    data = _request("GET", f"/opportunities/pipelines?locationId={location_id}")
    PIPELINES_REF_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"  [ghl] no stage map — wrote {PIPELINES_REF_FILE.name}. Copy the "
        f"pipeline id + stage ids you want into {STAGE_MAP_FILE.name} as "
        '{"pipeline_id": "...", "stages": {"contacted": "<stage id>", ...}}. '
        "Contacts sync meanwhile; opportunities start once the map exists."
    )


def _upsert_contact(lead: dict, location_id: str) -> str | None:
    payload = {
        "locationId": location_id,
        "email": lead["email"],
        "name": lead["name"],
        "tags": ["leadgen-agent"] + ([lead["category"]] if lead.get("category") else []),
    }
    if lead.get("phone"):
        payload["phone"] = lead["phone"]
    if lead.get("city"):
        payload["city"] = lead["city"]
    data = _request("POST", "/contacts/upsert", payload)
    return (data.get("contact") or {}).get("id")


def _sync_opportunity(con, lead: dict, contact_id: str, stage_map: dict,
                      location_id: str) -> bool:
    """Create or move the lead's opportunity. Returns True if it did anything."""
    stage_id = stage_map["stages"].get(lead["status"])
    opp_status = _OPP_STATUS.get(lead["status"], "open")
    if lead.get("ghl_opportunity_id"):
        body: dict = {"status": opp_status}
        if stage_id:
            body["pipelineStageId"] = stage_id
        _request("PUT", f"/opportunities/{lead['ghl_opportunity_id']}", body)
        return True
    if not stage_id:
        return False  # unmapped status and nothing to move yet
    data = _request("POST", "/opportunities/", {
        "pipelineId": stage_map["pipeline_id"],
        "locationId": location_id,
        "contactId": contact_id,
        "pipelineStageId": stage_id,
        "name": lead["name"],
        "status": opp_status,
    })
    opp_id = (data.get("opportunity") or {}).get("id")
    if opp_id:
        con.execute(
            "UPDATE leads SET ghl_opportunity_id = ? "
            "WHERE id = ? AND workspace_id = ?",
            (opp_id, lead["id"], SYNC_WORKSPACE_ID),
        )
    return True


def _push_notes(con, lead_id: int, contact_id: str) -> int:
    """Push processed-but-unpushed replies as GHL contact notes."""
    rows = con.execute(
        "SELECT * FROM replies WHERE workspace_id = ? AND lead_id = ? "
        "AND processed_at IS NOT NULL "
        "AND ghl_pushed_at IS NULL ORDER BY id",
        (SYNC_WORKSPACE_ID, lead_id),
    ).fetchall()
    pushed = 0
    for r in rows:
        body = (
            f"[leadgen-agent reply] {r['classification']} ({r['confidence']})\n"
            f"{r['summary'] or ''}\n"
            f"Suggested: {r['suggested_action'] or '-'}\n\n"
            f"{(r['body_text'] or '')[:2000]}"
        )
        _request("POST", f"/contacts/{contact_id}/notes", {"body": body})
        # Commit per note - a failure on note N must not roll back the pushed
        # marks of notes 1..N-1, or the retry would duplicate them in GHL.
        with con.tx():
            con.execute(
                "UPDATE replies SET ghl_pushed_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (db.utcnow(), r["id"], SYNC_WORKSPACE_ID),
            )
        pushed += 1
    return pushed


def sync_changed_leads() -> dict:
    """Push every lead whose status moved since its last sync. Never raises."""
    counters = {"contacts": 0, "opportunities": 0, "notes": 0, "failed": 0}
    if not sync_enabled():
        return counters

    try:
        location_id = require("GHL_LOCATION_ID")
    except RuntimeError as exc:
        print(f"ALERT: GHL sync enabled but misconfigured: {exc}", file=sys.stderr)
        counters["failed"] += 1
        return counters

    stage_map = _load_stage_map()
    if stage_map is None:
        try:
            _fetch_pipelines_reference(location_id)
        except Exception as exc:  # noqa: BLE001 - reference fetch is best-effort
            print(f"  [ghl] pipeline fetch failed: {exc}", file=sys.stderr)

    con = lead_db.connect()
    consecutive = 0
    try:
        marks = ",".join("?" for _ in SYNC_STATUSES)
        # "Changed" = never synced, status differs from the last one we pushed
        # (timestamp compare alone misses same-second moves - stored datetime
        # is second-resolution), or a processed reply still awaits its note.
        changed = con.execute(
            f"SELECT * FROM leads WHERE workspace_id = ? AND status IN ({marks}) "
            "AND email IS NOT NULL "
            "AND (ghl_synced_at IS NULL "
            "     OR ghl_synced_status IS NULL OR status != ghl_synced_status "
            "     OR updated_at > ghl_synced_at "
            "     OR id IN (SELECT lead_id FROM replies WHERE workspace_id = ? "
            "               AND lead_id IS NOT NULL "
            "               AND processed_at IS NOT NULL AND ghl_pushed_at IS NULL)) "
            "ORDER BY id",
            (SYNC_WORKSPACE_ID, *SYNC_STATUSES, SYNC_WORKSPACE_ID),
        ).fetchall()

        for row in changed:
            lead = dict(row)
            try:
                with con.tx():
                    contact_id = lead.get("ghl_contact_id") or _upsert_contact(lead, location_id)
                    if not contact_id:
                        raise ValueError("contact upsert returned no id")
                    if contact_id != lead.get("ghl_contact_id"):
                        con.execute(
                            "UPDATE leads SET ghl_contact_id = ? "
                            "WHERE id = ? AND workspace_id = ?",
                            (contact_id, lead["id"], SYNC_WORKSPACE_ID),
                        )
                    counters["contacts"] += 1
                    if stage_map and _sync_opportunity(con, lead, contact_id,
                                                       stage_map, location_id):
                        counters["opportunities"] += 1
                # Notes commit one by one (see _push_notes); ghl_synced_at
                # advances only after ALL of a lead's work succeeded, so a
                # partial failure retries next tick.
                counters["notes"] += _push_notes(con, lead["id"], contact_id)
                with con.tx():
                    con.execute(
                        "UPDATE leads SET ghl_synced_at = ?, "
                        "ghl_synced_status = ? WHERE id = ? AND workspace_id = ?",
                        (db.utcnow(), lead["status"], lead["id"], SYNC_WORKSPACE_ID),
                    )
                consecutive = 0
            except Exception as exc:  # noqa: BLE001 - one bad lead must not kill the tick
                counters["failed"] += 1
                consecutive += 1
                print(f"  [ghl] sync failed for lead {lead['id']} "
                      f"({lead['name']}): {exc}", file=sys.stderr)
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"ALERT: GHL sync aborted after {consecutive} consecutive "
                        "failures (bad token? GHL down?). Will retry next tick.",
                        file=sys.stderr,
                    )
                    break
    finally:
        con.close()

    if any(counters.values()):
        print(f"GHL sync: {counters}")
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="Push changed leads to GoHighLevel.")
    parser.add_argument("--sync", action="store_true", required=True)
    args = parser.parse_args()
    if not sync_enabled():
        print("GHL_SYNC_ENABLED is not 'true' - nothing to do.")
        return 0
    sync_changed_leads()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
