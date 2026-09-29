#!/bin/sh
# Render container entrypoint. Web UI always; outreach heartbeat loop only
# when TICK_LOOP=true (one container so both share the /data persistent disk —
# Render cron jobs can't mount a web service's disk).
set -e

# Normalize the same way web/jobs.py does (case- and whitespace-insensitive),
# so the two never disagree — a value like "True" must not launch the loop
# here while the in-process pacemaker thinks it did and stands down, leaving
# NO ticker running at all.
TICK_LOOP_NORM=$(printf '%s' "$TICK_LOOP" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
if [ "$TICK_LOOP_NORM" = "true" ]; then
  echo "[start] TICK_LOOP=true -> outreach tick loop every 15 min"
  python tools/outreach_tick.py --loop &
fi

exec uvicorn web.app:app --host 0.0.0.0 --port "${PORT:-10000}"
