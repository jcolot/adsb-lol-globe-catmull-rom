#!/usr/bin/env bash
# Daily ADS-B trace pipeline, split into phases so each can run as its own CI step:
#   resolve  -> find the latest adsblol release tag for the variant
#   fetch    -> stream the split-tar assets straight into tar (no 4-6 GB staged)
#   fit      -> fit sparse Catmull-Rom spline nodes (fit_spline.py)
#   legs     -> split into per-airport leg partitions (build_legs.py)
#   hexes    -> aggregate into H3 traffic-density vector tiles (build_hexes.py)
#   upload   -> rclone sync the legs to Cloudflare R2
# Run a single phase (`run_pipeline.sh fit`) or the whole thing (`run_pipeline.sh`
# / `run_pipeline.sh all`). Phases share state through $WORK (the resolved tag is
# written to $WORK/TAG), so the CI steps hand off via the persisted workspace.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO="${SRC_REPO:-adsblol/globe_history_2026}"
VARIANT="${VARIANT:-prod-0}"            # prod-0 (ADS-B) | mlatonly-0 | staging-0
WORK="${WORK:-$SCRIPT_DIR/work}"
OUT="${OUT:-$SCRIPT_DIR/out}"
R2_PREFIX="${R2_PREFIX:-legs}"
TOL_GROUND="${TOL_GROUND:-2}"
TOL_CRUISE="${TOL_CRUISE:-150}"
CORNER="${CORNER:-35}"
HEX_MAX_RES="${HEX_MAX_RES:-6}"        # 6 = 3.2 km edge, drawn at z8
HEX_MIN_RES="${HEX_MIN_RES:-0}"
HEX_STEP_KM="${HEX_STEP_KM:-1.5}"      # keep <= half the finest hex edge
HEX_BUCKETS="${HEX_BUCKETS:-16}"
HEX_MEMORY="${HEX_MEMORY:-}"           # e.g. 10GB; empty = DuckDB default
TAGFILE="$WORK/TAG"

resolve() {
    mkdir -p "$WORK"
    # Releases are newest-first, so the first page holds the latest of every
    # variant -- do NOT --paginate (that applies --jq per page -> one tag per page).
    local tag
    tag="$(gh api "repos/$REPO/releases?per_page=100" \
            --jq "[.[] | select(.tag_name | endswith(\"-planes-readsb-$VARIANT\"))][0].tag_name")"
    [ -n "$tag" ] && [ "$(printf '%s' "$tag" | wc -l)" -eq 0 ] \
        || { echo "bad/empty $VARIANT tag: '$tag'"; exit 1; }
    echo "$tag" >"$TAGFILE"
    echo "resolved $VARIANT release: $tag"
}

fetch() {
    local tag; tag="$(cat "$TAGFILE")"
    rm -rf "$WORK/traces"
    # browser_download_url is public, so curl concatenates the .tar.aa/.ab/.ac
    # parts (sorted) to stdout in one pass -> tar extracts, nothing staged on disk.
    mapfile -t urls < <(gh api "repos/$REPO/releases/tags/$tag" \
            --jq '.assets[].browser_download_url' | sort)
    echo "streaming ${#urls[@]} asset parts into tar..."
    curl -fsSL "${urls[@]}" | tar -x -C "$WORK"
    echo "extracted $(find "$WORK/traces" -name 'trace_full_*.json' | wc -l) traces"
}

fit() {
    python3 "$SCRIPT_DIR/fit_spline.py" "$WORK/traces" --ground-elevation \
        --airports "$SCRIPT_DIR/airports.csv" --parquet "$WORK/nodes" \
        --tol-ground "$TOL_GROUND" --tol-cruise "$TOL_CRUISE" --corner "$CORNER"
}

legs() {
    mkdir -p "$OUT"; rm -rf "$OUT/legs"
    python3 "$SCRIPT_DIR/build_legs.py" \
        --traces "$WORK/nodes/nodes.parquet" \
        --meta "$WORK/nodes/aircraft.parquet" --out-dir "$OUT/legs"
}

# Writes traffic.pmtiles INTO $OUT/legs so the existing upload picks it up with
# the rest of the day's prefix. Must run after legs() (which rm -rf's that dir).
hexes() {
    python3 "$SCRIPT_DIR/build_hexes.py" \
        --points "$OUT/legs/points_legs.parquet" \
        --out "$OUT/legs/traffic.pmtiles" \
        --tmp "$WORK/hex" \
        --max-res "$HEX_MAX_RES" --min-res "$HEX_MIN_RES" \
        --step-km "$HEX_STEP_KM" --buckets "$HEX_BUCKETS" \
        ${HEX_MEMORY:+--memory-limit "$HEX_MEMORY"}
}

upload() {
    : "${R2_BUCKET:?set R2_BUCKET (Cloudflare R2 bucket name)}"
    local keep="${RETENTION_DAYS:-30}"
    local base="r2:$R2_BUCKET/$R2_PREFIX"
    local tag; tag="$(cat "$TAGFILE" 2>/dev/null || echo '?')"
    # data date lives in the release tag: v2026.07.17-...-prod-0 -> 2026-07-17
    local date; date="$(printf '%s' "$tag" | sed -nE 's/^v([0-9]{4})\.([0-9]{2})\.([0-9]{2}).*/\1-\2-\3/p')"
    [ -n "$date" ] || { echo "could not parse date from tag: $tag"; exit 1; }

    # each day is its own self-contained prefix (airports/, points_legs, flights);
    # sync only touches THIS date, so other days are never deleted.
    rclone sync "$OUT/legs" "$base/date=$date" \
        --checksum --transfers 16 --fast-list --stats-one-line

    # prune to the newest $keep date partitions
    mapfile -t dates < <(rclone lsf --dirs-only "$base/" | sed 's#/$##' | grep '^date=' | sort)
    local total=${#dates[@]}
    if (( total > keep )); then
        for d in "${dates[@]:0:total-keep}"; do
            echo "pruning $d"; rclone purge "$base/$d"
        done
    fi

    # rebuild the date manifest (a browser can't list a bucket over HTTP)
    mapfile -t kept < <(rclone lsf --dirs-only "$base/" | sed 's#/$##' \
        | grep '^date=' | sed 's/^date=//' | sort)
    python3 -c "import json,sys; d=sys.argv[1:]; print(json.dumps({'dates':d,'latest':d[-1] if d else None}))" \
        "${kept[@]}" > "$WORK/dates.json"
    rclone copyto "$WORK/dates.json" "$base/dates.json"
    echo "DONE: $tag -> $base/date=$date  (kept ${#kept[@]} day(s), retention $keep)"
}

case "${1:-all}" in
    resolve) resolve ;;
    fetch)   fetch ;;
    fit)     fit ;;
    legs)    legs ;;
    hexes)   hexes ;;
    upload)  upload ;;
    all)     resolve; fetch; fit; legs; hexes; upload ;;
    *) echo "usage: $0 [resolve|fetch|fit|legs|hexes|upload|all]" >&2; exit 2 ;;
esac
