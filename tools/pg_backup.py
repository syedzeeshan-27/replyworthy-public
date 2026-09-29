"""Postgres backup + rehearsed restore for the lead DB (plans/v2/04 C).

Backup: pg_dump custom-format archive into data/backups/ (put this on a
weekly cron — on Render, a Cron Job running `python tools/pg_backup.py`).
Restore: ALWAYS into a scratch database URL, never the live one — the point
of the drill is to SEE recovery work before a customer's data depends on it
(plans/v2/04 F4). Restoring over the live DATABASE_URL is refused.

Requires the postgres client tools (pg_dump/pg_restore) on PATH.

Usage:
    python tools/pg_backup.py                                  # take a backup
    python tools/pg_backup.py --restore data/backups/x.dump \
        --target-url postgres://.../scratch_db                 # rehearsal
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DATA_DIR  # noqa: E402

BACKUP_DIR = DATA_DIR / "backups"


def take_backup(database_url: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = BACKUP_DIR / f"leadsdb_{stamp}.dump"
    subprocess.run(
        ["pg_dump", "--format=custom", "--file", str(out), database_url],
        check=True,
    )
    print(f"Backup written: {out} ({out.stat().st_size} bytes)")
    return out


def restore_backup(dump_file: Path, target_url: str, live_url: str | None) -> None:
    if not dump_file.exists():
        raise SystemExit(f"No such backup file: {dump_file}")
    if live_url and target_url.strip() == live_url.strip():
        raise SystemExit(
            "Refusing to restore over the LIVE DATABASE_URL. Restores are a "
            "drill into a scratch database — create one and pass its URL as "
            "--target-url."
        )
    subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "--no-owner",
         "--dbname", target_url, str(dump_file)],
        check=True,
    )
    print(f"Restored {dump_file.name} into the scratch database. "
          "Open it and check a known lead is present — that's the drill.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup / restore the Postgres lead DB.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                        help="Postgres to back up (default: DATABASE_URL from .env)")
    parser.add_argument("--restore", metavar="DUMP_FILE",
                        help="restore this backup instead of taking one")
    parser.add_argument("--target-url",
                        help="scratch database URL to restore into (required with --restore)")
    args = parser.parse_args()

    if args.restore:
        if not args.target_url:
            raise SystemExit("--restore needs --target-url (a SCRATCH database).")
        restore_backup(Path(args.restore), args.target_url,
                       os.environ.get("DATABASE_URL"))
        return 0

    if not args.database_url:
        raise SystemExit("No DATABASE_URL set and no --database-url given.")
    take_backup(args.database_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
