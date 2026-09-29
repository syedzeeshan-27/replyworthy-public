"""Shared settings for the LEADGEN WAT pipeline.

Single source of truth for secrets and key paths. Every tool imports from here
so secrets are only ever read from the environment (loaded from .env) and paths
resolve the same regardless of the current working directory.

Usage:
    from config.settings import APIFY_API_TOKEN, require, TMP_DIR

    token = require("APIFY_API_TOKEN")   # raises a clear error if unset
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# --- Paths (resolved relative to this file, not the CWD) ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_data_dir = os.environ.get("LEADGEN_DATA_DIR")
TMP_DIR = Path(_data_dir) / "tmp" if _data_dir else PROJECT_ROOT / ".tmp"
OUTPUTS_DIR = Path(_data_dir) / "outputs" if _data_dir else PROJECT_ROOT / "outputs"
# Persistent (non-disposable) state: lead DB, seen-places ledger. On Render
# this must live on the persistent disk (LEADGEN_DATA_DIR), like the above.
DATA_DIR = Path(_data_dir) / "data" if _data_dir else PROJECT_ROOT / "data"
WORKFLOWS_DIR = PROJECT_ROOT / "workflows"
ENV_PATH = PROJECT_ROOT / ".env"

TMP_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Load .env into the environment. override=True so our .env wins over any
# stale/empty value already set in the OS environment (e.g. an empty
# ANTHROPIC_API_KEY shell var would otherwise shadow the real key in .env).
load_dotenv(ENV_PATH, override=True)

# --- Secrets (None until the user fills in .env) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY")
# Optional: higher Jina Reader rate limits. Used by faq_agent's crawler; the
# lead-gen pipeline works fine without it.
JINA_API_KEY = os.environ.get("JINA_API_KEY")

# --- Model config ---
# Claude model used by tools/analyze_lead.py. Override in .env to switch
# models without a code change.
LEADGEN_MODEL = os.environ.get("LEADGEN_MODEL", "claude-sonnet-5")

# --- Copy guard ---
# Optional extra tool/brand names (comma-separated) to reject in generated
# email copy, on top of tools/copy_guard.py's DEFAULT_BLOCKLIST. Not a secret.
COPY_BLOCKLIST = os.environ.get("COPY_BLOCKLIST")


def require(key: str) -> str:
    """Return an environment variable, raising a clear error if it is missing.

    Use this inside tools right before a secret is needed so the script fails
    loudly and early instead of midway through an API call.
    """
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{key}'. "
            f"Add it to {ENV_PATH} (see .env.example)."
        )
    return value


if __name__ == "__main__":
    # Quick self-check: shows resolved paths and which secrets are present.
    print(f"PROJECT_ROOT : {PROJECT_ROOT}")
    print(f"TMP_DIR      : {TMP_DIR}")
    print(f".env present : {ENV_PATH.exists()}")
    for key in ("ANTHROPIC_API_KEY", "APIFY_API_TOKEN", "FIRECRAWL_API_KEY"):
        print(f"{key:18}: {'set' if os.environ.get(key) else 'MISSING'}")
