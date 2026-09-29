"""Export enriched leads to a formatted Excel deliverable (.xlsx).

Final pipeline step (Excel variant): reads the latest
.tmp/leads_enriched_<timestamp>.json (written by tools/enrich_leads.py) and
writes a styled workbook to outputs/leads_<date>.xlsx — header styling, column
widths, wrapped text, alternating row colors, score-based conditional fills,
frozen header, and borders. Rows are sorted by Score descending.

Usage:
    python tools/save_to_excel.py
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# Make `config` importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import OUTPUTS_DIR, TMP_DIR  # noqa: E402
from tools.parse_query import describe_spec  # noqa: E402

# (Header label, lead dict key, column width). Order defines column order.
COLUMNS = [
    ("Business Name", "name", 25),
    ("Phone Number", "phone", 15),
    ("Email", "email", 28),
    ("Website", "website", 30),
    ("Facebook", "facebook", 28),
    ("Instagram", "instagram", 28),
    ("Area/Locality", "area", 22),
    ("Google Maps Rating", "rating", 10),
    ("Number of Reviews", "reviews_count", 12),
    ("Pain Points", "pain_points", 50),
    ("Email Subject", "email_subject", 35),
    ("Email Body", "email_body", 70),
    ("Score", "score", 8),
]

# Person columns (V2 Stage 3, plans/v2/03 A): only added when the batch
# contains person leads, so business-only workbooks stay byte-identical.
_PERSON_EXTRA_COLUMNS = [
    ("Title", "title", 20),
    ("Company", "company", 26),
    ("LinkedIn", "linkedin_url", 34),
]

# List-only layout (the "just the leads, no emails" deliverable): every field
# here comes straight from Apify BEFORE any AI runs, so this workbook is free
# to produce. Deliberately a separate constant, not a slice of COLUMNS — the
# labels differ ("Area", not "Area/Locality"; "Google Rating", not "Google
# Maps Rating"), and it must never carry the AI columns.
LIST_COLUMNS = [
    ("Business Name", "name", 28),
    ("Phone Number", "phone", 16),
    ("Website", "website", 34),
    ("Facebook", "facebook", 28),
    ("Instagram", "instagram", 28),
    ("Area", "area", 22),
    ("Google Rating", "rating", 12),
    ("Number of Reviews", "reviews_count", 14),
]


def _columns_for(leads: list[dict], layout: str = "auto") -> list[tuple]:
    """Column layout for a batch.

    `layout="list"` forces the free, AI-free eight-column deliverable.
    `layout="auto"` (default) keeps the original behaviour: business-only
    batches use COLUMNS; batches containing person leads get
    Title/Company/LinkedIn inserted after the name (and the first header
    becomes just "Name", since rows may be people or businesses).

    Layout is chosen by the CALLER, never inferred from the data — a list
    batch and an enriched batch hold identical business dicts, so guessing
    from "no lead has a score" would silently downgrade a real enriched
    workbook whose scores all happened to be missing.
    """
    if layout == "list":
        return LIST_COLUMNS
    if not any((l.get("entity_type") or "") == "person" for l in leads):
        return COLUMNS
    cols = list(COLUMNS)
    cols[0] = ("Name", "name", 25)
    return cols[:1] + _PERSON_EXTRA_COLUMNS + cols[1:]

# Keys whose cells should wrap their (long) text.
WRAP_KEYS = {"pain_points", "email_subject", "email_body"}
# Keys rendered as numbers (kept numeric so Excel sorts/filters them as numbers).
NUMERIC_KEYS = {"rating", "reviews_count", "score"}
# Keys whose cells should center-align.
CENTER_KEYS = {"rating", "reviews_count", "score"}

HEADER_ROW_HEIGHT = 30
DATA_ROW_HEIGHT = None  # None => let Excel auto-fit the row height

# --- Reusable styles ---
HEADER_FILL = PatternFill("solid", fgColor="1A1A2E")
HEADER_FONT = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
DATA_FONT = Font(name="Calibri", size=11)
LINK_FONT = Font(name="Calibri", size=11, color="0563C1", underline="single")

ROW_FILL_WHITE = PatternFill("solid", fgColor="FFFFFF")
ROW_FILL_GRAY = PatternFill("solid", fgColor="F2F2F2")

SCORE_FILL_GREEN = PatternFill("solid", fgColor="C6EFCE")   # 4-5
SCORE_FILL_YELLOW = PatternFill("solid", fgColor="FFEB9C")  # 3
SCORE_FILL_RED = PatternFill("solid", fgColor="FFC7CE")     # 1-2

_thin = Side(style="thin", color="D0D0D0")
CELL_BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)


def latest_spec() -> dict | None:
    """The LeadSpec of the most recent run (.tmp/leadspec_*.json, written by
    run_lead_gen.py right before the steps), or None if there isn't one —
    e.g. an old run or this tool executed standalone against stale files."""
    candidates = sorted(
        TMP_DIR.glob("leadspec_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    try:
        spec = json.loads(candidates[0].read_text(encoding="utf-8"))
        return spec if isinstance(spec, dict) else None
    except (OSError, ValueError):
        return None


def latest_enriched() -> Path:
    """Return the most recently modified .tmp/leads_enriched_<timestamp>.json."""
    candidates = sorted(
        TMP_DIR.glob("leads_enriched_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No leads_enriched_*.json files in {TMP_DIR}. "
            "Run tools/enrich_leads.py first."
        )
    return candidates[0]


def _score_value(lead: dict) -> int:
    """Score as an int for sorting; missing/garbage scores sort to the bottom."""
    try:
        return int(lead.get("score"))
    except (TypeError, ValueError):
        return -1


def _score_fill(score: int | None) -> PatternFill | None:
    """Pick the conditional fill for a score: green 4-5, yellow 3, red 1-2."""
    if score is None:
        return None
    if score >= 4:
        return SCORE_FILL_GREEN
    if score == 3:
        return SCORE_FILL_YELLOW
    if score >= 1:
        return SCORE_FILL_RED
    return None


def _social_url(lead: dict, platform: str) -> str:
    """One platform's URL from lead["social_links"] — a dict on pipeline
    leads, a JSON string on DB rows, absent on pre-0006 rows (whose flat
    legacy key is the fallback)."""
    links = lead.get("social_links")
    if isinstance(links, str):
        try:
            links = json.loads(links)
        except ValueError:
            links = None
    if isinstance(links, dict) and links.get(platform):
        return str(links[platform])
    return str(lead.get(platform) or "")


def _cell_value(lead: dict, key: str):
    """Render one lead field for a cell (join pain_points; keep numerics numeric)."""
    value = lead.get(key)
    if key in ("facebook", "instagram"):
        return _social_url(lead, key)
    if key == "pain_points":
        if isinstance(value, list):
            return "; ".join(str(p) for p in value)
        return "" if value is None else str(value)
    if key == "score":
        sv = _score_value(lead)
        return sv if sv >= 0 else ""
    if key == "rating":
        try:
            return float(value) if value is not None else ""
        except (TypeError, ValueError):
            return ""
    if key == "reviews_count":
        try:
            return int(value) if value is not None else ""
        except (TypeError, ValueError):
            return ""
    return "" if value is None else str(value)


def _normalize_url(value: str) -> str:
    """Ensure a website value has a scheme so Excel treats it as a real link."""
    value = value.strip()
    if value and not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def _add_search_sheet(wb: Workbook, spec: dict) -> None:
    """Second worksheet recording what was asked for, so the deliverable
    itself answers "what search produced these leads" (plan v2/02 C)."""
    ws = wb.create_sheet("Search")
    label_font = Font(name="Calibri", size=11, bold=True)
    rows = [
        ("You asked for", spec.get("raw_query") or describe_spec(spec)),
        ("Understood as", describe_spec(spec)),
        ("Business type", spec.get("industry") or ""),
        ("Location", ", ".join(
            p for p in ((spec.get("geo") or {}).get(k) for k in ("city", "region", "country")) if p
        )),
        ("Max leads", spec.get("count", "")),
        ("Extra criteria", spec.get("qualification_hints") or ""),
        ("Couldn't do", "; ".join(spec.get("unsupported") or [])),
    ]
    for row_idx, (label, value) in enumerate(rows, start=1):
        cell = ws.cell(row=row_idx, column=1, value=label)
        cell.font = label_font
        ws.cell(row=row_idx, column=2, value=value).font = DATA_FONT
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 80


def build_workbook(leads: list[dict], spec: dict | None = None,
                   layout: str = "auto") -> Workbook:
    """Build the styled leads workbook in memory and return it (no disk I/O).
    Rows are sorted by Score descending; when `spec` is given a second
    "Search" sheet records what was asked for. Shared by save_to_excel (the
    on-disk deliverable) and tools/export_leads.py (the in-memory download).

    `layout="list"` produces the eight-column, AI-free deliverable; the list
    batch has no scores, so the sort is a stable no-op that preserves the
    scrape order the preview showed."""
    ordered = sorted(leads, key=_score_value, reverse=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    columns = _columns_for(ordered, layout)

    # --- Header row ---
    ws.row_dimensions[1].height = HEADER_ROW_HEIGHT
    for col_idx, (header, _key, width) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = CELL_BORDER
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # --- Data rows ---
    for row_offset, lead in enumerate(ordered):
        excel_row = row_offset + 2  # row 1 is the header
        row_fill = ROW_FILL_WHITE if row_offset % 2 == 0 else ROW_FILL_GRAY
        if DATA_ROW_HEIGHT is not None:
            ws.row_dimensions[excel_row].height = DATA_ROW_HEIGHT

        score = _score_value(lead)
        score = score if score >= 0 else None

        for col_idx, (_header, key, _width) in enumerate(columns, start=1):
            cell = ws.cell(row=excel_row, column=col_idx, value=_cell_value(lead, key))
            cell.font = DATA_FONT
            cell.border = CELL_BORDER
            cell.alignment = Alignment(
                vertical="top",
                wrap_text=key in WRAP_KEYS,
                horizontal="center" if key in CENTER_KEYS else "left",
            )
            # Make Website/LinkedIn/social cells real clickable hyperlinks.
            if key in ("website", "linkedin_url", "facebook", "instagram") and cell.value:
                url = _normalize_url(str(cell.value))
                cell.value = url
                cell.hyperlink = url
                cell.font = LINK_FONT
            # Score column gets a conditional fill; every other cell gets the
            # alternating zebra fill.
            if key == "score":
                cell.fill = _score_fill(score) or row_fill
            else:
                cell.fill = row_fill

    # Freeze the header row so it stays visible while scrolling.
    ws.freeze_panes = "A2"
    # Add a filter dropdown to every column header.
    ws.auto_filter.ref = ws.dimensions

    if spec:
        _add_search_sheet(wb, spec)

    return wb


def save_to_excel(leads: list[dict], spec: dict | None = None,
                  layout: str = "auto", *, run_id: str | None = None) -> Path:
    """Write `leads` to outputs/leads_<date>.xlsx with full formatting.
    When `spec` is given, a second "Search" sheet records what was asked
    for. Returns the path.

    `run_id` scopes the filename to one run (leads_<date>_<run_id>.xlsx).
    Without it every run on a given day writes the SAME path, so a second
    run overwrites the first — and since each job hands out that path as its
    download link, one workspace could be served another's workbook.
    """
    OUTPUTS_DIR.mkdir(exist_ok=True)
    wb = build_workbook(leads, spec, layout)
    date_str = datetime.now().strftime("%Y%m%d")
    suffix = f"_{run_id}" if run_id else ""
    out_path = OUTPUTS_DIR / f"leads_{date_str}{suffix}.xlsx"
    wb.save(out_path)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render enriched leads to the Excel deliverable in outputs/.")
    parser.add_argument(
        "--in-file", default=None,
        help="enriched-leads JSON to render (default: the most recent "
             "leads_enriched_*.json in .tmp).")
    parser.add_argument(
        "--spec-file", default=None,
        help="LeadSpec JSON for the Search sheet (default: the most recent "
             "leadspec_*.json in .tmp).")
    parser.add_argument(
        "--run-id", default=None,
        help="scopes the output filename (leads_<date>_<run-id>.xlsx) so "
             "same-day runs don't overwrite each other.")
    parser.add_argument(
        "--layout", default="auto", choices=("auto", "list"),
        help='"list" produces the free eight-column, AI-free deliverable.')
    args = parser.parse_args()

    src_path = Path(args.in_file) if args.in_file else latest_enriched()
    payload = json.loads(src_path.read_text(encoding="utf-8"))
    leads = payload.get("results", [])

    if args.spec_file:
        spec = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
    else:
        spec = latest_spec()

    out_path = save_to_excel(leads, spec=spec, layout=args.layout,
                             run_id=args.run_id)
    print(f"Wrote {len(leads)} leads from {src_path.name} to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
