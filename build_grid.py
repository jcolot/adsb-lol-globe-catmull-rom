#!/usr/bin/env python3
"""build_grid.py - a day's density grid straight from the raw traces.

This is the SHORTCUT path for backfilling render_video.py over a long span. It
produces the same artifact as `build_hexes.py --grid-out`, and skips everything
between:

    normal   fetch -> fit_spline -> build_legs -> build_hexes (DuckDB + H3)
    this     fetch -> build_grid

The reason it can is that the grid is coarse. At --grid-zoom 2 a pixel is ~20 km
across, and the whole apparatus in between exists to place spline nodes to metre
tolerance and roll H3 cells up a pyramid. None of that survives being binned
into a 20 km pixel, so for this one artifact it is work done and thrown away.
What remains is: read each trace's fixes, walk its path, mark the pixels it
crosses.

The unit is the LEG, matching build_hexes.py. That is not free to get right but
it is cheap: a leg needs no spline fit, only the rule in build_legs.segment --
airborne runs (`on_ground == False`) whose bounding box spans more than 2 km,
with the day split at the temporal midpoint between consecutive flights. Every
input to that rule is already in the raw fix. Counting whole TRACES instead
(one airframe's entire day) was the first thing this script did, and it is
wrong in a way that matters: an airframe flying four legs through the same pixel
counts once, so the measure saturates exactly at hubs and in dense short-haul
regions -- and it is blind to a frequency change, which is precisely the signal
a traffic animation is looking for.

One thing it deliberately does NOT do is bin the raw fixes. Fix density is an
artefact of receiver coverage and of what the aircraft was doing -- a holding
pattern over a well-covered field emits vastly more fixes per km than a cruise
leg over the ocean, so binning fixes would draw a map of the FEEDER NETWORK, not
of traffic. Instead each LEG contributes at most 1 to any pixel, however many
fixes fell in it, which is the pixel-resolution equivalent of the `DISTINCT
(cell, leg)` count that build_hexes.py does in SQL. That keeps the two paths
measuring the same thing, which is what makes them comparable -- see --compare.

Reads either an extracted directory (--traces) or a tar stream on stdin
(--tar-stream), which is how it stays disk-free:

    curl -fsSL "$url_aa" "$url_ab" | python3 build_grid.py --tar-stream \\
        --out grids/2026-09-02.npz
"""
import argparse
import gzip
import json
import math
import os
import sys
import tarfile
import time

import numpy as np

import hex_raster


FLIGHT_MIN_KM = 2.0        # build_legs.FLIGHT_MIN_KM -- keep in step
EARTH_KM = 40075.017       # equatorial circumference
GC_MIN_KM = 25.0           # above this, interpolate a segment as a great circle
# Spreading a coverage gap across a band instead of a hairline. Sigma is a
# fraction of the gap's own arc, because a longer hole means less knowledge.
# Calibrated against the envelope the aircraft was actually somewhere inside: a
# North Atlantic organised track structure spans roughly 7 degrees of latitude,
# about 780 km, so +/-390 km should be the 2-sigma envelope -> sigma ~195 km on
# a typical ~3,000 km ocean gap -> 0.065. Rounded down, because the OTS is the
# outer envelope and most traffic sits nearer its middle.
GAP_BAND_SIGMA_FRAC = 0.06
# ...but ONLY for real coverage holes. Great-circle interpolation is right for
# any segment; band spreading is a statement about not knowing the route, and a
# 100 km gap in covered airspace is not that -- p90 node spacing over land is
# 55-116 km, so 200 km is clear of ordinary sampling and matches
# fit_spline.GC_MAX_GAP_KM. Banding everything over GC_MIN_KM instead smeared
# the continents and lit 2.3x the pixels.
GAP_BAND_MIN_KM = 200.0
# Uncertainty stops growing with gap length once it fills the plausible route
# envelope; past that a wider band says less, not more.
GAP_BAND_SIGMA_MAX_KM = 250.0
GAP_BAND_MEMBERS = 25      # candidate offsets considered per gap
GAP_MODE = "best"          # best | band | arc -- see resolve_gaps


def _fixes(blob):
    """(lat, lon, on_ground) from one trace_full JSON blob, or None if unusable.

    Matches smooth_trace.parse's view of the format: d["trace"] is a list of
    fixes whose [1] and [2] are lat and lon and whose [3] is the altitude or the
    string "ground". The altitudes and speeds are irrelevant at 20 km/px; the
    ground flag is not -- it is what delimits a leg.
    """
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    d = json.loads(blob)
    raw = d.get("trace")
    if not raw or len(raw) < 2:
        return None
    lat = np.fromiter((p[1] for p in raw), np.float64, len(raw))
    lon = np.fromiter((p[2] for p in raw), np.float64, len(raw))
    gnd = np.fromiter((p[3] == "ground" for p in raw), bool, len(raw))
    ok = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90.0) \
        & (np.abs(lon) <= 180.0)
    if ok.sum() < 2:
        return None
    return lat[ok], lon[ok], gnd[ok]


def _unit(lat, lon):
    """Lat/lon in degrees to a unit vector on the sphere."""
    p, l = math.radians(lat), math.radians(lon)
    c = math.cos(p)
    return np.array([c * math.cos(l), c * math.sin(l), math.sin(p)])


def _legs(lat, lon, gnd):
    """Index ranges of this trace's legs, by build_legs.segment's rule.

    Airborne runs that actually go somewhere (>FLIGHT_MIN_KM across the run's
    bounding box) are flights; the day is cut at the midpoint between
    consecutive flights so taxi and parked fixes fall to the adjacent leg. The
    only simplification against build_legs is that the midpoint is taken over
    sample index rather than timestamp -- at 20 km/px the difference is a taxi
    fix landing in one pixel or its neighbour.
    """
    n = len(gnd)
    air = ~gnd
    if not air.any():
        return []
    # run boundaries of the airborne mask
    edge = np.diff(air.astype(np.int8))
    starts = list(np.nonzero(edge == 1)[0] + 1)
    ends = list(np.nonzero(edge == -1)[0])
    if air[0]:
        starts.insert(0, 0)
    if air[-1]:
        ends.append(n - 1)
    flights = []
    for s0, e0 in zip(starts, ends):
        la = lat[s0:e0 + 1]; lo = lon[s0:e0 + 1]
        span = math.hypot((la.max() - la.min()) * 111.32,
                          (lo.max() - lo.min()) * 111.32
                          * math.cos(math.radians(la.min())))
        if span > FLIGHT_MIN_KM:
            flights.append((s0, e0))
    if not flights:
        return []
    bounds = [0]
    for k in range(len(flights) - 1):
        bounds.append((flights[k][1] + flights[k + 1][0]) // 2 + 1)
    bounds.append(n)
    return [(bounds[k], bounds[k + 1] - 1) for k in range(len(flights))]


class Accumulator:
    """Marks the pixels each LEG's path crosses, one vote per leg."""

    def __init__(self, zoom, tile_size, band_members=GAP_BAND_MEMBERS,
                 band_sigma_frac=GAP_BAND_SIGMA_FRAC, gap_mode=GAP_MODE):
        self.side = tile_size << zoom
        self.zoom, self.tile_size = zoom, tile_size
        self.grid = np.zeros((self.side, self.side), np.float32)
        self.traces = self.legs = self.skipped = 0
        self.band_members = band_members
        self.band_sigma_frac = band_sigma_frac
        self.gap_mode = gap_mode
        self.banded = 0
        # Coverage gaps are DEFERRED, not drawn as they are met. In "best" mode
        # the route chosen for a gap depends on where traffic was actually
        # observed, which is only known once the whole day has been read.
        self.gaps = []
        self.gap_chosen = 0
        self.gap_fallback = 0

    def _project(self, lat, lon):
        x = (lon + 180.0) / 360.0 * self.side
        lat = np.clip(lat, -85.051129, 85.051129)
        s = np.sin(np.radians(lat))
        y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * self.side
        return x, y

    def add(self, lat, lon, gnd):
        """Split into legs and mark each one's pixels separately."""
        segs = _legs(lat, lon, gnd)
        if not segs:
            self.skipped += 1
            return
        self.traces += 1
        for i0, i1 in segs:
            if i1 - i0 >= 1:
                self._one(lat[i0:i1 + 1], lon[i0:i1 + 1])

    def _one(self, lat, lon):
        """Mark the pixels one leg crosses.

        Segments come in two very different sizes and get different treatment.
        Ordinary cruise sampling is ~20 km, where a great circle and a straight
        line in pixel space differ by metres, so those are interpolated the
        cheap way. But where the volunteer receiver network has no coverage --
        oceans, Siberia, Greenland -- consecutive fixes can be 3,000 km apart,
        and a straight line in mercator is a RHUMB line: constant bearing, not
        the route an aircraft flies. Those segments are interpolated as great
        circles, which is what the aircraft actually did.

        This does not make the gap observed. It is still interpolation across
        airspace nobody watched; it just no longer draws a path that is wrong
        on its own terms.
        """
        lat = np.asarray(lat, float)
        lon = np.asarray(lon, float)
        x, y = self._project(lat, lon)
        # A segment that jumps more than half the world is the antimeridian
        # wrap, not a flight path; drawing it would put a line across the map.
        keep = np.abs(x[1:] - x[:-1]) < self.side / 2
        if not keep.any():
            return
        seg = np.nonzero(keep)[0]
        km_per_deg = EARTH_KM / 360.0
        # rough chord length, only to decide which branch a segment takes
        dlat = lat[seg + 1] - lat[seg]
        dlon = (lon[seg + 1] - lon[seg]) * np.cos(np.radians(
            (lat[seg] + lat[seg + 1]) / 2.0))
        chord = np.hypot(dlat, dlon) * km_per_deg
        long_i = seg[chord > GC_MIN_KM]
        short_i = seg[chord <= GC_MIN_KM]

        def collapse(pairs, how):
            i = np.concatenate([p[0] for p in pairs])
            w = np.concatenate([p[1] for p in pairs])
            o = np.argsort(i, kind="stable")
            i, w = i[o], w[o]
            f = np.nonzero(np.r_[True, i[1:] != i[:-1]])[0]
            return i[f], how(w, f)

        parts = []
        for i in long_i:
            arc = self._arc_km(lat[i], lon[i], lat[i + 1], lon[i + 1])
            if arc >= GAP_BAND_MIN_KM and self.gap_mode != "arc":
                # defer: resolve_gaps decides this route later
                self.gaps.append((lat[i], lon[i], lat[i + 1], lon[i + 1]))
                continue
            got = self._great_circle(lat[i], lon[i], lat[i + 1], lon[i + 1],
                                     spread=False)
            if got is not None:
                parts.append(got)
        if len(short_i):
            c = self._straight(x[short_i], y[short_i],
                               x[short_i + 1], y[short_i + 1])
            parts.append((np.unique(c), np.ones(len(np.unique(c)), np.float32)))
        if not parts:
            return
        # ...and MAX across every part -- each gap's band, and the observed
        # track -- so the seam where coverage resumes is not brighter than
        # either side of it, and no pixel can exceed one vote for one leg.
        idx, wt = collapse(parts, np.maximum.reduceat)
        self.grid.reshape(-1)[idx] += wt
        self.legs += 1

    def _straight(self, x0, y0, x1, y1):
        """Flat pixel-space interpolation, one sample per pixel of length."""
        n = np.clip(np.ceil(np.hypot(x1 - x0, y1 - y0)), 1, 8000).astype(np.int64)
        idx = np.repeat(np.arange(len(n)), n)
        start = np.concatenate([[0], np.cumsum(n)[:-1]])
        f = (np.arange(int(n.sum())) - start[idx]) / n[idx]
        px = x0[idx] + f * (x1[idx] - x0[idx])
        py = y0[idx] + f * (y1[idx] - y0[idx])
        return (np.clip(py.astype(np.int64), 0, self.side - 1) * self.side
                + np.clip(px.astype(np.int64), 0, self.side - 1))

    def _arc_km(self, la0, lo0, la1, lo1):
        p0, p1 = _unit(la0, lo0), _unit(la1, lo1)
        ang = math.acos(float(np.clip(p0 @ p1, -1.0, 1.0)))
        return ang * EARTH_KM / (2.0 * math.pi)

    def _candidates(self, la0, lo0, la1, lo1, offsets):
        """Pixel sets for each laterally-offset great circle across a gap.

        Offsets rotate about the great circle's own pole and are tapered by
        sin(pi f), so every candidate starts and ends at the two OBSERVED fixes
        and differs only in the middle, where nothing was seen.
        """
        p0, p1 = _unit(la0, lo0), _unit(la1, lo1)
        ang = math.acos(float(np.clip(p0 @ p1, -1.0, 1.0)))
        if ang < 1e-9:
            return None, None
        arc_km = ang * EARTH_KM / (2.0 * math.pi)
        km_px = (EARTH_KM / self.side) * math.cos(math.radians(
            min(85.0, max(abs(la0), abs(la1)))))
        n = int(min(20000, max(2, math.ceil(arc_km / max(km_px, 1e-6)))))
        f = np.linspace(0.0, 1.0, n)
        sa = math.sin(ang)
        base = ((np.sin((1.0 - f) * ang) / sa)[:, None] * p0[None, :]
                + (np.sin(f * ang) / sa)[:, None] * p1[None, :])
        pole = np.cross(p0, p1)
        pn = np.linalg.norm(pole)
        if pn < 1e-12:
            return None, None
        pole = pole / pn
        taper = np.sin(math.pi * f)
        out = []
        for d in offsets:
            dd = d * taper
            pts = base * np.cos(dd)[:, None] + pole[None, :] * np.sin(dd)[:, None]
            got = self._project_cells(pts, 1.0)
            out.append(None if got is None else got[0])
        return out, arc_km

    def resolve_gaps(self, log):
        """Choose one route per coverage gap, from where traffic was OBSERVED.

        The alternative to a blur. A gap has no observations of its own, but the
        corridor it crosses usually does: other legs the same day were seen
        partway across, sparse mid-ocean reception does exist, and both ends of
        every gap are pinned to real fixes. So the observed field -- built in
        pass one from short, covered segments ONLY -- is used to score candidate
        routes, and the best-supported one is drawn as a single crisp line.

        Built from observed segments only, and that is not a detail: score
        against a field that already contains interpolated paths and every gap
        snaps onto the pipeline's own guesses, which then look like evidence.
        The prior term keeps a candidate from chasing a couple of stray pixels
        far off the great circle -- this is a MAP estimate, not maximum
        likelihood, so thin evidence leaves the great circle where it is.
        """
        if not self.gaps:
            return
        mode = self.gap_mode
        # Coarse support field: 4x4 blocks, so a candidate scores for passing
        # NEAR observed traffic rather than exactly through it.
        blk = 4
        c = self.side // blk
        support = self.grid.reshape(c, blk, c, blk).sum(axis=(1, 3))
        support = np.log1p(support)              # a busy corridor, not a hub, wins
        log(f"resolving {len(self.gaps)} coverage gaps against an observed "
            f"field of {int((self.grid > 0).sum())} px ({mode} mode)")

        for la0, lo0, la1, lo1 in self.gaps:
            arc_km = self._arc_km(la0, lo0, la1, lo1)
            sigma_km = min(self.band_sigma_frac * arc_km, GAP_BAND_SIGMA_MAX_KM)
            m = max(3, self.band_members)
            sigma = sigma_km / 6371.0
            offs = np.linspace(-2.0, 2.0, m) * sigma
            cands, _ = self._candidates(la0, lo0, la1, lo1, offs)
            if cands is None:
                continue
            prior = np.exp(-0.5 * (np.linspace(-2.0, 2.0, m)) ** 2)
            if mode == "band":
                w = (prior / prior.sum()).astype(np.float32)
                self._draw_weighted(cands, w)
                self.banded += 1
                continue
            mid = m // 2
            best, best_score, best_i = None, -1.0, mid
            for i, (cells, pw) in enumerate(zip(cands, prior)):
                if cells is None or not len(cells):
                    continue
                ys, xs = np.divmod(cells, self.side)
                mean_support = float(support[ys // blk, xs // blk].mean())
                score = mean_support * float(pw)
                if score > best_score:
                    best_score, best, best_i = score, cells, i
            if best is None:
                continue
            # "Routed on support" means the evidence actually moved the route.
            # Both ends of a gap sit on observed fixes and every candidate
            # passes through them, so a nonzero score proves nothing by itself;
            # only a choice away from the centre candidate does.
            if best_i == mid:
                self.gap_fallback += 1
            else:
                self.gap_chosen += 1
            self.grid.reshape(-1)[best] += 1.0
        log(f"gaps routed on observed support: {self.gap_chosen}, "
            f"fell back to the plain great circle: {self.gap_fallback}")

    def _draw_weighted(self, cands, weights):
        idx, wt = [], []
        for cells, w in zip(cands, weights):
            if cells is None or not len(cells):
                continue
            idx.append(cells); wt.append(np.full(len(cells), w, np.float32))
        if not idx:
            return
        i = np.concatenate(idx); w = np.concatenate(wt)
        o = np.argsort(i, kind="stable")
        i, w = i[o], w[o]
        f = np.nonzero(np.r_[True, i[1:] != i[:-1]])[0]
        self.grid.reshape(-1)[i[f]] += np.add.reduceat(w, f)

    def _great_circle(self, la0, lo0, la1, lo1, spread=True):
        """A coverage gap, drawn as a weighted BAND around the great circle.

        Sample count comes from the arc length divided by the SMALLEST mercator
        pixel on the segment, so the denser end of the path still gets one
        sample per pixel rather than being stepped over.

        The band is the honest part. A great circle is a better guess than a
        rhumb line, but over the North Atlantic aircraft fly an organised track
        structure whose latitude span is ~780 km, chosen daily from the jet
        stream -- information this data does not contain. Drawing one hairline
        asserts a precision of a couple of pixels where the real uncertainty is
        seventy. So the leg's weight is spread across a Gaussian band of
        offsets, each offset rotated about the great circle's own pole, and the
        band is tapered by sin(pi f) so it pinches to nothing at both ends,
        where the aircraft's position IS observed, and is widest mid-gap where
        nothing is known. The ocean then reads as a diffuse corridor, which is
        what "somewhere in here" looks like.
        """
        p0 = _unit(la0, lo0)
        p1 = _unit(la1, lo1)
        dot = float(np.clip(p0 @ p1, -1.0, 1.0))
        ang = math.acos(dot)
        if ang < 1e-9:
            return None
        arc_km = ang * EARTH_KM / (2.0 * math.pi)
        km_px = (EARTH_KM / self.side) * math.cos(math.radians(
            min(85.0, max(abs(la0), abs(la1)))))
        n = int(min(20000, max(2, math.ceil(arc_km / max(km_px, 1e-6)))))
        f = np.linspace(0.0, 1.0, n)
        sa = math.sin(ang)
        base = ((np.sin((1.0 - f) * ang) / sa)[:, None] * p0[None, :]
                + (np.sin(f * ang) / sa)[:, None] * p1[None, :])

        offs, weights = self._band(arc_km) if spread else (None, None)
        if offs is None:
            return self._project_cells(base, 1.0)
        # rotate about the great circle's pole: p cos(d) + pole sin(d)
        pole = np.cross(p0, p1)
        pn = np.linalg.norm(pole)
        if pn < 1e-12:
            return self._project_cells(base, 1.0)
        pole = pole / pn
        taper = np.sin(math.pi * f)            # 0 at both observed ends
        self.banded += 1
        cid, cw = [], []
        for d, w in zip(offs, weights):
            dd = d * taper
            pts = base * np.cos(dd)[:, None] + pole[None, :] * np.sin(dd)[:, None]
            got = self._project_cells(pts, float(w))
            if got is not None:
                cid.append(got[0]); cw.append(got[1])
        if not cid:
            return None
        # Sum ACROSS THIS GAP'S MEMBERS only -- they are disjoint samples of one
        # distribution totalling one vote. Summing across different gaps would
        # let two gaps of the same leg crossing one pixel reach 2.0, which
        # breaks the one-vote-per-pixel-per-leg rule the observed track obeys.
        i = np.concatenate(cid)
        w = np.concatenate(cw)
        o = np.argsort(i, kind="stable")
        i, w = i[o], w[o]
        f = np.nonzero(np.r_[True, i[1:] != i[:-1]])[0]
        return i[f], np.add.reduceat(w, f)

    def _band(self, arc_km):
        """Gaussian offsets (radians) and their normalised weights."""
        m = self.band_members
        if m < 3 or self.band_sigma_frac <= 0 or arc_km < GAP_BAND_MIN_KM:
            return None, None
        sigma_km = min(self.band_sigma_frac * arc_km, GAP_BAND_SIGMA_MAX_KM)
        if sigma_km <= 0:
            return None, None
        sigma = sigma_km / 6371.0                       # km -> radians
        d = np.linspace(-2.0, 2.0, m) * sigma           # +/- 2 sigma
        w = np.exp(-0.5 * (d / sigma) ** 2)
        # Normalised to SUM to one, not to peak at one. The band redistributes
        # the leg's single vote sideways; it must not mint new ones. Peaking at
        # 1.0 instead made every band worth ~14 votes and turned the oceans
        # brighter than the continents -- the opposite of the intent.
        return d, (w / w.sum()).astype(np.float32)

    def _project_cells(self, pts, weight):
        """Unit vectors -> DISTINCT pixel indices, at one constant weight.

        Distinct, because consecutive samples of the same path land in the same
        pixel and a path must count once per pixel however densely it was
        sampled -- the same rule the observed track obeys.
        """
        lat = np.degrees(np.arcsin(np.clip(pts[:, 2], -1.0, 1.0)))
        lon = np.degrees(np.arctan2(pts[:, 1], pts[:, 0]))
        x, y = self._project(lat, lon)
        jump = np.nonzero(np.abs(np.diff(x)) > self.side / 2)[0]
        if len(jump):
            ok = np.ones(len(x), bool)
            ok[jump + 1] = False
            x, y = x[ok], y[ok]
        if not len(x):
            return None
        cells = np.unique(np.clip(y.astype(np.int64), 0, self.side - 1) * self.side
                          + np.clip(x.astype(np.int64), 0, self.side - 1))
        return cells, np.full(len(cells), weight, np.float32)


def from_tar_stream(acc, stream, log):
    """Stream a (possibly multi-part concatenated) tar without staging it."""
    n = 0
    with tarfile.open(fileobj=stream, mode="r|*") as tf:
        for member in tf:
            if not member.isfile() or "trace_full_" not in member.name:
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            try:
                got = _fixes(f.read())
            except Exception:
                acc.skipped += 1
                continue
            if got is None:
                acc.skipped += 1
                continue
            acc.add(*got)
            n += 1
            if n % 20000 == 0:
                log(f"{n} traces, {acc.legs} legs, "
                    f"{int((acc.grid > 0).sum())} px lit")
    return n


def from_dir(acc, root, log):
    n = 0
    for dirpath, _, names in os.walk(root):
        for name in names:
            if "trace_full_" not in name:
                continue
            try:
                with open(os.path.join(dirpath, name), "rb") as f:
                    got = _fixes(f.read())
            except Exception:
                acc.skipped += 1
                continue
            if got is None:
                acc.skipped += 1
                continue
            acc.add(*got)
            n += 1
            if n % 20000 == 0:
                log(f"{n} traces, {acc.legs} legs, "
                    f"{int((acc.grid > 0).sum())} px lit")
    return n


def compare(a_path, b_path, log):
    """Agreement between this path and build_hexes' H3-derived grid.

    They cannot be identical -- one counts distinct H3 res-6 cells per leg and
    the other distinct pixels per trace, and legs are not traces (a trace is a
    whole day of one airframe, which build_legs splits into legs). So the test
    is whether they agree on SHAPE: the same pixels lit, and the same relative
    density. Pearson r on the lit union, plus the pixel-set overlap.
    """
    ga, za, _ = hex_raster.load_grid(a_path)
    gb, zb, _ = hex_raster.load_grid(b_path)
    if ga.shape != gb.shape:
        sys.exit(f"shape mismatch: {ga.shape} (z{za}) vs {gb.shape} (z{zb})")
    a, b = ga.reshape(-1), gb.reshape(-1)
    la, lb = a > 0, b > 0
    both, either = la & lb, la | lb
    log(f"lit: shortcut {int(la.sum())}, reference {int(lb.sum())}, "
        f"overlap {int(both.sum())} = {100*both.sum()/max(1,either.sum()):.1f}% of union")
    if both.sum() > 1:
        r = float(np.corrcoef(a[both], b[both])[0, 1])
        rl = float(np.corrcoef(np.log1p(a[both]), np.log1p(b[both]))[0, 1])
        log(f"correlation on shared pixels: r={r:.4f}, log r={rl:.4f}")
        # what matters for a video is that ratios per region hold, so report the
        # spread of the per-pixel ratio rather than only its centre
        ratio = a[both] / b[both]
        q = np.percentile(ratio, [5, 25, 50, 75, 95])
        log("shortcut/reference ratio p5/p25/p50/p75/p95: "
            + " ".join(f"{v:.3f}" for v in q))
    only_a = int((la & ~lb).sum()); only_b = int((~la & lb).sum())
    log(f"shortcut-only px {only_a} ({100*only_a/max(1,int(la.sum())):.1f}%), "
        f"reference-only px {only_b} ({100*only_b/max(1,int(lb.sum())):.1f}%)")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--traces", help="directory of extracted trace_full_*.json")
    src.add_argument("--tar-stream", action="store_true",
                     help="read the release tar from stdin, staging nothing")
    p.add_argument("--out", help="output .npz (hex_raster.save_grid format)")
    p.add_argument("--grid-zoom", type=int, default=2)
    p.add_argument("--gap-mode", choices=("best", "band", "arc"), default=GAP_MODE,
                   help="how to cross a coverage gap. best: pick the candidate "
                        "route with the most observed traffic support (crisp). "
                        "band: spread one vote across the candidates (blurred). "
                        "arc: plain great circle, no candidates")
    p.add_argument("--gap-band-members", type=int, default=GAP_BAND_MEMBERS,
                   help="how many candidate offsets are considered per gap")
    p.add_argument("--gap-band-sigma-frac", type=float,
                   default=GAP_BAND_SIGMA_FRAC,
                   help="band sigma as a fraction of the gap's arc length")
    p.add_argument("--tile-size", type=int, default=512)
    p.add_argument("--compare", nargs=2, metavar=("SHORTCUT", "REFERENCE"),
                   help="compare two grids instead of building one")
    a = p.parse_args()

    t0 = time.time()
    def log(m): print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)

    if a.compare:
        compare(a.compare[0], a.compare[1], log)
        return
    if not a.out or not (a.traces or a.tar_stream):
        p.error("need --out plus one of --traces / --tar-stream (or --compare)")

    acc = Accumulator(a.grid_zoom, a.tile_size,
                      band_members=a.gap_band_members,
                      band_sigma_frac=a.gap_band_sigma_frac,
                      gap_mode=a.gap_mode)
    if a.tar_stream:
        n = from_tar_stream(acc, sys.stdin.buffer, log)
    else:
        n = from_dir(acc, a.traces, log)
    if not n:
        sys.exit("no usable traces found")
    acc.resolve_gaps(log)
    lit = hex_raster.save_grid(acc.grid, a.grid_zoom, a.tile_size, a.out,
                               producer="build_grid")
    log(f"{n} traces -> {acc.legs} legs ({acc.skipped} never flew), "
        f"{len(acc.gaps)} coverage gaps ({acc.gap_chosen} routed on observed "
        f"support, {acc.gap_fallback} fell back, {acc.banded} banded) -> "
        f"{acc.side}x{acc.side}, {lit} px lit, "
        f"{os.path.getsize(a.out)/1e6:.2f} MB -> {a.out}")


if __name__ == "__main__":
    main()
