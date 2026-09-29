"""In-memory stand-in for Supabase Auth (not a test file).

tests/__init__.py swaps this in for `supabase_auth._call`, so the suite never
reaches the network and every web test can still sign up and log in.

Accounts are kept per test database: every test points `lead_db.DB_PATH` at
its own temp file, and the fake files its accounts under that path, so one
test's signups never show up in the next.

By default it behaves like a Supabase project with "Confirm email" switched
OFF (signup logs you straight in), which is what the many tests that only
need a signed-in client want. Set `fake_supabase.CONFIRM_EMAIL = True` to get
the real flow; the links Supabase would have emailed land in `outbox()`.
"""
from __future__ import annotations

import uuid

import lead_db
import supabase_auth
from supabase_auth import AuthError

CONFIRM_EMAIL = False
REAL_CALL = supabase_auth._call  # the real HTTP function, for client tests

_state: dict[str, dict] = {}


def _db() -> dict:
    return _state.setdefault(str(lead_db.DB_PATH), {
        "users": {},      # email -> user
        "tokens": {},     # token_hash -> (kind, email)
        "sessions": {},   # access_token -> email
        "outbox": [],     # {"kind", "email", "token_hash"}
    })


def outbox() -> list[dict]:
    return _db()["outbox"]


def _public(user: dict) -> dict:
    return {
        "id": user["id"], "email": user["email"],
        "user_metadata": user["metadata"],
        "email_confirmed_at": "2026-01-01T00:00:00Z" if user["confirmed"] else None,
    }


def _session(user: dict) -> dict:
    token = "at-" + uuid.uuid4().hex
    _db()["sessions"][token] = user["email"]
    return {"access_token": token, "user": _public(user)}


def _mail(kind: str, email: str) -> None:
    token_hash = "th-" + uuid.uuid4().hex
    _db()["tokens"][token_hash] = (kind, email)
    _db()["outbox"].append({"kind": kind, "email": email,
                            "token_hash": token_hash})


def call(method: str, path: str, payload: dict | None = None,
         access_token: str | None = None) -> dict:
    payload = payload or {}
    users = _db()["users"]
    email = (payload.get("email") or "").strip().lower()

    if path == "/signup":
        if email in users:
            if CONFIRM_EMAIL:
                return {"id": str(uuid.uuid4()), "email": email, "identities": []}
            raise AuthError("already_registered", "User already registered")
        user = users[email] = {
            "id": str(uuid.uuid4()), "email": email,
            "password": payload["password"],
            "metadata": payload.get("data") or {},
            "confirmed": not CONFIRM_EMAIL,
        }
        if CONFIRM_EMAIL:
            _mail("email", email)
            return {**_public(user), "identities": [{"provider": "email"}]}
        return _session(user)

    if path == "/token?grant_type=password":
        user = users.get(email)
        if not user or user["password"] != payload.get("password"):
            raise AuthError("invalid_credentials", "Invalid login credentials")
        if not user["confirmed"]:
            raise AuthError("email_not_confirmed", "Email not confirmed")
        return _session(user)

    if path == "/verify":
        kind, owner = _db()["tokens"].pop(payload.get("token_hash"), (None, None))
        if kind is None or kind != payload.get("type"):
            raise AuthError("link_expired", "Email link is invalid or has expired")
        users[owner]["confirmed"] = True
        return _session(users[owner])

    if path == "/recover":
        if email in users:
            _mail("recovery", email)
        return {}

    if path == "/resend":
        if email in users and not users[email]["confirmed"]:
            _mail("email", email)
        return {}

    if path == "/user" and method == "PUT":
        owner = _db()["sessions"].get(access_token)
        if owner is None:
            raise AuthError("link_expired", "invalid JWT")
        users[owner]["password"] = payload["password"]
        return _public(users[owner])

    raise AssertionError(f"fake Supabase has no {method} {path}")


def install() -> None:
    supabase_auth._call = call
