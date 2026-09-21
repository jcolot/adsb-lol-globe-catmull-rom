#!/usr/bin/env bash
# Daily ADS-B trace pipeline, split into phases so each can run as its own CI step:
#   resolve  -> find the adsblol release tag for the variant (SRC_DATE, else latest)
#   fetch    -> stream the split-tar assets straight into tar (no 4-6 GB staged)
#   fit      -> fit sparse Catmull-Rom spline nodes (fit_spline.py)
#   legs     -> split into per-airport leg partitions (build_legs.py)
#   hexes    -> H3 traffic-density tiles: a raster overview + vector hexbins
#   bundle   -> legs.parquet + cells.bin + tracks.bin, then verify (build_bundle.py)
#   overnight-> static overnight-classification artifacts (overnight.py)
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
IDX_BUCKET_MIN="${IDX_BUCKET_MIN:-60}" # cells.bin time bucket WIDTH in minutes
                                       # (60 = hourly; 180 trades precision for
                                       # ~3 MB less). Count follows the span.
BUNDLE_BUCKETS="${BUNDLE_BUCKETS:-8}"
BUNDLE_MEMORY="${BUNDLE_MEMORY:-}"
VERIFY_LEGS="${VERIFY_LEGS:-2000}"     # legs round-trip decoded by the gate
VERIFY_BOXES="${VERIFY_BOXES:-25}"
OVN_MAX_GAP_H="${OVN_MAX_GAP_H:-3}"    # splice_legs.py boundary gap tolerance
TAGFILE="$WORK/TAG"

# The data date lives in the release tag: v2026.07.17-...-prod-0 -> 2026-07-17.
# Three phases need it (leg_id carries it, the overnight artifacts are keyed on
# it, upload picks the partition with it), so it is parsed in one place.
data_date() {
    local tag; tag="$(cat "$TAGFILE" 2>/dev/null || echo '?')"
    local d; d="$(printf '%s' "$tag" \
        | sed -nE 's/^v([0-9]{4})\.([0-9]{2})\.([0-9]{2}).*/\1-\2-\3/p')"
    [ -n "$d" ] || { echo "could not parse date from tag: $tag" >&2; return 1; }
    printf '%s' "$d"
}

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

# --date is what every leg_id is prefixed with, so it is passed explicitly from
# the release tag rather than inferred: build_legs can derive it from base_ts,
# but the tag is the authority on which archive this is, and an id built from a
# guess would collide with a real day's.
legs() {
    local date; date="$(data_date)"
    mkdir -p "$OUT"; rm -rf "$OUT/legs"
    python3 "$SCRIPT_DIR/build_legs.py" \
        --traces "$WORK/nodes/nodes.parquet" --date "$date" \
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
        --bucket-minutes "$IDX_BUCKET_MIN" \
        ${BUNDLE_MEMORY:+--memory-limit "$BUNDLE_MEMORY"}
    python3 "$SCRIPT_DIR/verify_bundle.py" \
        --bundle "$OUT/legs" \
        --points "$OUT/legs/points_legs.parquet" \
        --meta "$WORK/nodes/aircraft.parquet" \
        --sample-legs "$VERIFY_LEGS" --boxes "$VERIFY_BOXES"
}

# Static artifacts for the overnight classifier. Deliberately AFTER bundle and
# before upload, so the date=$date files ride the normal sync.
#
# This phase is the one place the pipeline reaches BACKWARDS. A flight airborne
# at 00:00Z is split across two daily archives, and day D-1's half cannot be
# completed until day D exists -- so the settled per-day table published here is
# for D-1, not D, and it is copied into the PREVIOUS partition. Everything else
# (the UTC-offset table, the meta) is for D and needs no history.
overnight() {
    local date; date="$(data_date)"
    local prev; prev="$(date -u -d "$date -1 day" +%F 2>/dev/null \
                        || date -u -j -v-1d -f %F "$date" +%F)"

    # the UTC-offset table and the model coefficients: today's partition, no
    # history needed
    local table_args=()
    local prev_legs="$WORK/overnight/prev-flights.parquet"
    mkdir -p "$WORK/overnight"

    # D-1's leg index lives in R2, not locally -- fetch it to splice the
    # boundary. Absent on the first run and after a retention prune, in which
    # case D-1 simply gets no settled table.
    if [ -n "${R2_BUCKET:-}" ] && rclone copyto \
            "r2:$R2_BUCKET/$R2_PREFIX/date=$prev/flights.parquet" "$prev_legs" \
            --ignore-errors 2>/dev/null && [ -s "$prev_legs" ]; then
        python3 "$SCRIPT_DIR/splice_legs.py" \
            "$prev=$prev_legs" "$date=$OUT/legs/flights.parquet" \
            --airports-tz "$SCRIPT_DIR/airport_tz.csv" \
            --max-gap-h "$OVN_MAX_GAP_H" --dep-date "$prev" \
            --out "$WORK/overnight/spliced.parquet"
        python3 "$SCRIPT_DIR/overnight.py" \
            --airports-tz "$SCRIPT_DIR/airport_tz.csv" \
            --legs "$WORK/overnight/spliced.parquet" \
            --build-table "$WORK/overnight/overnight.parquet"
        table_args=(--legs "$WORK/overnight/spliced.parquet" --table-date "$prev")
    else
        echo "no leg index for $prev in R2 -- skipping the settled table for it"
    fi

    # --tz-back the whole retention window: the per-date copy of this table
    # only ever resolved D, D-1, D-2, so a client asking about a local date
    # further back had no way to turn its midnight into UTC. Widening it costs
    # ~2.5 KB a day (measured: 89 KB for 4 dates, 160 KB for 32) and upload()
    # copies the newest one to the bucket ROOT, where it is not pruned and one
    # fetch answers every date the archive still holds.
    python3 "$SCRIPT_DIR/overnight.py" \
        --airports-tz "$SCRIPT_DIR/airport_tz.csv" \
        --emit-client "$OUT/legs" --date "$date" \
        --tz-back "${RETENTION_DAYS:-30}" "${table_args[@]}"

    # the settled table belongs to the PREVIOUS partition, which this run's sync
    # does not touch -- copy, never sync, or the prior day's files are deleted.
    #
    # NON-FATAL, for the same reason the grid write in upload() is: this copyto
    # has never once succeeded. Every run that reached it got AccessDenied (403)
    # from R2 three times and died under `set -e`, taking the whole upload phase
    # with it -- so on those days the day partition itself never shipped. The
    # runs that went green only did so because D-1 had no leg index in R2 and
    # the splice was skipped entirely. A settled table we cannot write costs
    # D-1 its overnight column; aborting here costs D everything. The 403 is a
    # separate, open problem -- see the grid-write probe in upload().
    if [ -s "$WORK/overnight/overnight.parquet" ] && [ -n "${R2_BUCKET:-}" ]; then
        if rclone copyto "$WORK/overnight/overnight.parquet" \
               "r2:$R2_BUCKET/$R2_PREFIX/date=$prev/overnight.parquet" --checksum; then
            echo "settled overnight table -> date=$prev/overnight.parquet"
        else
            echo "WARNING: could not write date=$prev/overnight.parquet" >&2
            echo "  date=$prev keeps the leg index it already has; only its" >&2
            echo "  settled overnight column is missing." >&2
            # a plain `[ ... ] && echo` here would return 1 when not on CI,
            # and this is the last command in the function -- `set -e` would
            # kill the run over exactly the failure we just swallowed
            if [ -n "${GITHUB_ACTIONS:-}" ]; then
                echo "::warning title=Overnight table upload failed::overnight.parquet was not written to date=$prev - that day's settled overnight column is missing, but the pipeline is otherwise complete"
            fi
        fi
    fi
    # The spliced leg table itself, not just the offsets derived from it. A
    # flight airborne at 00:00Z reaches a client as two half-legs with a NULL
    # endpoint each, and nothing it can fetch says they are one flight -- the
    # repair was computed here and then thrown away. leg_id is "<tail>+<head>",
    # so a client can map a half back to the whole. Same partition and same
    # copyto-not-sync rule as the settled table: it is $prev's file -- and
    # NON-FATAL for the same reason, since it is the same write to the same
    # prefix that has been returning 403.
    if [ -s "$WORK/overnight/spliced.parquet" ] && [ -n "${R2_BUCKET:-}" ]; then
        if rclone copyto "$WORK/overnight/spliced.parquet" \
               "r2:$R2_BUCKET/$R2_PREFIX/date=$prev/spliced.parquet" --checksum; then
            echo "spliced leg table -> date=$prev/spliced.parquet"
        else
            echo "WARNING: could not write date=$prev/spliced.parquet" >&2
            echo "  date=$prev keeps its leg index; clients fall back to the" >&2
            echo "  unrepaired half-legs across that midnight." >&2
            if [ -n "${GITHUB_ACTIONS:-}" ]; then
                echo "::warning title=Spliced legs upload failed::spliced.parquet was not written to date=$prev - the midnight-cut legs stay unrepaired for that day, but the pipeline is otherwise complete"
            fi
        fi
    fi
}

upload() {
    : "${R2_BUCKET:?set R2_BUCKET (Cloudflare R2 bucket name)}"
    local keep="${RETENTION_DAYS:-30}"
    local base="r2:$R2_BUCKET/$R2_PREFIX"
    local date; date="$(data_date)"

    # each day is its own self-contained prefix; sync only touches THIS date, so
    # other days are never deleted. points_legs.parquet is a build intermediate
    # that tracks.bin now supersedes -- keeping both roughly doubles the per-day
    # storage, so it stays local.
    #
    # overnight.parquet and spliced.parquet are excluded because they are never
    # in $OUT/legs: they are D-1's files, written into this partition by the
    # FOLLOWING day's run. Without the exclude, re-running or backfilling a day
    # would sync them away as extras and they would not come back until that
    # day's successor ran again.
    rclone sync "$OUT/legs" "$base/date=$date" \
        --exclude 'points_legs.parquet' --exclude 'traffic-grid.npz' \
        --exclude 'overnight.parquet' --exclude 'spliced.parquet' \
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

    # `days` carries each partition's flights_schema, because the retained
    # window is NOT schema-homogeneous: days built before t_off/t_on/base_ts
    # existed are still in it and cannot be put on an absolute clock at all, so
    # a client that assumes otherwise silently drops them or dates them wrong.
    # This run knows its OWN schema from legs_meta.json; every other date is
    # carried forward from the manifest already in the bucket, so the map fills
    # in as days roll over and a missing entry honestly means "built before
    # this, feature-detect the columns".
    rclone cat "$base/dates.json" > "$WORK/dates_prev.json" 2>/dev/null || true
    python3 - "$date" "$OUT/legs/legs_meta.json" "$WORK/dates_prev.json" \
        "${kept[@]}" > "$WORK/dates.json" <<'PY'
import json, sys
date, meta_path, prev_path, *kept = sys.argv[1:]
days = {}
try:
    days = json.load(open(prev_path)).get("days") or {}
except Exception:
    pass
try:
    m = json.load(open(meta_path))
    days[date] = {k: m[k] for k in
                  ("flights_schema", "base_ts_uniform", "t_epoch", "n_legs")
                  if k in m}
except Exception:
    print(f"no legs_meta.json for {date}", file=sys.stderr)
days = {d: days[d] for d in kept if d in days}          # drop pruned dates
print(json.dumps({"dates": kept, "latest": kept[-1] if kept else None,
                  "days": days}))
PY
    rclone copyto "$WORK/dates.json" "$base/dates.json"

    # The UTC-offset table, at the ROOT rather than inside a partition. The
    # per-date copies resolve only their own D, D-1, D-2, so no single one of
    # them can turn "local midnight on any retained date" into UTC -- and the
    # oldest partitions predate the artifact entirely. This copy spans the whole
    # retention window (see --tz-back) and lives outside date=, where the prune
    # never reaches: one stable URL, one fetch, every date.
    #
    # Non-fatal, like every other write outside date=$date: these two land at
    # the root, the day partition is already up, and losing the root copy costs
    # local-time queries on older dates -- not this day's data. The per-date
    # copy of the table shipped with the sync either way.
    if [ -s "$OUT/legs/airports_utc.parquet" ]; then
        if rclone copyto "$OUT/legs/airports_utc.parquet" \
               "$base/airports_utc.parquet" --checksum \
           && rclone copyto "$OUT/legs/overnight_meta.json" \
               "$base/overnight_meta.json" --checksum; then
            echo "root UTC-offset table -> $base/airports_utc.parquet"
        else
            echo "WARNING: could not write the root airports_utc.parquet" >&2
            echo "  clients fall back to the per-date copies, which resolve" >&2
            echo "  only their own D+1..D-2." >&2
            if [ -n "${GITHUB_ACTIONS:-}" ]; then
                echo "::warning title=Root offset table upload failed::airports_utc.parquet was not written to the bucket root - local-time queries on older dates will not resolve, but the day partition is complete"
            fi
        fi
    fi
    # The raw density grid goes to its own prefix, NOT into date=$date, because
    # the prune only ever walks date= partitions -- so the grids survive
    # retention. That is the point of them: they are the only per-day artifact
    # comparable ACROSS days (the raster's alpha is normalised per day), so a
    # multi-year animation has to reach back past retention. ~2 MB/day.
    #
    # DELIBERATELY LAST, AND NON-FATAL. When this ran before the prune and the
    # manifest, an AccessDenied on this one 2 MB object aborted the step under
    # `set -e` -- so dates.json was never rebuilt and retention never applied,
    # and the frontend sat two days stale while the partitions it wanted were
    # already in the bucket, uploaded and unreachable. A grid we cannot write is
    # a missing frame in a future animation; a manifest we cannot write is a
    # broken site today. Never let the first break the second again.
    if [ -f "$OUT/legs/traffic-grid.npz" ]; then
        if rclone copyto "$OUT/legs/traffic-grid.npz" "$base/grids/$date.npz" \
               --stats-one-line; then
            echo "grid kept beyond retention: $base/grids/$date.npz"
        else
            echo "WARNING: could not upload $base/grids/$date.npz" >&2
            echo "  date=$date is complete; only the cross-day grid is missing." >&2
            # Distinguish a credential/scope problem from anything else without
            # dumping headers (these logs are public). If the prefix will not
            # even list, the R2 token does not reach outside date=*.
            if rclone lsf "$base/grids/" >/dev/null 2>&1; then
                echo "  the grids/ prefix lists fine, so this is not token scope" >&2
            else
                echo "  the grids/ prefix does not list either -- the R2 token" >&2
                echo "  probably has no access outside the date= partitions" >&2
            fi
            [ -n "${GITHUB_ACTIONS:-}" ] && \
                echo "::warning title=Grid upload failed::$date.npz was not written to $base/grids/ - the day partition is fine, but render_video will have a hole here"
        fi
    fi

    echo "DONE: $tag -> $base/date=$date  (kept ${#kept[@]} day(s), retention $keep)"
}

case "${1:-all}" in
    resolve) resolve ;;
    fetch)   fetch ;;
    fit)     fit ;;
    legs)    legs ;;
    hexes)   hexes ;;
    bundle)  bundle ;;
    overnight) overnight ;;
    upload)  upload ;;
    all)     resolve; fetch; fit; legs; hexes; bundle; overnight; upload ;;
    *) echo "usage: $0 [resolve|fetch|fit|legs|hexes|bundle|upload|all]" >&2; exit 2 ;;
esac
