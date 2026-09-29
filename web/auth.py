"""Auth + sessions for the multi-tenant web app.

Who you are is decided by Supabase Auth (tools/supabase_auth.py): it stores
the password, sends the "confirm your email" and "reset your password" mails,
and checks the password at login. Once Supabase says yes, this module sets
its own HMAC-signed session cookie; from then on the middleware resolves the
session ONCE per request and stashes `request.state.user` +
`request.state.workspace_id`. Every route reads the workspace from there (the
leakage firewall's single entry point).

The local `users` row (role + workspace) is created on the first CONFIRMED
login, never at signup, so an address nobody confirmed can't collect a
workspace and trial credits.

Roles: 'member' (sees exactly one workspace) and 'admin' (the owner; gets a
workspace switcher — the acting workspace rides inside the signed session
payload, so it can't be forged client-side). Admin goes to the address in
OWNER_EMAIL. With OWNER_EMAIL unset, the first account is admin on a local
(http) install only; on a deployed (https) one nobody becomes admin until
OWNER_EMAIL is set.

Cookie flags: HttpOnly + SameSite=Lax always; Secure when BASE_URL is https
(production) — a Secure cookie on plain-http localhost would never be sent
back by the browser. Secret from SESSION_SECRET in .env; a missing secret
falls back to a random per-process value with a loud warning (sessions then
reset on every restart — fine for local dev only).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

WEB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import lead_db  # noqa: E402
import supabase_auth  # noqa: E402
from supabase_auth import AuthError  # noqa: E402

router = APIRouter()
templates = Jinja2Templates(directory=WEB_DIR / "templates")

SESSION_COOKIE = "rw_session"
SESSION_TTL = 30 * 24 * 3600      # 30 days
RECOVERY_COOKIE = "rw_recovery"   # holds the reset session between two pages
RECOVERY_TTL = 30 * 60
MIN_PASSWORD_LEN = 8

# Paths reachable without a session. Everything else redirects to /login.
# /billing/webhook is the payment provider calling us — its gate is the
# HMAC signature (tools/billing.py), not a session.
PUBLIC_PREFIXES = ("/login", "/signup", "/forgot", "/reset", "/resend",
                   "/auth/confirm", "/unsubscribe/", "/billing/webhook",
                   "/static/", "/favicon.ico")

# What the person sees for each supabase_auth.AuthError code.
ERROR_TEXT = {
    "not_configured": "Login isn't set up yet on this server. The owner needs "
                      "to add the Supabase keys.",
    "invalid_credentials": "Wrong email or password.",
    "email_not_confirmed": "Confirm your email first. We sent you a link when "
                           "you signed up.",
    "already_registered": "That email already has an account. Log in instead.",
    "weak_password": "That password is too easy to guess. Pick a longer one.",
    "link_expired": "That link is invalid or expired. Ask for a fresh one.",
    "rate_limited": "Too many tries in a short time. Wait a few minutes and "
                    "try again.",
    "unavailable": "Couldn't reach the login service. Try again in a minute.",
}
ERROR_STATUS = {
    "not_configured": 503, "invalid_credentials": 401,
    "email_not_confirmed": 403, "rate_limited": 429, "unavailable": 503,
}

_ephemeral_secret: str | None = None


def _secret() -> bytes:
    configured = os.environ.get("SESSION_SECRET", "").strip()
    if configured:
        return configured.encode()
    global _ephemeral_secret
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_hex(32)
        print(
            "WARNING: SESSION_SECRET is not set in .env — using a random "
            "per-process secret. Sessions reset on every restart; set "
            "SESSION_SECRET before deploying.",
            file=sys.stderr,
        )
    return _ephemeral_secret.encode()


def _cookie_secure() -> bool:
    return os.environ.get("BASE_URL", "").strip().lower().startswith("https")


def safe_next(target: str, default: str = "/") -> str:
    """A same-site redirect target, else `default`. Must be a plain path:
    one leading '/', and no backslash or control character (browsers read
    '/\\host' as '//host', an open redirect)."""
    target = target or ""
    if (target.startswith("/") and not target.startswith("//")
            and "\\" not in target
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in target)):
        return target
    return default


def _error_text(exc: AuthError) -> str:
    return ERROR_TEXT.get(exc.code, "Something went wrong. Try again.")


def _error_status(exc: AuthError) -> int:
    return ERROR_STATUS.get(exc.code, 400)


# --- Session tokens ----------------------------------------------------------


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_token(user_id: int, acting_workspace_id: int | None = None) -> str:
    expires = int(time.time()) + SESSION_TTL
    acting = str(acting_workspace_id) if acting_workspace_id else ""
    payload = f"{user_id}:{expires}:{acting}"
    raw = f"{payload}:{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def read_session_token(token: str) -> tuple[int, int | None] | None:
    """(user_id, acting_workspace_id|None) if valid and unexpired, else None."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        user_str, exp_str, acting_str, sig = raw.split(":", 3)
        payload = f"{user_str}:{exp_str}:{acting_str}"
        if not hmac.compare_digest(sig.encode(), _sign(payload).encode()):
            return None
        if int(exp_str) < time.time():
            return None
        return int(user_str), (int(acting_str) if acting_str else None)
    except (ValueError, UnicodeDecodeError):
        return None


def _set_session(response, user_id: int, acting_workspace_id: int | None = None):
    response.set_cookie(
        SESSION_COOKIE,
        make_session_token(user_id, acting_workspace_id),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
    )
    return response


# --- Local account for a confirmed Supabase user -------------------------------


def _owner_email() -> str:
    return os.environ.get("OWNER_EMAIL", "").strip().lower()


def _is_owner(email: str) -> bool:
    owner = _owner_email()
    if owner:
        return email == owner
    if _cookie_secure():
        if lead_db.count_admins() == 0:
            print("WARNING: OWNER_EMAIL is not set, so no account can become "
                  "admin on this deployed server. Set OWNER_EMAIL in the "
                  "environment.", file=sys.stderr)
        return False
    return lead_db.count_users() == 0  # local install: first account is you


def ensure_local_user(auth_user: dict) -> dict:
    """The local account for a Supabase user who proved who they are. Created
    on first sight: the owner claims the migration-created workspace 1,
    everyone else gets a fresh workspace of their own."""
    user = lead_db.get_user_by_auth_id(auth_user["id"])
    if user:
        return user
    email = auth_user["email"]
    user = lead_db.get_user_by_email(email)
    if user:
        lead_db.link_auth_user(user["id"], auth_user["id"])
        return lead_db.get_user(user["id"])

    workspace_name = str(auth_user["metadata"].get("workspace_name") or "").strip()
    if _is_owner(email):
        role = "admin"
        workspace_id = lead_db.OWNER_WORKSPACE_ID
        if lead_db.get_workspace(workspace_id) is None:
            workspace_id = lead_db.create_workspace(workspace_name or "My workspace")
        elif workspace_name:
            lead_db.rename_workspace(workspace_id, workspace_name)
    else:
        role = "member"
        workspace_id = lead_db.create_workspace(
            workspace_name or f"{email.split('@')[0]}'s workspace"
        )
    user_id = lead_db.create_user(email, workspace_id, role, auth_user["id"])
    return lead_db.get_user(user_id)


# --- Middleware --------------------------------------------------------------


def is_public_path(path: str) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in PUBLIC_PREFIXES
    )


def _resolve_session(token: str | None) -> tuple[dict | None, int | None]:
    """(user, workspace_id) for a session cookie, or (None, None)."""
    parsed = read_session_token(token) if token else None
    user = lead_db.get_user(parsed[0]) if parsed else None
    if not user:
        return None, None
    workspace_id = user["workspace_id"]
    acting = parsed[1]
    if acting and acting != workspace_id:
        # Only the admin may act as another workspace, and only one that
        # exists.
        if user["role"] == "admin" and lead_db.get_workspace(acting):
            workspace_id = acting
    return user, workspace_id


async def auth_middleware(request: Request, call_next):
    """Resolve the session once per request (workspace_id is read from here
    and nowhere else). Unauthenticated hits on protected paths bounce to
    /login."""
    token = request.cookies.get(SESSION_COOKIE)
    # The lookup hits the database: off the event loop, like the routes.
    request.state.user, request.state.workspace_id = (
        await run_in_threadpool(_resolve_session, token))

    path = request.url.path
    if request.state.user is None and not is_public_path(path):
        if path.endswith(".json"):
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return RedirectResponse(
            url=f"/login?next={quote(path)}" if path != "/" else "/login",
            status_code=303,
        )
    return await call_next(request)


# --- Routes ------------------------------------------------------------------


def _auth_page(request: Request, name: str, status_code: int = 200, **ctx):
    return templates.TemplateResponse(
        request=request, name=name, context=ctx, status_code=status_code
    )


def _signed_in(user: dict, target: str = "/"):
    return _set_session(RedirectResponse(url=target, status_code=303), user["id"])


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    if request.state.user:
        return RedirectResponse(url="/", status_code=303)
    return _auth_page(request, "auth_login.html", next=next)


@router.post("/login")
def login(request: Request, email: str = Form(""),
          password: str = Form(""), next: str = Form("/")):
    email = (email or "").strip().lower()
    try:
        session = supabase_auth.sign_in(email, password)
    except AuthError as exc:
        return _auth_page(
            request, "auth_login.html", status_code=_error_status(exc),
            error=_error_text(exc), email=email, next=next,
            unconfirmed=exc.code == "email_not_confirmed",
        )
    return _signed_in(ensure_local_user(session["user"]), safe_next(next))


@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    if request.state.user:
        return RedirectResponse(url="/", status_code=303)
    return _auth_page(request, "auth_signup.html")


@router.post("/signup")
def signup(request: Request, email: str = Form(""),
           password: str = Form(""), workspace_name: str = Form("")):
    email = (email or "").strip().lower()
    form = {"email": email, "workspace_name": workspace_name}
    if "@" not in email or "." not in email.split("@")[-1]:
        return _auth_page(request, "auth_signup.html", status_code=400,
                          error="That doesn't look like an email address.", **form)
    if len(password) < MIN_PASSWORD_LEN:
        return _auth_page(request, "auth_signup.html", status_code=400,
                          error=f"Password needs at least {MIN_PASSWORD_LEN} characters.",
                          **form)
    if lead_db.get_user_by_email(email):
        return _auth_page(request, "auth_signup.html", status_code=400,
                          error=ERROR_TEXT["already_registered"], **form)
    try:
        result = supabase_auth.sign_up(
            email, password, {"workspace_name": workspace_name.strip()})
    except AuthError as exc:
        return _auth_page(request, "auth_signup.html",
                          status_code=_error_status(exc),
                          error=_error_text(exc), **form)
    if result["access_token"]:
        # The Supabase project has email confirmation switched off.
        return _signed_in(ensure_local_user(result["user"]))
    return _auth_page(request, "auth_check_inbox.html", email=email)


@router.post("/resend")
def resend(request: Request, email: str = Form("")):
    email = (email or "").strip().lower()
    try:
        if email:
            supabase_auth.resend_confirmation(email)
    except AuthError as exc:
        if exc.code in ("rate_limited", "not_configured", "unavailable"):
            return _auth_page(request, "auth_check_inbox.html",
                              status_code=_error_status(exc), email=email,
                              error=_error_text(exc))
    # Same answer whether the account exists or not (no enumeration).
    return _auth_page(request, "auth_check_inbox.html", email=email,
                      message="If that address is waiting to be confirmed, a "
                              "new link is on its way.")


@router.get("/auth/confirm")
def confirm(request: Request, token_hash: str = "", type: str = "email"):
    """Where the links in Supabase's emails land (signup confirmation and
    password reset)."""
    kind = "recovery" if type == "recovery" else "email"
    try:
        session = supabase_auth.verify_token_hash(token_hash, kind)
    except AuthError as exc:
        return _auth_page(
            request, "auth_forgot.html" if kind == "recovery" else "auth_login.html",
            status_code=_error_status(exc), error=_error_text(exc), next="/")
    if kind == "recovery":
        response = RedirectResponse(url="/reset", status_code=303)
        response.set_cookie(RECOVERY_COOKIE, session["access_token"],
                            max_age=RECOVERY_TTL, httponly=True,
                            samesite="lax", secure=_cookie_secure())
        return response
    return _signed_in(ensure_local_user(session["user"]),
                      "/?msg=Email+confirmed.+Welcome!")


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_form(request: Request):
    return _auth_page(request, "auth_forgot.html")


@router.post("/forgot")
def forgot(request: Request, email: str = Form("")):
    email = (email or "").strip().lower()
    try:
        if email:
            supabase_auth.send_recovery(email)
    except AuthError as exc:
        if exc.code in ("rate_limited", "not_configured", "unavailable"):
            return _auth_page(request, "auth_forgot.html",
                              status_code=_error_status(exc),
                              error=_error_text(exc))
    # Same answer whether the account exists or not (no enumeration).
    return _auth_page(request, "auth_forgot.html",
                      message="If that address has an account, a reset link is "
                              "on its way.")


def _no_recovery(request: Request):
    return _auth_page(request, "auth_forgot.html", status_code=400,
                      error=ERROR_TEXT["link_expired"])


@router.get("/reset", response_class=HTMLResponse)
async def reset_form(request: Request):
    if not request.cookies.get(RECOVERY_COOKIE):
        return _no_recovery(request)
    return _auth_page(request, "auth_reset.html")


@router.post("/reset")
def reset(request: Request, password: str = Form("")):
    access_token = request.cookies.get(RECOVERY_COOKIE)
    if not access_token:
        return _no_recovery(request)
    if len(password) < MIN_PASSWORD_LEN:
        return _auth_page(request, "auth_reset.html", status_code=400,
                          error=f"Password needs at least {MIN_PASSWORD_LEN} characters.")
    try:
        supabase_auth.update_password(access_token, password)
    except AuthError as exc:
        if exc.code == "weak_password":
            return _auth_page(request, "auth_reset.html", status_code=400,
                              error=_error_text(exc))
        response = _auth_page(request, "auth_forgot.html",
                              status_code=_error_status(exc),
                              error=_error_text(exc))
        response.delete_cookie(RECOVERY_COOKIE)
        return response
    response = RedirectResponse(url="/login?msg=Password+updated.+Log+in.",
                                status_code=303)
    response.delete_cookie(RECOVERY_COOKIE)
    return response


@router.post("/admin/switch")
def admin_switch(request: Request, workspace_id: int = Form(...)):
    """The owner's god-view switcher. Members don't get it."""
    user = request.state.user
    if not user or user["role"] != "admin":
        return HTMLResponse("Not allowed.", status_code=403)
    if not lead_db.get_workspace(workspace_id):
        return HTMLResponse("No such workspace.", status_code=404)
    acting = None if workspace_id == user["workspace_id"] else workspace_id
    return _set_session(RedirectResponse(url="/", status_code=303),
                        user["id"], acting)
