"""Supabase Auth client — the ONLY module that talks to Supabase Auth.

Supabase owns the passwords, the "confirm your email" mail and the password
reset mail. This module is a thin wrapper over its REST API (plain `requests`,
no SDK); web/auth.py calls it and keeps its own signed session cookie, so a
provider swap is this one file plus the two keys below.

Needs in .env:
    SUPABASE_URL        https://<project-ref>.supabase.co
    SUPABASE_ANON_KEY   the project's public "anon" / publishable key

No service-role key is used anywhere — nothing here can bypass Supabase's own
rules. Never log or print a password or an access token.

Every failure raises AuthError with a stable `code` the routes switch on:
    not_configured       keys missing, or Supabase rejected our API key
    invalid_credentials  wrong email or password
    email_not_confirmed  the account exists but the email link wasn't clicked
    already_registered   signup for an address that already has an account
    weak_password        Supabase's password policy said no
    link_expired         confirm/reset link is used up, expired or invalid
    rate_limited         too many emails / attempts, try again later
    unavailable          Supabase could not be reached
    unknown              anything else (message carries Supabase's text)
"""
from __future__ import annotations

import os
import sys

import requests

TIMEOUT_SECONDS = 15

# Supabase error_code values (and the older error strings) -> our codes.
_CODE_MAP = {
    "invalid_credentials": "invalid_credentials",
    "invalid_grant": "invalid_credentials",
    "email_not_confirmed": "email_not_confirmed",
    "user_already_exists": "already_registered",
    "email_exists": "already_registered",
    "weak_password": "weak_password",
    "otp_expired": "link_expired",
    "flow_state_expired": "link_expired",
    "flow_state_not_found": "link_expired",
    "bad_jwt": "link_expired",
    "session_not_found": "link_expired",
    "session_expired": "link_expired",
    "over_email_send_rate_limit": "rate_limited",
    "over_request_rate_limit": "rate_limited",
}


class AuthError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def configured() -> bool:
    return bool(_base_url() and _anon_key())


def _base_url() -> str:
    return os.environ.get("SUPABASE_URL", "").strip().rstrip("/")


def _anon_key() -> str:
    return os.environ.get("SUPABASE_ANON_KEY", "").strip()


def _error_from(status: int, body: dict) -> AuthError:
    raw_code = str(body.get("error_code") or body.get("error") or "")
    message = str(body.get("msg") or body.get("error_description")
                  or body.get("message") or "")
    code = _CODE_MAP.get(raw_code)
    if code is None:
        lowered = message.lower()
        if "not confirmed" in lowered:
            code = "email_not_confirmed"
        elif "already registered" in lowered:
            code = "already_registered"
        elif "invalid api key" in lowered or "no api key" in lowered:
            code = "not_configured"
        elif status == 429:
            code = "rate_limited"
        else:
            code = "unknown"
    if code == "not_configured":
        print("ERROR: Supabase rejected SUPABASE_ANON_KEY: check the key and "
              "SUPABASE_URL in .env.", file=sys.stderr)
    return AuthError(code, message)


def _call(method: str, path: str, payload: dict | None = None,
          access_token: str | None = None) -> dict:
    """One request to Supabase Auth. Returns the JSON body; raises AuthError.
    The test suite replaces this function with an in-memory fake."""
    if not configured():
        raise AuthError("not_configured",
                        "SUPABASE_URL / SUPABASE_ANON_KEY are not set")
    headers = {"apikey": _anon_key(), "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = requests.request(
            method, f"{_base_url()}/auth/v1{path}", json=payload or {},
            headers=headers, timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise AuthError("unavailable", type(exc).__name__) from exc
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code >= 400:
        raise _error_from(response.status_code, body)
    return body


def _user(raw: dict) -> dict:
    """Supabase's user object boiled down to what the app uses."""
    return {
        "id": raw.get("id"),
        "email": (raw.get("email") or "").strip().lower(),
        "metadata": raw.get("user_metadata") or {},
        "confirmed": bool(raw.get("email_confirmed_at") or raw.get("confirmed_at")),
    }


def _session(body: dict) -> dict:
    if not body.get("access_token") or not isinstance(body.get("user"), dict):
        raise AuthError("unknown", "Supabase returned no session")
    return {"access_token": body["access_token"], "user": _user(body["user"])}


def sign_up(email: str, password: str, metadata: dict | None = None) -> dict:
    """Create the account. Returns {"user", "access_token"}; access_token is
    None while the email still has to be confirmed (the normal case)."""
    body = _call("POST", "/signup", {
        "email": email, "password": password, "data": metadata or {},
    })
    if body.get("access_token"):  # project has "Confirm email" switched off
        return _session(body)
    # With confirmation on, Supabase answers a signup for an existing account
    # with a look-alike user that has no identities.
    if body.get("identities") == []:
        raise AuthError("already_registered", "User already registered")
    return {"access_token": None, "user": _user(body)}


def sign_in(email: str, password: str) -> dict:
    """Check the password. Returns {"user", "access_token"}."""
    return _session(_call("POST", "/token?grant_type=password",
                          {"email": email, "password": password}))


def verify_token_hash(token_hash: str, kind: str) -> dict:
    """Redeem the token from an emailed link (kind: 'email' for signup
    confirmation, 'recovery' for a password reset). Single use."""
    return _session(_call("POST", "/verify",
                          {"type": kind, "token_hash": token_hash}))


def send_recovery(email: str) -> None:
    _call("POST", "/recover", {"email": email})


def resend_confirmation(email: str) -> None:
    _call("POST", "/resend", {"type": "signup", "email": email})


def update_password(access_token: str, password: str) -> None:
    _call("PUT", "/user", {"password": password}, access_token=access_token)
