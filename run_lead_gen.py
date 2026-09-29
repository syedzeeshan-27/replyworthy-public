"""Run the full lead-gen pipeline end to end.

Chains the three WAT tools, each of which writes a timestamped file that the
next one picks up as "latest":

    1. tools/scrape_google_maps.py  --category --city --limit  -> .tmp/maps_raw_*.json
    2. tools/enrich_leads.py                                   -> .tmp/leads_enriched_*.json
    3. tools/save_to_excel.py                                  -> outputs/leads_<date>.xlsx

Then prints a summary: total leads found, how many scored 4-5 (the hot
prospects), and the path to the Excel deliverable.

Usage:
    python run_lead_gen.py --category "dentist" --city "Austin TX" --limit 50

NOTE: every step runs live and spends real credits — Apify (APIFY_API_TOKEN),
Jina/Firecrawl website scraping (FIRECRAWL_API_KEY), and the Claude API
(ANTHROPIC_API_KEY). Make sure all keys are set in .env before running.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent
TOOLS_DIR = PROJECT_ROOT / "tools"

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TOOLS_DIR))
import billing  # noqa: E402
from config.settings import TMP_DIR, OUTPUTS_DIR, require  # noqa: E402
from lead_db import OWNER_WORKSPACE_ID, count_by_status, upsert_leads  # noqa: E402
from parse_query import spec_from_args, to_maps_params  # noqa: E402


def _preflight(source: str = "google_maps", need_ai: bool = True) -> None:
    """Fail fast on missing keys before any credits are spent (a missing
    ANTHROPIC_API_KEY would otherwise only surface deep into enrichment).
    Which keys are needed depends on the source connector (plans/v2/03).

    `need_ai=False` is the scrape-only preflight: the list fork of the gate
    produces name/phone/website with no Claude call, so it must NOT demand
    ANTHROPIC_API_KEY. Enrichment always passes need_ai=True.
    """
    if source == "b2b_data_api":
        require("B2B_DATA_API_KEY")
        require("B2B_DATA_API_URL")
    else:
        require("APIFY_API_TOKEN")
    if not need_ai:
        return
    require("ANTHROPIC_API_KEY")
    if not os.environ.get("FIRECRAWL_API_KEY"):
        print(
            "WARNING: FIRECRAWL_API_KEY not set - website scraping will rely "
            "on Jina Reader only (no fallback)."
        )


def run_step(label: str, args: list[str]) -> None:
    """Run one pipeline tool as a subprocess, aborting the pipeline if it fails."""
    print(f"\n=== {label} ===")
    result = subprocess.run([sys.executable, *args], cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise SystemExit(f"Step failed ({label}); aborting pipeline.")


# Slack on the freshness check: file mtimes are coarser than time.time() on
# some filesystems, so a file written moments after t0 can stamp just before
# it. A stale file is minutes old, never within this window.
_MTIME_TOLERANCE = 2.0


def _latest(pattern: str, newer_than: float | None = None) -> Path | None:
    """Most recently modified file in .tmp/ matching `pattern`, or None.

    `newer_than` (an epoch time recorded before the step ran) rejects a match
    that predates this run. Without it, a run whose step produced no file
    silently reads the PREVIOUS run's output as its own — wrong funnel, wrong
    debit, and with concurrent jobs, another workspace's leads.
    """
    candidates = sorted(
        TMP_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        return None
    newest = candidates[0]
    if newer_than is not None and newest.stat().st_mtime < newer_than - _MTIME_TOLERANCE:
        return None
    return newest


def _new_run_id() -> str:
    """A per-run id: a timestamp for readability plus a short random tail.

    The tail matters — a bare second-resolution timestamp collides once two
    jobs overlap (the gate lets them), and colliding run ids would let one
    run read another's .tmp files. Every intermediate this run writes is
    named with it, so no step ever globs for "the latest" file.
    """
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def _write_spec(spec: dict, run_id: str) -> Path:
    """Persist the LeadSpec so the (subprocess) Excel step can put it on the
    deliverable's Search sheet. Run-scoped name — see _new_run_id."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    spec_path = TMP_DIR / f"leadspec_{run_id}.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return spec_path


def _empty_funnel(seed: dict | None = None) -> dict:
    """The funnel skeleton the job page reads. `seed` carries the scrape-side
    counts (found + the three drop reasons known before enrichment)."""
    seed = seed or {}
    return {
        "found": seed.get("found", 0),
        "dropped_dupes": seed.get("dropped_dupes", 0),
        "dropped_chains": seed.get("dropped_chains", 0),
        "dropped_seen": seed.get("dropped_seen", 0),
        "skipped_no_contact": 0,
        "excluded_equipped": 0,
        "excluded_non_us": 0,
        "delivered": 0,
    }


def scrape_phase(
    spec: dict,
    workspace_id: int,
    limit: int | None = None,
    *,
    filter_seen: bool = False,
    need_ai: bool = False,
    run_id: str | None = None,
    spec_path: Path | None = None,
) -> dict:
    """PHASE 1 — the Apify scrape only. No Claude, no credits moved, no DB
    write. google_maps only (the b2b person path stays one-shot).

    `filter_seen=False` (the gate default) shows everything and marks nothing,
    so a preview the owner may cancel can't poison the cross-run ledger. The
    one-shot pipeline passes True to keep the CLI's "fresh leads only,
    recorded immediately" behaviour.

    Returns a phase-1 payload the fork functions (list_only / enrich_phase)
    consume: {run_id, spec, source, spec_path, raw_path, results, funnel_seed,
    found, already_seen, truncated, seen_marked}.
    """
    source = spec.get("source")
    if source != "google_maps":
        raise RuntimeError(
            f"scrape_phase handles google_maps only (got '{source}'); "
            "the b2b person path runs one-shot through run_pipeline."
        )
    _preflight(source, need_ai=need_ai)
    run_id = run_id or _new_run_id()
    if spec_path is None:
        spec_path = _write_spec(spec, run_id)

    params = to_maps_params(spec)
    req_limit = limit if limit is not None else params["limit"]

    # Lazy import: pulls requests + the maps deps only when a scrape runs, and
    # keeps the module patchable (tests stub sources.google_maps.run_search).
    from sources import google_maps  # noqa: E402
    search = google_maps.run_search(
        params["category"], params["city"], req_limit,
        keywords=spec.get("keywords"), filter_seen=filter_seen,
    )
    results = search["results"]

    raw_path = TMP_DIR / f"maps_raw_{run_id}.json"
    raw_path.write_text(json.dumps({
        "query": {"category": params["category"], "city": params["city"],
                  "limit": req_limit},
        "scraped_at": run_id,
        "count": len(results),
        "found": search["found"],
        "dropped_dupes": search["dropped_dupes"],
        "dropped_chains": search["dropped_chains"],
        "dropped_seen": search["dropped_seen"],
        "already_seen": search["already_seen"],
        "truncated": search["truncated"],
        "results": results,
        "raw": search["raw"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "run_id": run_id,
        "spec": spec,
        "source": source,
        "spec_path": spec_path,
        "raw_path": raw_path,
        "results": results,
        "funnel_seed": {
            "found": search["found"],
            "dropped_dupes": search["dropped_dupes"],
            "dropped_chains": search["dropped_chains"],
            "dropped_seen": search["dropped_seen"],
        },
        "found": search["found"],
        "already_seen": search["already_seen"],
        "truncated": search["truncated"],
        "seen_marked": filter_seen,
    }


def _mark_delivered_seen(phase1: dict, leads: list[dict]) -> None:
    """Record delivered leads in the cross-run ledger — but only for a maps
    scrape that didn't already mark them (the gate's filter_seen=False path).
    The one-shot CLI marks at scrape time, so this is a no-op there."""
    if phase1["source"] != "google_maps" or phase1.get("seen_marked"):
        return
    from seen_ledger import mark_seen  # noqa: E402
    params = to_maps_params(phase1["spec"])
    mark_seen(leads, params["category"], params["city"])


def list_only(phase1: dict, workspace_id: int) -> dict:
    """FREE FORK — the "just the leads, no emails" deliverable.

    Marks the delivered leads seen and returns them plus a funnel. Writes
    NOTHING to outputs/ and NOTHING to the lead DB (unscored, copy-less rows
    would leak into the outreach pipeline), and moves zero credits.
    """
    results = phase1["results"]
    _mark_delivered_seen(phase1, results)

    funnel = _empty_funnel(phase1["funnel_seed"])
    funnel["dropped_seen"] = 0  # the list shows everything, on purpose
    funnel["delivered"] = len(results)

    return {
        "total_found": funnel["found"],
        "funnel": funnel,
        "source": phase1["source"],
        "results": results,
        "already_seen": phase1.get("already_seen", 0),
        "hot_leads": 0,
        "credits_charged": 0,
        "output_path": None,
    }


def enrich_phase(
    phase1: dict,
    workspace_id: int,
    us_only: bool = False,
    offer_id: int | None = None,
) -> dict:
    """PAID FORK — enrich (Claude) → Excel → finalize. Same summary dict as
    run_pipeline. Explicit --in-file/--out-file, so no step ever reads another
    run's .tmp file."""
    _preflight(phase1["source"], need_ai=True)
    run_id = phase1["run_id"]
    enriched_path = TMP_DIR / f"leads_enriched_{run_id}.json"

    enrich_args = [str(TOOLS_DIR / "enrich_leads.py"),
                   "--workspace-id", str(workspace_id),
                   "--in-file", str(phase1["raw_path"]),
                   "--run-id", run_id]
    if us_only:
        enrich_args.append("--us-only")
    if offer_id is not None:
        enrich_args.extend(["--offer-id", str(offer_id)])
    run_step("Enrich leads", enrich_args)
    run_step("Save to Excel", [
        str(TOOLS_DIR / "save_to_excel.py"),
        "--in-file", str(enriched_path),
        "--spec-file", str(phase1["spec_path"]),
        "--run-id", run_id,
    ])
    return _finalize_run(phase1, workspace_id, enriched_path)


def _finalize_run(phase1: dict, workspace_id: int, enriched_path: Path) -> dict:
    """Assemble the funnel, persist the run to the lead DB, meter delivered
    leads, and record the widget-excluded audit. The one home of that logic —
    run_pipeline and the gate's continue fork both land here."""
    spec = phase1["spec"]
    source = phase1["source"]
    run_id = phase1["run_id"]
    funnel = _empty_funnel(phase1["funnel_seed"])

    hot_leads = 0
    credits_charged = 0
    db_counts = {"inserted": 0, "updated": 0}

    if enriched_path.exists():
        enriched_payload = json.loads(enriched_path.read_text(encoding="utf-8"))
        enriched_results = enriched_payload.get("results", [])
        funnel["skipped_no_contact"] = enriched_payload.get("skipped", 0)
        funnel["excluded_equipped"] = enriched_payload.get("excluded_existing", 0)
        funnel["excluded_non_us"] = enriched_payload.get("excluded_non_us", 0)
        funnel["delivered"] = enriched_payload.get("count", len(enriched_results))
        # Stamp what was asked onto every lead from this run, so "what did I
        # ask for" survives to the DB and the lead detail page (plan v2/02 C).
        for lead in enriched_results:
            lead["search_spec"] = spec
        hot_leads = sum(
            1
            for lead in enriched_results
            if isinstance(lead.get("score"), int) and lead["score"] >= 4
        )

        # Persist this run into the lead DB — the cross-run source of truth
        # the outreach stages read. Excel stays the client deliverable.
        db_counts = upsert_leads(workspace_id, enriched_results, status="new")
        # Meter the run (plans/v2/05 A): DELIVERED leads only — the excluded
        # ones below cost the customer nothing. The owner's internal plan
        # debits nothing.
        n_person = sum(1 for lead in enriched_results
                       if (lead.get("entity_type") or "business") == "person")
        credits_charged = billing.debit_leads(
            workspace_id, len(enriched_results) - n_person, n_person,
            ref=f"run-{run_id}",
        )
        # Widget-excluded leads share the run id; recorded with
        # status='excluded' so the audit survives .tmp/ cleanup.
        excl_file = TMP_DIR / f"leads_excluded_{run_id}.json"
        if excl_file.exists():
            excluded_results = json.loads(excl_file.read_text(encoding="utf-8")).get(
                "results", []
            )
            for lead in excluded_results:
                lead["search_spec"] = spec
                detected = lead.get("detected") or []
                excl_counts = upsert_leads(
                    workspace_id, [lead],
                    status="excluded", status_reason=", ".join(detected)
                )
                for k in db_counts:
                    db_counts[k] += excl_counts[k]

        # A delivered enrich run records its leads in the ledger too (gate
        # path only; the one-shot CLI already marked them at scrape).
        _mark_delivered_seen(phase1, enriched_results)

    output_path = _latest_output(run_id)

    return {
        "total_found": funnel["found"],
        "funnel": funnel,
        # Which connector ran — the UI words its funnel around it ("Found on
        # Google Maps" vs "Found in the contact database").
        "source": source,
        "hot_leads": hot_leads,
        "credits_charged": credits_charged,
        "output_path": str(output_path) if output_path else None,
        "db_inserted": db_counts["inserted"],
        "db_updated": db_counts["updated"],
        "db_totals": count_by_status(workspace_id),
    }


def _latest_output(run_id: str) -> Path | None:
    """The Excel this run wrote (outputs/leads_<date>_<run_id>.xlsx). Scoped
    to the run id so overlapping jobs never hand out each other's file."""
    matches = sorted(
        OUTPUTS_DIR.glob(f"leads_*_{run_id}.xlsx"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return matches[0] if matches else None


def run_pipeline(
    category: str | None = None,
    city: str | None = None,
    limit: int = 50,
    us_only: bool = False,
    offer_id: int | None = None,
    spec: dict | None = None,
    workspace_id: int = OWNER_WORKSPACE_ID,
) -> dict:
    """Run the full pipeline programmatically, one shot. Returns summary dict.

    Takes either the classic category/city/limit args (CLI and the business
    search form — mapped to a hand-built LeadSpec) or a ready `spec`, which
    is how the form asks for people. The spec's source (picked by the
    deterministic router, never a model) decides the connector: business →
    Google Maps, person → the B2B data API (plans/v2/03).

    The web business path no longer comes through here — it runs the two
    phases either side of the approve gate. This stays the CLI entry point and
    the one-shot person path.

    Every lead this run produces lands in `workspace_id` (V2 Stage 4). The
    CLI keeps the owner workspace; the web layer passes the session's.
    """
    if spec is None:
        if not (category and city):
            raise ValueError("run_pipeline needs either a spec or category + city")
        spec = spec_from_args(category, city, limit)
    source = spec.get("source")
    if source not in ("google_maps", "b2b_data_api"):
        raise RuntimeError(
            f"Unknown lead source '{source}' — this spec can't run. "
            "Supported sources: google_maps (businesses), b2b_data_api (people)."
        )

    # Billing gate BEFORE any vendor money is spent (plans/v2/05 A): the
    # workspace must be in good standing and hold enough credits for the
    # worst case (the full requested count).
    guard = billing.run_guard(workspace_id, spec.get("count") or limit,
                              spec.get("entity_type"))
    if guard:
        raise billing.BillingBlocked(guard)

    req_limit = spec.get("count") or limit
    run_id = _new_run_id()
    spec_path = _write_spec(spec, run_id)

    if source == "b2b_data_api":
        _preflight(source, need_ai=True)  # fail fast before vendor money
        raw_path = TMP_DIR / f"b2b_raw_{run_id}.json"
        run_step("Search B2B data API", [
            str(TOOLS_DIR / "sources" / "b2b_data_api.py"),
            "--spec-file", str(spec_path),
            "--run-id", run_id,
        ])
        b2b_payload = json.loads(raw_path.read_text(encoding="utf-8"))
        phase1 = {
            "run_id": run_id, "spec": spec, "source": source,
            "spec_path": spec_path, "raw_path": raw_path,
            "results": b2b_payload.get("results", []),
            "funnel_seed": {
                "found": b2b_payload.get("found", 0),
                "dropped_dupes": b2b_payload.get("dropped_dupes", 0),
                "dropped_chains": b2b_payload.get("dropped_chains", 0),
                "dropped_seen": b2b_payload.get("dropped_seen", 0),
            },
            "found": b2b_payload.get("found", 0),
            "already_seen": 0, "truncated": 0, "seen_marked": True,
        }
    else:
        # One-shot maps: filter_seen=True keeps the CLI's fresh-leads-only
        # behaviour; need_ai=True fails fast on a missing Claude key before
        # the Apify spend.
        phase1 = scrape_phase(
            spec, workspace_id, limit=req_limit,
            filter_seen=True, need_ai=True, run_id=run_id, spec_path=spec_path,
        )

    return enrich_phase(phase1, workspace_id, us_only=us_only, offer_id=offer_id)


# Friendly labels for the funnel's drop reasons, shared by the CLI summary
# below and reused as reference wording for the web job page.
FUNNEL_DROP_LABELS = (
    ("dropped_dupes", "the same business listed more than once"),
    ("dropped_chains", "chains or duplicate listings"),
    ("dropped_seen", "already found in a previous search"),
    ("excluded_equipped", "already have chat/booking software"),
    ("excluded_non_us", "no US phone number"),
    ("skipped_no_contact", "no website or email to reach them"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full lead-gen pipeline.")
    parser.add_argument("--category", required=True, help='e.g. "dentist"')
    parser.add_argument("--city", required=True, help='e.g. "Austin TX"')
    parser.add_argument("--limit", type=int, default=50, help="max places to scrape (default 50, capped at 500)")
    parser.add_argument("--us-only", action="store_true", help="keep only US/Canada phone numbers")
    parser.add_argument(
        "--offer-id",
        type=int,
        default=None,
        help="offer_profiles id to analyze/score leads against (default: the workspace's default offer)",
    )
    args = parser.parse_args()

    result = run_pipeline(args.category, args.city, args.limit, args.us_only, args.offer_id)

    print("\n" + "=" * 40)
    print("PIPELINE COMPLETE")
    print("=" * 40)
    funnel = result["funnel"]
    print(f"Found on Google Maps : {funnel['found']}")
    print(f"Delivered            : {funnel['delivered']}")
    for key, label in FUNNEL_DROP_LABELS:
        if funnel.get(key):
            print(f"  filtered: {funnel[key]} {label}")
    print(f"Leads scored 4-5  : {result['hot_leads']}")
    print(f"Output XLSX       : {result['output_path'] or '(none written)'}")
    print(f"Lead DB           : +{result['db_inserted']} new, {result['db_updated']} refreshed")
    print(f"Lead DB totals    : {result['db_totals']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
