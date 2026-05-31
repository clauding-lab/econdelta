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

# README hero banner (SVG). GitHub serves README SVGs in a sandbox that blocks
# web fonts, so this uses system mono/serif stacks + the brand palette. Numbers
# refresh whenever this script runs (daily via the stats-badges workflow).
TOTAL_G="$(group "$TOTAL")"
cat > "$OUT_DIR/hero.svg" <<SVG
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="340" viewBox="0 0 1200 340" role="img" aria-label="EconDelta — ${TOTAL_G} data points across ${INDICATORS} indicators, ${YEARS} years of history since ${SINCE_YEAR}">
  <defs>
    <pattern id="g" width="40" height="40" patternUnits="userSpaceOnUse">
      <path d="M40 0H0V40" fill="none" stroke="#141c2b" stroke-width="1"/>
    </pattern>
    <style>
      .mono{font-family:ui-monospace,'SF Mono','Cascadia Code',Menlo,Consolas,monospace}
      .serif{font-family:Georgia,'Times New Roman',serif}
    </style>
  </defs>
  <rect width="1200" height="340" fill="#0b1220"/>
  <rect width="1200" height="340" fill="url(#g)"/>
  <rect width="560" height="6" fill="#ff5a1f"/>
  <rect x="560" width="150" height="6" fill="#098e8e"/>
  <text x="64" y="74" class="mono" font-size="15" letter-spacing="4" fill="#ff5a1f">AUTONOMOUS DATA PIPELINE &#183; BANGLADESH MACRO</text>
  <text x="61" y="150" class="serif" font-size="62" font-weight="700" fill="#f1f4fa">Econ<tspan fill="#ff5a1f">&#916;</tspan>elta</text>
  <text x="64" y="192" class="serif" font-size="22" fill="#b6bfd0">Bangladesh&#8217;s macroeconomy, captured autonomously &#8212; every day.</text>
  <line x1="64" y1="228" x2="1136" y2="228" stroke="#232a3a" stroke-width="1"/>
  <text x="64"  y="272" class="mono" font-size="13" letter-spacing="2.5" fill="#7a8497">DATA POINTS</text>
  <text x="64"  y="316" class="mono" font-size="46" font-weight="700" fill="#ff5a1f">${TOTAL_G}</text>
  <text x="430" y="272" class="mono" font-size="13" letter-spacing="2.5" fill="#7a8497">YEARS OF HISTORY</text>
  <text x="430" y="316" class="mono" font-size="46" font-weight="700" fill="#f1f4fa">${YEARS}<tspan font-size="22" fill="#7a8497"> yrs</tspan></text>
  <text x="720" y="272" class="mono" font-size="13" letter-spacing="2.5" fill="#7a8497">INDICATORS</text>
  <text x="720" y="316" class="mono" font-size="46" font-weight="700" fill="#f1f4fa">${INDICATORS}</text>
  <text x="1136" y="74" text-anchor="end" class="mono" font-size="12" letter-spacing="1.5" fill="#5b6577">BB &#183; BBS &#183; NBR &#183; DSE &#183; DAM &#183; COMMODITIES</text>
  <text x="1136" y="316" text-anchor="end" class="mono" font-size="15" fill="#4ed1d1">econdelta.clauding-lab.com</text>
</svg>
SVG

echo "badges + hero.svg → $OUT_DIR  (total=$TOTAL daily=$DAILY archive=$ARCHIVE indicators=$INDICATORS years=$YEARS since=$SINCE_YEAR)"
