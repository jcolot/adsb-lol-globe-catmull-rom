#!/usr/bin/env bash
# Daily ADS-B trace pipeline, split into phases so each can run as its own CI step:
#   resolve  -> find the latest adsblol release tag for the variant
#   fetch    -> stream the split-tar assets straight into tar (no 4-6 GB staged)
#   fit      -> fit sparse Catmull-Rom spline nodes (fit_spline.py)
#   legs     -> split into per-airport leg partitions (build_legs.py)
#   hexes    -> H3 traffic-density tiles: a raster overview + vector hexbins
#   bundle   -> legs.parquet + cells.bin + tracks.bin, then verify (build_bundle.py)
#   upload   -> rclone sync the legs to Cloudflare R2
# Run a single phase (`run_pipeline.sh fit`) or the whole thing (`run_pipeline.sh`
# / `run_pipeline.sh all`). Phases share state through $WORK (the resolved tag is
# written to $WORK/TAG), so the CI steps hand off via the persisted workspace.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO="${SRC_REPO:-adsblol/globe_history_2026}"
VARIANT="${VARIANT:-prod-0}"            # prod-0 (ADS-B) | mlatonly-0 | staging-0
SRC_DATE="${SRC_DATE:-}"                # YYYY-MM-DD to backfill; empty = latest
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
HEX_RASTER_MAX_Z="${HEX_RASTER_MAX_Z:-4}"   # 512px tiles -> 4.9 km/px, ~= res 6
HEX_RASTER_TILE="${HEX_RASTER_TILE:-512}"
HEX_RASTER_GAMMA="${HEX_RASTER_GAMMA:-2}"   # 1 = linear ramp; 2 = sqrt, see hex_raster.py
HEX_GRID_ZOOM="${HEX_GRID_ZOOM:-2}"    # raw density grid for render_video.py; 2 = 2048px
HEX_VECTOR="${HEX_VECTOR:-}"           # non-empty also builds traffic.pmtiles
IDX_RES="${IDX_RES:-4}"                # H3 resolution of cells.bin (~45 km cells)
BUNDLE_BUCKETS="${BUNDLE_BUCKETS:-8}"
BUNDLE_MEMORY="${BUNDLE_MEMORY:-}"
VERIFY_LEGS="${VERIFY_LEGS:-2000}"     # legs round-trip decoded by the gate
VERIFY_BOXES="${VERIFY_BOXES:-25}"
TAGFILE="$WORK/TAG"

resolve() {
    mkdir -p "$WORK"
    local tag
    if [ -n "$SRC_DATE" ]; then
        # tags carry the data date: v2026.07.23-....-planes-readsb-prod-0
        local pfx="v${SRC_DATE//-/.}"
        # --paginate IS safe here: the filter streams every match rather than
        # indexing into a per-page array. Take the first line by expansion, NOT
        # `| head -1` -- head would close the pipe mid-pagination and SIGPIPE gh
        # into a pipefail exit.
        tag="$(gh api "repos/$REPO/releases?per_page=100" --paginate \
                --jq ".[] | select(.tag_name | startswith(\"$pfx\"))
                          | select(.tag_name | endswith(\"-planes-readsb-$VARIANT\"))
                          | .tag_name")"
        tag="${tag%%$'\n'*}"
        [ -n "$tag" ] || { echo "no $VARIANT release for $SRC_DATE"; exit 1; }
    else
        # Releases are newest-first, so the first page holds the latest of every
        # variant -- do NOT --paginate (that applies --jq per page -> one tag per page).
        tag="$(gh api "repos/$REPO/releases?per_page=100" \
                --jq "[.[] | select(.tag_name | endswith(\"-planes-readsb-$VARIANT\"))][0].tag_name")"
    fi
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

# Writes both archives INTO $OUT/legs so the existing upload picks them up with
# the rest of the day's prefix. Must run after legs() (which rm -rf's that dir).
#
# Only the raster is built by default. The vector hexbin archive is off because
# nothing draws it -- the raster replaced it, and at --max-res 6 it was 115 MB a
# day (85 of that in res 6 alone) against the raster's 8 MB. Set HEX_VECTOR=1 to
# get it back for per-cell tooltips or queries; that also needs tippecanoe.
hexes() {
    python3 "$SCRIPT_DIR/build_hexes.py" \
        --points "$OUT/legs/points_legs.parquet" \
        --raster-out "$OUT/legs/traffic-raster.pmtiles" \
        ${HEX_VECTOR:+--out "$OUT/legs/traffic.pmtiles"} \
        --raster-max-zoom "$HEX_RASTER_MAX_Z" \
        --raster-tile-size "$HEX_RASTER_TILE" \
        --raster-gamma "$HEX_RASTER_GAMMA" \
        --grid-out "$OUT/legs/traffic-grid.npz" --grid-zoom "$HEX_GRID_ZOOM" \
        --tmp "$WORK/hex" \
        --max-res "$HEX_MAX_RES" --min-res "$HEX_MIN_RES" \
        --step-km "$HEX_STEP_KM" --buckets "$HEX_BUCKETS" \
        ${HEX_MEMORY:+--memory-limit "$HEX_MEMORY"}
}

# The queryable day bundle. Verification is part of this phase, not a separate
# one: a bundle whose byte offsets are wrong is worse than no bundle at all, so
# it must not be possible to upload one that hasn't been checked.
bundle() {
    python3 "$SCRIPT_DIR/build_bundle.py" \
        --points "$OUT/legs/points_legs.parquet" \
        --meta "$WORK/nodes/aircraft.parquet" \
        --out-dir "$OUT/legs" \
        --index-res "$IDX_RES" --buckets "$BUNDLE_BUCKETS" \
        ${BUNDLE_MEMORY:+--memory-limit "$BUNDLE_MEMORY"}
    python3 "$SCRIPT_DIR/verify_bundle.py" \
        --bundle "$OUT/legs" \
        --points "$OUT/legs/points_legs.parquet" \
        --meta "$WORK/nodes/aircraft.parquet" \
        --sample-legs "$VERIFY_LEGS" --boxes "$VERIFY_BOXES"
}

upload() {
    : "${R2_BUCKET:?set R2_BUCKET (Cloudflare R2 bucket name)}"
    local keep="${RETENTION_DAYS:-30}"
    local base="r2:$R2_BUCKET/$R2_PREFIX"
    local tag; tag="$(cat "$TAGFILE" 2>/dev/null || echo '?')"
    # data date lives in the release tag: v2026.07.17-...-prod-0 -> 2026-07-17
    local date; date="$(printf '%s' "$tag" | sed -nE 's/^v([0-9]{4})\.([0-9]{2})\.([0-9]{2}).*/\1-\2-\3/p')"
    [ -n "$date" ] || { echo "could not parse date from tag: $tag"; exit 1; }

    # each day is its own self-contained prefix; sync only touches THIS date, so
    # other days are never deleted. points_legs.parquet is a build intermediate
    # that tracks.bin now supersedes -- keeping both roughly doubles the per-day
    # storage, so it stays local.
    rclone sync "$OUT/legs" "$base/date=$date" \
        --exclude 'points_legs.parquet' --exclude 'traffic-grid.npz' \
        --checksum --transfers 16 --fast-list --stats-one-line

    # The raw density grid goes to its own prefix, NOT into date=$date, because
    # the prune below only ever walks date= partitions -- so the grids survive
    # retention. That is the point of them: they are the only per-day artifact
    # that is comparable ACROSS days (the raster's alpha is normalised per day),
    # so a multi-year animation has to be able to reach back past retention.
    # At ~2 MB/day this is 0.7 GB/year, which is noise against the 30-day
    # working set. See render_video.py.
    if [ -f "$OUT/legs/traffic-grid.npz" ]; then
        rclone copyto "$OUT/legs/traffic-grid.npz" "$base/grids/$date.npz" \
            --checksum --stats-one-line
        echo "grid kept beyond retention: $base/grids/$date.npz"
    fi

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
    bundle)  bundle ;;
    upload)  upload ;;
    all)     resolve; fetch; fit; legs; hexes; bundle; upload ;;
    *) echo "usage: $0 [resolve|fetch|fit|legs|hexes|bundle|upload|all]" >&2; exit 2 ;;
esac
