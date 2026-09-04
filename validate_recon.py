#!/usr/bin/env python3
"""
validate_recon.py - correctness gate for the spline-node output (positions +
cusp, no stored handles). Reconstruct the curve the FRONTEND will draw --
centripetal Catmull-Rom through the nodes, broken at cusp nodes -- and measure
its distance from the RAW points against the graduated tolerance (2 m ground ->
150 m cruise). If reconstruction stays within tolerance, the nodes alone are
enough to draw the track.
"""
import argparse, math, os, sys
import importlib.util
import numpy as np

def _load(m, p):
    s = importlib.util.spec_from_file_location(m, p); x = importlib.util.module_from_spec(s)
    s.loader.exec_module(x); return x
HERE = os.path.dirname(os.path.abspath(__file__))
ct = _load("ct", os.path.join(HERE, "compress_trace.py"))
fb = _load("fb", os.path.join(HERE, "fit_spline.py"))

def tol_at(alt, tg, tc, lo=500.0, hi=12000.0):
    if alt is None or alt <= lo: return tg
    if alt >= hi: return tc
    return tg + (tc-tg)*(alt-lo)/(hi-lo)

def cr_seg(P, sub=16):
    """Dense polyline of a centripetal Catmull-Rom curve through node list P."""
    if len(P) < 2: return list(P)
    out = []
    for i in range(len(P)-1):
        p0 = P[i-1] if i > 0 else P[i]
        p1 = P[i]; p2 = P[i+1]
        p3 = P[i+2] if i+2 < len(P) else P[i+1]
        for k in range(sub):
            out.append(ct.cr_point(p0, p1, p2, p3, k/sub))
    out.append(P[-1])
    return out

def pt_seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx-ax, by-ay
    L2 = dx*dx+dy*dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px-ax)*dx+(py-ay)*dy)/L2))
    cx, cy = ax+t*dx, ay+t*dy
    return math.hypot(px-cx, py-cy)

def nearest_to_polyline(pt, poly, lo, hi):
    px, py = pt; best = 1e18
    for j in range(max(0, lo), min(len(poly)-1, hi)):
        d = pt_seg_dist(px, py, poly[j][0], poly[j][1], poly[j+1][0], poly[j+1][1])
        if d < best: best = d
    return best

def validate_one(d, elev_fn, tg, tc, corner, report_worst=False):
    fit = fb.build_nodes_cr(d, elev_fn, tg, tc, corner)
    if not fit: return None
    nodes = fit["nodes"]; kx = fit["kx"]
    # reconstruct CR polyline, broken at cusp nodes
    poly = []
    seg = []
    for i, nd in enumerate(nodes):
        seg.append(nd)
        if (nd["cusp"] and i > 0) or i == len(nodes)-1:
            poly.extend(cr_seg([n["p"] for n in seg]))
            seg = [nd]
    poly = np.array(poly)                       # (M,2)
    px = poly[:, 0]; py = poly[:, 1]
    # segment endpoints for vectorized point->polyline distance
    ax, ay = px[:-1], py[:-1]; bx, by = px[1:], py[1:]
    dx, dy = bx-ax, by-ay; L2 = dx*dx+dy*dy; L2[L2 == 0] = 1e-9
    errs = []; within = 0; ntot = 0; worst = (0.0, None)
    for p in d.get("trace", []):
        la, lo = p[1], p[2]
        if la is None: continue
        X = lo*kx*111320.0; Y = la*111320.0
        t = np.clip(((X-ax)*dx+(Y-ay)*dy)/L2, 0.0, 1.0)
        cxx = ax+t*dx; cyy = ay+t*dy
        d_ = float(np.sqrt(np.min((X-cxx)**2+(Y-cyy)**2)))
        alt = None if p[3] == "ground" else (p[3] if isinstance(p[3], (int, float)) else None)
        tol = tol_at(alt, tg, tc)
        errs.append(d_); ntot += 1
        if d_ <= tol*1.5 + 1.0: within += 1
        if d_ > worst[0]:
            worst = (d_, (int(p[0]), alt, p[3] == "ground", tol))
    errs = np.array(errs)
    r = dict(icao=d.get("icao"), n=ntot, nodes=len(nodes),
             med=float(np.median(errs)), p95=float(np.percentile(errs, 95)),
             mx=float(errs.max()), within_pct=100.0*within/max(ntot, 1))
    if report_worst: r["worst"] = worst
    return r

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--tol-ground", dest="tg", type=float, default=2.0)
    ap.add_argument("--tol-cruise", dest="tc", type=float, default=150.0)
    ap.add_argument("--corner", type=float, default=35.0)
    ap.add_argument("--ground-elevation", dest="ge", action="store_true")
    ap.add_argument("--airports", default=None)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    elev_fn = None
    if a.ge:
        csvp = a.airports or os.path.join(HERE, "airports.csv")
        elev_fn = ct.make_elev_resolver(ct.build_airport_index(csvp))
    files = ct.collect(a.paths, a.limit)
    rows = []
    for f in files:
        try:
            r = validate_one(ct.load_trace(f), elev_fn, a.tg, a.tc, a.corner,
                             report_worst=(len(files) == 1))
        except Exception as e:
            print(f"  skip {os.path.basename(f)}: {e}", file=sys.stderr); continue
        if r: rows.append(r)
    if not rows:
        print("nothing"); return
    if len(rows) == 1:
        r = rows[0]
        print(f"{r['icao']}: {r['n']} raw pts, {r['nodes']} nodes")
        print(f"  CR-recon error vs raw:  med {r['med']:.1f}m  p95 {r['p95']:.1f}m  max {r['mx']:.1f}m")
        print(f"  within graduated tol:   {r['within_pct']:.1f}%")
        if r.get("worst") and r["worst"][1]:
            t, alt, gnd, tol = r["worst"][1]
            print(f"  worst point: t={t} alt={alt} {'GND' if gnd else 'AIR'} "
                  f"(tol {tol:.0f}m, err {r['worst'][0]:.0f}m)")
        return
    med = np.median([r["med"] for r in rows])
    p95 = np.median([r["p95"] for r in rows])
    mx = np.max([r["mx"] for r in rows])
    win = np.mean([r["within_pct"] for r in rows])
    worst = sorted(rows, key=lambda r: -r["p95"])[:6]
    print(f"CR reconstruction vs raw over {len(rows)} aircraft (tol {a.tg:.0f}->{a.tc:.0f}m):")
    print(f"  median-of-medians {med:.1f}m   median-p95 {p95:.1f}m   worst-max {mx:.0f}m")
    print(f"  mean within-tol   {win:.1f}%")
    print("  worst by p95:")
    for r in worst:
        print(f"    {r['icao']}: p95 {r['p95']:.0f}m max {r['mx']:.0f}m within {r['within_pct']:.0f}% ({r['nodes']} nodes)")

if __name__ == "__main__":
    main()
