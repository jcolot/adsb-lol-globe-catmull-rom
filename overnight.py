#!/usr/bin/env python3
"""
overnight.py - given a flight ARRIVING at a known scheduled local time, decide
whether it departed its origin on the same local date or the day before.

The question is a DATE OFFSET, `arr_local_date - dep_local_date`, which is what
schedule data calls the arrival day indicator. It is not binary: crossing the
date line westbound makes it +2 (LAX->SYD) and eastbound makes it 0 even on a
14-hour flight (SYD->LAX), so this reports the signed integer and lets the
caller collapse it. "Overnight" is `offset >= 1`.

  arr_utc   = arr_local  @ tz(arr)          -> UTC
  dep_utc   = arr_utc - block_time
  dep_local = dep_utc                       -> tz(dep)
  offset    = arr_local.date() - dep_local.date()

Two things follow from that, and they are the whole design:

  * The tz conversion carries the classification, not the block time. The block
    time only has to be accurate enough not to walk the computed departure
    across local midnight -- so what matters is the MARGIN, the distance from
    the computed departure to the nearest midnight. Report it on every answer:
    a 4-hour margin is immune to any plausible block-time error, and a 20-minute
    margin is a coin toss no matter how good the estimate is. Precision in the
    block time is worth buying only for the marginal cases.

  * Block time is gate-to-gate, but ADS-B measures wheels-off to wheels-on.
    `t_on - t_off` from flights.parquet is AIRBORNE time; taxi is added on top.
    It is deliberately a constant (TAXI_MIN) rather than a measurement: this
    segmentation cuts legs at turnaround midpoints, so a leg's first point is
    ramp time, not pushback, and a "measured" taxi from it would be fiction.
    See the margin argument above for why a constant is good enough.

Sources of block time, best first:
  1. observed median for that exact directed pair (--legs), which carries the
     wind asymmetry -- eastbound transatlantic is ~an hour quicker than west,
     and a symmetric model gets one of the two directions wrong;
  2. the distance model, for pairs never observed.

Usage:
  ./overnight.py --dep KJFK --arr EGLL --arr-local '2026-09-08 10:15'
  ./overnight.py --legs airport_ds/flights.parquet --fit
  ./overnight.py --legs airport_ds/flights.parquet --build-table overnight.parquet
"""
import argparse, csv, datetime as dt, math, os, sys
from zoneinfo import ZoneInfo

# Rules of thumb, NOT fitted -- replace with --fit against real legs.
# 45 min fixed + 800 km/h reproduces published block times within ~15 min from
# CDG-LHR (348 km) to SIN-LHR (10,850 km), which is well inside a typical margin.
BLOCK_FIXED_MIN = 45.0
BLOCK_KMH = 800.0
TAXI_MIN = 25.0          # taxi-out + taxi-in, added to a measured AIRBORNE time
MIN_SAMPLES = 3          # below this, an observed median is noise
R_KM = 6371.0088


def load_airports(path):
    out = {}
    for r in csv.DictReader(open(path, newline="")):
        try:
            out[r["ident"]] = (float(r["lat"]), float(r["lon"]), ZoneInfo(r["tz"]))
        except (KeyError, ValueError):
            continue
    return out


def gc_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_KM * math.asin(math.sqrt(h))


def model_block_min(dist_km, fixed=BLOCK_FIXED_MIN, kmh=BLOCK_KMH):
    return fixed + 60.0 * dist_km / kmh


def classify(dep, arr, arr_local, airports, blocks=None, coef=None):
    """-> dict(offset, overnight, dep_local, block_min, margin_min, source).

    `arr_local` is a NAIVE datetime read as local time at `arr`.
    """
    if dep not in airports:
        raise KeyError(f"no timezone for departure airport {dep!r}")
    if arr not in airports:
        raise KeyError(f"no timezone for arrival airport {arr!r}")
    dlat, dlon, tz_dep = airports[dep]
    alat, alon, tz_arr = airports[arr]

    dist = gc_km(dlat, dlon, alat, alon)
    block, source = None, None
    if blocks:
        hit = blocks.get((dep, arr))
        if hit and hit[1] >= MIN_SAMPLES:
            block, source = hit[0], f"observed median of {hit[1]} legs"
    if block is None:
        f, k = coef or (BLOCK_FIXED_MIN, BLOCK_KMH)
        block = model_block_min(dist, f, k)
        source = f"distance model ({dist:.0f} km)"

    # Go through UTC. Subtracting a timedelta from a zoneinfo-aware datetime
    # does WALL-CLOCK arithmetic and silently gains or loses an hour across a
    # DST boundary -- which is precisely the error this tool exists to avoid.
    arr_utc = arr_local.replace(tzinfo=tz_arr).astimezone(dt.timezone.utc)
    dep_local = (arr_utc - dt.timedelta(minutes=block)).astimezone(tz_dep)

    offset = (arr_local.date() - dep_local.date()).days
    into_day = dep_local.hour * 60 + dep_local.minute
    return dict(offset=offset, overnight=offset >= 1, dep_local=dep_local,
                block_min=block, dist_km=dist, source=source,
                margin_min=min(into_day, 1440 - into_day))


# ---------------------------------------------------------------------------
# observed legs
def load_legs(path, require_ground=True):
    """Legs as (dep, arr, dep_utc, arr_utc, airborne_min), UTC-rebased.

    `require_ground` keeps only legs whose BOTH ends produced a surface message.
    That filter is not optional for timing: build_legs splits the day on the
    on_ground bit alone, so an aircraft that never emits one has its whole day
    collapsed into a single leg whose t_off/t_on span every flight it made.
    """
    import duckdb
    con = duckdb.connect()
    cols = {c[0] for c in con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()}
    need = {"t_off", "t_on", "base_ts", "dep_gnd", "arr_gnd"}
    if not need <= cols:
        sys.exit(f"{path} lacks {sorted(need - cols)} -- re-run build_legs.py "
                 f"(t_end - t_start is a leg envelope, not a flight time, so "
                 f"there is no usable fallback here)")
    where = ("WHERE dep IS NOT NULL AND arr IS NOT NULL AND dep <> arr "
             "AND base_ts IS NOT NULL AND t_on > t_off")
    if require_ground:
        where += " AND dep_gnd AND arr_gnd"
    return con.execute(f"""
        SELECT dep, arr,
               base_ts + t_off / 10.0        AS dep_utc,
               base_ts + t_on  / 10.0        AS arr_utc,
               (t_on - t_off) / 600.0        AS airborne_min
        FROM '{path}' {where}
    """).fetchall()


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def pair_blocks(legs):
    """(dep,arr) -> (median block minutes, n). Airborne + TAXI_MIN."""
    acc = {}
    for dep, arr, _, _, air in legs:
        acc.setdefault((dep, arr), []).append(air)
    return {k: (median(v) + TAXI_MIN, len(v)) for k, v in acc.items()}


def fit(legs, tz_path="airport_tz.csv"):
    """Least squares airborne_min ~ fixed + 60*d/kmh, on per-pair medians.

    Fitted on medians rather than raw legs so that one busy shuttle route cannot
    outvote the whole long-haul network, and so a mis-segmented outlier leg is
    already suppressed before it reaches the fit.
    """
    import statistics
    ap = load_airports(tz_path)
    xs, ys = [], []
    for (dep, arr), (blk, n) in pair_blocks(legs).items():
        if n < MIN_SAMPLES or dep not in ap or arr not in ap:
            continue
        d = gc_km(ap[dep][0], ap[dep][1], ap[arr][0], ap[arr][1])
        if d > 50:
            xs.append(d); ys.append(blk)
    if len(xs) < 10:
        sys.exit(f"only {len(xs)} usable pairs -- not enough to fit")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    res = [y - (a + b * x) for x, y in zip(xs, ys)]
    print(f"fitted on {len(xs)} pairs:  block_min = {a:.1f} + 60*d/{60/b:.0f}")
    print(f"  BLOCK_FIXED_MIN = {a:.1f}   BLOCK_KMH = {60/b:.0f}")
    print(f"  residual: median |e| {median([abs(r) for r in res]):.1f} min, "
          f"p90 {sorted(abs(r) for r in res)[int(.9*len(res))]:.1f} min")
    return a, 60 / b


def build_table(legs, airports, out):
    """Empirical offsets per (dep, arr, arrival local hour).

    Keyed on arrival hour because a route can run both a daytime and a red-eye
    service with different offsets, and the caller knows the scheduled arrival --
    so it is a free conditioning variable. This is the "large sparse matrix",
    and keying it this way keeps it an edge list of a few 10k rows rather than
    anything needing a matrix layout.
    """
    import pyarrow as pa, pyarrow.parquet as pq
    acc = {}
    for dep, arr, dutc, autc, _ in legs:
        if dep not in airports or arr not in airports:
            continue
        dl = dt.datetime.fromtimestamp(dutc, airports[dep][2])
        al = dt.datetime.fromtimestamp(autc, airports[arr][2])
        key = (dep, arr, al.hour)
        acc.setdefault(key, []).append((al.date() - dl.date()).days)
    # Actual arrivals scatter across adjacent hours (delay, wind), so an hour
    # bucket next to a busy one can hold a single leg. Emit a route-level
    # rollup at hour = -1 as the fallback for a thin or missing bucket: look up
    # (dep, arr, hour) first, then (dep, arr, -1).
    route = {}
    for (dep, arr, _), offs in acc.items():
        route.setdefault((dep, arr), []).extend(offs)
    rows = []
    for (dep, arr), offs in route.items():
        mode = max(set(offs), key=offs.count)
        rows.append((dep, arr, -1, mode, offs.count(mode) / len(offs), len(offs)))
    for (dep, arr, hh), offs in acc.items():
        mode = max(set(offs), key=offs.count)
        rows.append((dep, arr, hh, mode, offs.count(mode) / len(offs), len(offs)))
    rows.sort()
    t = pa.table({
        "dep": [r[0] for r in rows], "arr": [r[1] for r in rows],
        "arr_local_hour": pa.array([r[2] for r in rows], pa.int8()),
        # NOT "offset": that is a reserved word in DuckDB/Postgres SQL, so a
        # bare SELECT of it is a parser error for every downstream caller.
        "day_offset": pa.array([r[3] for r in rows], pa.int8()),
        "agreement": [r[4] for r in rows],
        "n": pa.array([r[5] for r in rows], pa.int32()),
    })
    pq.write_table(t, out, compression="zstd", use_dictionary=["dep", "arr"])
    import os
    print(f"{len(rows)} (dep,arr,hour) rows -> {out} "
          f"({os.path.getsize(out)/1e6:.2f} MB)")
    unan = sum(1 for r in rows if r[4] < 1.0)
    print(f"  {unan} rows ({100*unan/max(1,len(rows)):.1f}%) disagree across days")
    print(f"  {sum(1 for r in rows if r[2] == -1)} route-level fallback rows "
          f"(arr_local_hour = -1)")


def emit_client(airports, date, out_dir, coef, table_date=None, n_legs=0):
    """Write the static artifacts a browser needs, into out_dir.

    airports_utc.parquet -- one row per airport: ident, lat/lon (degrees * 1e5,
    the repo's Q_POS convention), the IANA zone name, and the UTC offset in
    MINUTES already resolved for this partition's date and the two before it.

    Resolved offsets rather than zone names because the conversion a client
    needs is local-wall-clock -> UTC, which is the direction Intl.DateTimeFormat
    does NOT do; doing it from a zone name needs Temporal or an
    iterate-and-correct loop, including the ambiguous DST hour. An integer
    offset makes both directions addition, and bakes DST in for the date. The
    zone name is carried anyway because it dictionary-compresses to nearly
    nothing and a client that wants to do it properly should be able to.

    Three dates because a departure can be up to two days before the arrival
    date (a westbound date-line crossing is +2). Only 7 dates in 2026 have a
    shift at any of these airports, so it rarely matters -- and costs ~4 KB.

    Parquet, not a custom binary: measured at 86 KB against 137 KB raw for an
    equivalent .bin. The .bin is ~18 KB smaller gzipped, which is not worth a
    format to decode, version and verify when hyparquet is already loaded for
    legs.parquet. cells.bin/tracks.bin are binary because posting lists and
    byte-range records are things Parquet cannot express; a flat 6k-row airport
    table is not one of those.
    """
    import json
    import pyarrow as pa, pyarrow.parquet as pq
    d0 = dt.date.fromisoformat(date)
    rows = []
    for ident, (lat, lon, tz) in airports.items():
        o = []
        for k in (0, 1, 2):
            d = d0 - dt.timedelta(days=k)
            o.append(int(dt.datetime(d.year, d.month, d.day, 12,
                                     tzinfo=tz).utcoffset().total_seconds() // 60))
        rows.append((ident, int(lat * 1e5), int(lon * 1e5), str(tz), *o))
    rows.sort()
    t = pa.table({
        "ident": [r[0] for r in rows],
        "lat": pa.array([r[1] for r in rows], pa.int32()),
        "lon": pa.array([r[2] for r in rows], pa.int32()),
        "tz": [r[3] for r in rows],
        "off_d0": pa.array([r[4] for r in rows], pa.int16()),
        "off_dm1": pa.array([r[5] for r in rows], pa.int16()),
        "off_dm2": pa.array([r[6] for r in rows], pa.int16()),
    })
    ap_path = os.path.join(out_dir, "airports_utc.parquet")
    pq.write_table(t, ap_path, compression="zstd", use_dictionary=["tz", "ident"])

    fixed, kmh = coef or (BLOCK_FIXED_MIN, BLOCK_KMH)
    meta = {
        "date": date,
        "airports": len(rows),
        # tier 2: block_min = fixed + 60 * gc_km / kmh
        "block_fixed_min": round(fixed, 2),
        "block_kmh": round(kmh, 1),
        "taxi_min": TAXI_MIN,
        "min_samples": MIN_SAMPLES,
        # overnight.parquet is one day behind: day D's midnight-crossing legs
        # cannot be spliced until D+1 exists, so the settled table is for D-1.
        "table_date": table_date,
        "table_legs": n_legs,
        "table_settled": table_date is not None,
    }
    meta_path = os.path.join(out_dir, "overnight_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")

    # round-trip the artifact rather than trusting the write, in the spirit of
    # verify_bundle.py gating the bundle
    back = pq.read_table(ap_path)
    assert back.num_rows == len(rows), "airports_utc.parquet row count changed"
    chk = dict(zip(back["ident"].to_pylist(), back["off_d0"].to_pylist()))
    for ident, _, _, _, o0, _, _ in rows[:200]:
        assert chk[ident] == o0, f"offset round-trip failed for {ident}"
    print(f"{len(rows)} airports -> {ap_path} "
          f"({os.path.getsize(ap_path)/1024:.1f} KB, round-trip ok)")
    print(f"  -> {meta_path}  block_min = {fixed:.1f} + 60*d/{kmh:.0f}"
          f"{'' if table_date else '   (no settled table this run)'}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--airports-tz", default="airport_tz.csv")
    p.add_argument("--legs", help="flights.parquet from build_legs.py")
    p.add_argument("--all-legs", action="store_true",
                   help="do not require a ground fix at both ends (noisier)")
    p.add_argument("--dep"); p.add_argument("--arr")
    p.add_argument("--arr-local", help="scheduled arrival, local at --arr: "
                                       "'YYYY-MM-DD HH:MM'")
    p.add_argument("--fit", action="store_true")
    p.add_argument("--build-table", metavar="PARQUET")
    p.add_argument("--emit-client", metavar="DIR",
                   help="write airports_utc.parquet + overnight_meta.json for "
                        "static clients")
    p.add_argument("--date", help="partition date YYYY-MM-DD, for --emit-client")
    p.add_argument("--table-date",
                   help="the date --build-table's legs cover, recorded in "
                        "overnight_meta.json; omit if no settled table")
    a = p.parse_args()

    airports = load_airports(a.airports_tz)
    legs = load_legs(a.legs, not a.all_legs) if a.legs else None
    if legs is not None:
        print(f"{len(legs)} usable legs", file=sys.stderr)
    coef = fit(legs, a.airports_tz) if a.fit else None
    if a.build_table:
        if legs is None:
            sys.exit("--build-table needs --legs")
        build_table(legs, airports, a.build_table)
    if a.emit_client:
        if not a.date:
            sys.exit("--emit-client needs --date")
        os.makedirs(a.emit_client, exist_ok=True)
        emit_client(airports, a.date, a.emit_client, coef,
                    a.table_date, len(legs) if legs else 0)
    if not (a.dep and a.arr and a.arr_local):
        return

    arr_local = dt.datetime.strptime(a.arr_local, "%Y-%m-%d %H:%M")
    r = classify(a.dep, a.arr, arr_local, airports,
                 pair_blocks(legs) if legs else None, coef)
    print(f"{a.dep} -> {a.arr}, arriving {arr_local:%Y-%m-%d %H:%M} local")
    print(f"  departed  {r['dep_local']:%Y-%m-%d %H:%M} local  "
          f"({r['dep_local'].tzname()})")
    print(f"  offset    {r['offset']:+d} day  -> "
          f"{'OVERNIGHT' if r['overnight'] else 'SAME DAY'}")
    print(f"  block     {r['block_min']:.0f} min  [{r['source']}]")
    print(f"  margin    {r['margin_min']:.0f} min from local midnight"
          f"{'   <-- MARGINAL' if r['margin_min'] < 60 else ''}")


if __name__ == "__main__":
    main()
