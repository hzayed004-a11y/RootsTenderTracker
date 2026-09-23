#!/bin/bash
# Daily screening cycle, run on a machine inside the company network.
#
# The MOH portal refuses connections from datacentre addresses, so the hosted
# app can never screen for itself. This runs the cycle here and publishes the
# result up to it.
#
# Scheduled by ~/Library/LaunchAgents/com.roots.tendercycle.plist at 12:00
# Kuwait time. Run it by hand any time to force a cycle.

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR" || exit 1

mkdir -p logs
LOG="$APP_DIR/logs/daily_cycle.log"

# Keep the log from growing without bound: 2 MB, one generation back.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 2097152 ]; then
    mv -f "$LOG" "$LOG.1"
fi
exec >> "$LOG" 2>&1

echo ""
echo "================================================================"
echo "cycle starting $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "================================================================"

# Credentials live outside the repository.
ENV_FILE="$APP_DIR/.sync_env"
if [ ! -f "$ENV_FILE" ]; then
    echo "FAILED: $ENV_FILE is missing - cannot publish without the app URL and token."
    exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${APP_URL:?APP_URL not set in .sync_env}"
: "${APP_TOKEN:?APP_TOKEN not set in .sync_env}"

PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || { echo "FAILED: no virtualenv at $PY"; exit 1; }

# ---------------------------------------------------------------- screen
echo "--- screening cycle ---"
if ! "$PY" run.py run; then
    echo "FAILED: the screening cycle errored; nothing published."
    exit 1
fi

# ------------------------------------------------------------- safeguard
# Publishing replaces the hosted screening tables outright. A cycle that came
# back empty - portal down, network blocked, search form changed - must never
# be allowed to wipe yesterday's good data.
COUNT=$("$PY" - <<'PY'
import sqlite3, yaml, sys
cfg = yaml.safe_load(open("config.yaml"))
try:
    conn = sqlite3.connect(cfg["database"])
    print(conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0])
except Exception as exc:
    print(0, file=sys.stderr)
    print(0)
PY
)
echo "tenders in the local database: ${COUNT:-0}"
if [ -z "${COUNT:-}" ] || [ "$COUNT" -lt 1 ]; then
    echo "ABORTED: the cycle produced no tenders, so the hosted copy was left"
    echo "         untouched. Check the portal and the calibration."
    exit 1
fi

# ------------------------------------------------------------- snapshot
echo "--- snapshot ---"
if ! "$PY" run.py snapshot -o screening_snapshot.json; then
    echo "FAILED: could not write the snapshot."
    exit 1
fi

# -------------------------------------------------------------- publish
echo "--- publishing to $APP_URL ---"
# Retried: an unattended job gets one chance a day, and a redeploy or a
# momentary network blip should not cost the whole cycle.
for attempt in 1 2 3 4; do
    HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 300 \
        -b "app_token=$APP_TOKEN" \
        -X POST -F "snapshot=@screening_snapshot.json" \
        "$APP_URL/sync/import")

    if [ "$HTTP" = "303" ] || [ "$HTTP" = "200" ]; then
        echo "published OK (HTTP $HTTP) on attempt $attempt at $(date '+%Y-%m-%d %H:%M:%S %Z')"
        exit 0
    fi

    echo "attempt $attempt failed (HTTP $HTTP)"
    [ "$attempt" -lt 4 ] && sleep $((attempt * 30))
done

echo "FAILED: upload did not succeed after 4 attempts (last HTTP $HTTP)."
echo "        The hosted copy still shows the previous cycle, and the"
echo "        snapshot is kept at screening_snapshot.json - upload it by"
echo "        hand from the Publish cycle page."
exit 1
