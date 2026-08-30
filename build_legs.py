#!/usr/bin/env python3
"""
build_legs.py - segment each aircraft's day into flight LEGS (with departure and
arrival airports) and write per-airport Parquet files a browser (hyparquet) can
fetch one at a time.

Pipeline:
  1. read the decimated points (prod-0-parquet-single/traces.parquet), grouped by
     icao and ordered by time;
  2. find airborne runs that actually go somewhere (>2 km horizontal), split the
     day at the temporal midpoint between consecutive flights -> one leg each,
     taxi-out + airborne + taxi-in;
  3. departure = nearest airport to the leg's first end-window anchor, arrival =
     nearest to its last (airports.csv, <=10 km). The anchor is a ground fix when
     the window has one, else the window's lowest fix, so airports with thin
     surface coverage still resolve;
  4. write points_legs.parquet (one row/point, with dep/arr/leg_id), flights.parquet
     (one row/leg = the index, sorted by dep), and airports/airport=XXXX/*.parquet
     (each leg written under BOTH its dep and arr airport).

Usage:  ./build_legs.py [--limit-aircraft N] [--traces PATH] [--meta PATH]
                        [--airports CSV] [--out-dir DIR]
"""
import argparse, math, os, statistics, csv as csvmod
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

FLIGHT_MIN_KM = 2.0     # an airborne run must span this to count as a flight
NEAR_KM = 10.0          # max distance from a fix to call it "at" an airport
ENDPOINT_FRAC = 0.25    # search this much of the leg at each end for an anchor
ENDPOINT_MIN_PTS = 5    # ...but always look at at least this many points


def build_airport_index(path):
    grid = {}
    with open(path, newline="") as f:
        for row in csvmod.DictReader(f):
            try:
                lat = float(row["latitude_deg"]); lon = float(row["longitude_deg"])
                ident = row["ident"]
            except (KeyError, ValueError):
                continue
            if not ident:
                continue
            grid.setdefault((round(lat), round(lon)), []).append((lat, lon, ident))
    return grid


def make_resolver(grid, max_km=NEAR_KM):
    cache = {}
    def resolve(lat, lon):
        ck = (round(lat, 2), round(lon, 2))
        if ck in cache:
            return cache[ck]
        best, bd = None, max_km
        rl, ro = round(lat), round(lon)
        for dla in (-1, 0, 1):
            for dlo in (-1, 0, 1):
                for (alat, alon, ident) in grid.get((rl + dla, ro + dlo), ()):
                    d = _km(lat, lon, alat, alon)
                    if d < bd:
                        bd, best = d, ident
        cache[ck] = best
        return best
    return resolve


def _km(la1, lo1, la2, lo2):
    return math.hypot((la2 - la1) * 111.32,
                      (lo2 - lo1) * 111.32 * math.cos(math.radians(la1)))


def segment(ts, la, lo, alt, gnd):
    """Return (leg_of_point[list], legs[list of (i0,i1)]) for one aircraft.
    Points are assigned to the nearest flight in time; taxi/parked points fall to
    the adjacent leg. Returns ([], []) if the aircraft never really flew."""
    n = len(ts)
    # airborne runs (on_ground == False)
    runs = []
    i = 0
    while i < n:
        if not gnd[i]:
            j = i
            while j < n and not gnd[j]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    # keep only runs that travel > FLIGHT_MIN_KM horizontally
    flights = []
    for (s, e) in runs:
        lats = la[s:e + 1]; lons = lo[s:e + 1]
        span = _km(min(lats) / 1e5, min(lons) / 1e5, max(lats) / 1e5, max(lons) / 1e5)
        if span > FLIGHT_MIN_KM:
            flights.append((s, e))
    if not flights:
        return [], []
    # leg boundaries: split at temporal midpoint between consecutive flights
    bounds = [0]
    for k in range(len(flights) - 1):
        e_k = flights[k][1]; s_k1 = flights[k + 1][0]
        mid_t = (ts[e_k] + ts[s_k1]) / 2
        b = e_k
        while b < s_k1 and ts[b] < mid_t:
            b += 1
        bounds.append(b)
    bounds.append(n)
    leg_of = [0] * n
    legs = []
    for k in range(len(flights)):
        i0, i1 = bounds[k], bounds[k + 1] - 1
        for idx in range(i0, i1 + 1):
            leg_of[idx] = k
        legs.append((i0, i1))
    return leg_of, legs


def _anchor(al, gnd, idxs, head):
    """Index of the best airport-anchor within one end-window of a leg.

    on_ground is the least reliably received bit in the feed: surface position
    messages are low-power and need a receiver with near line-of-sight to the
    field, so half the aircraft in a day's traces never produce one at all, and
    airports with thin local coverage produce almost none. Prefer a ground fix
    when the window has one, but fall back to the window's LOWEST fix -- the
    climb-out or the approach -- instead of giving up, so a field can still be
    resolved from the air.
    """
    grounds = [i for i in idxs if gnd[i]]
    if grounds:
        return grounds[0] if head else grounds[-1]
    return min(idxs, key=lambda i: al[i])


def endpoint_airport(la, lo, al, gnd, i0, i1, resolve):
    """Nearest airport to each TEMPORAL end of the leg.

    The two windows are disjoint by construction, and that is what keeps a leg
    from being labelled X->X: scanning the WHOLE leg for "first ground fix" and
    "last ground fix" puts both of them on whichever end happened to have
    surface coverage, so a flight out of a thinly-covered field was recorded as
    departing from its own destination -- losing it for the real origin and
    inventing a self-loop at the destination.
    """
    n = i1 - i0 + 1
    w = max(ENDPOINT_MIN_PTS, int(n * ENDPOINT_FRAC))
    w = min(w, max(1, n // 2))              # never let head and tail overlap
    dep_i = _anchor(al, gnd, range(i0, i0 + w), True)
    arr_i = _anchor(al, gnd, range(i1 - w + 1, i1 + 1), False)
    dep = resolve(la[dep_i] / 1e5, lo[dep_i] / 1e5)
    arr = resolve(la[arr_i] / 1e5, lo[arr_i] / 1e5)
    return dep, arr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", default="prod-0-parquet-single/traces.parquet")
    ap.add_argument("--meta", default="prod-0-parquet-single/aircraft.parquet")
    ap.add_argument("--airports", default="airports.csv")
    ap.add_argument("--out-dir", default="airport_ds")
    ap.add_argument("--limit-aircraft", type=int, default=0)
    ap.add_argument("--airport-radius-km", dest="airport_radius", type=float,
                    default=0.0, help="if > 0, per-airport files keep only the "
                    "leg's points within this radius of the airport (local taxi + "
                    "approach/departure). Default 0 = keep the FULL leg path in "
                    "each airport partition (legs are already split by leg_id, so "
                    "the frontend draws each as one continuous dep->arr track)")
    a = ap.parse_args()

    con = duckdb.connect()
    meta = {r[0]: (r[1], r[2]) for r in
            con.execute(f"SELECT icao, reg, type FROM '{a.meta}'").fetchall()}
    resolve = make_resolver(build_airport_index(a.airports))
    os.makedirs(a.out_dir, exist_ok=True)

    # carry the cusp bit through if the source has one (CR-fit output); the
    # frontend breaks its Catmull-Rom spline at cusp nodes for sharp corners
    src_cols = {c[0] for c in con.execute(
        f"DESCRIBE SELECT * FROM '{a.traces}'").fetchall()}
    has_cusp = "cusp" in src_cols

    pcols = ["icao", "t", "lat", "lon", "alt", "on_ground", "leg_id", "dep", "arr",
             "reg", "type"] + (["cusp"] if has_cusp else [])
    pbuf = {c: [] for c in pcols}
    legs_rows = []
    pw = [None]
    pfields = [("icao", pa.string()), ("t", pa.int32()), ("lat", pa.int32()),
               ("lon", pa.int32()), ("alt", pa.int32()), ("on_ground", pa.bool_()),
               ("leg_id", pa.string()), ("dep", pa.string()), ("arr", pa.string()),
               ("reg", pa.string()), ("type", pa.string())]
    if has_cusp:
        pfields.append(("cusp", pa.bool_()))
    pschema = pa.schema(pfields)
    pl_path = os.path.join(a.out_dir, "points_legs.parquet")

    def flush_points():
        if not pbuf["t"]:
            return
        if pw[0] is None:
            pw[0] = pq.ParquetWriter(pl_path, pschema, compression="zstd",
                                     use_dictionary=["icao", "leg_id", "dep", "arr",
                                                     "reg", "type"])
        pw[0].write_table(pa.table(pbuf, schema=pschema))
        for c in pcols:
            pbuf[c] = []

    # stream points grouped by icao
    lim = f"WHERE icao IN (SELECT DISTINCT icao FROM '{a.traces}' LIMIT {a.limit_aircraft})" \
          if a.limit_aircraft else ""
    sel = "icao,t,lat,lon,alt,on_ground" + (",cusp" if has_cusp else "")
    reader = con.execute(
        f"SELECT {sel} FROM '{a.traces}' {lim} "
        f"ORDER BY icao, t").fetch_record_batch(rows_per_batch=1_000_000)

    cur = None
    ts = la = lo = al = gd = cu = None
    ac = 0

    def finish(icao, ts, la, lo, al, gd, cu):
        leg_of, legs = segment(ts, la, lo, al, gd)
        if not legs:
            return 0
        reg, typ = meta.get(icao, (None, None))
        for k, (i0, i1) in enumerate(legs):
            dep, arr = endpoint_airport(la, lo, al, gd, i0, i1, resolve)
            leg_id = f"{icao}_{k}"
            for idx in range(i0, i1 + 1):
                pbuf["icao"].append(icao); pbuf["t"].append(ts[idx])
                pbuf["lat"].append(la[idx]); pbuf["lon"].append(lo[idx])
                pbuf["alt"].append(al[idx]); pbuf["on_ground"].append(gd[idx])
                pbuf["leg_id"].append(leg_id); pbuf["dep"].append(dep); pbuf["arr"].append(arr)
                pbuf["reg"].append(reg); pbuf["type"].append(typ)
                if has_cusp:
                    pbuf["cusp"].append(cu[idx])
            legs_rows.append((leg_id, icao, reg, typ, dep, arr,
                              ts[i0], ts[i1], i1 - i0 + 1))
        return len(legs)

    for batch in reader:
        d = batch.to_pydict()
        ic = d["icao"]; T = d["t"]; LA = d["lat"]; LO = d["lon"]; AL = d["alt"]; GD = d["on_ground"]
        CU = d["cusp"] if has_cusp else None
        for r in range(len(ic)):
            if ic[r] != cur:
                if cur is not None:
                    finish(cur, ts, la, lo, al, gd, cu)
                    ac += 1
                    if ac % 10000 == 0:
                        print(f"  ... {ac} aircraft, {len(legs_rows)} legs", flush=True)
                    if len(pbuf["t"]) >= 800_000:
                        flush_points()
                cur = ic[r]; ts = []; la = []; lo = []; al = []; gd = []; cu = []
            ts.append(T[r]); la.append(LA[r]); lo.append(LO[r])
            al.append(AL[r] if AL[r] is not None else 0); gd.append(GD[r])
            if has_cusp:
                cu.append(CU[r])
    if cur is not None:
        finish(cur, ts, la, lo, al, gd, cu)
    flush_points()
    if pw[0] is not None:
        pw[0].close()

    # flights index (one row per leg), sorted by dep
    lt = pa.table({
        "leg_id": [x[0] for x in legs_rows], "icao": [x[1] for x in legs_rows],
        "reg": [x[2] for x in legs_rows], "type": [x[3] for x in legs_rows],
        "dep": [x[4] for x in legs_rows], "arr": [x[5] for x in legs_rows],
        "t_start": [x[6] for x in legs_rows], "t_end": [x[7] for x in legs_rows],
        "n_points": [x[8] for x in legs_rows],
    }).sort_by("dep")
    pq.write_table(lt, os.path.join(a.out_dir, "flights.parquet"),
                   compression="zstd", use_dictionary=["dep", "arr", "type", "reg"])

    # per-airport files: each leg under BOTH dep and arr (hive-partitioned). With
    # --airport-radius-km > 0 the copy is CLIPPED to the airport's vicinity; by
    # default the FULL leg path is kept in each partition (legs are split by
    # leg_id and drawn per-leg, so a full dep->arr track is continuous, not a
    # teleport between fields).
    apath = os.path.join(a.out_dir, "airports")
    if a.airport_radius and a.airport_radius > 0:
        con.execute("""
            CREATE OR REPLACE TEMP MACRO km(la1, lo1, la2, lo2) AS
              6371 * 2 * asin(sqrt(
                power(sin(radians(la2 - la1) / 2), 2) +
                cos(radians(la1)) * cos(radians(la2)) *
                power(sin(radians(lo2 - lo1) / 2), 2)))
        """)
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE ap AS
            SELECT ident AS code, latitude_deg AS lat, longitude_deg AS lon
            FROM read_csv_auto('{a.airports}')
            WHERE ident IS NOT NULL AND latitude_deg IS NOT NULL
        """)
        dep_sel = (f"SELECT p.*, p.dep AS airport FROM '{pl_path}' p "
                   f"JOIN ap d ON p.dep = d.code WHERE p.dep IS NOT NULL "
                   f"AND km(d.lat, d.lon, p.lat / 1e5, p.lon / 1e5) < {a.airport_radius}")
        arr_sel = (f"SELECT p.*, p.arr AS airport FROM '{pl_path}' p "
                   f"JOIN ap r ON p.arr = r.code WHERE p.arr IS NOT NULL "
                   f"AND p.arr <> p.dep "
                   f"AND km(r.lat, r.lon, p.lat / 1e5, p.lon / 1e5) < {a.airport_radius}")
    else:
        dep_sel = (f"SELECT p.*, p.dep AS airport FROM '{pl_path}' p "
                   f"WHERE p.dep IS NOT NULL")
        arr_sel = (f"SELECT p.*, p.arr AS airport FROM '{pl_path}' p "
                   f"WHERE p.arr IS NOT NULL AND p.arr <> p.dep")
    # Single-thread this one write so every partition gets exactly ONE file
    # (data_0.parquet). DuckDB's parallel partitioned write otherwise has each
    # thread flush its own data_N.parquet per partition, and a browser can't list
    # a directory over HTTP to discover data_1/data_2 -- so the frontend contract
    # must be one predictable file per airport. This write is cheap vs the fit.
    con.execute("SET threads TO 1")
    con.execute(f"""
        COPY (
          {dep_sel}
          UNION ALL
          {arr_sel}
          ORDER BY airport, icao, t
        ) TO '{apath}' (FORMAT parquet, PARTITION_BY (airport),
                        OVERWRITE_OR_IGNORE, COMPRESSION zstd)
    """)
    con.execute("RESET threads")

    n_air = con.execute(f"SELECT count(DISTINCT airport) FROM (SELECT dep AS airport "
                        f"FROM '{pl_path}' WHERE dep IS NOT NULL UNION SELECT arr "
                        f"FROM '{pl_path}' WHERE arr IS NOT NULL)").fetchone()[0]
    print(f"\nDONE: {ac} aircraft, {len(legs_rows)} legs, {n_air} airports")
    print(f"  {pl_path}")
    print(f"  {os.path.join(a.out_dir,'flights.parquet')}  (leg index, sorted by dep)")
    print(f"  {apath}/airport=XXXX/*.parquet  (per-airport, fetch one at a time)")


if __name__ == "__main__":
    main()
