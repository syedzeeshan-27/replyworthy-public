"""Scrape Google Maps business listings via Apify — thin wrapper.

V2 Stage 3 (plans/v2/03 B): the logic moved to tools/sources/google_maps.py
behind the SourceConnector interface. This module stays so existing imports
(`from scrape_google_maps import clean_website_url`, ...) and the CLI keep
working unchanged:

    python tools/scrape_google_maps.py --category "dentist" --city "Austin TX" --limit 50

This is the first node of the business lead-gen pipeline (Find + filter).
Downstream steps consume the .tmp/maps_raw_<timestamp>.json written here.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the sources package importable when run as a script or imported flat.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources.google_maps import (  # noqa: E402,F401
    ACTOR_ID,
    API_BASE,
    GoogleMapsConnector,
    MAX_WAIT_SECONDS,
    POLL_INTERVAL_SECONDS,
    _TRACKING_PARAMS,
    _area_from,
    _first,
    _pick,
    build_actor_input,
    clean_website_url,
    fetch_items,
    main,
    normalize,
    run_search,
    start_run,
    wait_for_run,
)

if __name__ == "__main__":
    raise SystemExit(main())
