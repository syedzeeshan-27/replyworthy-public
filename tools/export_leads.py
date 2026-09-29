"""In-memory lead exports for the pipeline download buttons (CSV + Excel).

The run *deliverable* stays Excel-only on disk (tools/save_to_excel.py). This
module is the interactive `/pipeline/export` path: it renders the user's
currently-filtered leads to bytes for an HTTP download, in either format,
WITHOUT touching outputs/. Columns come straight from save_to_excel
(_columns_for / _cell_value) so an export always matches the deliverable's
layout — business vs person columns included — and there is one column source
of truth. CSV lives here, and only here (see the CLAUDE.md deliverables rule).

No disk I/O, no side effects — callers get bytes and set the HTTP headers.
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse the deliverable's column definitions and cell rendering verbatim — the
# single source of truth for what a lead export looks like.
from save_to_excel import (  # noqa: E402
    _cell_value, _columns_for, _score_value, build_workbook)


def _ordered(leads: list[dict]) -> list[dict]:
    """Same row order as the Excel deliverable: Score descending."""
    return sorted(leads, key=_score_value, reverse=True)


def to_csv_bytes(leads: list[dict], layout: str = "auto") -> bytes:
    """The filtered leads as CSV bytes, same columns as the Excel deliverable
    (_columns_for picks the business, person, or list layout). Encoded UTF-8
    with a BOM so Excel opens it in the right encoding; rows sorted by score
    desc."""
    cols = _columns_for(leads, layout)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow([header for header, _key, _width in cols])
    for lead in _ordered(leads):
        writer.writerow([_cell_value(lead, key) for _header, key, _width in cols])
    return buf.getvalue().encode("utf-8-sig")


def to_xlsx_bytes(leads: list[dict], layout: str = "auto") -> bytes:
    """The filtered leads as .xlsx bytes — the full styled workbook
    (save_to_excel.build_workbook) rendered to memory instead of disk.
    `layout="list"` produces the free eight-column, AI-free deliverable."""
    buf = io.BytesIO()
    build_workbook(leads, layout=layout).save(buf)
    return buf.getvalue()


def preview_rows(leads: list[dict], limit: int = 10) -> tuple[list[str], list[list]]:
    """(headers, rows) for the gate's preview table — literally the first
    `limit` rows of the list deliverable, rendered through the same
    _cell_value path so what the owner previews is what the file contains.
    Order matches the scrape (list layout has no scores to sort by)."""
    cols = _columns_for(leads, "list")
    headers = [header for header, _key, _width in cols]
    rows = [
        [_cell_value(lead, key) for _header, key, _width in cols]
        for lead in _ordered(leads)[:limit]
    ]
    return headers, rows
