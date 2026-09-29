"""FastAPI web app — SaaS-style UI for the lead-gen pipeline + outreach.

V2 Stage 4: every request belongs to exactly one workspace. web/auth.py's
middleware resolves the session ONCE and stashes request.state.workspace_id;
routes read it through ws(request) and pass it into every lead_db call and
every raw query — that plus lead_db's required-parameter API is the leakage
firewall (plans/v2/04 B). Object routes fetch scoped, so another workspace's
ids simply 404.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web import auth
from web import jobs as jobs_mod
from web.jobs import (TICK, auto_tick_minutes, cancel_gate, external_loop_active,
                      get_job, start_auto_tick, start_choice, start_job,
                      start_tick)

WEB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import billing  # noqa: E402
import db  # noqa: E402
import export_leads  # noqa: E402
import lead_db  # noqa: E402
import mailboxes  # noqa: E402
import parse_query  # noqa: E402
import write_dm  # noqa: E402
from config.settings import DATA_DIR  # noqa: E402
from enroll_leads import create_campaign, enroll  # noqa: E402
from sequencer import send_manual_reply  # noqa: E402
from send_email import (dry_run_enabled, verify_unsub_token,  # noqa: E402
                        workspace_live)

PAUSED_FILE = DATA_DIR / "PAUSED"

app = FastAPI(title="Replyworthy")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.on_event("startup")
def _start_engine_pacemaker():
    start_auto_tick()  # replies checked + due emails sent every few minutes
app.middleware("http")(auth.auth_middleware)
app.include_router(auth.router)
templates = Jinja2Templates(directory=WEB_DIR / "templates")


def _safe_href(url) -> str:
    """Scraped links go into href="": only plain web addresses get through
    (a `javascript:` link would run in the owner's session)."""
    url = str(url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else "#"


templates.env.filters["safe_href"] = _safe_href

QUEUE_STATUSES = ("interested", "needs_human")

# The pipeline stages, in the order a lead travels them (plans/04 A1). The
# grouping is what ACTIVE_STATUSES is built from; suppressed/dead states are
# listed separately and only show under the table's "suppressed" filter.
PIPELINE_STAGES = (
    ("New", ("new", "queued")),
    ("Contacted", ("contacted",)),
    ("Replied — needs you", ("needs_human",)),
    ("Interested", ("interested",)),
    ("Meeting", ("meeting",)),
    ("Won", ("won",)),
    ("Lost", ("lost",)),
)
HIDDEN_STATUSES = ("not_interested", "unsubscribed", "bounced", "excluded")

# The table's default "All active" filter.
ACTIVE_STATUSES = tuple(s for _, group in PIPELINE_STAGES for s in group)

# Every status the UI dropdown may set manually.
ALL_STATUSES = (
    "new", "queued", "contacted", "needs_human", "interested", "meeting",
    "won", "lost", "not_interested", "unsubscribed",
)

MAX_PAGE = 100_000  # a bigger page number only overflows the SQL OFFSET


def _safe_back(back: str, default: str) -> str:
    """A safe same-site redirect target for a form's `back` field: must start
    with a single '/'. Rejects protocol-relative '//host' (open redirect),
    matching the login route's guard in web/auth.py."""
    return auth.safe_next(back, default)


# Plain-words labels for lead statuses on customer-facing pages.
STATUS_LABELS = {
    "new": "New", "queued": "Queued to send", "contacted": "Contacted",
    "needs_human": "Needs you", "interested": "Interested", "meeting": "Meeting",
    "won": "Won", "lost": "Lost", "not_interested": "Not interested",
    "unsubscribed": "Unsubscribed", "bounced": "Bounced", "excluded": "Excluded",
    "suppressed": "Suppressed",
}


def ws(request: Request) -> int:
    """The session's workspace — the ONLY place routes take it from."""
    workspace_id = getattr(request.state, "workspace_id", None)
    if workspace_id is None:
        # The auth middleware redirects unauthenticated requests before any
        # route runs; reaching this means a route was wired around it.
        raise RuntimeError("no workspace on request — auth middleware missing?")
    return workspace_id


def queue_count(workspace_id: int) -> int:
    con = lead_db.connect()
    try:
        return con.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE workspace_id = ? "
            "AND status IN (?, ?)",
            (workspace_id, *QUEUE_STATUSES),
        ).fetchone()["n"]
    finally:
        con.close()


def page_ctx(request: Request, active_nav: str) -> dict:
    """Context every page needs: nav highlight, sidebar chips, queue badge,
    the signed-in user and (admin only) the workspace switcher."""
    user = request.state.user
    workspace_id = ws(request)
    is_admin = bool(user and user["role"] == "admin")
    workspace = lead_db.get_workspace(workspace_id)
    return {
        "active_nav": active_nav,
        # Per-workspace since v2/06: the chip shows THIS workspace's mode
        # (the global DRY_RUN master override is applied inside
        # workspace_live, so a global dry-run always shows practice).
        "dry_run": not workspace_live(workspace),
        "paused": PAUSED_FILE.exists(),
        "ws_paused": bool(workspace and workspace.get("outreach_paused")),
        "queue_count": queue_count(workspace_id),
        "user": user,
        "is_admin": is_admin,
        "workspace": workspace,
        # Admin god-view: every workspace for the switcher; members get none.
        "workspaces": lead_db.list_workspaces() if is_admin else [],
    }


def dashboard_stats(workspace_id: int) -> dict:
    """Headline numbers + recent alert feed for the dashboard, one DB pass."""
    con = lead_db.connect()
    try:
        row = con.execute(
            "SELECT COUNT(*) AS total, "
            "  SUM(CASE WHEN score >= 4 THEN 1 ELSE 0 END) AS hot, "
            "  SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) AS with_email "
            "FROM leads WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        sends = con.execute(
            "SELECT SUM(CASE WHEN status = 'scheduled' THEN 1 ELSE 0 END) AS scheduled, "
            "  MIN(CASE WHEN status = 'scheduled' THEN scheduled_at END) AS next_send_at, "
            "  SUM(CASE WHEN status = 'sent' AND substr(sent_at, 1, 10) = ? "
            "      THEN 1 ELSE 0 END) AS sent_today, "
            "  SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_total "
            "FROM sends WHERE workspace_id = ?",
            (db.utctoday(), workspace_id),
        ).fetchone()
        replies = con.execute(
            "SELECT COUNT(*) AS n FROM replies WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        alerts = [dict(r) for r in con.execute(
            "SELECT * FROM alerts WHERE workspace_id = ? ORDER BY id DESC LIMIT 8",
            (workspace_id,),
        ).fetchall()]
    finally:
        con.close()
    return {
        "total_leads": row["total"] or 0,
        "hot_leads": row["hot"] or 0,
        "with_email": row["with_email"] or 0,
        "scheduled": sends["scheduled"] or 0,
        "next_send_at": sends["next_send_at"],
        "sent_today": sends["sent_today"] or 0,
        "sent_total": sends["sent_total"] or 0,
        "replies": replies["n"] or 0,
        "alerts": alerts,
    }


def offer_choices(workspace_id: int) -> tuple[list[dict], int | None]:
    """Non-deleted offers plus the id that the generate form should preselect
    (the default offer, or the first one, or None if there are none)."""
    offers = lead_db.list_offers(workspace_id)
    default_id = next((o["id"] for o in offers if o["is_default"]),
                      offers[0]["id"] if offers else None)
    return offers, default_id


def b2b_ready() -> bool:
    """True once the owner has opened the B2B data account and put its key +
    endpoint in .env (V2 Stage 3). Until then person searches parse fine but
    the UI says honestly that the account is missing — never pretends."""
    return bool(os.environ.get("B2B_DATA_API_KEY")
                and os.environ.get("B2B_DATA_API_URL"))


def _render_index(request: Request, error: str | None = None,
                  status_code: int = 200):
    workspace_id = ws(request)
    offers, default_offer_id = offer_choices(workspace_id)
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={**page_ctx(request, "dashboard"),
                 "stats": dashboard_stats(workspace_id),
                 "offers": offers, "default_offer_id": default_offer_id,
                 "max_count": parse_query.MAX_LEAD_COUNT,
                 "error": error},
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return _render_index(request)


@app.post("/jobs")
def create_job(
    request: Request,
    category: str | None = Form(None),
    city: str | None = Form(None),
    limit: int = Form(50),
    offer_id: int | None = Form(None),
    lead_type: str = Form("business"),
):
    """Start a pipeline run from the search form. Businesses go straight to
    the Maps connector on category/city/limit; people get a hand-built spec
    so route_source() sends them to the B2B data API instead. No parser call
    on either path."""
    workspace_id = ws(request)
    # An offer id posted from another workspace's form must not stick.
    if offer_id is not None and not lead_db.get_offer(workspace_id, offer_id):
        offer_id = None
    category = (category or "").strip()
    city = (city or "").strip()
    if not (category and city):
        return _render_index(request, error="Category and city are required.",
                             status_code=400)

    spec = None
    if lead_type == "person":
        spec = parse_query.spec_from_args(category, city, limit,
                                          entity_type="person")
        if not billing.plan_allows_person(workspace_id):
            # Plan capability gate (2026-07-23) — checked BEFORE b2b_ready()
            # so a starter/trial workspace gets the upgrade message, not the
            # vendor-setup one.
            return _render_index(
                request,
                error="Person leads (named people at companies) are on the "
                      "Growth and Scale plans. Your plan covers business "
                      "leads — upgrade on the Billing page to search people.",
                status_code=403,
            )
        if not b2b_ready():
            # Honest degradation (V2-9): the person path is built, but the
            # data account isn't open yet — say so, never quietly run
            # something else.
            return _render_index(
                request,
                error="People search is built but needs the contact-database "
                      "account first. Pick a vendor from the comparison note, "
                      "put its key in .env, and this exact search will run.",
                status_code=400,
            )

    # Per-plan server-side caps + the status/credit gate (plans/v2/05 C):
    # the plan's search ceiling clamps the count on BOTH entry paths, then
    # the run must clear billing before a job even gets queued.
    plan_cap = billing.search_cap(workspace_id)
    limit = max(1, min(limit, plan_cap))
    if spec is not None:
        spec["count"] = limit
        requested, entity = limit, spec.get("entity_type")
    else:
        requested, entity = limit, None
    guard = billing.run_guard(workspace_id, requested, entity)
    if guard:
        return _render_index(request, error=guard, status_code=402)

    # us_only stays on: the pitch is an SMS text-back workflow, so a business
    # without a US/Canada number can't be served. It was a pre-checked box
    # nobody had reason to untick; the filter is business-only either way.
    #
    # Business searches run two-phase: scrape, then pause at the approve gate
    # so the owner sees the real count before spending on enrichment. Person
    # searches stay one-shot (the B2B connector has no free preview to offer).
    two_phase = (spec is None)
    job_id = start_job(workspace_id, category, city, limit,
                       True, offer_id, spec=spec, two_phase=two_phase)
    if job_id is None:
        return _render_index(
            request, error="A job is already running. Wait for it to finish.",
            status_code=409,
        )
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


# Registered BEFORE /jobs/{job_id}: that pattern also matches "abc.json".
@app.get("/jobs/{job_id}.json")
def job_status_json(request: Request, job_id: str):
    job = get_job(job_id, ws(request))
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "id": job.id,
        "status": job.status,
        "phase": job.phase,
        "category": job.category,
        "city": job.city,
        "limit": job.limit,
        "us_only": job.us_only,
        "total_found": job.total_found,
        "hot_leads": job.hot_leads,
        "funnel": job.funnel,
        "credits_charged": job.credits_charged,
        "output_path": job.output_path,
        "error_msg": job.error_msg,
    }


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: str):
    job = get_job(job_id, ws(request))
    if not job:
        return HTMLResponse("Job not found.", status_code=404)
    ctx = {**page_ctx(request, "dashboard"), "job": job}
    # At the gate, show the first rows of the list exactly as the file will
    # render them, so the owner previews what they're about to download.
    if job.status == "awaiting_choice" and job.scrape:
        headers, rows = export_leads.preview_rows(job.scrape["results"], limit=10)
        ctx["preview_headers"] = headers
        ctx["preview_rows"] = rows
        ctx["gate_ttl_minutes"] = jobs_mod.GATE_TTL_MINUTES
    return templates.TemplateResponse(
        request=request, name="status.html", context=ctx,
    )


_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.post("/jobs/{job_id}/list")
def job_list(request: Request, job_id: str):
    """FREE fork: deliver the scraped list, no enrichment, 0 credits."""
    busy = _gate_busy(request, job_id)
    if busy is not None:
        return busy
    job = start_choice(job_id, ws(request), "list")
    if job is None:
        return _gate_gone(request, job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/continue")
def job_continue(request: Request, job_id: str):
    """PAID fork: score + write emails. Re-check billing — the balance may
    have drained since the scrape — then run enrichment."""
    workspace_id = ws(request)
    job = get_job(job_id, workspace_id)
    if not job or job.status != "awaiting_choice":
        return _gate_gone(request, job_id)
    busy = _gate_busy(request, job_id)
    if busy is not None:
        return busy
    delivered = len(job.scrape["results"]) if job.scrape else job.limit
    guard = billing.run_guard(workspace_id, delivered, "business")
    if guard:
        return templates.TemplateResponse(
            request=request, name="status.html",
            context={**page_ctx(request, "dashboard"), "job": job,
                     "gate_error": guard},
            status_code=402,
        )
    if start_choice(job_id, workspace_id, "enrich") is None:
        return _gate_gone(request, job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: str):
    """Discard a gated job. No credits moved, nothing recorded."""
    if cancel_gate(job_id, ws(request)) is None:
        return _gate_gone(request, job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


def _gate_busy(request: Request, job_id: str):
    """The gate page with a plain reason when this workspace already has
    another run going; None when the way is clear."""
    workspace_id = ws(request)
    job = get_job(job_id, workspace_id)
    if (job is None or job.status != "awaiting_choice"
            or not jobs_mod.other_job_running(workspace_id, job_id)):
        return None
    return templates.TemplateResponse(
        request=request, name="status.html",
        context={**page_ctx(request, "dashboard"), "job": job,
                 "gate_error": "Another search is still running. Pick this "
                               "one up again when it finishes."},
        status_code=409,
    )


def _gate_gone(request: Request, job_id: str):
    """The gate was already answered, expired, or evicted (JOBS is in-memory,
    so a restart drops it). Nothing was charged — invite a free re-run."""
    job = get_job(job_id, ws(request))
    if job is not None:
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
    return _render_index(
        request,
        error="That preview expired — run the search again, it costs nothing "
              "until you choose to enrich.",
        status_code=409,
    )


@app.get("/jobs/{job_id}/download")
def download(request: Request, job_id: str):
    job = get_job(job_id, ws(request))
    if not job or job.status != "done":
        return HTMLResponse("File not ready.", status_code=404)

    # The free list fork holds its rows in memory and renders to bytes — the
    # eight-column, AI-free layout — so nothing ever lands in outputs/.
    if job.phase == "list":
        results = (job.scrape or {}).get("results", [])
        data = export_leads.to_xlsx_bytes(results, layout="list")
        return Response(
            content=data, media_type=_XLSX_MEDIA,
            headers={"content-disposition":
                     f'attachment; filename="leads_list_{job_id}.xlsx"'},
        )

    if not job.output_path:
        return HTMLResponse("File not ready.", status_code=404)
    path = Path(job.output_path)
    if not path.exists():
        return HTMLResponse("File missing from disk.", status_code=404)
    return FileResponse(path, media_type=_XLSX_MEDIA, filename=path.name)


# --- Offers (V2 Stage 1) ---

# The 10 editable columns, in form order. is_default/deleted are managed by
# their own POST actions, never by the create/edit form.
OFFER_FORM_FIELDS = (
    "name", "product_description", "value_prop", "target_customer",
    "tone", "cta_style", "sender_name", "sender_title",
    "qualification_hints", "disqualifiers",
)


def _render_offers(request: Request, *, edit_offer=None, form=None,
                   error=None, status_code=200):
    return templates.TemplateResponse(
        request=request, name="offers.html",
        context={**page_ctx(request, "offers"),
                 "offers": lead_db.list_offers(ws(request)),
                 "edit_offer": edit_offer, "form": form, "error": error},
        status_code=status_code,
    )


@app.get("/offers", response_class=HTMLResponse)
def offers(request: Request, edit: int | None = None):
    edit_offer = None
    if edit:
        candidate = lead_db.get_offer(ws(request), edit)
        if candidate and not candidate.get("deleted"):
            edit_offer = candidate
    return _render_offers(request, edit_offer=edit_offer)


@app.post("/offers/create")
def offer_create(
    request: Request,
    name: str = Form(""),
    product_description: str = Form(""),
    value_prop: str = Form(""),
    target_customer: str = Form(""),
    tone: str = Form(""),
    cta_style: str = Form(""),
    sender_name: str = Form(""),
    sender_title: str = Form(""),
    qualification_hints: str = Form(""),
    disqualifiers: str = Form(""),
):
    posted = locals()
    fields = {k: posted[k] for k in OFFER_FORM_FIELDS}
    # Plan capability gate (2026-07-23): offer profiles are a tier limit
    # (1 / 5 / unlimited). Counted server-side so the UI can't be talked
    # around; None means unlimited.
    limit = billing.offer_limit(ws(request))
    if limit is not None and len(lead_db.list_offers(ws(request))) >= limit:
        noun = "offer profile" if limit == 1 else "offer profiles"
        return _render_offers(
            request, form=fields, status_code=403,
            error=(f"Your plan includes {limit} {noun}. Upgrade on the "
                   "Billing page to add more."))
    try:
        lead_db.create_offer(ws(request), fields)
    except ValueError as exc:
        # Keep the typed input on screen so nothing is lost.
        return _render_offers(request, form=fields, error=str(exc), status_code=400)
    return RedirectResponse(url="/offers?msg=Offer+created", status_code=303)


@app.post("/offers/{offer_id}/update")
def offer_update(
    request: Request,
    offer_id: int,
    name: str = Form(""),
    product_description: str = Form(""),
    value_prop: str = Form(""),
    target_customer: str = Form(""),
    tone: str = Form(""),
    cta_style: str = Form(""),
    sender_name: str = Form(""),
    sender_title: str = Form(""),
    qualification_hints: str = Form(""),
    disqualifiers: str = Form(""),
):
    posted = locals()
    fields = {k: posted[k] for k in OFFER_FORM_FIELDS}
    existing = lead_db.get_offer(ws(request), offer_id)
    if not existing or existing.get("deleted"):
        return HTMLResponse("Offer not found.", status_code=404)
    missing = [k for k in ("name", "product_description", "value_prop")
               if not (fields.get(k) or "").strip()]
    if missing:
        edit_offer = {**fields, "id": offer_id}
        error = f"offer '{missing[0]}' is required"
        return _render_offers(request, edit_offer=edit_offer, error=error,
                              status_code=400)
    lead_db.update_offer(ws(request), offer_id, fields)
    return RedirectResponse(url="/offers?msg=Offer+saved", status_code=303)


@app.post("/offers/{offer_id}/default")
def offer_set_default(request: Request, offer_id: int):
    if not lead_db.get_offer(ws(request), offer_id):
        return HTMLResponse("Offer not found.", status_code=404)
    try:
        lead_db.set_default_offer(ws(request), offer_id)
        msg = "Default offer updated"
    except ValueError as exc:
        msg = str(exc)
    return RedirectResponse(url=f"/offers?msg={quote(msg)}", status_code=303)


@app.post("/offers/{offer_id}/delete")
def offer_delete(request: Request, offer_id: int):
    if not lead_db.get_offer(ws(request), offer_id):
        return HTMLResponse("Offer not found.", status_code=404)
    try:
        lead_db.delete_offer(ws(request), offer_id)
        msg = "Offer deleted"
    except ValueError as exc:
        # e.g. trying to delete the default — show the reason nicely.
        msg = str(exc)
    return RedirectResponse(url=f"/offers?msg={quote(msg)}", status_code=303)


# --- Outreach (Stage 2) ---

# Counter keys -> sentences a non-technical reader understands at a glance.
# Only per-lead outcomes — the tick's cross-workspace tallies (skipped_billing,
# skipped_paused count whole WORKSPACES) are platform-operational and would read
# as nonsense on one tenant's own page, so they are deliberately omitted.
_SEND_STORY = (
    ("sent", "{n} sent"),
    ("skipped_window", "{n} waiting for the lead's business hours "
                       "(8am–5pm Mon–Fri in their own timezone)"),
    ("skipped_cap", "{n} held back — today's warmup limit is used up, "
                    "they go tomorrow"),
    ("skipped_no_mailbox", "{n} waiting for a connected mailbox"),
    ("cancelled", "{n} cancelled (the lead replied, unsubscribed or bounced)"),
    ("failed", "{n} failed — see the alert feed"),
)


def _tick_story(results: dict | None) -> list[dict]:
    """The last engine run as short sentences; quiet phases stay hidden."""
    if not results:
        return []
    story = []
    for phase, result in results.items():
        if isinstance(result, str) and result.startswith("ERROR"):
            story.append({"phase": phase, "text": result, "error": True})
        elif phase == "read inbox" and result:
            n = result
            story.append({"phase": "Replies",
                          "text": f"{n} new repl{'y' if n == 1 else 'ies'} "
                                  "pulled from the mailbox"})
        elif phase == "process replies" and isinstance(result, dict) \
                and result.get("processed"):
            text = f"{result['processed']} handled"
            if result.get("alerts"):
                text += f", {result['alerts']} flagged for you"
            story.append({"phase": "Replies handled", "text": text})
        elif phase == "send due emails" and isinstance(result, dict):
            parts = [tpl.format(n=result[key])
                     for key, tpl in _SEND_STORY if result.get(key)]
            story.append({"phase": "Emails",
                          "text": " · ".join(parts) or "nothing was due"})
        elif phase == "daily digest" and result is True:
            story.append({"phase": "Digest", "text": "daily summary sent"})
        # "sync CRM" and quiet phases say nothing — no row is the message.
    return story


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns(request: Request):
    workspace_id = ws(request)
    con = lead_db.connect()
    try:
        rows = con.execute(
            """
            SELECT c.*,
              SUM(CASE WHEN s.status = 'scheduled' THEN 1 ELSE 0 END) AS n_scheduled,
              SUM(CASE WHEN s.status = 'sent' AND substr(s.sent_at, 1, 10) = ?
                  THEN 1 ELSE 0 END) AS n_sent_today,
              SUM(CASE WHEN s.status = 'sent' THEN 1 ELSE 0 END) AS n_sent_total,
              SUM(CASE WHEN s.status = 'failed' THEN 1 ELSE 0 END) AS n_failed
            FROM campaigns c LEFT JOIN sends s ON s.campaign_id = c.id
                 AND s.workspace_id = c.workspace_id
            WHERE c.workspace_id = ?
            GROUP BY c.id ORDER BY c.id DESC
            """,
            (db.utctoday(), workspace_id),
        ).fetchall()
        campaign_rows = [dict(r) for r in rows]
        # How many leads are ready to enroll right now (status new, verified email).
        enrollable = con.execute(
            "SELECT COUNT(*) AS n FROM leads WHERE workspace_id = ? "
            "AND status = 'new' AND email_verified = 'mx_ok'",
            (workspace_id,),
        ).fetchone()["n"]
        waiting_n = con.execute(
            "SELECT COUNT(*) AS n FROM sends "
            "WHERE workspace_id = ? AND status = 'scheduled'",
            (workspace_id,),
        ).fetchone()["n"]
    finally:
        con.close()
    return templates.TemplateResponse(
        request=request,
        name="campaigns.html",
        context={**page_ctx(request, "campaigns"),
                 "campaigns": campaign_rows,
                 "enrollable": enrollable,
                 "waiting_n": waiting_n,
                 # The outbox card lists the first few; waiting_n covers the rest.
                 "outbox": lead_db.list_scheduled_sends(workspace_id, limit=10),
                 "now": db.utcnow(),
                 "tick": TICK,
                 "tick_story": _tick_story(TICK.get("results")),
                 "auto_tick_minutes": auto_tick_minutes()},
    )


@app.post("/campaigns/pause")
def pause_outreach(request: Request):
    # The PAUSED file stops EVERY workspace's sending (v1 kill switch) —
    # that power stays with the owner.
    if not request.state.user or request.state.user["role"] != "admin":
        return HTMLResponse("Only the admin can pause all sending.", status_code=403)
    PAUSED_FILE.write_text("paused via web UI", encoding="utf-8")
    return RedirectResponse(url="/campaigns?msg=All+sending+paused", status_code=303)


@app.post("/campaigns/resume")
def resume_outreach(request: Request):
    if not request.state.user or request.state.user["role"] != "admin":
        return HTMLResponse("Only the admin can resume sending.", status_code=403)
    PAUSED_FILE.unlink(missing_ok=True)
    return RedirectResponse(url="/campaigns?msg=Sending+resumed", status_code=303)


@app.post("/campaigns/create")
def campaign_create(request: Request, name: str = Form(...),
                          daily_cap: int = Form(30)):
    if not name.strip():
        return RedirectResponse(url="/campaigns?msg=Name+required", status_code=303)
    daily_cap = max(1, min(daily_cap, 200))
    # Plan capability gate (2026-07-23): ACTIVE campaigns are a tier limit
    # (3 / 15 / unlimited). Paused/archived ones never count, so pausing an
    # old campaign always frees a slot.
    limit = billing.campaign_limit(ws(request))
    if limit is not None:
        con = lead_db.connect()
        try:
            n_active = con.execute(
                "SELECT COUNT(*) AS n FROM campaigns "
                "WHERE workspace_id = ? AND status = 'active'",
                (ws(request),),
            ).fetchone()["n"]
        finally:
            con.close()
        if n_active >= limit:
            noun = "active campaign" if limit == 1 else "active campaigns"
            msg = (f"Your plan includes {limit} {noun}. Pause one "
                   "or upgrade on the Billing page.")
            return RedirectResponse(url=f"/campaigns?msg={quote(msg)}",
                                    status_code=303)
    try:
        campaign_id = create_campaign(ws(request), name.strip(), daily_cap)
    except billing.BillingBlocked as exc:
        return RedirectResponse(url=f"/campaigns?msg={quote(str(exc))}",
                                status_code=303)
    return RedirectResponse(
        url=f"/campaigns?msg={quote(f'Campaign {campaign_id} created (3-step sequence)')}",
        status_code=303,
    )


@app.post("/campaigns/{campaign_id}/enroll")
def campaign_enroll(request: Request, campaign_id: int,
                          min_score: int = Form(4), limit: int = Form(50)):
    try:
        counters = enroll(ws(request), campaign_id, min_score=min_score,
                          limit=max(1, min(limit, 500)))
    except billing.BillingBlocked as exc:
        return RedirectResponse(url=f"/campaigns?msg={quote(str(exc))}",
                                status_code=303)
    except SystemExit:
        return HTMLResponse("Campaign not found.", status_code=404)
    msg = (f"Enrolled {counters['enrolled']} leads "
           f"({counters['skipped_no_email']} no verified email, "
           f"{counters['skipped_suppressed']} suppressed)")
    return RedirectResponse(url=f"/campaigns?msg={quote(msg)}", status_code=303)


@app.post("/campaigns/{campaign_id}/toggle")
def campaign_toggle(request: Request, campaign_id: int):
    workspace_id = ws(request)
    con = lead_db.connect()
    try:
        row = con.execute(
            "SELECT status, name FROM campaigns WHERE id = ? AND workspace_id = ?",
            (campaign_id, workspace_id),
        ).fetchone()
        if row is None:
            return HTMLResponse("Campaign not found.", status_code=404)
        new_status = "paused" if row["status"] == "active" else "active"
        with con.tx():
            con.execute(
                "UPDATE campaigns SET status = ? WHERE id = ? AND workspace_id = ?",
                (new_status, campaign_id, workspace_id),
            )
    finally:
        con.close()
    msg = quote(f"{row['name']} → {new_status}")
    return RedirectResponse(url=f"/campaigns?msg={msg}", status_code=303)


@app.post("/campaigns/tick")
def campaign_tick():
    if external_loop_active():
        # A dedicated loop process already owns the engine here (deployed
        # container). Running a second tick in the web process would race it
        # on the same scheduled rows and could double-send, so the button is
        # informational — the engine is already checking on its own.
        msg = (f"The engine runs on its own every {auto_tick_minutes()} minutes "
               "here — no need to trigger it.")
    elif start_tick():
        msg = ("Checking replies and sending due emails — refresh in a few "
               "seconds for results")
    else:
        msg = "Already running — refresh in a moment"
    return RedirectResponse(url=f"/campaigns?msg={quote(msg)}", status_code=303)


@app.post("/campaigns/ws-pause")
def workspace_pause(request: Request):
    """Pause THIS workspace's sending (v2/06 C2) — any member may."""
    lead_db.set_outreach_paused(ws(request), True)
    return RedirectResponse(url="/campaigns?msg=Your+sending+is+paused",
                            status_code=303)


@app.post("/campaigns/ws-resume")
def workspace_resume(request: Request):
    lead_db.set_outreach_paused(ws(request), False)
    return RedirectResponse(url="/campaigns?msg=Your+sending+is+resumed",
                            status_code=303)


# --- Mailboxes (V2 Stage 6: BYO sending mailbox per workspace) ---


def _enc_ready() -> bool:
    """Mailbox credentials can only be stored once MAILBOX_ENC_KEY exists."""
    return bool(os.environ.get("MAILBOX_ENC_KEY"))


def _golive_missing(workspace_id: int, workspace: dict) -> list[str]:
    """The go-live checklist (plans/v2/06 B), server-enforced. Empty list =
    clear to flip live."""
    missing = []
    if workspace.get("plan") == "trial":
        missing.append("Trial accounts stay in practice mode. Upgrade on the "
                       "Billing page to unlock live sending.")
    else:
        blocked = billing.account_block_reason(workspace)
        if blocked:
            missing.append(blocked)
    if not (workspace.get("postal_address") or "").strip():
        missing.append("Add your business postal address (the law requires "
                       "it on every cold email).")
    if not any(b["status"] == "active"
               for b in mailboxes.list_mailboxes(workspace_id)):
        missing.append("Connect and verify at least one mailbox.")
    return missing


@app.get("/mailboxes", response_class=HTMLResponse)
def mailboxes_page(request: Request):
    workspace_id = ws(request)
    ctx = page_ctx(request, "mailboxes")
    workspace = ctx["workspace"]
    boxes = mailboxes.list_mailboxes(workspace_id)
    for b in boxes:
        b["ramp"] = mailboxes.ramp_cap(b["warmup_started_at"])
        b["today_cap"] = mailboxes.effective_cap(b)
    return templates.TemplateResponse(
        request=request,
        name="mailboxes.html",
        context={**ctx,
                 "boxes": boxes,
                 "enc_ready": _enc_ready(),
                 "ws_live": workspace_live(workspace),
                 "global_dry": dry_run_enabled(),
                 "golive_missing": _golive_missing(workspace_id, workspace),
                 "default_cap": mailboxes.DEFAULT_CAP},
    )


@app.post("/mailboxes/create")
def mailbox_create(request: Request, preset: str = Form("gmail"),
                         address: str = Form(...), password: str = Form(...),
                         smtp_host: str = Form(""), smtp_port: int = Form(465),
                         imap_host: str = Form(""), imap_port: int = Form(993)):
    workspace_id = ws(request)
    if not _enc_ready():
        return RedirectResponse(
            url="/mailboxes?msg=" + quote(
                "Mailbox storage isn't set up yet (missing encryption key). "
                "Ask the admin."),
            status_code=303)
    if preset in mailboxes.PRESETS:
        p = mailboxes.PRESETS[preset]
        smtp_host, smtp_port = p["smtp_host"], p["smtp_port"]
        imap_host, imap_port = p["imap_host"], p["imap_port"]
    if not (smtp_host.strip() and imap_host.strip()):
        return RedirectResponse(
            url="/mailboxes?msg=" + quote(
                "SMTP and IMAP server names are required."),
            status_code=303)
    try:
        mailbox_id = mailboxes.add_mailbox(
            workspace_id, address, password,
            smtp_host=smtp_host.strip(), smtp_port=smtp_port,
            imap_host=imap_host.strip(), imap_port=imap_port)
    except ValueError as exc:
        return RedirectResponse(url=f"/mailboxes?msg={quote(str(exc))}",
                                status_code=303)
    _ok, message = mailboxes.verify_mailbox(workspace_id, mailbox_id)
    return RedirectResponse(url=f"/mailboxes?msg={quote(message)}",
                            status_code=303)


@app.post("/mailboxes/{mailbox_id}/verify")
def mailbox_verify(request: Request, mailbox_id: int):
    workspace_id = ws(request)
    if mailboxes.get_mailbox(workspace_id, mailbox_id) is None:
        return HTMLResponse("Mailbox not found.", status_code=404)
    _ok, message = mailboxes.verify_mailbox(workspace_id, mailbox_id)
    return RedirectResponse(url=f"/mailboxes?msg={quote(message)}",
                            status_code=303)


@app.post("/mailboxes/{mailbox_id}/cap")
def mailbox_cap(request: Request, mailbox_id: int,
                      daily_cap: int = Form(...)):
    workspace_id = ws(request)
    applied = mailboxes.set_daily_cap(workspace_id, mailbox_id, daily_cap)
    if applied is None:
        return HTMLResponse("Mailbox not found.", status_code=404)
    if applied != max(1, daily_cap):
        msg = (f"Cap set to {applied} — the warmup ramp doesn't allow more "
               "yet. It rises weekly on its own.")
    else:
        msg = f"Daily cap set to {applied}."
    return RedirectResponse(url=f"/mailboxes?msg={quote(msg)}", status_code=303)


@app.post("/mailboxes/{mailbox_id}/delete")
def mailbox_delete(request: Request, mailbox_id: int):
    workspace_id = ws(request)
    if mailboxes.get_mailbox(workspace_id, mailbox_id) is None:
        return HTMLResponse("Mailbox not found.", status_code=404)
    mailboxes.delete_mailbox(workspace_id, mailbox_id)
    return RedirectResponse(
        url="/mailboxes?msg=" + quote("Mailbox removed (its stored login "
                                      "was deleted too)."),
        status_code=303)


@app.post("/mailboxes/identity")
def mailbox_identity(request: Request, postal_address: str = Form(""),
                           alert_email: str = Form("")):
    lead_db.set_workspace_identity(ws(request), postal_address, alert_email)
    return RedirectResponse(url="/mailboxes?msg=Saved", status_code=303)


@app.post("/mailboxes/golive")
def mailbox_golive(request: Request, warmup_ack: str = Form("")):
    workspace_id = ws(request)
    workspace = lead_db.get_workspace(workspace_id)
    missing = _golive_missing(workspace_id, workspace)
    if not warmup_ack:
        missing.append("Tick the warmup box — new mailboxes must start slow "
                       "or they land in spam.")
    if missing:
        return RedirectResponse(url=f"/mailboxes?msg={quote(missing[0])}",
                                status_code=303)
    lead_db.set_live_sending(workspace_id, True, warmup_ack=True)
    return RedirectResponse(
        url="/mailboxes?msg=" + quote("Live sending is ON for this "
                                      "workspace."),
        status_code=303)


@app.post("/mailboxes/practice")
def mailbox_practice(request: Request):
    lead_db.set_live_sending(ws(request), False)
    return RedirectResponse(
        url="/mailboxes?msg=" + quote("Back to practice mode — nothing "
                                      "will really send."),
        status_code=303)


# --- Mini-CRM (Stage 4) ---


@app.get("/pipeline/table", include_in_schema=False)
def pipeline_table_legacy(request: Request):
    """The table used to live here, beside a kanban board. It is the only
    pipeline view now, so this keeps old links and bookmarks working."""
    qs = request.url.query
    return RedirectResponse(url="/pipeline" + (f"?{qs}" if qs else ""),
                            status_code=307)


def _resolve_lead_filters(status: str, min_score: int, score: int, entity: str,
                          channel: str = ""):
    """Normalize the raw pipeline filter params the SAME way for the table view
    and the export, so a download matches exactly what the filtered table
    shows. Returns (status, statuses, min_score, score, entity, channel):
    status/entity/channel cleaned to '' when out of range, statuses the tuple
    to query, min_score and score each clamped 0-5. `min_score` is the "N and
    up" range (Home's Hot tile deep-links it); `score` is the dropdown's EXACT
    match — the UI sets at most one of the two."""
    if status == "suppressed":
        statuses = HIDDEN_STATUSES
    elif status in ACTIVE_STATUSES or status in HIDDEN_STATUSES:
        statuses = (status,)
    else:
        status = ""
        statuses = ACTIVE_STATUSES
    min_score = min(max(min_score, 0), 5)
    score = min(max(score, 0), 5)
    entity = entity if entity in ("business", "person") else ""
    channel = channel if channel in ("email", "dm", "phone") else ""
    return status, statuses, min_score, score, entity, channel


# Display order for social profile icons, everywhere they render.
SOCIAL_PLATFORMS = ("facebook", "instagram", "x", "linkedin", "tiktok")


def _lead_socials(lead: dict) -> dict:
    """lead["social_links"] (a JSON string on DB rows) as an ordered dict of
    {platform: url}, empty when none is stored."""
    try:
        links = json.loads(lead.get("social_links") or "null")
    except (TypeError, ValueError):
        links = None
    if not isinstance(links, dict):
        return {}
    return {p: links[p] for p in SOCIAL_PLATFORMS if links.get(p)}


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request, status: str = "", q: str = "",
                   min_score: int = 0, score: int = 0, city: str = "",
                   entity: str = Query("", alias="type"), channel: str = "",
                   sort: str = "updated_desc", page: int = 1,
                   partial: int = 0):
    """The one lead view: filtered, sorted, 50 leads per page. `partial=1`
    returns just the results fragment — what the live search box fetches on
    every keystroke, so the page never reloads."""
    workspace_id = ws(request)
    per_page = 50
    status, statuses, min_score, score, entity, channel = _resolve_lead_filters(
        status, min_score, score, entity, channel)
    page = min(max(1, page), MAX_PAGE)
    rows, total = lead_db.query_leads(
        workspace_id, statuses=statuses, q=q, min_score=min_score or None,
        score=score or None, city=city or None, entity_type=entity or None,
        channel=channel or None, sort=sort, page=page, per_page=per_page)
    pages = max(1, -(-total // per_page))
    for row in rows:
        row["socials"] = _lead_socials(row)

    counts = lead_db.count_by_status(workspace_id)
    filters = {"status": status, "q": q.strip(), "min_score": min_score,
               "score": score, "city": city, "type": entity,
               "channel": channel, "sort": sort}
    # Query string for pagination links (page appended per link) and the
    # bulk form's round-trip back to this exact filtered page. Defaults stay
    # out of the URL so the untouched view reads as unfiltered.
    filter_qs = urlencode({
        k: v for k, v in filters.items()
        if v and not (k == "sort" and v == "updated_desc")})
    current_url = "/pipeline" + (
        "?" + filter_qs + f"&page={page}" if filter_qs else f"?page={page}")
    return templates.TemplateResponse(
        request=request,
        name="_leads_rows.html" if partial else "leads_table.html",
        context={**page_ctx(request, "pipeline"),
                 "rows": rows,
                 "total": total,
                 "page": page,
                 "pages": pages,
                 "f": filters,
                 "filtered": bool(filter_qs),
                 "filter_qs": filter_qs,
                 "current_url": current_url,
                 "counts": counts,
                 "active_total": sum(counts.get(s, 0) for s in ACTIVE_STATUSES),
                 "suppressed_total": sum(counts.get(s, 0) for s in HIDDEN_STATUSES),
                 "active_statuses": ACTIVE_STATUSES,
                 "cities": lead_db.distinct_cities(workspace_id),
                 "sorts": (("updated_desc", "Newest activity"),
                           ("updated_asc", "Oldest activity"),
                           ("score_desc", "Highest score"),
                           ("name_asc", "Name A–Z")),
                 "statuses": ALL_STATUSES},
    )


@app.get("/pipeline/export")
def pipeline_export(request: Request, status: str = "", q: str = "",
                          min_score: int = 0, score: int = 0, city: str = "",
                          entity: str = Query("", alias="type"),
                          channel: str = "",
                          sort: str = "updated_desc", fmt: str = "xlsx"):
    """Download every lead matching the current pipeline filters — all pages,
    not just the visible 50 — as CSV or Excel. Filters resolve exactly like the
    /pipeline table (_resolve_lead_filters), so the file is what the user
    filtered to. `fmt` defaults to xlsx; anything but 'csv' is treated as xlsx.
    Workspace-scoped through export_leads, so no other tenant's leads leak."""
    workspace_id = ws(request)
    status, statuses, min_score, score, entity, channel = _resolve_lead_filters(
        status, min_score, score, entity, channel)
    rows = lead_db.export_leads(
        workspace_id, statuses=statuses, q=q, min_score=min_score or None,
        score=score or None, city=city or None, entity_type=entity or None,
        channel=channel or None, sort=sort)
    # The DB stores pain_points as a JSON string; decode to a list so the
    # export cells read like the deliverable (same guard as lead_detail).
    for row in rows:
        try:
            row["pain_points"] = (
                json.loads(row["pain_points"]) if row.get("pain_points") else [])
        except (TypeError, ValueError):
            row["pain_points"] = []

    date_str = datetime.now().strftime("%Y%m%d")
    if fmt == "csv":
        return Response(
            content=export_leads.to_csv_bytes(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="leads_export_{date_str}.csv"'})
    return Response(
        content=export_leads.to_xlsx_bytes(rows),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition":
                 f'attachment; filename="leads_export_{date_str}.xlsx"'})


@app.get("/lead/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: int):
    workspace_id = ws(request)
    lead = lead_db.get_lead(workspace_id, lead_id)
    if not lead:
        return HTMLResponse("Lead not found.", status_code=404)
    for key in ("pain_points", "soft_signals"):
        try:
            lead[key] = json.loads(lead[key]) if lead[key] else []
        except (TypeError, ValueError):
            lead[key] = []
    lead["socials"] = _lead_socials(lead)

    # Which offer this lead was scored against. get_offer returns soft-deleted
    # offers too, so historical leads still show a name after an offer is
    # removed; only a fully-missing row falls back to the bare id.
    scored_against = None
    if lead.get("offer_id"):
        offer = lead_db.get_offer(workspace_id, lead["offer_id"])
        scored_against = offer["name"] if offer else f"offer #{lead['offer_id']}"

    # The search that produced this lead ("what did I ask for", plan v2/02 C).
    searched_for = None
    try:
        stored_spec = json.loads(lead.get("search_spec") or "null")
    except (TypeError, ValueError):
        stored_spec = None
    if isinstance(stored_spec, dict):
        searched_for = stored_spec.get("raw_query") or parse_query.describe_spec(stored_spec)

    con = lead_db.connect()
    try:
        sends = [dict(r) for r in con.execute(
            "SELECT * FROM sends WHERE workspace_id = ? AND lead_id = ? "
            "ORDER BY step_number, id",
            (workspace_id, lead_id),
        ).fetchall()]
        replies = [dict(r) for r in con.execute(
            "SELECT * FROM replies WHERE workspace_id = ? AND lead_id = ? "
            "ORDER BY id DESC",
            (workspace_id, lead_id),
        ).fetchall()]
    finally:
        con.close()

    # One merged activity feed: sends + replies + notes, newest first.
    timeline: list[dict] = []
    for s in sends:
        timeline.append({
            "kind": "send", "when": s["sent_at"] or s["scheduled_at"],
            "title": f"Step {s['step_number']} — {s['status']}",
            "body": (f"via {s['mailbox']}" if s["mailbox"] else "")
                    + (f" · error: {s['error']}" if s["error"] else ""),
            "badge": None,
        })
    for r in replies:
        body = (r["summary"] or "").strip()
        if r["suggested_action"]:
            body += f"\nSuggested: {r['suggested_action']}"
        if r["body_text"]:
            body += ("\n\n" if body else "") + r["body_text"][:600]
        timeline.append({
            "kind": "reply", "when": r["received_at"],
            "title": f"Reply from {r['from_email']}",
            "body": body, "badge": r["classification"],
            "confidence": r["confidence"],
        })
    for n in lead_db.get_notes(workspace_id, lead_id):
        timeline.append({
            "kind": "note", "when": n["created_at"],
            "title": f"Note · {n['author']}", "body": n["body"], "badge": None,
        })
    timeline.sort(key=lambda t: t["when"] or "", reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="lead_detail.html",
        context={**page_ctx(request, "pipeline"),
                 "lead": lead,
                 "timeline": timeline,
                 "scored_against": scored_against,
                 "searched_for": searched_for,
                 "statuses": ALL_STATUSES},
    )


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    """The human work surface: leads waiting for a reply from YOU."""
    workspace_id = ws(request)
    con = lead_db.connect()
    try:
        leads = [dict(r) for r in con.execute(
            "SELECT * FROM leads WHERE workspace_id = ? AND status IN (?, ?) "
            "ORDER BY updated_at DESC",
            (workspace_id, *QUEUE_STATUSES),
        ).fetchall()]
        for lead in leads:
            reply = con.execute(
                "SELECT * FROM replies WHERE workspace_id = ? AND lead_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (workspace_id, lead["id"]),
            ).fetchone()
            lead["last_reply"] = dict(reply) if reply else None
    finally:
        con.close()
    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={**page_ctx(request, "queue"),
                 "rows": leads,
                 # Where a handled lead usually goes next.
                 "done_statuses": ("meeting", "won", "contacted", "not_interested", "lost")},
    )


@app.get("/dms", response_class=HTMLResponse)
def dms(request: Request):
    """The DM queue: leads with no email but a social profile — reachable by
    a human-sent DM instead of going to waste. The system only WRITES the
    message; a person copies, pastes, and sends it on the platform (never
    automated, same promise as replies)."""
    workspace_id = ws(request)
    rows = lead_db.dm_queue(workspace_id)
    for row in rows:
        row["socials"] = _lead_socials(row)
    return templates.TemplateResponse(
        request=request,
        name="dms.html",
        context={**page_ctx(request, "dms"), "rows": rows},
    )


@app.post("/dms/{lead_id}/write")
def dms_write(request: Request, lead_id: int):
    """Generate one DM for one lead (a paid Claude call, one click each).
    Returns JSON for the page's fetch: {ok, dm_text, copy_flagged} or
    {ok: False, error} — failures surface loudly, never as filler text."""
    workspace_id = ws(request)
    lead = lead_db.get_lead(workspace_id, lead_id)
    if not lead:
        return JSONResponse({"ok": False, "error": "Lead not found."},
                            status_code=404)
    # A paid Claude call: a frozen, lapsed or expired-trial workspace can't
    # start one (same rule as searches and enrolling).
    blocked = billing.account_block_reason(lead_db.get_workspace(workspace_id))
    if blocked:
        return JSONResponse({"ok": False, "error": blocked}, status_code=402)

    offer = None
    if lead.get("offer_id"):
        offer = lead_db.get_offer(workspace_id, lead["offer_id"])
    if offer is None:
        offer = lead_db.get_default_offer(workspace_id)
    if offer is None:
        return JSONResponse({"ok": False, "error":
                             "Create an offer first — the DM pitches it."})

    from anthropic import AuthenticationError, PermissionDeniedError

    try:
        result = write_dm.write_dm(
            lead, offer,
            avoid_openings=lead_db.recent_dm_openings(workspace_id))
    except (AuthenticationError, PermissionDeniedError):
        # Same rule as analyze_lead: a bad key must be loud, never junk copy.
        return JSONResponse({"ok": False, "error":
                             "Claude API key rejected — check "
                             "ANTHROPIC_API_KEY in settings."})
    except ValueError as exc:  # write_dm's own plain-words refusals
        return JSONResponse({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 — vendor detail goes to the log
        print(f"DM write failed for lead {lead_id}: {exc!r}", file=sys.stderr)
        return JSONResponse({"ok": False, "error":
                             "Couldn't write that DM just now. Try again in "
                             "a minute."})
    return JSONResponse({"ok": True, "dm_text": result["dm_text"],
                         "copy_flagged": bool(result.get("copy_flagged"))})


@app.post("/dms/{lead_id}/sent")
def dms_sent(request: Request, lead_id: int,
                   platform: str = Form(""), dm_text: str = Form("")):
    """Record that a human sent the DM: note (author 'dm', keeps the exact
    text — it also feeds opener variation) + status 'contacted', which takes
    the lead out of the queue so it can never be DM'd twice."""
    workspace_id = ws(request)
    lead = lead_db.get_lead(workspace_id, lead_id)
    if not lead:
        return HTMLResponse("Lead not found.", status_code=404)
    platform = platform if platform in SOCIAL_PLATFORMS else "social"
    if dm_text.strip():
        lead_db.add_note(workspace_id, lead_id,
                         f"DM sent ({platform}):\n{dm_text.strip()}",
                         author="dm")
    lead_db.set_status(workspace_id, lead_id, "contacted", f"dm_{platform}")
    return RedirectResponse(
        url=f"/dms?msg={quote('DM recorded — lead moved to contacted.')}",
        status_code=303)


def _apply_status(workspace_id: int, lead: dict, status: str) -> None:
    """One manual stage move plus its side effects — shared by the single-lead
    route and the table's bulk move so the rules can never drift."""
    lead_db.set_status(workspace_id, lead["id"], status, "manual")
    # A human decided this lead's fate - stop any scheduled follow-ups unless
    # the move keeps it in active-outreach territory.
    if status not in ("new", "queued", "contacted"):
        lead_db.cancel_pending_sends(workspace_id, lead["id"])
    if status in ("not_interested", "unsubscribed") and lead.get("email"):
        lead_db.add_suppression(workspace_id, lead["email"], status)
        if status == "unsubscribed":
            # A real opt-out blocks every workspace (plans/v2/04 B).
            lead_db.add_hard_suppression(lead["email"], "unsubscribed")


@app.post("/lead/{lead_id}/status")
def lead_set_status(request: Request, lead_id: int,
                          status: str = Form(""), back: str = Form("/pipeline")):
    workspace_id = ws(request)
    back = _safe_back(back, "/pipeline")
    if status not in ALL_STATUSES:
        # Nothing picked (or junk posted) - bounce back, change nothing.
        return RedirectResponse(url=back, status_code=303)
    lead = lead_db.get_lead(workspace_id, lead_id)
    if not lead:
        return HTMLResponse("Lead not found.", status_code=404)
    _apply_status(workspace_id, lead, status)
    sep = "&" if "?" in back else "?"
    msg = quote(f"{lead['name']} → {status}")
    return RedirectResponse(url=f"{back}{sep}msg={msg}", status_code=303)


@app.post("/pipeline/bulk-status")
def pipeline_bulk_status(request: Request,
                               lead_id: list[int] = Form([]),
                               status: str = Form(""),
                               back: str = Form("/pipeline")):
    """Move the table's checked leads in one go. Foreign/missing ids are
    skipped silently — the scoped get_lead is the tenancy firewall here."""
    workspace_id = ws(request)
    back = _safe_back(back, "/pipeline")
    if status not in ALL_STATUSES or not lead_id:
        return RedirectResponse(url=back, status_code=303)
    moved = 0
    for lid in lead_id[:100]:
        lead = lead_db.get_lead(workspace_id, lid)
        if not lead:
            continue
        _apply_status(workspace_id, lead, status)
        moved += 1
    sep = "&" if "?" in back else "?"
    noun = "lead" if moved == 1 else "leads"
    msg = quote(f"Moved {moved} {noun} → {status}")
    return RedirectResponse(url=f"{back}{sep}msg={msg}", status_code=303)


@app.post("/lead/{lead_id}/notes")
def lead_add_note(request: Request, lead_id: int, body: str = Form(...)):
    workspace_id = ws(request)
    if not lead_db.get_lead(workspace_id, lead_id):
        return HTMLResponse("Lead not found.", status_code=404)
    lead_db.add_note(workspace_id, lead_id, body)
    return RedirectResponse(url=f"/lead/{lead_id}?msg=Note+saved", status_code=303)


@app.post("/lead/{lead_id}/reply")
def lead_reply(request: Request, lead_id: int, body: str = Form(""),
                     back: str = Form("/queue")):
    """Send a human-written 1:1 reply through the workspace's own mailbox.
    All gating (practice/live, suppression, mailbox resolution, threading) is
    in sequencer.send_manual_reply — the route just relays the outcome."""
    workspace_id = ws(request)
    back = _safe_back(back, "/queue")
    result = send_manual_reply(workspace_id, lead_id, body)
    if result["error"]:
        msg = result["error"]
    elif result["sent"]:
        msg = f"Reply sent to {result['to']} — it's in the same email thread."
    else:
        msg = ("Practice mode: your reply was composed but not actually sent. "
               "Go live on the Mailboxes page to send for real.")
    sep = "&" if "?" in back else "?"
    return RedirectResponse(url=f"{back}{sep}msg={quote(msg)}", status_code=303)


# --- Billing (V2 Stage 5) ---

# Plain-words labels for credit_ledger.reason on the Billing page.
LEDGER_REASON_LABELS = {
    "trial_grant": "Free trial credits",
    "monthly_grant": "Monthly plan credits",
    "rollover_expiry": "Unused credits expired (rollover cap)",
    "lead_business": "Business leads delivered",
    "lead_person": "People leads delivered",
    "adjustment": "Manual adjustment",
}


def _admin_only(request: Request) -> HTMLResponse | None:
    user = request.state.user
    if not user or user["role"] != "admin":
        return HTMLResponse("Not allowed.", status_code=403)
    return None


@app.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    workspace_id = ws(request)
    workspace = lead_db.get_workspace(workspace_id)
    checkout = {p: billing.checkout_url(workspace_id, p)
                for p in billing.PAID_PLANS}
    return templates.TemplateResponse(
        request=request, name="billing.html",
        context={**page_ctx(request, "billing"),
                 "plan_key": workspace["plan"],
                 "plan": billing.plan_info(workspace["plan"]),
                 "balance": workspace["credits_balance"] or 0,
                 "trial_expires": workspace.get("trial_expires_at"),
                 "blocked": billing.account_block_reason(workspace),
                 "ledger": billing.ledger_rows(workspace_id),
                 "reason_labels": LEDGER_REASON_LABELS,
                 "plans": billing.PLANS,
                 "paid_plans": billing.PAID_PLANS,
                 "provider_ready": billing.provider_ready(),
                 "checkout": checkout},
    )


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    """The payment provider calling us — public path, HMAC signature is the
    gate (tools/billing.py verifies before anything is parsed)."""
    body = await request.body()
    status_code, message = await run_in_threadpool(
        billing.process_webhook, dict(request.headers), body)
    return PlainTextResponse(message, status_code=status_code)


@app.get("/admin/billing", response_class=HTMLResponse)
def admin_billing(request: Request):
    """Reconciliation view (plans/v2/05 D): every workspace's billing state
    next to the raw provider events, plus the freeze lever."""
    denied = _admin_only(request)
    if denied:
        return denied
    con = lead_db.connect()
    try:
        events = [dict(r) for r in con.execute(
            "SELECT * FROM billing_events ORDER BY id DESC LIMIT 30"
        ).fetchall()]
    finally:
        con.close()
    return templates.TemplateResponse(
        request=request, name="admin_billing.html",
        context={**page_ctx(request, "billing"),
                 "rows": lead_db.list_workspaces(),
                 "events": events,
                 "provider_ready": billing.provider_ready()},
    )


@app.post("/admin/billing/{workspace_id}/freeze")
def admin_freeze(request: Request, workspace_id: int,
                       unfreeze: int = Form(0)):
    denied = _admin_only(request)
    if denied:
        return denied
    if not lead_db.get_workspace(workspace_id):
        return HTMLResponse("No such workspace.", status_code=404)
    billing.set_frozen(workspace_id, not unfreeze)
    msg = "Workspace unfrozen" if unfreeze else "Workspace frozen"
    return RedirectResponse(url=f"/admin/billing?msg={quote(msg)}", status_code=303)


@app.post("/admin/billing/{workspace_id}/adjust")
def admin_adjust(request: Request, workspace_id: int,
                       delta: int = Form(...), note: str = Form("")):
    denied = _admin_only(request)
    if denied:
        return denied
    if not lead_db.get_workspace(workspace_id):
        return HTMLResponse("No such workspace.", status_code=404)
    if delta == 0:
        return RedirectResponse(url="/admin/billing?msg=Nothing+to+adjust",
                                status_code=303)
    billing.adjust_credits(workspace_id, delta, note)
    return RedirectResponse(url="/admin/billing?msg=Adjustment+recorded",
                            status_code=303)


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str):
    """Public one-click opt-out. The token encodes (workspace, lead) and is
    HMAC-signed, so a token minted for workspace A can only touch A's lead —
    plus the global hard suppression, which is global on purpose."""
    parsed = verify_unsub_token(token)
    if parsed is None:
        return HTMLResponse("Invalid link.", status_code=404)
    workspace_id, lead_id = parsed
    lead = lead_db.get_lead(workspace_id, lead_id)
    if lead and lead.get("email"):
        lead_db.add_suppression(workspace_id, lead["email"], "unsubscribed")
        lead_db.add_hard_suppression(lead["email"], "unsubscribed")
    lead_db.set_status(workspace_id, lead_id, "unsubscribed",
                       "clicked unsubscribe link")
    lead_db.cancel_pending_sends(workspace_id, lead_id)
    return templates.TemplateResponse(request=request, name="unsubscribed.html")
