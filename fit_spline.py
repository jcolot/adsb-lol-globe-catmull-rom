#!/usr/bin/env python3
"""
fit_spline.py - fit each flight track to a sparse set of centripetal
Catmull-Rom spline nodes with altitude-graduated tolerance.

Fit: per phase-segment (split at sharp taxi corners and ground<->air, marked as
cusps), greedily place interpolation nodes until the centripetal Catmull-Rom curve
through them -- the exact curve the frontend draws -- stays within a per-point
tolerance that ramps from --tol-ground (m) on the surface to --tol-cruise (m) up
high. Only the nodes are stored; no handles or coefficients are needed, because
the curve is fully determined by the points it passes through (build_nodes_cr).

Output (--parquet DIR): nodes.parquet + aircraft.parquet. Columns t (deciseconds),
lat, lon (fixed-point), alt (ft), on_ground, cusp -- DELTA_BINARY_PACKED + zstd.

The Schneider cubic-Bezier fitter (gen_bezier / fit_curve / build_nodes / the
predictive-handle codec) is retained below as reference; it is not on the output
path -- the stored format is Catmull-Rom nodes, not Beziers.

Usage:
    ./fit_spline.py subset_ebbr/traces --ground-elevation --parquet nodes   # batch
    ./fit_spline.py path/to/trace_full_XXXX.json --dump                     # one flight
"""
import argparse, gzip, heapq, math, os, sys, time
import importlib.util
import numpy as np

def _load(mod, path):
    s = importlib.util.spec_from_file_location(mod, path); m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m); return m
HERE = os.path.dirname(os.path.abspath(__file__))
ct = _load("ct", os.path.join(HERE, "compress_trace.py"))
st = _load("st", os.path.join(HERE, "smooth_trace.py"))

FT = 0.3048
Q_POS = 1e5            # deg quantum (~1.1 m lat)
# Above this, a node gap is a coverage hole rather than a tolerance decision, and
# gets filled along the great circle (insert_gc_nodes). 200 km sits clear of
# normal cruise node spacing, which is ~20 km median and 55-116 km at p90 over
# land, so ordinary flight is untouched and only the receiver-less gaps are hit.
GC_MAX_GAP_KM = float(os.environ.get("GC_MAX_GAP_KM", "200"))
Q_HAND = 0.5           # handle quantum, metres
Q_ALT = 25            # ft

# ---------------- vector ops ----------------
def sub(a, b): return (a[0]-b[0], a[1]-b[1])
def add(a, b): return (a[0]+b[0], a[1]+b[1])
def mul(a, k): return (a[0]*k, a[1]*k)
def dot(a, b): return a[0]*b[0]+a[1]*b[1]
def length(a): return math.hypot(a[0], a[1])
def norm(a):
    l = length(a) or 1.0
    return (a[0]/l, a[1]/l)

# ---------------- Schneider fit (Graphics Gems) ----------------
def _B(u):
    t = 1-u
    return (t*t*t, 3*u*t*t, 3*u*u*t, u*u*u)
def bez(b, u):
    c = _B(u)
    return (b[0][0]*c[0]+b[1][0]*c[1]+b[2][0]*c[2]+b[3][0]*c[3],
            b[0][1]*c[0]+b[1][1]*c[1]+b[2][1]*c[2]+b[3][1]*c[3])
def chord_param(pts):
    u = [0.0]
    for i in range(1, len(pts)):
        u.append(u[-1] + length(sub(pts[i], pts[i-1])))
    T = u[-1] or 1.0
    return [x/T for x in u]
def gen_bezier(pts, u, t1, t2):
    n = len(pts); p0, pn = pts[0], pts[-1]
    C00=C01=C11=X0=X1=0.0
    for i in range(n):
        c = _B(u[i]); a0 = mul(t1, c[1]); a1 = mul(t2, c[2])
        C00 += dot(a0, a0); C01 += dot(a0, a1); C11 += dot(a1, a1)
        tmp = sub(pts[i], (p0[0]*(c[0]+c[1])+pn[0]*(c[2]+c[3]),
                           p0[1]*(c[0]+c[1])+pn[1]*(c[2]+c[3])))
        X0 += dot(a0, tmp); X1 += dot(a1, tmp)
    det = C00*C11 - C01*C01; segL = length(sub(pn, p0))
    aL = 0.0 if det == 0 else (X0*C11 - C01*X1)/det
    aR = 0.0 if det == 0 else (C00*X1 - C01*X0)/det
    if aL < 1e-6*segL or aR < 1e-6*segL:
        d = segL/3.0
        return [p0, add(p0, mul(t1, d)), add(pn, mul(t2, d)), pn]
    return [p0, add(p0, mul(t1, aL)), add(pn, mul(t2, aR)), pn]
def max_error(pts, b, u, tolp):
    mx = -1e18; split = len(pts)//2
    for i in range(1, len(pts)-1):
        d = length(sub(bez(b, u[i]), pts[i]))
        ex = d - tolp[i]
        if ex > mx: mx = ex; split = i
    return mx, split
def reparam(pts, b, u):
    out = []
    for i, ui in enumerate(u):
        d = sub(bez(b, ui), pts[i])
        t = 1-ui
        d1 = add(mul(sub(b[1], b[0]), 3*t*t), add(mul(sub(b[2], b[1]), 6*t*ui),
                 mul(sub(b[3], b[2]), 3*ui*ui)))
        d2 = add(mul(add(sub(b[2], mul(b[1], 2)), b[0]), 6*t),
                 mul(add(sub(b[3], mul(b[2], 2)), b[1]), 6*ui))
        num = dot(d, d1); den = dot(d1, d1) + dot(d, d2)
        out.append(ui if den == 0 else ui - num/den)
    return out
def fit_cubic(pts, tolp, t1, t2, depth=0):
    if len(pts) < 2: return []
    if len(pts) == 2:
        d = length(sub(pts[1], pts[0]))/3.0
        return [[pts[0], add(pts[0], mul(t1, d)), add(pts[1], mul(t2, d)), pts[1]]]
    u = chord_param(pts); b = gen_bezier(pts, u, t1, t2)
    mx, split = max_error(pts, b, u, tolp)
    if mx <= 0: return [b]
    for _ in range(5):
        up = reparam(pts, b, u); b2 = gen_bezier(pts, up, t1, t2)
        mx2, sp2 = max_error(pts, b2, up, tolp)
        if mx2 <= 0: return [b2]
        if mx2 < mx: b, u, mx, split = b2, up, mx2, sp2
        else: break
    if depth > 300 or split <= 0 or split >= len(pts)-1:
        return [b]
    ct_ = norm(sub(pts[split-1], pts[split+1]))
    return (fit_cubic(pts[:split+1], tolp[:split+1], t1, ct_, depth+1) +
            fit_cubic(pts[split:], tolp[split:], mul(ct_, -1), t2, depth+1))
def fit_curve(pts, tolp):
    if len(pts) < 2: return []
    return fit_cubic(pts, tolp, norm(sub(pts[1], pts[0])),
                     norm(sub(pts[-2], pts[-1])), 0)

# ---------------- tolerance ramp ----------------
def tol_at(alt, tg, tc, lo=500.0, hi=12000.0):
    if alt is None or alt <= lo: return tg
    if alt >= hi: return tc
    return tg + (tc-tg)*(alt-lo)/(hi-lo)

# ---------------- centripetal Catmull-Rom (matches the frontend renderer) ----
def _cr(p0, p1, p2, p3, u):
    def tj(t, a, b):
        return t + max(math.hypot(b[0]-a[0], b[1]-a[1]), 1e-9)**0.5
    t0 = 0.0; t1 = tj(t0, p0, p1); t2 = tj(t1, p1, p2); t3 = tj(t2, p2, p3)
    if not (t1 > t0 and t2 > t1 and t3 > t2):
        return (p1[0]+(p2[0]-p1[0])*u, p1[1]+(p2[1]-p1[1])*u)
    t = t1+(t2-t1)*u
    def L(a, b, ta, tb):
        w = (t-ta)/(tb-ta); return (a[0]+(b[0]-a[0])*w, a[1]+(b[1]-a[1])*w)
    A1 = L(p0, p1, t0, t1); A2 = L(p1, p2, t1, t2); A3 = L(p2, p3, t2, t3)
    B1 = L(A1, A2, t0, t2); B2 = L(A2, A3, t1, t3)
    return L(B1, B2, t1, t2)

def cr_polyline(P, sub=12):
    if len(P) < 2: return list(P)
    out = []
    for i in range(len(P)-1):
        p0 = P[i-1] if i > 0 else P[i]; p3 = P[i+2] if i+2 < len(P) else P[i+1]
        for k in range(sub):
            out.append(_cr(p0, P[i], P[i+1], p3, k/sub))
    out.append(P[-1])
    return out

def _span_worst(P, TL, ctrl, L, R, sub):
    """Worst (dist-tol) of raw points strictly between kept nodes L and R, measured
    against ONLY this span's centripetal-CR curve. `ctrl`=(P0,P1,P2,P3) are the four
    control points (P1=P[L], P2=P[R]; P0/P3 the neighbour nodes). Vectorized over
    all interior points at once. Returns (ex, wi) with wi a pts index, or (-inf,-1)
    when the span has no interior points."""
    if R - L <= 1:
        return -1e18, -1
    P0, P1, P2, P3 = ctrl
    poly = [_cr(P0, P1, P2, P3, k/sub) for k in range(sub)]
    poly.append((P2[0], P2[1]))
    poly = np.asarray(poly)
    ax = poly[:-1, 0]; ay = poly[:-1, 1]
    dx = poly[1:, 0]-ax; dy = poly[1:, 1]-ay
    L2 = dx*dx+dy*dy; L2[L2 == 0] = 1e-9
    Q = P[L+1:R]                                   # (m,2) interior raw points
    X = Q[:, 0:1]; Y = Q[:, 1:2]                    # (m,1) broadcast vs (s,) segments
    t = np.clip(((X-ax)*dx+(Y-ay)*dy)/L2, 0.0, 1.0)
    cx = ax+t*dx; cy = ay+t*dy
    d = np.sqrt(np.min((X-cx)**2+(Y-cy)**2, axis=1))  # (m,) nearest dist per point
    ex = d - TL[L+1:R]
    j = int(np.argmax(ex))
    return float(ex[j]), L+1+j

def fit_cr_segment(pts, tolp, sub=12):
    """Greedy node placement so centripetal-CR through the kept nodes stays within
    the per-point tolerance. Returns kept indices (incl. both ends).

    Incremental: kept nodes form a linked list (nxt/prv); each inter-node span's
    worst error lives in a lazy max-heap. Inserting the worst point splits one span
    and only recomputes the two halves plus the two adjacent spans (a centripetal-CR
    span depends on one neighbour node on each side) -- near-linear, vs the old
    O(K^2 n) full-polyline rebuild. Stale heap entries are skipped via an adjacency
    + value check."""
    n = len(pts)
    if n <= 2: return list(range(n))
    P = np.asarray(pts, dtype=float)
    TL = np.asarray(tolp, dtype=float)
    nxt = {0: n-1}; prv = {n-1: 0}
    worst = {}; heap = []

    def ctrl_of(L, R):
        p0 = P[prv[L]] if L in prv else P[L]
        p3 = P[nxt[R]] if R in nxt else P[R]
        return (p0, P[L], P[R], p3)

    def refresh(L, R):
        ex, wi = _span_worst(P, TL, ctrl_of(L, R), L, R, sub)
        worst[(L, R)] = ex
        if wi >= 0:
            heapq.heappush(heap, (-ex, L, R, wi))

    refresh(0, n-1)
    while heap:
        negex, L, R, wi = heapq.heappop(heap)
        if nxt.get(L) != R or worst.get((L, R)) != -negex:
            continue                               # stale (span split or recomputed)
        if -negex <= 0:
            break
        nxt[L] = wi; prv[wi] = L; nxt[wi] = R; prv[R] = wi   # splice in
        del worst[(L, R)]
        refresh(L, wi); refresh(wi, R)             # two new spans
        if L in prv: refresh(prv[L], L)            # left neighbour's p3 changed
        if R in nxt: refresh(R, nxt[R])            # right neighbour's p0 changed

    out = [0]; c = 0
    while c in nxt:
        c = nxt[c]; out.append(c)
    return out

# ---------------- build nodes ----------------
def build_nodes(d, elev_fn, tg, tc, corner_deg):
    ms = st.parse(d)
    n0 = len(ms)
    if n0 < 3: return None
    # altitude datum (geometric preferred, baro+local offset fallback), ground snap
    geom = [m["geom"] for m in ms]
    baro = [m["baro"] if not m["gnd"] else None for m in ms]
    offk = [(geom[i]-baro[i]) if (geom[i] is not None and baro[i] is not None) else None
            for i in range(n0)]
    off = ct._interp_fill(offk, [m["t"] for m in ms])
    lat0 = sum(m["lat"] for m in ms)/n0; kx = math.cos(math.radians(lat0))
    P=[]; G=[]; A=[]
    for i, m in enumerate(ms):
        w = (m["lon"]*kx*111320.0, m["lat"]*111320.0)
        if P and length(sub(w, P[-1])) <= 1.5:      # dedup stationary
            continue
        if elev_fn is not None and m["gnd"]:
            e = elev_fn(m["lat"], m["lon"]); a = e if e is not None else geom[i]
        elif geom[i] is not None: a = geom[i]
        elif baro[i] is not None: a = baro[i]+off[i]
        else: a = None
        P.append(w); G.append(m["gnd"]); A.append(a)
    if len(P) < 3: return None
    T=[]
    # rebuild T parallel to the deduped P (same filter as above)
    Pchk=[]; T=[]
    for i, m in enumerate(ms):
        w = (m["lon"]*kx*111320.0, m["lat"]*111320.0)
        if Pchk and length(sub(w, Pchk[-1])) <= 1.5:
            continue
        Pchk.append(w); T.append(int(round(m["t"])))
    # corners
    corners=[0]
    for i in range(1, len(P)-1):
        a = norm(sub(P[i], P[i-1])); b = norm(sub(P[i+1], P[i]))
        ang = math.degrees(math.acos(max(-1, min(1, dot(a, b)))))
        if ang > corner_deg or G[i] != G[i-1]: corners.append(i)
    corners.append(len(P)-1)
    TP = [tol_at(a, tg, tc) for a in A]
    bs=[]; cuspset=set()
    for c in range(len(corners)-1):
        piece = P[corners[c]:corners[c+1]+1]; tp = TP[corners[c]:corners[c+1]+1]
        if len(piece) < 2: continue
        pb = fit_curve(piece, tp)
        if bs: cuspset.add(len(bs))
        bs += pb
    if not bs: return None
    # nearest-ground/alt lookup for nodes
    def near(w):
        bd=1e18; g=False; a=None
        for i in range(len(P)):
            dd = length(sub(w, P[i]))
            if dd < bd: bd=dd; g=G[i]; a=A[i]
        return g, a
    nodes=[dict(p=bs[0][0], hin=None, hout=bs[0][1], cusp=True)]
    for s in range(len(bs)):
        last = s == len(bs)-1
        nodes.append(dict(p=bs[s][3], hin=bs[s][2],
                          hout=None if last else bs[s+1][1],
                          cusp=True if last else (s+1 in cuspset)))
    for nd in nodes:
        g, a = near(nd["p"]); nd["gnd"]=g; nd["alt"]=a if a is not None else 0
    # assign each node a timestamp by forward-matching to the nearest raw point
    # in path order (node endpoints coincide with raw points; keep t monotone)
    ptr = 0
    for nd in nodes:
        best = ptr; bd = 1e18
        for k in range(ptr, len(Pchk)):
            dd = length(sub(nd["p"], Pchk[k]))
            if dd < bd: bd = dd; best = k
            if dd > bd + 200 and k > best + 3:   # moved away; stop scanning
                break
        nd["t"] = T[best]; ptr = best
    return dict(nodes=nodes, lat0=lat0, kx=kx, n_raw=n0, n_used=len(P))

# ---------------- codec ----------------
def _uv(v, out):
    while True:
        b = v & 0x7F; v >>= 7
        out.append(b | 0x80 if v else b)
        if not v: break
def _sv(v, out): _uv((v << 1) ^ (v >> 63), out)

def encode(fit):
    """Naive codec: store both handle offsets explicitly (delta+varint+gzip)."""
    nodes = fit["nodes"]; kx = fit["kx"]
    out = bytearray()
    _uv(len(nodes), out)
    bits = bytearray((len(nodes)+7)//8)
    for i, nd in enumerate(nodes):
        if nd["cusp"]: bits[i>>3] |= 1 << (i & 7)
    out += bits
    plat = plon = palt = 0
    for nd in nodes:
        la = int(round((nd["p"][1]/111320.0)*Q_POS))
        lo = int(round((nd["p"][0]/(kx*111320.0))*Q_POS))
        al = int(round(nd["alt"]/Q_ALT))
        _sv(la-plat, out); _sv(lo-plon, out); _sv(al-palt, out)
        plat, plon, palt = la, lo, al
        for h in (nd["hin"], nd["hout"]):
            if h is None:
                _uv(0, out)
            else:
                dx = int(round((h[0]-nd["p"][0])/Q_HAND))
                dy = int(round((h[1]-nd["p"][1])/Q_HAND))
                _uv(1, out); _sv(dx, out); _sv(dy, out)
    return gzip.compress(bytes(out), 9)


def encode_pred(fit):
    """'Right' codec: PREDICT each handle from the node's neighbours (a
    Catmull-Rom-style tangent, length = 1/3 of the adjacent chord) and store only
    the quantized RESIDUAL. On a smooth cruise/approach node the fitted handle
    almost equals the prediction, so the residual is (0,0) -> 1 byte. Positions
    delta+zig-zag varint as before. This is predictive coding on the handles."""
    nodes = fit["nodes"]; kx = fit["kx"]
    out = bytearray()
    _uv(len(nodes), out)
    bits = bytearray((len(nodes)+7)//8)
    for i, nd in enumerate(nodes):
        if nd["cusp"]: bits[i>>3] |= 1 << (i & 7)
    out += bits
    plat = plon = palt = 0
    for i, nd in enumerate(nodes):
        p = nd["p"]
        la = int(round((p[1]/111320.0)*Q_POS)); lo = int(round((p[0]/(kx*111320.0))*Q_POS))
        al = int(round(nd["alt"]/Q_ALT))
        _sv(la-plat, out); _sv(lo-plon, out); _sv(al-palt, out)
        plat, plon, palt = la, lo, al
        prev = nodes[i-1]["p"] if i > 0 else None
        nxt = nodes[i+1]["p"] if i+1 < len(nodes) else None
        # tangent prediction from neighbours (centripetal-ish direction)
        if prev is not None and nxt is not None:
            tdir = norm(sub(nxt, prev))
        elif nxt is not None:
            tdir = norm(sub(nxt, p))
        elif prev is not None:
            tdir = norm(sub(p, prev))
        else:
            tdir = (0.0, 0.0)
        for h, other in ((nd["hin"], prev), (nd["hout"], nxt)):
            if h is None:
                _uv(0, out); continue
            L = length(sub(other, p))/3.0 if other is not None else length(sub(h, p))
            sgn = -1.0 if h is nd["hin"] else 1.0
            pred = add(p, mul(tdir, sgn*L))                 # predicted handle
            rx = int(round((h[0]-pred[0])/Q_HAND)); ry = int(round((h[1]-pred[1])/Q_HAND))
            if rx == 0 and ry == 0:
                _uv(1, out)                                 # 1 = handle == prediction
            else:
                _uv(2, out); _sv(rx, out); _sv(ry, out)     # 2 = prediction + residual
    return gzip.compress(bytes(out), 9)

def _decimate_1hz(ms, min_dt=1.0):
    """Thin to <=1 sample/s in 1 s windows, collapsing each window to the mean of
    BOTH position and time (not snapped to a grid label). readsb sub-second
    samples that would otherwise round onto the same output tick -- and rewind
    between them, making an approach zigzag -- become one honest centroid at their
    true mean time. Node t is stored to 0.1 s (see light_columns), so two distinct
    windows can never round back into a duplicate tick; monotonic time is
    guaranteed because windows advance strictly. Also shrinks the fit's input."""
    if not ms: return ms
    out = []; i = 0; n = len(ms)
    while i < n:
        t0 = ms[i]["t"]; j = i + 1
        while j < n and ms[j]["t"] < t0 + min_dt: j += 1
        g = ms[i:j]
        if len(g) == 1:
            out.append(dict(g[0])); i = j; continue
        def mean(k):
            v = [x[k] for x in g if x[k] is not None]
            return sum(v)/len(v) if v else None
        gnd = sum(1 for x in g if x["gnd"]) * 2 >= len(g)
        last = g[-1]
        out.append(dict(t=mean("t"), lat=mean("lat"), lon=mean("lon"), gnd=gnd,
                        baro=mean("baro"), geom=mean("geom"), gs=mean("gs"),
                        trk=last["trk"], vr=mean("vr"), src=last["src"]))
        i = j
    return out


def _snap_stationary(ms, stop_kt=3.0, dwell_s=20.0, bbox_m=40.0):
    """Reclassify stationary points as on-ground, and collapse a genuine dwell to its
    centroid. A transponder parked at a gate sometimes drops the on-ground bit while
    stopped (gs~0), so its geom altitude (GPS, ~100 ft off field elevation) is kept
    and its position jitter is drawn -- a scribble hovering above the stand.
    Groundspeed < stop_kt is unambiguously on the surface (fixed wing can't fly that
    slow), so gnd=True is always set -> altitude drops to field elevation. Position
    is snapped to the centroid when the run is a real dwell (lasts >= dwell_s -- a
    taxiing aircraft never sustains < stop_kt that long, even while drifting on GPS)
    or is spatially tight; the >=1 m dedup then keeps just the dwell's endpoints. A
    brief low-speed taxi wobble (short and not tight) keeps its positions."""
    if not ms: return ms
    def stat(m): return m["gs"] is not None and m["gs"] < stop_kt
    i = 0; n = len(ms)
    while i < n:
        if not stat(ms[i]): i += 1; continue
        j = i
        while j < n and stat(ms[j]): j += 1
        run = ms[i:j]
        lats = [m["lat"] for m in run]; lons = [m["lon"] for m in run]
        kx = math.cos(math.radians(sum(lats)/len(run)))
        dm = max((max(lats)-min(lats))*111320.0, (max(lons)-min(lons))*111320.0*kx)
        collapse = (run[-1]["t"]-run[0]["t"] >= dwell_s) or (len(run) >= 3 and dm < bbox_m)
        clat = sum(lats)/len(run); clon = sum(lons)/len(run)
        for m in run:
            m["gnd"] = True
            if collapse: m["lat"] = clat; m["lon"] = clon
        i = j
    return ms


def insert_gc_nodes(nodes, kx, max_gap_km=200.0):
    """Fill long node gaps with points along the GREAT CIRCLE.

    A gap of hundreds or thousands of km between consecutive nodes is not a
    tolerance decision -- it is the volunteer receiver network having no
    coverage there. Over the open North Atlantic the median gap is ~2,600 km;
    over land it is ~20 km. Nothing constrains the curve across such a gap, and
    what gets drawn is whatever the reconstruction happens to do: a
    centripetal-CR span between two nodes 3,000 km apart, or (in a pixel-space
    densifier) a rhumb line of constant bearing. Neither is the route flown.

    So the gap is filled with nodes on the great circle between its ends. This
    does NOT make the airspace observed -- it is still interpolation across a
    hole -- but the interpolation is at least the path an aircraft would fly,
    and every consumer inherits it: the frontend's spline, the H3 hexbins, the
    raster, the bundle. Fixing it here rather than in each densifier is the
    difference between one change and four.

    Nodes carry positions in the local equirectangular frame that
    build_nodes_cr set up (x = lon * kx * 111320, y = lat * 111320), so the
    conversion back to lat/lon is exact for this purpose.
    """
    if len(nodes) < 2:
        return nodes
    M = 111320.0
    out = [nodes[0]]
    for a, b in zip(nodes, nodes[1:]):
        la0, lo0 = a["p"][1] / M, a["p"][0] / (kx * M)
        la1, lo1 = b["p"][1] / M, b["p"][0] / (kx * M)
        p0 = _sphere(la0, lo0)
        p1 = _sphere(la1, lo1)
        d = max(-1.0, min(1.0, sum(u * v for u, v in zip(p0, p1))))
        ang = math.acos(d)
        arc = ang * 6371.0
        if arc > max_gap_km and ang > 1e-9:
            k = int(arc // max_gap_km)
            sa = math.sin(ang)
            for j in range(1, k + 1):
                f = j / (k + 1.0)
                w0 = math.sin((1.0 - f) * ang) / sa
                w1 = math.sin(f * ang) / sa
                v = [w0 * p0[i] + w1 * p1[i] for i in range(3)]
                nl = math.hypot(math.hypot(v[0], v[1]), v[2])
                v = [c / nl for c in v]
                la = math.degrees(math.asin(max(-1.0, min(1.0, v[2]))))
                lo = math.degrees(math.atan2(v[1], v[0]))
                out.append(dict(p=(lo * kx * M, la * M), gi=a["gi"],
                                alt=a["alt"] + (b["alt"] - a["alt"]) * f,
                                gnd=False,
                                t=a["t"] + (b["t"] - a["t"]) * f,
                                cusp=False, gc=True))
        out.append(b)
    return out


def _sphere(lat, lon):
    p, l = math.radians(lat), math.radians(lon)
    c = math.cos(p)
    return (c * math.cos(l), c * math.sin(l), math.sin(p))


def build_nodes_cr(d, elev_fn, tg, tc, corner_deg):
    """Node placement whose centripetal-CR reconstruction (what the frontend
    draws) stays within the graduated tolerance -- so NO handles need storing.
    Splits at sharp corners / ground<->air (cusps), greedy-inserts within each."""
    ms = st.parse(d)
    ms = _decimate_1hz(ms); ms = _snap_stationary(ms); n0 = len(ms)
    if n0 < 3: return None
    geom = [m["geom"] for m in ms]
    baro = [m["baro"] if not m["gnd"] else None for m in ms]
    offk = [(geom[i]-baro[i]) if (geom[i] is not None and baro[i] is not None) else None
            for i in range(n0)]
    off = ct._interp_fill(offk, [m["t"] for m in ms])
    lat0 = sum(m["lat"] for m in ms)/n0; kx = math.cos(math.radians(lat0))
    P=[]; G=[]; A=[]; T=[]; GE=[]
    for i, m in enumerate(ms):
        w = (m["lon"]*kx*111320.0, m["lat"]*111320.0)
        if P and length(sub(w, P[-1])) <= 1.0:
            continue
        e = elev_fn(m["lat"], m["lon"]) if (elev_fn is not None and m["gnd"]) else None
        if e is not None: a = e
        elif geom[i] is not None: a = geom[i]
        elif baro[i] is not None: a = baro[i]+off[i]
        else: a = None
        P.append(w); G.append(m["gnd"]); A.append(a if a is not None else 0)
        T.append(float(m["t"])); GE.append(e)
    if len(P) < 3: return None
    # ground altitude: hold ONE field elevation per contiguous on-ground run. On the
    # ground baro/geom are unavailable, so elev_fn is the sole source -- and resolving
    # per point makes it flip between neighbouring airport records (e.g. two EBBR
    # entries at 175 vs 184 ft) as the aircraft taxis, a saw-tooth with no physical
    # basis (ADS-B carries no ground-slope data). Pin the run's dominant elevation.
    i = 0; N = len(P)
    while i < N:
        if not G[i] or GE[i] is None: i += 1; continue
        j = i
        while j < N and G[j]: j += 1
        cnt = {}
        for k in range(i, j):
            if GE[k] is not None: cnt[GE[k]] = cnt.get(GE[k], 0) + 1
        if cnt:
            ev = max(cnt.items(), key=lambda kv: kv[1])[0]
            for k in range(i, j):
                if GE[k] is not None: A[k] = ev
        i = j
    corners=[0]
    for i in range(1, len(P)-1):
        a = norm(sub(P[i], P[i-1])); b = norm(sub(P[i+1], P[i]))
        ang = math.degrees(math.acos(max(-1, min(1, dot(a, b)))))
        if ang > corner_deg or G[i] != G[i-1]: corners.append(i)
    corners.append(len(P)-1)
    TP = [tol_at(a, tg, tc) for a in A]
    nodes=[];
    for c in range(len(corners)-1):
        lo, hi = corners[c], corners[c+1]
        seg = P[lo:hi+1]; segtol = TP[lo:hi+1]
        keep = fit_cr_segment(seg, segtol)
        for j, ki in enumerate(keep):
            gi = lo+ki
            if nodes and gi == nodes[-1]["gi"]:      # shared corner node
                if c > 0: nodes[-1]["cusp"] = True
                continue
            nodes.append(dict(p=P[gi], gi=gi, alt=A[gi], gnd=G[gi], t=T[gi],
                              cusp=(j == 0 and c > 0)))
    if len(nodes) < 2: return None
    n_fit = len(nodes)
    nodes = insert_gc_nodes(nodes, kx, GC_MAX_GAP_KM)
    nodes[0]["cusp"] = True; nodes[-1]["cusp"] = True
    return dict(nodes=nodes, lat0=lat0, kx=kx, n_raw=n0, n_used=len(P),
                n_fit=n_fit, n_gc=len(nodes) - n_fit)


def light_columns(fit):
    """positions + cusp only (no handles) -- the CR-reconstruction schema."""
    nodes = fit["nodes"]; kx = fit["kx"]
    C = dict(t=[], lat=[], lon=[], alt=[], on_ground=[], cusp=[])
    for nd in nodes:
        p = nd["p"]
        C["t"].append(int(round(nd["t"]*10)))   # deciseconds (0.1 s resolution)
        C["lat"].append(int(round((p[1]/111320.0)*Q_POS)))
        C["lon"].append(int(round((p[0]/(kx*111320.0))*Q_POS)))
        C["alt"].append(int(round(nd["alt"])))
        C["on_ground"].append(bool(nd["gnd"]))
        C["cusp"].append(bool(nd["cusp"]))
    return C


def pred_columns(fit):
    """Per-node column arrays with PREDICTIVE handle residuals (same prediction as
    encode_pred). Residual columns are mostly zero -> DELTA_BINARY_PACKED+zstd
    packs them to almost nothing. Handles are reconstructed client-side as
    prediction(neighbours) + residual*Q_HAND."""
    nodes = fit["nodes"]; kx = fit["kx"]
    n = len(nodes)
    cols = dict(t=[], lat=[], lon=[], alt=[], on_ground=[], cusp=[],
                has_in=[], has_out=[], hin_dx=[], hin_dy=[], hout_dx=[], hout_dy=[])
    for i, nd in enumerate(nodes):
        p = nd["p"]
        cols["t"].append(int(nd["t"]))
        cols["lat"].append(int(round((p[1]/111320.0)*Q_POS)))
        cols["lon"].append(int(round((p[0]/(kx*111320.0))*Q_POS)))
        cols["alt"].append(int(round(nd["alt"])))
        cols["on_ground"].append(bool(nd["gnd"]))
        cols["cusp"].append(bool(nd["cusp"]))
        prev = nodes[i-1]["p"] if i > 0 else None
        nxt = nodes[i+1]["p"] if i+1 < len(nodes) else None
        if prev is not None and nxt is not None: tdir = norm(sub(nxt, prev))
        elif nxt is not None: tdir = norm(sub(nxt, p))
        elif prev is not None: tdir = norm(sub(p, prev))
        else: tdir = (0.0, 0.0)
        for h, other, hask, dxk, dyk, sgn in (
                (nd["hin"], prev, "has_in", "hin_dx", "hin_dy", -1.0),
                (nd["hout"], nxt, "has_out", "hout_dx", "hout_dy", 1.0)):
            if h is None:
                cols[hask].append(False); cols[dxk].append(0); cols[dyk].append(0)
            else:
                L = length(sub(other, p))/3.0 if other is not None else length(sub(h, p))
                pred = add(p, mul(tdir, sgn*L))
                cols[hask].append(True)
                cols[dxk].append(int(round((h[0]-pred[0])/Q_HAND)))
                cols[dyk].append(int(round((h[1]-pred[1])/Q_HAND)))
    return cols


# ---- multiprocessing worker (globals set by initializer, avoids pickling) ----
_W = {}
def _init(csv_path, ge, tg, tc, corner):
    _W["elev"] = ct.make_elev_resolver(ct.build_airport_index(csv_path)) if ge else None
    _W["tg"], _W["tc"], _W["corner"] = tg, tc, corner

def _work(path):
    try:
        d = ct.load_trace(path)
        fit = build_nodes_cr(d, _W["elev"], _W["tg"], _W["tc"], _W["corner"])
        if not fit:
            return None
        cols = light_columns(fit)
        return dict(icao=d.get("icao") or os.path.basename(path)[11:17],
                    reg=d.get("r"), type=d.get("t"), desc=d.get("desc"),
                    base_ts=int(d.get("timestamp", 0)),
                    n_raw=fit["n_raw"], cols=cols)
    except Exception as e:
        return dict(err=f"{os.path.basename(path)}: {e}")


def write_parquet(files, a):
    import multiprocessing as mp
    import pyarrow as pa, pyarrow.parquet as pq
    csvp = a.airports or os.path.join(HERE, "airports.csv")
    C = dict(icao=[], t=[], lat=[], lon=[], alt=[], on_ground=[], cusp=[])
    meta = dict(icao=[], reg=[], type=[], desc=[], base_ts=[])
    tot_raw = n_ac = 0
    t0 = time.perf_counter()
    ctx = mp.get_context("fork")
    with ctx.Pool(a.workers, initializer=_init,
                  initargs=(csvp, a.ge, a.tg, a.tc, a.corner)) as pool:
        for i, r in enumerate(pool.imap_unordered(_work, files, chunksize=8)):
            if r is None: continue
            if "err" in r:
                print("  skip", r["err"], file=sys.stderr); continue
            c = r["cols"]; m = len(c["t"])
            C["icao"] += [r["icao"]]*m
            for k in ("t", "lat", "lon", "alt", "on_ground", "cusp"):
                C[k] += c[k]
            for k in ("icao", "reg", "type", "desc", "base_ts"):
                meta[k].append(r[k])
            tot_raw += r["n_raw"]; n_ac += 1
            if n_ac % 2000 == 0:
                print(f"  ... {n_ac} aircraft, {len(C['t']):,} nodes", flush=True)
    wall = time.perf_counter()-t0

    os.makedirs(a.parquet, exist_ok=True)
    schema = pa.schema([("icao", pa.string()), ("t", pa.int32()),
        ("lat", pa.int32()), ("lon", pa.int32()), ("alt", pa.int32()),
        ("on_ground", pa.bool_()), ("cusp", pa.bool_())])
    tbl = pa.table({k: C[k] for k in schema.names}, schema=schema
                   ).sort_by([("icao", "ascending"), ("t", "ascending")])
    ppath = os.path.join(a.parquet, "nodes.parquet")
    pq.write_table(tbl, ppath, compression="zstd", version="2.6", use_dictionary=["icao"],
                   column_encoding={c: "DELTA_BINARY_PACKED" for c in ("t", "lat", "lon", "alt")})
    mt = pa.table(meta)
    mpath = os.path.join(a.parquet, "aircraft.parquet")
    pq.write_table(mt, mpath, compression="zstd", use_dictionary=["icao", "reg", "type", "desc"])

    nodes = len(C["t"]); psz = os.path.getsize(ppath)
    msz = os.path.getsize(mpath); src = sum(os.path.getsize(f) for f in files)
    print(f"\nCR-FIT PARQUET -> {a.parquet}  ({n_ac} aircraft, {a.workers} workers)")
    print(f"  raw points     : {tot_raw:,}")
    print(f"  nodes          : {nodes:,}  ({tot_raw/max(nodes,1):.1f}x fewer than raw)")
    print(f"  fit+write time : {wall:.1f}s   ({wall/max(n_ac,1)*78000/60:.1f} min for 78k)")
    print(f"  nodes.parquet  : {psz:,} B  ({psz/max(nodes,1):.1f} B/node, {psz/max(tot_raw,1):.2f} B/raw-pt)")
    print(f"  aircraft.parquet: {msz:,} B")
    print(f"  source (gz)    : {src:,} B   -> {src/max(psz+msz,1):.1f}x overall")


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--tol-ground", dest="tg", type=float, default=2.0)
    ap.add_argument("--tol-cruise", dest="tc", type=float, default=150.0)
    ap.add_argument("--corner", type=float, default=35.0)
    ap.add_argument("--ground-elevation", dest="ge", action="store_true")
    ap.add_argument("--airports", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--parquet", default=None, help="write one nodes.parquet here (parallel)")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    a = ap.parse_args()

    files = ct.collect(a.paths, a.limit)

    if a.parquet:
        return write_parquet(files, a)

    elev_fn = None
    if a.ge:
        csvp = a.airports or os.path.join(HERE, "airports.csv")
        elev_fn = ct.make_elev_resolver(ct.build_airport_index(csvp))

    if a.dump:
        d = ct.load_trace(files[0])
        t0 = time.perf_counter()
        fit = build_nodes(d, elev_fn, a.tg, a.tc, a.corner)
        dt = time.perf_counter()-t0
        blob = encode(fit)
        print(f"{d.get('icao')}: {fit['n_raw']} raw -> {len(fit['nodes'])} nodes")
        print(f"  fit {dt*1000:.1f} ms   blob {len(blob):,} B   "
              f"({len(blob)/len(fit['nodes']):.1f} B/node)")
        return

    tot_raw = tot_nodes = tot_blob = tot_pred = 0
    n_ac = 0; t0 = time.perf_counter()
    per_ms = []
    for f in files:
        try:
            d = ct.load_trace(f)
            ts = time.perf_counter()
            fit = build_nodes(d, elev_fn, a.tg, a.tc, a.corner)
            per_ms.append((time.perf_counter()-ts)*1000)
            if not fit: continue
            blob = encode(fit); pblob = encode_pred(fit)
        except Exception as e:
            print(f"  skip {os.path.basename(f)}: {e}", file=sys.stderr); continue
        tot_raw += fit["n_raw"]; tot_nodes += len(fit["nodes"])
        tot_blob += len(blob); tot_pred += len(pblob)
        n_ac += 1
    wall = time.perf_counter()-t0
    per_ms.sort()
    src = sum(os.path.getsize(f) for f in files)
    print(f"\nSPLINE FIT  ({n_ac} aircraft, tol {a.tg:.0f}m->{a.tc:.0f}m, corner {a.corner:.0f})")
    print(f"  raw points        : {tot_raw:,}")
    print(f"  nodes             : {tot_nodes:,}  ({tot_raw/max(tot_nodes,1):.1f}x fewer than raw)")
    print(f"  fit time          : {wall:.1f}s total, "
          f"{per_ms[len(per_ms)//2]:.1f}ms median/ac, {per_ms[-1]:.1f}ms max")
    print(f"  extrapolated 78k  : {wall/max(n_ac,1)*78000/60:.1f} min single-thread")
    print(f"  naive codec (gz)  : {tot_blob:,} B  ({tot_blob/max(tot_nodes,1):.1f} B/node)")
    print(f"  PRED codec (gz)   : {tot_pred:,} B  ({tot_pred/max(tot_nodes,1):.1f} B/node, "
          f"{tot_pred/max(tot_raw,1):.2f} B/raw-point)")
    print(f"  source (gz JSON)  : {src:,} B   -> naive {src/max(tot_blob,1):.1f}x, "
          f"pred {src/max(tot_pred,1):.1f}x")


if __name__ == "__main__":
    main()
