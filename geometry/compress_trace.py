#!/usr/bin/env python3
"""
compress_trace.py - Phase-aware lossy compression for readsb globe_history traces.

Pipeline per aircraft:
  1. OUTLIER GATE   - drop physically impossible samples (implied ground speed
                      above --max-speed km/h), i.e. raw ADS-B position glitches.
  2. GRADUATED DP   - Douglas-Peucker whose tolerance is altitude-graduated:
                      tight on the ground / at low altitude, loosening to a cruise
                      tolerance up high. Horizontal (m) and vertical (ft) channels
                      simplified independently; UNION of kept samples retained.
  3. ENCODE         - columnar delta + zig-zag varint + gzip.

Two modes:
  * ANALYSE (default): report ratios and per-phase reconstruction error.
  * CONVERT (--out DIR): write a compact archive -- one <icao>.tz blob per
    aircraft plus manifest.json -- and report total archive shrink. --verify
    round-trips the blobs back and checks them.

Usage:
    ./geometry/compress_trace.py FILE_OR_DIR ... [options]
      --max-speed 1300  outlier gate, km/h                     (default 1300)
      --h-ground 10     horiz tol on the ground, m             (default 10)
      --h-low    20     horiz tol at/below --low-alt, m         (default 20)
      --h-cruise 150    horiz tol at/above --high-alt, m        (default 150)
      --v-ground 25     vert tol on ground / low, ft            (default 25)
      --v-cruise 250    vert tol at/above --high-alt, ft        (default 250)
      --low-alt  2000   below this = "low" (tight), ft          (default 2000)
      --high-alt 12000  at/above this = full cruise tol, ft     (default 12000)
      --recon    linear|catmull|segment   reconstruction to score (default linear)
      --out DIR         write compressed archive here (convert mode)
      --verify          in convert mode, round-trip decode & check
      --limit N         sample at most N files from a directory
Files may be plain or gzipped `trace_full_*.json`.
"""
import argparse, gzip, json, math, os, random, statistics, sys, glob

LOW_PHASE_FT = 3000
LAT_SCALE = 1e5
MAGIC = b"TZ01"
GS_TAXI_KT = 40        # below this, near an airport, a fix is taxiing not flying
SURFACE_AGL_FT = 500   # ...and within this of field elevation (baro) -> surface


# ------------------------------- I/O ---------------------------------------
def load_trace(path):
    with open(path, "rb") as f:
        head = f.read(2)
    opener = gzip.open if head == b"\x1f\x8b" else open
    with opener(path, "rb") as f:
        return json.loads(f.read())


def km_between(lat1, lon1, lat2, lon2):
    dlat = (lat2 - lat1) * 111.32
    dlon = (lon2 - lon1) * 111.32 * math.cos(math.radians(lat1))
    return math.hypot(dlat, dlon)


def _interp_fill(vals, xs):
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


# ------------------------------ outlier gate -------------------------------
def _gate_alt(p):
    """Barometric altitude (ft) of a raw trace point; 'ground'/missing -> 0."""
    a = p[3]
    if a is None or a == "ground":
        return 0
    try:
        return int(a)
    except (TypeError, ValueError):
        return 0


def gate(pts, max_kmh, max_jump_km, max_kmh_low=555.0, jump_low_km=0.25,
         cap_lo_ft=1000.0, cap_hi_ft=10000.0):
    """Two-pass outlier rejection with ALTITUDE-GRADUATED thresholds.

    A single cruise-scale threshold is blind to the airport area: a glitch that
    throws an aircraft to 288 m/s at 350 ft is impossible there yet sits well
    under a flat 1300 km/h cap. So both thresholds ramp with altitude -- tight
    near the ground, loosening to the cruise value up high -- tracking the real
    speed envelope (taxi/approach are a fraction of cruise; 250 kt below FL100):
        speed cap : max_kmh_low  (<= cap_lo_ft)  ->  max_kmh      (>= cap_hi_ft)
        jump dist : jump_low_km  (<= cap_lo_ft)  ->  max_jump_km  (>= cap_hi_ft)

    Pass 1 (speed): reject a sample implying a ground speed above the cap for ITS
    altitude, measured from the last accepted fix. A glitch does not advance the
    anchor, so a lone spike is dropped and following fixes are judged against the
    last plausible position. Catches fast teleports; misses a glitch spread over
    a long time gap.

    Pass 2 (distance/isolated spike): among survivors, reject a point far (> the
    jump cap for its altitude) from BOTH neighbours while those neighbours are
    close to each other -- a teleport-and-return that stayed under the speed cap
    because the gaps were long. Near the ground the cap is ~250 m, so the sub-km
    zig-zags that a flat 25 km never caught are now dropped; the neighbour-
    proximity test still protects genuine fast motion (its neighbours spread far
    apart too)."""
    def ramp(alt, lo, hi):
        if alt <= cap_lo_ft:
            return lo
        if alt >= cap_hi_ft:
            return hi
        return lo + (hi - lo) * (alt - cap_lo_ft) / (cap_hi_ft - cap_lo_ft)

    n = len(pts)
    keep = [True] * n
    la = lo = lt = None
    for i, p in enumerate(pts):
        if lt is None:
            la, lo, lt = p[1], p[2], p[0]
            continue
        dt = p[0] - lt
        dist = km_between(la, lo, p[1], p[2])
        if dt > 0:
            spd = dist / (dt / 3600.0)
        else:
            spd = 0.0 if dist < 0.5 else float("inf")
        if spd > ramp(_gate_alt(p), max_kmh_low, max_kmh):
            keep[i] = False              # reject, keep old anchor
        else:
            la, lo, lt = p[1], p[2], p[0]

    surv = [i for i in range(n) if keep[i]]
    for k in range(1, len(surv) - 1):
        a, b, c = surv[k - 1], surv[k], surv[k + 1]
        dp = km_between(pts[a][1], pts[a][2], pts[b][1], pts[b][2])
        dn = km_between(pts[b][1], pts[b][2], pts[c][1], pts[c][2])
        dac = km_between(pts[a][1], pts[a][2], pts[c][1], pts[c][2])
        jcap = ramp(_gate_alt(pts[b]), jump_low_km, max_jump_km)
        if dp > jcap and dn > jcap and dac < 0.5 * max(dp, dn):
            keep[b] = False
    return keep


# ---------------------- altitude-graduated tolerances ----------------------
class Tol:
    def __init__(self, a):
        self.hg, self.hl, self.hc = a.h_ground, a.h_low, a.h_cruise
        self.vg, self.vc = a.v_ground, a.v_cruise
        self.lo, self.hi = a.low_alt, a.high_alt

    def _ramp(self, alt, lo_val, hi_val):
        if alt <= self.lo:
            return lo_val
        if alt >= self.hi:
            return hi_val
        return lo_val + (hi_val - lo_val) * (alt - self.lo) / (self.hi - self.lo)

    def horiz(self, alt, ground):
        return self.hg if ground else self._ramp(alt, self.hl, self.hc)

    def vert(self, alt, ground):
        return self.vg if ground else self._ramp(alt, self.vg, self.vc)


# ------------------- airport elevation (ground altitude) -------------------
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


# ------------------------ ground jitter removal ----------------------------
def denoise_ground(pts, gs_stat, radius_m):
    """Collapse GPS wander while the aircraft is stationary. A maximal run of
    consecutive on-ground fixes with groundspeed < gs_stat (kt) that stays within
    radius_m of its start (truly parked, not slow-taxiing) is replaced by its
    median position, emitted at the run's first and last timestamps so the dwell
    is preserved. Moving taxi is untouched. Uses trace field 4 (groundspeed);
    a missing gs is treated as a stationary candidate but still guarded by the
    spatial-containment test, so real movement is never erased."""
    out = []
    i, n = 0, len(pts)
    while i < n:
        p = pts[i]
        stat = p[3] == "ground" and (p[4] is None or p[4] < gs_stat)
        if not stat:
            out.append(p)
            i += 1
            continue
        j = i
        while j < n and pts[j][3] == "ground" and (pts[j][4] is None or pts[j][4] < gs_stat):
            j += 1
        run = pts[i:j]
        bbox = max(km_between(run[0][1], run[0][2], r[1], r[2]) for r in run) * 1000.0
        # Truly stopped (gs ~0 throughout) => there is NO real displacement, so the
        # whole spread is jitter and collapses regardless of the tight radius (the
        # forward-then-back taxi wobble the radius guard used to let through). The
        # radius guard still governs slow-creep runs (gs up to gs_stat) where some
        # of the spread could be real. 200 m caps pathological cases.
        stopped = statistics.median([r[4] if r[4] is not None else 0.0
                                     for r in run]) < 1.0
        if len(run) >= 3 and (bbox < radius_m or (stopped and bbox < 200.0)):
            mlat = statistics.median(r[1] for r in run)
            mlon = statistics.median(r[2] for r in run)
            first = list(run[0]); first[1] = mlat; first[2] = mlon
            out.append(first)
            if run[-1][0] != run[0][0]:
                last = list(run[-1]); last[1] = mlat; last[2] = mlon
                out.append(last)
        else:
            out.extend(run)
        i = j
    return out


# ------------------------ vertical spike repair ----------------------------
def despike_alt(alt_store, ground, t, dmax_ft=400.0, dnb_ft=400.0, span_s=30,
                max_run=2):
    """Repair short altitude-glitch RUNS -- the vertical analogue of the
    horizontal isolated-spike gate. A run of 1..max_run consecutive airborne
    samples that all differ from the fixes flanking it by > dmax_ft, while those
    flanks agree within dnb_ft (over a < span_s window), is a geometric/barometric
    glitch, not real vertical motion. Replace each with the time-interpolated
    value between the flanks; positions are untouched (only altitude is bad).

    The flank-agreement guard (dnb_ft) is what makes this safe on real data: a
    genuine climb/descent is monotonic, so its flanks are far apart and can never
    trip the test -- only an out-and-return excursion (physically absent over a
    few seconds) does. The span guard drops sparse-but-real steeps. max_run > 1
    catches glitch CLUSTERS (2-3 bad samples) that single-point repair cannot,
    since within a cluster each sample's neighbour is also bad. Mutates and
    returns alt_store."""
    idx = [i for i in range(len(alt_store)) if alt_store[i] is not None]
    m = len(idx)
    k = 1
    while k < m - 1:
        matched = False
        for L in range(1, max_run + 1):        # try shortest run first
            if k + L >= m:
                break
            a, c = idx[k - 1], idx[k + L]
            mids = idx[k:k + L]
            va, vc = alt_store[a], alt_store[c]
            if any(ground[b] for b in mids) or (t[c] - t[a]) > span_s:
                continue
            if abs(va - vc) >= dnb_ft:
                continue
            if all(abs(alt_store[b] - va) > dmax_ft and
                   abs(alt_store[b] - vc) > dmax_ft for b in mids):
                span = (t[c] - t[a]) or 1
                for b in mids:
                    alt_store[b] = int(round(va + (vc - va) * (t[b] - t[a]) / span))
                k += L + 1
                matched = True
                break
        if not matched:
            k += 1
    return alt_store


def deglitch_ground(pts, ground, jitter_m=30.0, min_step_m=8.0, frac=0.5,
                    max_gap_s=600, stop_gs=1.5):
    """Return a keep-mask dropping on-ground REVERSAL spikes -- the taxi-scale
    analogue of the horizontal isolated-spike gate. Among consecutive on-ground
    fixes, a point whose two neighbours are much closer to each OTHER than to it
    (dac < frac * max(step)) is an out-and-return wobble. It is GPS jitter, not
    motion, when EITHER the aircraft is essentially stopped there (gs < stop_gs,
    so any displacement is jitter regardless of amplitude) OR the excursion is
    tiny (< jitter_m). A real backtrack (taxi to a runway end and turn around) is
    KEPT because it happens while MOVING (gs well above stop_gs). Real taxi
    corners survive too -- there the neighbours stay far apart (large dac)."""
    n = len(pts)
    keep = [True] * n
    gi = [i for i in range(n) if ground[i]]
    for k in range(1, len(gi) - 1):
        a, b, c = gi[k - 1], gi[k], gi[k + 1]
        if pts[c][0] - pts[a][0] > max_gap_s:
            continue
        dp = km_between(pts[a][1], pts[a][2], pts[b][1], pts[b][2]) * 1000
        dn = km_between(pts[b][1], pts[b][2], pts[c][1], pts[c][2]) * 1000
        dac = km_between(pts[a][1], pts[a][2], pts[c][1], pts[c][2]) * 1000
        if max(dp, dn) <= min_step_m:
            continue
        gs = pts[b][4]
        if gs is not None and gs < stop_gs:
            # Parked: neighbours are co-located, so ANY point that sticks out
            # farther than they are apart (dac < max step, i.e. a turn > ~120 deg)
            # is jitter -- the aircraft is not moving at all.
            if dac < max(dp, dn):
                keep[b] = False
        elif max(dp, dn) < jitter_m and dac < frac * max(dp, dn):
            # Moving: only a tight out-and-return wobble; real backtracks/corners
            # (larger, or gentler) are kept.
            keep[b] = False
    return keep


# ---------------------- simplification primitives --------------------------
def dp_horizontal(P, alt, ground, tol):
    n = len(P)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = P[a]
        bx, by = P[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        dmax, idx, tmin = -1.0, -1, float("inf")
        for i in range(a + 1, b):
            px, py = P[i]
            d = abs((px - ax) * dy - (py - ay) * dx) / L
            if d > dmax:
                dmax, idx = d, i
            ti = tol.horiz(alt[i], ground[i])
            if ti < tmin:
                tmin = ti
        if dmax > tmin:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return keep


def dp_vertical(t, y, ground, tol):
    n = len(t)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ta, tb, ya, yb = t[a], t[b], y[a], y[b]
        span = (tb - ta) or 1e-9
        dmax, idx, tmin = -1.0, -1, float("inf")
        for i in range(a + 1, b):
            yi = ya + (yb - ya) * (t[i] - ta) / span
            d = abs(y[i] - yi)
            if d > dmax:
                dmax, idx = d, i
            ti = tol.vert(y[i], ground[i])
            if ti < tmin:
                tmin = ti
        if dmax > tmin:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return keep


# --------------------------- serialisation ---------------------------------
def _uv(v, out):                         # unsigned varint
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break


def _sv(v, out):                         # zig-zag signed varint
    _uv((v << 1) ^ (v >> 63), out)


def _rd_uv(buf, pos):
    shift = res = 0
    while True:
        b = buf[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, pos
        shift += 7


def _rd_sv(buf, pos):
    v, pos = _rd_uv(buf, pos)
    return (v >> 1) ^ -(v & 1), pos


def encode(kept_pts):
    """kept_pts: list of (t_int, lat_i, lon_i, alt_ft). Returns gzipped blob."""
    out = bytearray()
    pt = plat = plon = pal = 0
    for (ti, la, lo, al) in kept_pts:
        _sv(ti - pt, out)
        _sv(la - plat, out)
        _sv(lo - plon, out)
        _sv(al - pal, out)
        pt, plat, plon, pal = ti, la, lo, al
    return gzip.compress(bytes(out), 9)


def decode(blob, base_ts):
    """Inverse of encode; returns list of (unix_time, lat, lon, alt_ft)."""
    buf = gzip.decompress(blob)
    pos = 0
    pt = plat = plon = pal = 0
    out = []
    n = len(buf)
    while pos < n:
        d, pos = _rd_sv(buf, pos); pt += d
        d, pos = _rd_sv(buf, pos); plat += d
        d, pos = _rd_sv(buf, pos); plon += d
        d, pos = _rd_sv(buf, pos); pal += d
        out.append((base_ts + pt, plat / LAT_SCALE, plon / LAT_SCALE, pal))
    return out


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


def reconstruct_errors(P, keep, alt, ground, t, mode):
    ki = [i for i in range(len(P)) if keep[i]]
    Pk = [P[i] for i in ki]
    tk = [t[i] for i in ki]
    m = len(ki)
    errs, phases = [], []
    j = 0
    for i in range(len(P)):
        while j + 1 < m - 1 and ki[j + 1] <= i:
            j += 1
        if j + 1 >= m:
            pos = Pk[j]
        else:
            a, b = ki[j], ki[j + 1]
            span = tk[j + 1] - tk[j]
            u = 0.0 if span <= 0 else max(0.0, min(1.0, (t[i] - tk[j]) / span))
            if mode == "linear":
                pos = _lerp(Pk[j], Pk[j + 1], u)
            else:
                span_low = (ground[a] or alt[a] < LOW_PHASE_FT or
                            ground[b] or alt[b] < LOW_PHASE_FT)
                if mode == "segment" and not span_low:
                    pos = _lerp(Pk[j], Pk[j + 1], u)
                else:
                    p0 = Pk[j - 1] if j - 1 >= 0 else Pk[j]
                    p3 = Pk[j + 2] if j + 2 < m else Pk[j + 1]
                    pos = cr_point(p0, Pk[j], Pk[j + 1], p3, u)
        errs.append(math.hypot(pos[0] - P[i][0], pos[1] - P[i][1]))
        phases.append("ground" if ground[i] else
                      ("low" if alt[i] < LOW_PHASE_FT else "high"))
    return errs, phases


# ------------------------------ per file -----------------------------------
def simplify(path, tol, max_kmh, max_jump_km,
             denoise=False, gs_stat=2.0, radius_m=20.0, elev_fn=None,
             max_kmh_low=555.0, jump_low_km=0.25):
    """Run gate + optional ground denoise + graduated DP; return
    (meta, orig_bytes, kept) where kept is a list of (t, lat_i, lon_i, alt, gnd).
    If elev_fn is given, on-ground altitude is imputed from the nearest airport's
    elevation (denoised, flat) instead of NULL."""
    d = load_trace(path)
    raw = d.get("trace", [])
    n_raw = len(raw)
    if n_raw < 2:
        return None
    gmask = gate(raw, max_kmh, max_jump_km, max_kmh_low, jump_low_km)
    pts = [raw[i] for i in range(n_raw) if gmask[i]]
    if denoise:
        pts = denoise_ground(pts, gs_stat, radius_m)
    n = len(pts)
    if n < 2:
        return None
    raw_ground = [p[3] == "ground" for p in pts]

    def _num(v):
        return int(v) if isinstance(v, (int, float)) else None

    # geometric (GPS/MSL) altitude idx 10; barometric (pressure) altitude idx 3
    geom_raw = [(_num(pts[i][10]) if len(pts[i]) > 10 else None) for i in range(n)]
    baro_raw = [(None if raw_ground[i] else _num(pts[i][3])) for i in range(n)]
    tt = [p[0] for p in pts]
    # Reconcile the two datums so the whole leg shares ONE reference (geometric).
    # Barometric here is pressure altitude (1013 hPa); it diverges from true MSL
    # with height, so a SINGLE median offset over-corrects near the ground -- it
    # would push a liftoff baro sample (~0) up by the cruise-scale offset into a
    # spurious spike. Instead take the LOCAL offset (geom - baro) wherever both
    # exist and interpolate it across the gaps, so each baro-only sample gets the
    # offset appropriate to its altitude. Continuous with field elevation, every
    # airport, no hard-coded table.
    off = [(geom_raw[i] - baro_raw[i])
           if (geom_raw[i] is not None and baro_raw[i] is not None) else None
           for i in range(n)]
    off = _interp_fill(off, tt)

    def air_alt(i):
        if geom_raw[i] is not None:
            return geom_raw[i]
        if baro_raw[i] is not None:
            return int(round(baro_raw[i] + off[i]))
        return None

    baro = [None if raw_ground[i] else air_alt(i) for i in range(n)]
    ground = list(raw_ground)
    alt_store = [None if raw_ground[i] else baro[i] for i in range(n)]
    if elev_fn is not None:
        # A point near a known airport that is slow AND low is TAXIING, whatever
        # the on_ground flag says. Snap it to field elevation (kills baro dropouts
        # and the pressure-altitude offset), and mark it on-ground.
        for i in range(n):
            e = elev_fn(pts[i][1], pts[i][2])
            if e is None:
                continue
            gs = pts[i][4]
            surface = raw_ground[i] or (gs is not None and gs < GS_TAXI_KT and
                                        (baro[i] is None or baro[i] < e + SURFACE_AGL_FT))
            if surface:
                ground[i] = True
                alt_store[i] = e
    t = [p[0] for p in pts]
    alt_store = despike_alt(alt_store, ground, t)  # repair lone vertical glitches
    alt = [a if a is not None else 0 for a in alt_store]  # numeric, for DP
    lat0 = sum(p[1] for p in pts) / n
    mlon = 111320.0 * math.cos(math.radians(lat0))
    P = [(p[2] * mlon, p[1] * 111320.0) for p in pts]
    kh = dp_horizontal(P, alt, ground, tol)
    kv = dp_vertical(t, alt, ground, tol)
    keep = [x or y for x, y in zip(kh, kv)]
    # Force-drop on-ground reversal spikes (taxi/parked GPS jitter). DP otherwise
    # KEEPS them precisely because a wobble is a large horizontal deviation.
    dg = deglitch_ground(pts, ground)
    keep = [k and dg[i] for i, k in enumerate(keep)]
    keep[0] = keep[n - 1] = True
    # Second despike pass, now on the DECIMATED series: raw glitch CLUSTERS (2-3
    # consecutive bad samples) survive the pre-DP pass because their neighbours
    # are also bad, but DP collapses each cluster to one representative point --
    # a lone spike the same test can now catch.
    ki = [i for i in range(n) if keep[i]]
    ka = [alt_store[i] for i in ki]
    kg = [ground[i] for i in ki]
    kt = [t[i] for i in ki]
    despike_alt(ka, kg, kt)
    for j, i in enumerate(ki):
        alt_store[i] = ka[j]
    # Post-decimation ground reversal pass: DP can connect two kept fixes across a
    # gap so the KEPT sequence forms a reversal the dense path did not. Re-run the
    # taxi deglitch on just the kept points (mirrors the post-DP alt despike).
    ki2 = [i for i in range(n) if keep[i]]
    dg2 = deglitch_ground([pts[i] for i in ki2], [ground[i] for i in ki2])
    for j, i in enumerate(ki2):
        if not dg2[j]:
            keep[i] = False
    keep[0] = keep[n - 1] = True
    kept = [(int(round(pts[i][0])), int(round(pts[i][1] * LAT_SCALE)),
             int(round(pts[i][2] * LAT_SCALE)), alt_store[i], ground[i])
            for i in range(n) if keep[i]]
    # Drop duplicate timestamps: the ground<->air transition can sample one second
    # twice (an on-ground and an airborne fix at the same t). A replay can't
    # interpolate a zero time-gap (0/0 -> NaN, the dot teleports). t is
    # non-decreasing here, so same-t points are adjacent; keep the last per second.
    dedup = []
    for row in kept:
        if dedup and dedup[-1][0] == row[0]:
            dedup[-1] = row
        else:
            dedup.append(row)
    kept = dedup
    meta = {"icao": d.get("icao"), "reg": d.get("r"), "type": d.get("t"),
            "desc": d.get("desc"), "base_ts": d.get("timestamp", 0),
            "n_orig": n_raw, "n_kept": len(kept), "n_rejected": n_raw - n}
    return meta, os.path.getsize(path), kept


def process(path, tol, mode, max_kmh, max_jump_km):
    d = load_trace(path)
    raw = d.get("trace", [])
    n_raw = len(raw)
    if n_raw < 2:
        return None
    gmask = gate(raw, max_kmh, max_jump_km)
    pts = [raw[i] for i in range(n_raw) if gmask[i]]
    n = len(pts)
    rejected = n_raw - n
    if n < 2:
        return None
    orig_bytes = os.path.getsize(path)

    ground = [p[3] == "ground" for p in pts]
    alt = [0 if (p[3] == "ground" or p[3] is None) else int(p[3]) for p in pts]
    lat0 = sum(p[1] for p in pts) / n
    mlat = 111320.0
    mlon = mlat * math.cos(math.radians(lat0))
    P = [(p[2] * mlon, p[1] * mlat) for p in pts]
    t = [p[0] for p in pts]

    kh = dp_horizontal(P, alt, ground, tol)
    kv = dp_vertical(t, alt, ground, tol)
    keep = [x or y for x, y in zip(kh, kv)]
    kept = sum(keep)

    base_ts = d.get("timestamp", 0)
    kept_pts = [(int(round(pts[i][0])), int(round(pts[i][1] * LAT_SCALE)),
                 int(round(pts[i][2] * LAT_SCALE)), alt[i])
                for i in range(n) if keep[i]]
    blob = encode(kept_pts)
    errs, phases = reconstruct_errors(P, keep, alt, ground, t, mode)

    def bucket(name):
        e = [errs[i] for i in range(n) if phases[i] == name]
        if not e:
            return (0, 0.0, 0.0)
        e.sort()
        return (len(e), e[int(0.95 * (len(e) - 1))], e[-1])

    meta = {"icao": d.get("icao"), "reg": d.get("r"), "type": d.get("t"),
            "desc": d.get("desc"), "base_ts": base_ts, "scale": LAT_SCALE,
            "n_orig": n_raw, "n_kept": kept, "n_rejected": rejected}
    row = {"icao": d.get("icao"), "type": d.get("t"), "points": n_raw,
           "kept": kept, "rejected": rejected, "keep_pct": 100 * kept / n_raw,
           "orig_gz": orig_bytes, "comp": len(blob), "ratio": orig_bytes / len(blob),
           "grd": bucket("ground"), "low": bucket("low"), "high": bucket("high")}
    return row, blob, meta


# ------------------------------ archive I/O --------------------------------
def write_blob(dirpath, meta, blob):
    p = os.path.join(dirpath, f"{meta['icao']}.tz")
    with open(p, "wb") as f:
        f.write(MAGIC)
        f.write(blob)
    return p


def read_blob(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:4] == MAGIC, "bad magic"
    return data[4:]


# --------------------------------- main ------------------------------------
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


def write_parquet(files, tol, a):
    """Decimate every trace and write ONE Parquet file of points, sorted by
    (icao, t) so row-group min/max stats prune single-aircraft lookups without
    physical partitioning:
       DIR/traces.parquet     points   [icao, t, lat, lon, alt]   (t = s past base_ts)
       DIR/aircraft.parquet   per-aircraft metadata (reg, type, desc, base_ts)
    With --partition-by date, one traces file per UTC date is written instead.
    Points stream into Arrow record batches (int32, ~4 B/val), so the 78k-aircraft
    archive holds ~0.5 GB before the final sort+write."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    import datetime

    os.makedirs(a.parquet, exist_ok=True)
    denorm = getattr(a, "denormalize", False)
    fields = [("icao", pa.string()), ("t", pa.int32()), ("lat", pa.int32()),
              ("lon", pa.int32()), ("alt", pa.int32()), ("on_ground", pa.bool_())]
    dicts = ["icao"]
    if denorm:  # fold per-aircraft metadata into every point row (self-contained file)
        fields += [("reg", pa.string()), ("type", pa.string()),
                   ("desc", pa.string()), ("base_ts", pa.int64())]
        dicts += ["reg", "type", "desc"]
    schema = pa.schema(fields)
    COLS = tuple(f[0] for f in fields)
    wopts = dict(compression="zstd", version="2.6", use_dictionary=dicts,
                 column_encoding={"t": "DELTA_BINARY_PACKED", "lat": "DELTA_BINARY_PACKED",
                                  "lon": "DELTA_BINARY_PACKED", "alt": "DELTA_BINARY_PACKED"})
    # optional airport-elevation resolver for on-ground altitude
    elev_fn = None
    if a.ground_elev:
        csvp = a.airports
        if not csvp:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for cand in (os.path.join(root, "airports.csv"), "airports.csv"):
                if os.path.exists(cand):
                    csvp = cand
                    break
        if not csvp:
            sys.exit("--ground-elevation needs airports.csv (pass --airports)")
        print(f"  loading airport elevations from {csvp} ...", flush=True)
        elev_fn = make_elev_resolver(build_airport_index(csvp))

    FLUSH = 500_000
    batches = {}        # partition key -> list of record batches
    buf = {}            # partition key -> column lists
    meta_all = []
    tot_o = tot_pts = tot_rej = 0

    def newbuf():
        return {k: [] for k in COLS}

    def flush(key):
        b = buf[key]
        if not b["t"]:
            return
        batches.setdefault(key, []).append(pa.record_batch(b, schema=schema))
        buf[key] = newbuf()

    def keyfor(base_ts):
        if a.partition_by == "date":
            return datetime.datetime.fromtimestamp(base_ts, datetime.UTC).strftime("%Y-%m-%d")
        return "traces"

    for i, f in enumerate(files):
        try:
            r = simplify(f, tol, a.max_speed, a.max_jump,
                         a.denoise, a.gs_stat, a.radius_m, elev_fn,
                         a.max_speed_low, a.max_jump_low)
        except Exception as e:
            print(f"  skip {os.path.basename(f)}: {e}", file=sys.stderr)
            continue
        if not r:
            continue
        meta, obytes, kept = r
        ic = meta["icao"] or "??????"
        key = keyfor(meta["base_ts"])
        b = buf.setdefault(key, newbuf())
        for (ti, la, lo, al, gnd) in kept:
            b["icao"].append(ic); b["t"].append(ti)
            b["lat"].append(la); b["lon"].append(lo)
            b["alt"].append(al); b["on_ground"].append(gnd)
            if denorm:
                b["reg"].append(meta["reg"]); b["type"].append(meta["type"])
                b["desc"].append(meta["desc"]); b["base_ts"].append(int(meta["base_ts"]))
        tot_o += obytes; tot_pts += meta["n_kept"]; tot_rej += meta["n_rejected"]
        meta_all.append(meta)
        if len(b["t"]) >= FLUSH:
            flush(key)
        if (i + 1) % 5000 == 0:
            print(f"  ... {i+1}/{len(files)} traces, {tot_pts:,} points", flush=True)

    for key in list(buf):
        flush(key)

    pq_bytes = 0
    for key, blist in batches.items():
        tbl = pa.Table.from_batches(blist, schema=schema)
        tbl = tbl.combine_chunks().sort_by([("icao", "ascending"), ("t", "ascending")])
        path = os.path.join(a.parquet, f"{key}.parquet")
        pq.write_table(tbl, path, row_group_size=1_000_000, **wopts)
        pq_bytes += os.path.getsize(path)

    # metadata sidecar (skipped when denormalized -- names are in every row)
    meta_bytes = 0
    if not denorm:
        mt = pa.table({k: [m[k] for m in meta_all] for k in
                       ("icao", "reg", "type", "desc", "base_ts",
                        "n_orig", "n_kept", "n_rejected")})
        mpath = os.path.join(a.parquet, "aircraft.parquet")
        pq.write_table(mt, mpath, compression="zstd",
                       use_dictionary=["icao", "reg", "type", "desc"])
        meta_bytes = os.path.getsize(mpath)

    n_files = len(batches)
    print(f"\nPARQUET -> {a.parquet}   (partition_by={a.partition_by}, "
          f"denormalized={denorm})")
    print(f"  aircraft          : {len(meta_all):,}")
    print(f"  kept points       : {tot_pts:,}")
    print(f"  dropped (gate+denoise): {tot_rej:,}")
    print(f"  traces parquet    : {n_files} file(s)  ({pq_bytes:,} B)")
    if not denorm:
        print(f"  aircraft.parquet  : {meta_bytes:,} B")
    print(f"  source (gz JSON)  : {tot_o:,} B")
    total = pq_bytes + meta_bytes
    if total:
        print(f"  dataset total     : {total:,} B   = {tot_o/total:.1f}x overall")
    return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--max-speed", dest="max_speed", type=float, default=1300.0,
                    help="outlier speed cap at cruise, km/h (>= --cap-hi-ft)")
    ap.add_argument("--max-speed-low", dest="max_speed_low", type=float, default=555.0,
                    help="outlier speed cap near the ground, km/h (<= --cap-lo-ft, "
                         "~300 kt); ramps up to --max-speed with altitude")
    ap.add_argument("--max-jump", dest="max_jump", type=float, default=25.0,
                    help="zig-zag jump cap at cruise, km")
    ap.add_argument("--max-jump-low", dest="max_jump_low", type=float, default=0.25,
                    help="zig-zag jump cap near the ground, km (catches sub-km "
                         "teleport-and-return glitches); ramps up with altitude")
    ap.add_argument("--h-ground", dest="h_ground", type=float, default=10.0)
    ap.add_argument("--h-low", dest="h_low", type=float, default=20.0)
    ap.add_argument("--h-cruise", dest="h_cruise", type=float, default=150.0)
    ap.add_argument("--v-ground", dest="v_ground", type=float, default=25.0)
    ap.add_argument("--v-cruise", dest="v_cruise", type=float, default=250.0)
    ap.add_argument("--low-alt", dest="low_alt", type=float, default=2000.0)
    ap.add_argument("--high-alt", dest="high_alt", type=float, default=12000.0)
    ap.add_argument("--recon", choices=["linear", "catmull", "segment"], default="linear")
    ap.add_argument("--denoise-ground", dest="denoise", action="store_true",
                    help="collapse stationary GPS wander on the ground (uses gs)")
    ap.add_argument("--gs-stationary", dest="gs_stat", type=float, default=2.0,
                    help="groundspeed (kt) below which a ground fix is 'parked'")
    ap.add_argument("--stationary-radius", dest="radius_m", type=float, default=20.0,
                    help="max spread (m) of a parked run to collapse it")
    ap.add_argument("--ground-elevation", dest="ground_elev", action="store_true",
                    help="impute on-ground altitude from nearest airport (airports.csv)")
    ap.add_argument("--airports", default=None,
                    help="OurAirports airports.csv for --ground-elevation "
                         "(auto-detected next to this script if omitted)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--parquet", default=None,
                    help="write a single sorted Parquet dataset into this dir")
    ap.add_argument("--partition-by", dest="partition_by",
                    choices=["none", "date"], default="none",
                    help="none = one traces.parquet; date = one file per UTC date")
    ap.add_argument("--denormalize", action="store_true",
                    help="fold reg/type/desc/base_ts into every point row and skip "
                         "aircraft.parquet (one self-contained file, ~same size)")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    tol = Tol(a)
    files = collect(a.paths, a.limit)

    if a.parquet:
        return write_parquet(files, tol, a)

    if a.out:
        os.makedirs(a.out, exist_ok=True)

    rows, manifest = [], []
    tot_rej = 0
    for f in files:
        try:
            r = process(f, tol, a.recon, a.max_speed, a.max_jump)
        except Exception as e:
            print(f"  skip {os.path.basename(f)}: {e}", file=sys.stderr)
            continue
        if not r:
            continue
        row, blob, meta = r
        rows.append(row)
        tot_rej += row["rejected"]
        if a.out:
            meta["bytes"] = len(blob) + len(MAGIC)
            write_blob(a.out, meta, blob)
            manifest.append(meta)

    if not rows:
        print("no traces processed")
        return

    tot_o = sum(r["orig_gz"] for r in rows)
    tot_c = sum(r["comp"] + len(MAGIC) for r in rows)

    # ---- convert mode ----
    if a.out:
        mpath = os.path.join(a.out, "manifest.json")
        with open(mpath, "w") as f:
            json.dump({"tolerances": vars(a), "count": len(manifest),
                       "aircraft": manifest}, f)
        man_gz = len(gzip.compress(json.dumps(manifest).encode()))
        print(f"CONVERT -> {a.out}")
        print(f"  aircraft written : {len(manifest)}")
        print(f"  outlier samples dropped : {tot_rej}")
        print(f"  source (gz JSON) : {tot_o:,} B")
        print(f"  blobs            : {tot_c:,} B   ({tot_o/tot_c:.1f}x)")
        print(f"  + manifest (gz)  : {man_gz:,} B")
        total = tot_c + man_gz
        print(f"  archive total    : {total:,} B   = {tot_o/total:.1f}x overall")

        if a.verify:
            random.seed(1)
            sample = random.sample(manifest, min(8, len(manifest)))
            ok = 0
            worst = 0.0
            for m in sample:
                blob = read_blob(os.path.join(a.out, f"{m['icao']}.tz"))
                dec = decode(blob, m["base_ts"])
                assert len(dec) == m["n_kept"], f"{m['icao']} count mismatch"
                # monotone time + sane coords
                for k in range(1, len(dec)):
                    assert dec[k][0] >= dec[k - 1][0] - 1
                    assert -90 <= dec[k][1] <= 90 and -180 <= dec[k][2] <= 180
                ok += 1
            print(f"  verify: {ok}/{len(sample)} blobs round-tripped cleanly "
                  f"(count + monotonic time + coord bounds)")
        return

    # ---- analyse mode ----
    print(f"Processed {len(rows)} traces   recon={a.recon}   gate={a.max_speed:.0f} km/h")
    print(f"horiz tol: ground {tol.hg:.0f}m -> low {tol.hl:.0f}m (<= {tol.lo:.0f}ft) "
          f"-> cruise {tol.hc:.0f}m (>= {tol.hi:.0f}ft)   |  dropped {tot_rej} outliers\n")
    print(f"{'icao':>7} {'type':>5} {'pts':>6} {'rej':>4} {'kept%':>6} {'ratio':>6}  "
          f"{'grd_p95':>7} {'low_p95':>7} {'high_p95':>8}")
    for r in sorted(rows, key=lambda x: -x["ratio"])[:16]:
        print(f"{str(r['icao']):>7} {str(r['type']):>5} {r['points']:>6} "
              f"{r['rejected']:>4} {r['keep_pct']:>5.1f}% {r['ratio']:>5.1f}x  "
              f"{r['grd'][1]:>6.0f}m {r['low'][1]:>6.0f}m {r['high'][1]:>7.0f}m")
    ratios = sorted(r["ratio"] for r in rows)

    def pool(name, idx):
        vals = [r[name][idx] for r in rows if r[name][0] > 0]
        return max(vals) if vals else 0.0

    print("\n--- distribution ---")
    print(f"ratio: min {ratios[0]:.1f}x  median {statistics.median(ratios):.1f}x  "
          f"p90 {ratios[9*len(ratios)//10]:.1f}x  max {ratios[-1]:.1f}x")
    print(f"kept: mean {statistics.mean(r['keep_pct'] for r in rows):.1f}%   "
          f"outliers dropped: {tot_rej}")
    print(f"aggregate: {tot_o:,} B -> {tot_c:,} B  = {tot_o/tot_c:.1f}x")
    print("worst reconstruction error by phase (max over files):")
    print(f"   ground {pool('grd',2):6.0f} m   low {pool('low',2):6.0f} m   "
          f"high {pool('high',2):6.0f} m")


if __name__ == "__main__":
    main()
