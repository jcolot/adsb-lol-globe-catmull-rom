"""
trace_io.py - shared helpers for reading readsb globe_history traces.

  load_trace / collect      read one trace_full_*.json (plain or gzipped) / list them
  parse                     decoded trace -> per-sample measurement dicts
  interp_fill               linear gap-fill over a monotonic axis
  build_airport_index /     nearest-airport field elevation, for on-ground altitude
    make_elev_resolver
  cr_point                  one point on a centripetal Catmull-Rom span

Imported by fit_spline.py, validate_recon.py and smooth_trace.py.
"""
import glob, gzip, json, math, os, random


# ------------------------------- I/O ---------------------------------------
def load_trace(path):
    with open(path, "rb") as f:
        head = f.read(2)
    opener = gzip.open if head == b"\x1f\x8b" else open
    with opener(path, "rb") as f:
        return json.loads(f.read())


def collect(paths, limit):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += glob.glob(os.path.join(p, "**", "trace_full_*.json"),
                               recursive=True)
        else:
            files.append(p)
    files = sorted(set(files))
    if limit and len(files) > limit:
        random.seed(0)
        files = sorted(random.sample(files, limit))
    return files


def parse(d, elev_fn=None):
    """Extract per-sample measurements from a decoded trace."""
    raw = d.get("trace", [])
    ms = []
    for p in raw:
        t = p[0]
        gnd = p[3] == "ground"
        baro = p[3] if isinstance(p[3], (int, float)) else None
        geom = p[10] if len(p) > 10 and isinstance(p[10], (int, float)) else None
        gs = p[4] if isinstance(p[4], (int, float)) else None
        trk = p[5] if isinstance(p[5], (int, float)) else None
        vr = None
        if len(p) > 11 and isinstance(p[11], (int, float)):
            vr = p[11]                          # geometric vertical rate, ft/min
        elif isinstance(p[7], (int, float)):
            vr = p[7]                           # barometric vertical rate
        src = None
        if len(p) > 9 and isinstance(p[9], dict):
            src = p[9].get("type")
        elif len(p) > 9 and isinstance(p[9], str):
            src = p[9]
        # readsb emits the aircraft-details object only on the samples where a
        # field CHANGED, so `flight` is sparse and its index varies by trace
        # variant (8 = details, 9 = source type; some writers shift them). Scan
        # both for a dict CARRYING A CALLSIGN -- stopping at the first dict is
        # not the same thing and silently drops callsigns, because index 9 is
        # itself a dict in the common variant (see `src` above): a writer that
        # puts the source type at 8 would end the scan before the details object
        # at 9 was ever looked at, and so would a details object whose `flight`
        # is absent or blank on this particular sample.
        flight = None
        for k in (8, 9):
            if len(p) > k and isinstance(p[k], dict):
                f = p[k].get("flight")
                if isinstance(f, str) and f.strip():
                    flight = f.strip()
                    break
        ms.append(dict(t=t, lat=p[1], lon=p[2], gnd=gnd, baro=baro, geom=geom,
                       gs=gs, trk=trk, vr=vr, src=src, flight=flight))
    return ms


def interp_fill(vals, xs):
    """Fill None gaps in vals by linear interpolation over xs (monotonic), holding
    flat beyond the first/last known sample. All-None -> zeros."""
    n = len(vals)
    known = [i for i in range(n) if vals[i] is not None]
    if not known:
        return [0.0] * n
    out = [float(v) if v is not None else None for v in vals]
    for i in range(known[0]):
        out[i] = float(vals[known[0]])
    for i in range(known[-1] + 1, n):
        out[i] = float(vals[known[-1]])
    for a, b in zip(known, known[1:]):
        if b == a + 1:
            continue
        va, vb, xa, xb = vals[a], vals[b], xs[a], xs[b]
        span = (xb - xa) or 1
        for i in range(a + 1, b):
            out[i] = va + (vb - va) * (xs[i] - xa) / span
    return out


# ------------------- airport elevation (ground altitude) -------------------
def km_between(lat1, lon1, lat2, lon2):
    dlat = (lat2 - lat1) * 111.32
    dlon = (lon2 - lon1) * 111.32 * math.cos(math.radians(lat1))
    return math.hypot(dlat, dlon)


def build_airport_index(csv_path):
    """Grid-bucketed airport index {(round(lat),round(lon)): [(lat,lon,elev_ft)]}
    from an OurAirports-style airports.csv (needs elevation_ft)."""
    import csv as _csv
    grid = {}
    with open(csv_path, newline="") as f:
        for row in _csv.DictReader(f):
            try:
                lat = float(row["latitude_deg"]); lon = float(row["longitude_deg"])
                ev = row.get("elevation_ft", "")
                if ev in ("", None):
                    continue
                elev = int(float(ev))
            except (KeyError, ValueError):
                continue
            grid.setdefault((round(lat), round(lon)), []).append((lat, lon, elev))
    return grid


def make_elev_resolver(grid, max_km=10.0):
    """Return f(lat,lon)->elevation_ft of the nearest airport within max_km (or
    None). Cached on a ~1 km position grid so repeated ground fixes are cheap."""
    cache = {}
    def resolve(lat, lon):
        ck = (round(lat, 2), round(lon, 2))
        if ck in cache:
            return cache[ck]
        best, bd = None, max_km
        rl, ro = round(lat), round(lon)
        for dla in (-1, 0, 1):
            for dlo in (-1, 0, 1):
                for (alat, alon, elev) in grid.get((rl + dla, ro + dlo), ()):
                    d = km_between(lat, lon, alat, alon)
                    if d < bd:
                        bd, best = d, elev
        cache[ck] = best
        return best
    return resolve


# ---------------------- Catmull-Rom (centripetal) --------------------------
def _lerp(a, b, u):
    return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)


def cr_point(p0, p1, p2, p3, u):
    def tj(ti, a, b):
        return ti + max(math.hypot(b[0] - a[0], b[1] - a[1]), 1e-9) ** 0.5
    t0 = 0.0
    t1 = tj(t0, p0, p1)
    t2 = tj(t1, p1, p2)
    t3 = tj(t2, p2, p3)
    if t1 <= t0 or t2 <= t1 or t3 <= t2:
        return _lerp(p1, p2, u)
    t = t1 + (t2 - t1) * u
    a1 = _lerp(p0, p1, (t - t0) / (t1 - t0))
    a2 = _lerp(p1, p2, (t - t1) / (t2 - t1))
    a3 = _lerp(p2, p3, (t - t2) / (t3 - t2))
    b1 = _lerp(a1, a2, (t - t0) / (t2 - t0))
    b2 = _lerp(a2, a3, (t - t1) / (t3 - t1))
    return _lerp(b1, b2, (t - t1) / (t2 - t1))
