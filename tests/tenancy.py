"""Shared scaffolding for the Stage-4 tenancy test suite (not a test file).

Gives every test the same three guarantees:
  1. A fresh temp SQLite DB (never data/leads.db).
  2. No DATABASE_URL leaking in from .env — unit tests must never touch a
     real Postgres; the Postgres leg runs only via tests/test_postgres.py.
  3. Stable SESSION_SECRET / UNSUBSCRIBE_SECRET so signed tokens verify.

Also the signup/login helpers the web tests share.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import lead_db  # noqa: E402

OWNER = {"email": "owner@example.com", "password": "ownerpass123"}
TENANT = {"email": "tenant@test-co.example", "password": "tenantpass123"}


class TenancyTestMixin:
    """setUp/tearDown pieces every tenancy test needs. Call
    setup_tenancy()/teardown_tenancy() from the TestCase hooks."""

    def setup_tenancy(self) -> None:
        self._env_patch = mock.patch.dict(os.environ, {
            "SESSION_SECRET": "test-session-secret",
            "UNSUBSCRIBE_SECRET": "test-unsub-secret",
        })
        self._env_patch.start()
        os.environ.pop("DATABASE_URL", None)  # restored by the patch on stop
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = lead_db.DB_PATH
        lead_db.DB_PATH = Path(self._tmpdir.name) / "leads.db"

    def teardown_tenancy(self) -> None:
        lead_db.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()
        self._env_patch.stop()


def signup(client, email: str, password: str, workspace_name: str = ""):
    """POST /signup; the client keeps the session cookie. Returns response."""
    return client.post("/signup", data={
        "email": email, "password": password, "workspace_name": workspace_name,
    }, follow_redirects=False)


def login(client, email: str, password: str):
    return client.post("/login", data={"email": email, "password": password},
                       follow_redirects=False)


def signup_owner(client):
    """First account = admin claiming workspace 1 (the migration workspace)."""
    return signup(client, OWNER["email"], OWNER["password"], "Owner HQ")


def signup_tenant(client):
    """A second, plain-member account with its own fresh workspace."""
    return signup(client, TENANT["email"], TENANT["password"], "Test Co")
