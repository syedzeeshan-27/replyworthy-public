"""Wipe one workspace's pipeline data — leads, campaigns and their history.

For a fresh start after test runs: the workspace keeps its account, offers,
mailboxes, settings and billing; everything the pipeline produced goes away.
Global unsubscribes (hard_suppression) are NEVER touched — someone who opted
out stays opted out, in every workspace, forever.

Deleting is permanent, so a bare run only PREVIEWS. Add --yes to delete.

Usage:
    python tools/reset_workspace.py --workspace 1
    python tools/reset_workspace.py --workspace 1 --yes
    python tools/reset_workspace.py --workspace 1 --yes --include-suppression
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lead_db  # noqa: E402


def _print_counts(counts: dict[str, int], verb: str) -> None:
    for table, n in counts.items():
        print(f"  {table:16} {n} {verb}")
    print(f"  {'TOTAL':16} {sum(counts.values())} {verb}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete one workspace's leads, campaigns and outreach history.")
    parser.add_argument("--workspace", type=int, required=True,
                        help="workspace id to reset")
    parser.add_argument("--yes", action="store_true",
                        help="actually delete (without this it only previews)")
    parser.add_argument("--include-suppression", action="store_true",
                        help="also clear this workspace's own suppression list")
    args = parser.parse_args(argv)

    workspace = lead_db.get_workspace(args.workspace)
    if workspace is None:
        print(f"No workspace with id {args.workspace}.")
        return 2

    label = f"workspace {workspace['id']} — {workspace['name']}"
    if not args.yes:
        counts = lead_db.count_workspace_data(
            args.workspace, include_suppression=args.include_suppression)
        print(f"Preview for {label}:")
        _print_counts(counts, "row(s) would be deleted")
        print("\nNothing was deleted. Re-run with --yes to go ahead.")
        return 0

    counts = lead_db.reset_workspace_data(
        args.workspace, include_suppression=args.include_suppression)
    print(f"Reset {label}:")
    _print_counts(counts, "row(s) deleted")
    kept = ["offers", "mailboxes", "users", "settings", "billing",
            "global unsubscribes"]
    if not args.include_suppression:
        kept.append("this workspace's suppression list")
    print("Kept: " + ", ".join(kept) + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
