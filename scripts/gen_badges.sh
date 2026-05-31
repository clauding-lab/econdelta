#!/usr/bin/env bash
# Generate shields.io endpoint-badge JSON from live EconDelta Supabase counts.
#
# Reads the PUBLIC anon config from pwa/config.js (no secrets needed — the anon
# key is read-only on these tables by design). Writes one JSON file per badge to
# the output directory ($1, default ./badges-out). The stats-badges workflow
# publishes these to the `badges` branch, which the README endpoint badges read.
#
# Run locally to seed the badges branch:  bash scripts/gen_badges.sh
set -euo pipefail

OUT_DIR="${1:-badges-out}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/pwa/config.js"

URL=$(grep -oE "url:[[:space:]]*'[^']+'" "$CONFIG" | sed -E "s/.*'([^']+)'.*/\1/")
KEY=$(grep -oE "anonKey:[[:space:]]*'[^']+'" "$CONFIG" | sed -E "s/.*'([^']+)'.*/\1/")
if [[ -z "$URL" || -z "$KEY" ]]; then
  echo "ERROR: could not parse url/anonKey from $CONFIG" >&2
  exit 1
fi

# Exact row count via PostgREST Content-Range header ("0-0/<total>").
count() {
  curl -fsS -o /dev/null -D - \
    -H "apikey: $KEY" -H "Authorization: Bearer $KEY" \
    -H "Prefer: count=exact" -H "Range: 0-0" \
    "$URL/rest/v1/$1?select=metric_id" \
    | tr -d '\r' | awk 'tolower($0) ~ /^content-range:/ {n=split($0,a,"/"); print a[n]}'
}

DAILY=$(count metric_history)
ARCHIVE=$(count metric_history_monthly)
INDICATORS=$(count metric_definitions)
EARLIEST=$(curl -fsS -H "apikey: $KEY" -H "Authorization: Bearer $KEY" \
  "$URL/rest/v1/metric_history_monthly?select=as_of&order=as_of.asc&limit=1" \
  | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)

: "${DAILY:=0}"; : "${ARCHIVE:=0}"; : "${INDICATORS:=0}"
TOTAL=$(( DAILY + ARCHIVE ))
SINCE_YEAR="${EARLIEST%%-*}"; : "${SINCE_YEAR:=2012}"
YEARS=$(( $(date -u +%Y) - SINCE_YEAR ))
TODAY=$(date -u +%F)

# Portable thousands grouping (works with both BSD and GNU awk; no locale dep).
group() { awk -v n="$1" 'BEGIN{ s=""; while(n>=1000){ s=sprintf(",%03d",n%1000) s; n=int(n/1000) } printf "%d%s\n", n, s }'; }

mkdir -p "$OUT_DIR"
badge() { # file label message color
  printf '{"schemaVersion":1,"label":"%s","message":"%s","color":"%s"}\n' \
    "$2" "$3" "$4" > "$OUT_DIR/$1.json"
}

badge datapoints "data points"  "$(group "$TOTAL")"  "ff5a1f"
badge backlog    "history"       "${YEARS} years"    "006d6d"
badge indicators "indicators"    "$INDICATORS"        "5b6577"
badge updated    "data updated"  "$TODAY"             "informational"

echo "badges → $OUT_DIR  (total=$TOTAL daily=$DAILY archive=$ARCHIVE indicators=$INDICATORS years=$YEARS since=$SINCE_YEAR)"
