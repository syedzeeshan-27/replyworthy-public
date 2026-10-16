# Test-only environment, applied before any test module imports app code.
# This package __init__ runs first under `unittest discover`, so every test
# inherits it.
import os

# config.settings loads .env with override=True (the only load_dotenv site),
# so anything we set BEFORE it would be clobbered. Import it first, then pin.
import config.settings  # noqa: F401

# The suite must behave the same on any machine, whatever the owner's .env
# says: never a real send (DRY_RUN is default-true in code; this pins it even
# when a live .env sets false), never the owner's real mailbox list (the
# workspace-1 env fallback would otherwise read it; tests that need specific
# mailboxes set their own env in setUp and still win over this), never the
# owner's real database or login service (Supabase Auth is replaced by the
# in-memory fake in tests/fake_supabase.py), and no OWNER_EMAIL so the first
# signup in a test is the admin.
os.environ["DRY_RUN"] = "true"
os.environ["SMTP_MAILBOXES"] = "dryrun@localhost"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("OWNER_EMAIL", None)
os.environ["BASE_URL"] = "http://localhost:8000"
os.environ["SUPABASE_URL"] = "http://supabase.invalid"
os.environ["SUPABASE_ANON_KEY"] = "test-anon-key"
# The email signature needs a sender; a fresh clone has no .env to give one.
os.environ["SENDER_NAME"] = "Test Sender"
os.environ["SENDER_PHONE"] = "555-0100"

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from . import fake_supabase  # noqa: E402

fake_supabase.install()

# Connecting a mailbox looks the server name up in DNS: never in the suite.
import mailboxes  # noqa: E402

mailboxes._resolve = lambda host: []
