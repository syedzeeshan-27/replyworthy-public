"""Automated leakage audit (plans/v2/04 D): every SQL string in the app and
tool code that touches a tenant table must mention workspace_id, or be on the
explicit allowlist below (each entry documents WHY it may span workspaces).

This is the tripwire for "forgot the filter": add a query on leads/sends/...
without workspace scoping anywhere in these files and this test goes red.

Run (from the repo root):
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Files that may contain SQL against tenant tables.
AUDITED_FILES = (
    "tools/lead_db.py",
    "tools/sequencer.py",
    "tools/enroll_leads.py",
    "tools/process_replies.py",
    "tools/read_inbox.py",
    "tools/daily_digest.py",
    "tools/notify.py",
    "tools/ghl_sync.py",
    "tools/billing.py",
    "tools/mailboxes.py",
    "web/app.py",
)

# Tables holding tenant data. sequence_steps is deliberately absent: it is
# reachable only through an already-scoped campaign id (plans/v2/04 B lists
# the tables that get workspace_id; sequence_steps isn't one).
# billing_events is also absent on purpose: it is the raw provider-event log
# — the workspace is only KNOWN after parsing the event, and /admin/billing
# reads it globally for reconciliation (admin-only route).
TENANT_TABLES = ("leads", "campaigns", "sends", "suppression", "replies",
                 "notes", "alerts", "offer_profiles", "mailboxes",
                 "credit_ledger")

TABLE_RE = re.compile(
    r"\b(?:FROM|INTO|UPDATE|JOIN)\s+(" + "|".join(TENANT_TABLES) + r")\b",
    re.IGNORECASE,
)

# (filename, distinctive-substring) -> why it may skip the literal
# workspace_id check. NOTE: the inbox-router queries in read_inbox.py need no
# entry — they SELECT workspace_id (that's their whole job), so the literal
# check already passes.
ALLOWLIST = {
    ("tools/lead_db.py", "INSERT INTO leads ("):
        "dynamic column INSERT; workspace_id is always in the values dict "
        "(_row_values injects it first)",
    ("tools/lead_db.py", "INSERT INTO offer_profiles ("):
        "dynamic column INSERT; create_offer puts workspace_id in the values "
        "dict first",
}


def _string_value(node) -> str | None:
    """Flatten Constant / f-string / '+'-concatenation into one string.
    FormattedValue slots become '{}' placeholders."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            else:
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_value(node.left)
        right = _string_value(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def sql_strings(path: Path) -> list[tuple[int, str]]:
    """(lineno, flattened_string) for every string expression in a file.
    A '+'-joined or f-string expression is recorded ONCE as its whole — its
    pieces are not re-visited (a fragment alone could false-positive)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []

    def visit(node) -> None:
        value = _string_value(node)
        if value is not None:
            found.append((getattr(node, "lineno", 0), value))
            return  # consumed whole — don't descend into fragments
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


class SqlAuditTestCase(unittest.TestCase):
    def test_every_tenant_table_query_is_workspace_scoped(self):
        offences = []
        allow_hits = set()
        for rel in AUDITED_FILES:
            path = PROJECT_ROOT / rel
            for lineno, text in sql_strings(path):
                if not TABLE_RE.search(text):
                    continue
                if "workspace_id" in text:
                    continue
                allowed = False
                for (allow_file, marker), _why in ALLOWLIST.items():
                    if rel == allow_file and marker in text:
                        allowed = True
                        allow_hits.add((allow_file, marker))
                        break
                if allowed:
                    continue
                offences.append(f"{rel}:{lineno}: {text[:90]!r}")
        self.assertEqual(offences, [],
                         "SQL on tenant tables without workspace_id:\n"
                         + "\n".join(offences))

    def test_allowlist_has_no_stale_entries(self):
        """Every allowlisted pattern must still exist in its file — a stale
        entry means the code changed and the allowlist should shrink."""
        for (rel, marker), _why in ALLOWLIST.items():
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(marker, text,
                          f"stale allowlist entry: {rel} / {marker!r}")

    def test_lead_db_api_requires_workspace_id(self):
        """Forgetting the workspace is a TypeError, not a data breach
        (plans/v2/04 B: required parameter, no default)."""
        import lead_db
        for fn in (lead_db.get_leads, lead_db.count_by_status,
                   lead_db.list_offers, lead_db.get_default_offer):
            with self.assertRaises(TypeError, msg=fn.__name__):
                fn()
        with self.assertRaises(TypeError):
            lead_db.get_lead(7)  # needs (workspace_id, lead_id)
        with self.assertRaises(TypeError):
            lead_db.upsert_leads([{"name": "X"}])
        with self.assertRaises(TypeError):
            lead_db.is_suppressed("a@b.example")


if __name__ == "__main__":
    unittest.main()
