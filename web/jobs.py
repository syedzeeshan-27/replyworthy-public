"""In-memory job registry and background runner for the lead-gen pipeline."""
from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import run_lead_gen  # noqa: E402
from run_lead_gen import run_pipeline  # noqa: E402
from parse_query import spec_from_args, to_maps_params  # noqa: E402

JOBS: dict[str, Job] = {}
_lock = threading.Lock()

# A gate the owner never answers must not sit forever holding their workspace's
# attention. After this it's quietly canceled — nothing was charged.
GATE_TTL_MINUTES = 30

# Statuses that count as "this workspace already has a run in flight". A job
# parked at the gate (awaiting_choice) deliberately does NOT — otherwise a
# preview the owner is still reading would block their next search.
BUSY_STATUSES = ("queued", "running")
FINISHED_JOB_TTL = timedelta(hours=24)


@dataclass
class Job:
    id: str
    category: str
    city: str
    limit: int
    workspace_id: int = 1
    us_only: bool = False
    offer_id: Optional[int] = None
    spec: Optional[dict] = None
    status: str = "queued"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    output_path: Optional[str] = None
    total_found: int = 0
    hot_leads: int = 0
    funnel: Optional[dict] = None
    source: Optional[str] = None  # which connector ran (google_maps | b2b_data_api)
    error_msg: Optional[str] = None
    # Two-phase (approve-gate) fields.
    phase: str = "scrape"          # scrape | gate | list | enrich
    scrape: Optional[dict] = None  # phase-1 payload held across the gate
    gate_expires_at: Optional[datetime] = None
    credits_charged: int = 0
    run_id: Optional[str] = None
    already_seen: int = 0


def _expire_stale_gates() -> None:
    """Cancel any gate the owner never answered. 0 credits — nothing was
    debited on a fork that never ran. Callers already hold _lock."""
    now = datetime.now(timezone.utc)
    for job in JOBS.values():
        if (job.status == "awaiting_choice" and job.gate_expires_at
                and now > job.gate_expires_at):
            job.status = "canceled"
            job.phase = "gate"
            job.finished_at = now
    # Finished jobs keep their scraped rows in memory: drop day-old ones.
    for job_id in [j.id for j in JOBS.values()
                   if j.finished_at and j.status not in BUSY_STATUSES
                   and j.status != "awaiting_choice"
                   and now - j.finished_at > FINISHED_JOB_TTL]:
        del JOBS[job_id]


def other_job_running(workspace_id: int, job_id: str) -> bool:
    """True while this workspace has a run going other than `job_id`."""
    with _lock:
        return any(j.workspace_id == workspace_id and j.id != job_id
                   and j.status in BUSY_STATUSES for j in JOBS.values())


def _is_running(workspace_id: int) -> bool:
    """Per-workspace now: one tenant's run must never block another's, and a
    job parked at the gate must not block its own workspace's next search."""
    _expire_stale_gates()
    return any(j.workspace_id == workspace_id and j.status in BUSY_STATUSES
               for j in JOBS.values())


def start_job(workspace_id: int, category: str, city: str, limit: int,
              us_only: bool = False, offer_id: int | None = None,
              spec: dict | None = None, *, two_phase: bool = False) -> str | None:
    """Queue a job. `two_phase=True` (business searches) runs the scrape then
    PAUSES at the approve gate; everything else keeps the one-shot path.

    NOTE: the positional order (…, limit, us_only, …) is asserted by tests —
    new params stay keyword-only at the end.
    """
    if spec is not None:
        # Search-box path: display fields come from the spec, same mapping
        # the pipeline itself uses.
        params = to_maps_params(spec)
        category, city, limit = params["category"], params["city"], params["limit"]
        if spec.get("entity_type") == "person" and spec.get("role"):
            category = f"{spec['role']} of {category}"  # display only
    with _lock:
        if _is_running(workspace_id):
            return None
        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, category=category, city=city, limit=limit,
                  workspace_id=workspace_id, us_only=us_only,
                  offer_id=offer_id, spec=spec,
                  phase="scrape" if two_phase else "run")
        JOBS[job_id] = job

    target = _run_scrape if two_phase else _run
    t = threading.Thread(target=target, args=(job_id,), daemon=True)
    t.start()
    return job_id


def get_job(job_id: str, workspace_id: int | None = None) -> Job | None:
    """Look up a job; a workspace only ever sees its own jobs."""
    with _lock:
        _expire_stale_gates()
        job = JOBS.get(job_id)
    if job is None:
        return None
    if workspace_id is not None and job.workspace_id != workspace_id:
        return None
    return job


def claim_gate(job_id: str, workspace_id: int, mode: str) -> Job | None:
    """Atomically move a gated job to running for `mode` (list | enrich).
    Returns None if it isn't at the gate any more — a double-click, an expired
    gate, or the wrong workspace — so the route just re-renders instead of
    spawning a second fork (which would double-charge)."""
    with _lock:
        _expire_stale_gates()
        job = JOBS.get(job_id)
        if job is None or job.workspace_id != workspace_id:
            return None
        if job.status != "awaiting_choice":
            return None
        # One run per workspace: a second search started while this one sat
        # at the gate must finish first (both would pass the credit check).
        if any(j.workspace_id == workspace_id and j.id != job_id
               and j.status in BUSY_STATUSES for j in JOBS.values()):
            return None
        job.status = "running"
        job.phase = mode
        job.finished_at = None
        return job


def cancel_gate(job_id: str, workspace_id: int) -> Job | None:
    """Discard a gated job at the owner's request. No ledger mark, no DB write,
    0 credits — the Apify scrape was already spent, nothing else."""
    with _lock:
        _expire_stale_gates()
        job = JOBS.get(job_id)
        if job is None or job.workspace_id != workspace_id:
            return None
        if job.status == "awaiting_choice":
            job.status = "canceled"
            job.scrape = None
            job.finished_at = datetime.now(timezone.utc)
        return job


def start_choice(job_id: str, workspace_id: int, mode: str) -> Job | None:
    """Claim the gate and spawn the chosen fork. Returns the job, or None if
    the gate was already taken/expired."""
    job = claim_gate(job_id, workspace_id, mode)
    if job is None:
        return None
    t = threading.Thread(target=_run_choice, args=(job_id, mode), daemon=True)
    t.start()
    return job


def _run_scrape(job_id: str) -> None:
    """PHASE 1: scrape only, then park at the gate. No Claude, no credits."""
    job = JOBS[job_id]
    job.status = "running"
    job.phase = "scrape"
    job.started_at = datetime.now(timezone.utc)
    try:
        spec = job.spec or spec_from_args(job.category, job.city, job.limit)
        phase1 = run_lead_gen.scrape_phase(
            spec, job.workspace_id,
            limit=spec.get("count") or job.limit, filter_seen=False,
        )
        job.scrape = phase1
        job.run_id = phase1["run_id"]
        job.source = phase1["source"]
        job.total_found = phase1["found"]
        job.already_seen = phase1.get("already_seen", 0)
        seed = phase1["funnel_seed"]
        job.funnel = {
            "found": seed.get("found", 0),
            "dropped_dupes": seed.get("dropped_dupes", 0),
            "dropped_chains": seed.get("dropped_chains", 0),
            "dropped_seen": seed.get("dropped_seen", 0),
            "already_seen": phase1.get("already_seen", 0),
            "delivered": len(phase1["results"]),
        }
        job.status = "awaiting_choice"
        job.phase = "gate"
        job.gate_expires_at = (datetime.now(timezone.utc)
                               + timedelta(minutes=GATE_TTL_MINUTES))
    except BaseException:
        job.status = "error"
        job.error_msg = traceback.format_exc()
        job.finished_at = datetime.now(timezone.utc)


def _run_choice(job_id: str, mode: str) -> None:
    """PHASE 2: the owner's fork — the free list or the paid enrichment."""
    job = JOBS[job_id]
    try:
        if mode == "list":
            result = run_lead_gen.list_only(job.scrape, job.workspace_id)
            # Keep the delivered rows for the download (served as bytes).
            job.scrape["results"] = result["results"]
        else:
            result = run_lead_gen.enrich_phase(
                job.scrape, job.workspace_id,
                us_only=job.us_only, offer_id=job.offer_id,
            )
        job.total_found = result["total_found"]
        job.hot_leads = result.get("hot_leads", 0)
        job.funnel = result.get("funnel")
        job.source = result.get("source")
        job.output_path = result.get("output_path")
        job.credits_charged = result.get("credits_charged", 0)
        job.status = "done"
    except BaseException:
        job.status = "error"
        job.error_msg = traceback.format_exc()
    finally:
        job.finished_at = datetime.now(timezone.utc)


def _run(job_id: str) -> None:
    job = JOBS[job_id]
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    try:
        result = run_pipeline(job.category, job.city, job.limit, job.us_only,
                              offer_id=job.offer_id, spec=job.spec,
                              workspace_id=job.workspace_id)
        job.total_found = result["total_found"]
        job.hot_leads = result["hot_leads"]
        job.funnel = result.get("funnel")
        job.source = result.get("source")
        job.output_path = result["output_path"]
        job.status = "done"
    except BaseException:
        # BaseException, not Exception: run_step() aborts a failed pipeline
        # step with SystemExit, which Exception does NOT catch. Missing it
        # left the job stuck on "running" forever — and because the busy
        # check scans job statuses, one failed step used to block every
        # later search until the process restarted.
        job.status = "error"
        job.error_msg = traceback.format_exc()
    finally:
        job.finished_at = datetime.now(timezone.utc)


# --- Outreach heartbeat runner (Stage 5b: browser-triggered tick) ---

TICK: dict = {"running": False, "started_at": None, "finished_at": None, "results": None}


def start_tick() -> bool:
    """Run one outreach tick in the background. False if one is already running."""
    with _lock:
        if TICK["running"]:
            return False
        TICK["running"] = True
        TICK["started_at"] = datetime.now(timezone.utc)
        TICK["finished_at"] = None

    t = threading.Thread(target=_run_tick, daemon=True)
    t.start()
    return True


def _run_tick() -> None:
    try:
        from outreach_tick import run_once  # lazy: tools path set by web.app
        TICK["results"] = run_once()
    except BaseException:  # noqa: BLE001 - same rule as the job runners
        TICK["results"] = {"tick": "ERROR: " + traceback.format_exc(limit=2)}
    finally:
        TICK["running"] = False
        TICK["finished_at"] = datetime.now(timezone.utc)


# The engine paces itself: while the web server is up, a tick runs every few
# minutes without anyone clicking. The button on /campaigns becomes "run it
# right now" rather than the only way the machine moves.
AUTO_TICK_SECONDS = int(os.environ.get("AUTO_TICK_SECONDS", "300") or 0)

_auto_tick_started = False


def external_loop_active() -> bool:
    """True in the deployed container: start.sh runs `outreach_tick.py --loop`
    as its OWN process when TICK_LOOP=true. That external loop is then the sole
    ticker — the in-process pacemaker AND the manual button must stand down, or
    two tickers race on the same scheduled rows (the sequencer has no atomic
    claim) and a lead can be emailed twice. start.sh normalizes TICK_LOOP the
    same case/whitespace-insensitive way, so the two never disagree."""
    return os.environ.get("TICK_LOOP", "").strip().lower() == "true"


def auto_tick_minutes() -> int:
    """Effective self-run cadence for the UI copy: the in-process pacemaker if
    it runs here, else the production TICK_LOOP subprocess, else 0 (the button
    is the only trigger)."""
    if external_loop_active():
        try:
            from outreach_tick import LOOP_INTERVAL
            return LOOP_INTERVAL // 60
        except Exception:
            return 15
    return AUTO_TICK_SECONDS // 60 if AUTO_TICK_SECONDS > 0 else 0


def start_auto_tick() -> None:
    """Start the pacemaker thread (idempotent). Sleeps BEFORE the first tick,
    so short-lived processes — above all the test suite's TestClient, which
    triggers app startup — exit long before anything fires. Stands down when a
    dedicated tick loop already runs the engine (TICK_LOOP=true) so there is
    never more than one ticker. Set AUTO_TICK_SECONDS=0 to disable entirely."""
    global _auto_tick_started
    if _auto_tick_started or AUTO_TICK_SECONDS <= 0 or external_loop_active():
        return
    _auto_tick_started = True

    def _loop() -> None:
        while True:
            time.sleep(AUTO_TICK_SECONDS)
            start_tick()  # no-op if a manual tick is already running

    threading.Thread(target=_loop, daemon=True, name="auto-tick").start()
