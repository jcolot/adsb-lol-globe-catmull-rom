#!/usr/bin/env python3
"""
smooth_trace.py - PROTOTYPE physics-constrained smoother for readsb traces
(design option "B"): a constant-acceleration Kalman filter (forward) + RTS
smoother (backward) that

  * runs in a local ENU metre frame, per aircraft;
  * FUSES every channel as a measurement: position (lat/lon), reported velocity
    (groundspeed + track), vertical rate, and altitude (geometric preferred,
    barometric datum-corrected as fallback);
  * sets measurement noise R PER SOURCE (adsb vs mlat vs tisb) from the trace's
    source field, so noisy MLAT tracks are smoothed hard and clean ADS-B is not;
  * applies a zero-velocity update (ZUPT) when the aircraft is stopped on the
    ground, which pins the position and dissolves parked GPS wander;
  * rejects/downweights outliers Huber-style on each scalar innovation, so a
    glitch that violates inertia cannot pull the estimate.

The constant-acceleration motion model IS the inertia constraint: process noise
is the jerk PSD, tuned to the real maneuver envelope, so the estimate cannot
contain motion the airframe could not produce. Impossible speed jumps, altitude
spikes and taxi reversals all fall out of this one estimator instead of separate
heuristic gates.

Usage:
    ./smooth_trace.py subset_ebbr/traces --out subset_smooth [--ground-elevation]
    ./smooth_trace.py path/to/trace_full_XXXX.json --dump      # print one track
"""
import argparse, math, os, sys
import numpy as np
import importlib.util

# reuse loaders / airport elevation from compress_trace.py
_spec = importlib.util.spec_from_file_location(
    "ct", os.path.join(os.path.dirname(os.path.abspath(__file__)), "compress_trace.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)

FT = 0.3048          # ft -> m
LAT_SCALE = 1e5

# per-axis state: [pos, vel, acc]; full state = East(0:3) North(3:6) Up(6:9)
def _F(dt):
    f = np.array([[1, dt, 0.5 * dt * dt], [0, 1, dt], [0, 0, 1]])
    F = np.zeros((9, 9))
    F[0:3, 0:3] = F[3:6, 3:6] = F[6:9, 6:9] = f
    return F


def _Q(dt, qh, qv):
    # continuous white-noise-jerk process noise per axis
    def blk(q):
        return q * np.array([[dt**5 / 20, dt**4 / 8, dt**3 / 6],
                             [dt**4 / 8,  dt**3 / 3, dt**2 / 2],
                             [dt**3 / 6,  dt**2 / 2, dt]])
    Q = np.zeros((9, 9))
    Q[0:3, 0:3] = blk(qh)
    Q[3:6, 3:6] = blk(qh)
    Q[6:9, 6:9] = blk(qv)
    return Q


def source_noise(src):
    """(pos_sigma_m, vel_sigma_mps, alt_sigma_m) by position source."""
    s = src or ""
    if "mlat" in s:
        return 120.0, 10.0, 60.0
    if "adsb" in s or "adsr" in s:
        return 18.0, 2.5, 15.0
    if "tisb" in s:
        return 60.0, 6.0, 30.0
    return 40.0, 5.0, 25.0


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
        ms.append(dict(t=t, lat=p[1], lon=p[2], gnd=gnd, baro=baro, geom=geom,
                       gs=gs, trk=trk, vr=vr, src=src))
    return ms


def smooth(ms, elev_fn=None, qh=2.0, qv=1.0, qh_gnd=0.05, gate=16.0, gnd_pos_mult=3.0):
    """Forward CA-KF + RTS smoother. Returns list of (t, lat, lon, alt_ft, gnd)."""
    n = len(ms)
    if n < 2:
        return None

    lat0 = sum(m["lat"] for m in ms) / n
    lon0 = sum(m["lon"] for m in ms) / n
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))

    # datum: prefer geometric altitude; interpolate a LOCAL baro->geom offset for
    # baro-only samples (pressure altitude diverges from MSL with height)
    off_known = [(m["geom"] - m["baro"]) if (m["geom"] is not None and m["baro"] is not None)
                 else None for m in ms]
    off = ct._interp_fill(off_known, [m["t"] for m in ms])

    def alt_meas(i):
        m = ms[i]
        if elev_fn is not None and m["gnd"]:
            e = elev_fn(m["lat"], m["lon"])
            if e is not None:
                return e * FT, 5.0                     # field elevation, tight
        if m["geom"] is not None:
            return m["geom"] * FT, None                # source alt sigma
        if m["baro"] is not None:
            return (m["baro"] + off[i]) * FT, None
        return None, None

    # storage for RTS
    xf = [None] * n           # filtered mean
    Pf = [None] * n
    xp = [None] * n           # predicted mean (for smoother)
    Pp = [None] * n
    Fs = [None] * n

    # init from first sample
    x = np.zeros(9)
    x[0] = (ms[0]["lon"] - lon0) * mlon
    x[3] = (ms[0]["lat"] - lat0) * mlat
    a0, _ = alt_meas(0)
    x[6] = a0 if a0 is not None else 0.0
    if not ms[0]["gnd"] and ms[0]["gs"] is not None and ms[0]["trk"] is not None:
        tr = math.radians(ms[0]["trk"]); v = ms[0]["gs"] * 0.514444
        x[1] = v * math.sin(tr); x[4] = v * math.cos(tr)
    P = np.diag([1e4, 1e2, 1e1, 1e4, 1e2, 1e1, 1e4, 1e2, 1e1]).astype(float)

    def scalar_update(x, P, h, z, r):
        """Sequential scalar Kalman update with Huber downweighting."""
        h = np.asarray(h)
        y = z - h @ x
        S = h @ P @ h + r
        d2 = y * y / S
        if d2 > gate:                          # downweight, don't delete
            r = r * (d2 / gate)
            S = h @ P @ h + r
        K = (P @ h) / S
        x = x + K * y
        P = P - np.outer(K, h @ P)
        return x, P

    for i in range(n):
        m = ms[i]
        if i == 0:
            dt = 0.0
            F = np.eye(9)
        else:
            dt = max(1e-3, min(60.0, ms[i]["t"] - ms[i - 1]["t"]))
            F = _F(dt)
            gh = qh_gnd if (m["gnd"] and ms[i - 1]["gnd"]) else qh
            x = F @ x
            P = F @ P @ F.T + _Q(dt, gh, qv)
        xp[i] = x.copy(); Pp[i] = P.copy(); Fs[i] = F

        pos_s, vel_s, alt_s = source_noise(m["src"])
        if m["gnd"]:
            pos_s *= gnd_pos_mult      # trust the stiff ground model over noisy fixes
        # position east/north
        e = (m["lon"] - lon0) * mlon
        nn = (m["lat"] - lat0) * mlat
        x, P = scalar_update(x, P, [1, 0, 0, 0, 0, 0, 0, 0, 0], e, pos_s**2)
        x, P = scalar_update(x, P, [0, 0, 0, 1, 0, 0, 0, 0, 0], nn, pos_s**2)
        # altitude
        az, az_over = alt_meas(i)
        if az is not None:
            r = (az_over if az_over is not None else alt_s)**2
            x, P = scalar_update(x, P, [0, 0, 0, 0, 0, 0, 1, 0, 0], az, r)
        # reported velocity (gs + track) -> ve, vn, but ONLY airborne. On the
        # ground the reported track is the aircraft's NOSE heading, not its
        # direction of travel: during pushback it is ~180 deg off, and at low taxi
        # speed it is just noisy. Using it there injects motion the wrong way and
        # makes the filter scribble. On the ground we trust position + the stiff
        # ground model + ZUPT instead.
        if not m["gnd"] and m["gs"] is not None and m["trk"] is not None:
            tr = math.radians(m["trk"]); v = m["gs"] * 0.514444
            x, P = scalar_update(x, P, [0, 1, 0, 0, 0, 0, 0, 0, 0], v * math.sin(tr), vel_s**2)
            x, P = scalar_update(x, P, [0, 0, 0, 0, 1, 0, 0, 0, 0], v * math.cos(tr), vel_s**2)
        # On the ground, suppress the acceleration state (taxi is ~constant
        # velocity). The CA model's free acceleration otherwise overshoots at
        # every turn / stop-and-go and the RTS pass turns that into a scribble;
        # a soft zero-acceleration pseudo-measurement makes the ground behave
        # like a constant-velocity model, which does not ring.
        if m["gnd"]:
            x, P = scalar_update(x, P, [0, 0, 1, 0, 0, 0, 0, 0, 0], 0.0, 0.15**2)
            x, P = scalar_update(x, P, [0, 0, 0, 0, 0, 1, 0, 0, 0], 0.0, 0.15**2)
        # ZUPT: stopped on the ground -> velocity is zero (pins position)
        if m["gnd"] and (m["gs"] is None or m["gs"] < 1.5):
            x, P = scalar_update(x, P, [0, 1, 0, 0, 0, 0, 0, 0, 0], 0.0, 0.3**2)
            x, P = scalar_update(x, P, [0, 0, 0, 0, 1, 0, 0, 0, 0], 0.0, 0.3**2)
        # vertical rate -> vu
        if m["vr"] is not None:
            x, P = scalar_update(x, P, [0, 0, 0, 0, 0, 0, 0, 1, 0], m["vr"] * FT / 60.0, 2.0**2)

        xf[i] = x.copy(); Pf[i] = P.copy()

    # RTS backward smoother
    xs = [None] * n; Ps = [None] * n
    xs[-1] = xf[-1]; Ps[-1] = Pf[-1]
    for i in range(n - 2, -1, -1):
        Ppn = Pp[i + 1]
        try:
            C = Pf[i] @ Fs[i + 1].T @ np.linalg.inv(Ppn)
        except np.linalg.LinAlgError:
            C = Pf[i] @ Fs[i + 1].T @ np.linalg.pinv(Ppn)
        xs[i] = xf[i] + C @ (xs[i + 1] - xp[i + 1])
        Ps[i] = Pf[i] + C @ (Ps[i + 1] - Ppn) @ C.T

    out = []
    for i in range(n):
        lon = lon0 + xs[i][0] / mlon
        lat = lat0 + xs[i][3] / mlat
        alt_ft = xs[i][6] / FT
        out.append((int(round(ms[i]["t"])), lat, lon, alt_ft, ms[i]["gnd"]))
    return out


def decimate(res, tol):
    """Graduated Douglas-Peucker on the ALREADY-SMOOTH curve, + timestamp dedup.
    No gate/despike/deglitch needed -- the smoother removed the impossibilities."""
    n = len(res)
    if n < 2:
        return res
    ts = [r[0] for r in res]
    lats = [r[1] for r in res]
    lons = [r[2] for r in res]
    alts = [int(round(r[3])) for r in res]
    gnd = [r[4] for r in res]
    lat0 = sum(lats) / n
    mlon = 111320.0 * math.cos(math.radians(lat0))
    P = [(lons[i] * mlon, lats[i] * 111320.0) for i in range(n)]
    kh = ct.dp_horizontal(P, alts, gnd, tol)
    kv = ct.dp_vertical(ts, alts, gnd, tol)
    keep = [x or y for x, y in zip(kh, kv)]
    kept = [(ts[i], lats[i], lons[i], alts[i], gnd[i]) for i in range(n) if keep[i]]
    dedup = []
    for row in kept:
        if dedup and dedup[-1][0] == row[0]:
            dedup[-1] = row
        else:
            dedup.append(row)
    return dedup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", default=None, help="write smoothed traces.parquet here")
    ap.add_argument("--ground-elevation", dest="ge", action="store_true")
    ap.add_argument("--airports", default=None)
    ap.add_argument("--qh", type=float, default=2.0, help="horizontal jerk PSD")
    ap.add_argument("--qv", type=float, default=1.0, help="vertical jerk PSD")
    ap.add_argument("--qh-gnd", dest="qh_gnd", type=float, default=0.05,
                    help="horizontal jerk PSD on the ground (lower = stiffer/smoother taxi)")
    ap.add_argument("--gnd-pos-mult", dest="gnd_pos_mult", type=float, default=3.0,
                    help="inflate on-ground position noise by this (trust model over fixes)")
    ap.add_argument("--dense", action="store_true", help="skip decimation (debug)")
    # graduated DP tolerances (same as compress_trace.py)
    ap.add_argument("--h-ground", dest="h_ground", type=float, default=5.0)
    ap.add_argument("--h-low", dest="h_low", type=float, default=20.0)
    ap.add_argument("--h-cruise", dest="h_cruise", type=float, default=150.0)
    ap.add_argument("--v-ground", dest="v_ground", type=float, default=25.0)
    ap.add_argument("--v-cruise", dest="v_cruise", type=float, default=250.0)
    ap.add_argument("--low-alt", dest="low_alt", type=float, default=2000.0)
    ap.add_argument("--high-alt", dest="high_alt", type=float, default=12000.0)
    ap.add_argument("--dump", action="store_true", help="print one smoothed track")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    tol = ct.Tol(a)

    elev_fn = None
    if a.ge:
        csvp = a.airports
        if not csvp:
            here = os.path.dirname(os.path.abspath(__file__))
            for c in (os.path.join(here, "airports.csv"), "airports.csv"):
                if os.path.exists(c):
                    csvp = c; break
        elev_fn = ct.make_elev_resolver(ct.build_airport_index(csvp))
        print(f"  airport elevations from {csvp}", flush=True)

    files = ct.collect(a.paths, a.limit)

    if a.dump:
        d = ct.load_trace(files[0])
        res = smooth(parse(d, elev_fn), elev_fn, a.qh, a.qv, qh_gnd=a.qh_gnd, gnd_pos_mult=a.gnd_pos_mult)
        for (t, la, lo, al, g) in res[:60]:
            print(f"  t={t} lat={la:.6f} lon={lo:.6f} alt={al:7.1f} {'GND' if g else ''}")
        return

    import pyarrow as pa, pyarrow.parquet as pq
    cols = {c: [] for c in ("icao", "t", "lat", "lon", "alt", "on_ground")}
    meta = {c: [] for c in ("icao", "reg", "type", "desc", "base_ts")}
    done = 0
    for f in files:
        try:
            d = ct.load_trace(f)
            res = smooth(parse(d, elev_fn), elev_fn, a.qh, a.qv, qh_gnd=a.qh_gnd, gnd_pos_mult=a.gnd_pos_mult)
        except Exception as e:
            print(f"  skip {os.path.basename(f)}: {e}", file=sys.stderr); continue
        if not res:
            continue
        if not a.dense:
            res = decimate(res, tol)
        ic = d.get("icao") or os.path.basename(f)[11:17]
        for (t, la, lo, al, g) in res:
            cols["icao"].append(ic); cols["t"].append(t)
            cols["lat"].append(int(round(la * LAT_SCALE)))
            cols["lon"].append(int(round(lo * LAT_SCALE)))
            cols["alt"].append(int(round(al))); cols["on_ground"].append(g)
        meta["icao"].append(ic); meta["reg"].append(d.get("r"))
        meta["type"].append(d.get("t")); meta["desc"].append(d.get("desc"))
        meta["base_ts"].append(int(d.get("timestamp", 0)))
        done += 1
        if done % 50 == 0:
            print(f"  ... {done}/{len(files)}", flush=True)

    os.makedirs(a.out, exist_ok=True)
    tbl = pa.table({"icao": pa.array(cols["icao"]),
                    "t": pa.array(cols["t"], pa.int32()),
                    "lat": pa.array(cols["lat"], pa.int32()),
                    "lon": pa.array(cols["lon"], pa.int32()),
                    "alt": pa.array(cols["alt"], pa.int32()),
                    "on_ground": pa.array(cols["on_ground"], pa.bool_())}
                   ).sort_by([("icao", "ascending"), ("t", "ascending")])
    path = os.path.join(a.out, "traces.parquet")
    pq.write_table(tbl, path, compression="zstd", version="2.6", use_dictionary=["icao"],
                   column_encoding={"t": "DELTA_BINARY_PACKED", "lat": "DELTA_BINARY_PACKED",
                                    "lon": "DELTA_BINARY_PACKED", "alt": "DELTA_BINARY_PACKED"})
    mpath = os.path.join(a.out, "aircraft.parquet")
    pq.write_table(pa.table(meta), mpath, compression="zstd",
                   use_dictionary=["icao", "reg", "type", "desc"])
    print(f"\n{'SMOOTHED (dense)' if a.dense else 'SMOOTHED + DECIMATED'} -> {path}"
          f"\n  {len(cols['t']):,} points, {done} aircraft, {os.path.getsize(path):,} B"
          f"  (+ {mpath})")


if __name__ == "__main__":
    main()
