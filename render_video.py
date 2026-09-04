#!/usr/bin/env python3
"""render_video.py - animate a stack of daily traffic grids.

Input is a directory of `YYYY-MM-DD.npz` files written by hex_raster.save_grid
(`build_hexes.py --grid-out`). Those hold RAW density, which matters: the
`traffic-raster.pmtiles` archives cannot be used for this. Their alpha is
normalised against each day's OWN p99, so a day where a region's traffic
collapses gets scaled back up to look like any other day -- the animation would
normalise away the thing it exists to show.

Two modes, because they answer different questions:

  absolute  each frame is that day's density on ONE fixed divisor for the whole
            run, sequential palette. Honest, and mostly shows the weekly cycle
            and the season.

  anomaly   each frame is that day against its own trailing baseline, diverging
            palette. This is the one that shows events: an airspace closure is a
            hole, a reroute is a bright new corridor beside a dark old one. The
            trailing baseline absorbs seasonality for free.

Four corrections are applied in both modes, and none of them are optional if
the output is going to be believed:

  1. FIXED normalisation across frames (see above).

  2. Incomplete days are dropped and interpolated over. A pipeline run that
     ingested half a day renders as a dark frame, and a dark frame reads as an
     event with nothing to distinguish it from a real one. Three of the 30 days
     in R2 were partial when this was written (0.66x, 0.81x, 0.84x of the local
     median, on days whose neighbours were 0.99x), so this is the common case,
     not a corner. --min-day-frac 0 keeps them.

  3. A centred 7-day rolling mean, because weekday/weekend swing is large enough
     to read as a strobe under any slower signal. --smooth 1 disables it.

  4. Coverage correction. adsb.lol is volunteer-fed and the receiver population
     changes over months, so apparent traffic growth can be feeder growth --
     which looks identical to a real trend. So a reference region assumed to
     have dense, stable coverage (Western Europe by default) has its SLOW trend
     divided out, leaving every frame a statement about traffic relative to that
     region. Only the trend: dividing by the reference's daily total would also
     delete the weekly cycle and any event large enough to move the reference
     itself. --no-coverage-fix turns it off; the frames are then absolute and
     you own the confound.

Also writes `ranking.csv`: the largest regional anomalies over the run, biggest
first. That is a shot list -- it nominates the days and places worth looking at
instead of requiring you to guess them in advance.
"""
import argparse
import csv
import math
import os
import re
import struct
import subprocess
import sys
import zlib

import numpy as np

import hex_raster

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Named boxes, (lat0, lat1, lon0, lon1). Used for the coverage reference and for
# the anomaly ranking. Deliberately coarse -- they are for reading a trend, not
# for measuring an FIR.
REGIONS = {
    "w-europe":      (36.0, 60.0, -10.0, 20.0),
    "e-europe":      (44.0, 60.0, 20.0, 40.0),
    "russia-w":      (50.0, 70.0, 30.0, 60.0),
    "ukraine":       (44.0, 52.5, 22.0, 40.0),
    "turkey":        (36.0, 42.0, 26.0, 45.0),
    "iran":          (25.0, 40.0, 44.0, 63.0),
    "iraq-syria":    (29.0, 37.5, 38.0, 49.0),
    "israel-jordan": (29.0, 33.5, 34.0, 39.5),
    "gulf":          (22.0, 30.0, 45.0, 60.0),
    # the two documented bypasses around a closed Iran/Iraq: north over
    # Turkey/the Caucasus/Central Asia, south over Saudi Arabia. If a decline
    # over the conflict zone is a reroute rather than a cancellation, it has to
    # show up in one of these.
    "saudi":         (16.0, 32.0, 34.0, 56.0),
    "caucasus":      (38.0, 45.0, 40.0, 51.0),
    "central-asia":  (35.0, 47.0, 52.0, 76.0),
    "red-sea":       (12.0, 30.0, 32.0, 44.0),
    "n-atlantic":    (35.0, 65.0, -60.0, -10.0),
    "conus":         (25.0, 49.0, -125.0, -67.0),
    "s-asia":        (6.0, 37.0, 60.0, 90.0),
    "e-asia":        (20.0, 46.0, 100.0, 146.0),
    "se-asia":       (-10.0, 22.0, 95.0, 130.0),
}

# --- palettes -------------------------------------------------------------
# Sequential stops for `absolute`, sampled and interpolated to 256 entries.
# Dark-grounded and roughly perceptually ordered, so a brighter pixel is always
# a busier pixel -- a palette that loops or dips in lightness turns a density
# map into a decoration.
SEQUENTIAL = {
    # near-black -> deep violet -> magenta -> orange -> pale yellow
    "inferno": [(0, 0, 4), (40, 11, 84), (101, 21, 110), (159, 42, 99),
                (212, 72, 66), (245, 125, 21), (250, 193, 39), (252, 255, 164)],
    # the project's own accent, kept for continuity with the map layer
    "amber":   [(8, 11, 15), (48, 26, 12), (109, 50, 12), (176, 87, 20),
                (255, 158, 61), (255, 205, 140), (255, 245, 225)],
    # Strava-ish blue, for when the subject is the network and not the heat
    "ice":     [(3, 6, 14), (10, 32, 66), (16, 68, 122), (30, 116, 165),
                (86, 168, 200), (170, 214, 230), (240, 250, 255)],
}
# Diverging stops for `anomaly`: below baseline -> at baseline -> above.
# The midpoint is the page ground, not white, so "no change" reads as absence.
DIVERGING = {
    "cyan-orange": [(120, 220, 255), (36, 140, 190), (16, 44, 60),
                    (24, 28, 34),
                    (70, 40, 16), (190, 105, 24), (255, 190, 90)],
    "blue-red":    [(140, 190, 255), (48, 96, 190), (20, 32, 60),
                    (24, 26, 30),
                    (70, 24, 28), (190, 48, 44), (255, 170, 140)],
}


def ramp(stops, n=256):
    """Piecewise-linear interpolation of `stops` into an (n, 3) uint8 LUT."""
    stops = np.asarray(stops, np.float32)
    xs = np.linspace(0, 1, len(stops))
    out = np.empty((n, 3), np.float32)
    t = np.linspace(0, 1, n)
    for c in range(3):
        out[:, c] = np.interp(t, xs, stops[:, c])
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


# --- geometry -------------------------------------------------------------
def merc_y(lat, side):
    lat = np.clip(np.asarray(lat, float), -85.051129, 85.051129)
    s = np.sin(np.radians(lat))
    return (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * side


def box_slice(side, lat0, lat1, lon0, lon1):
    """Region box -> array slices on a `side` x `side` mercator grid."""
    y0 = int(merc_y(lat1, side)); y1 = int(merc_y(lat0, side))
    x0 = int((lon0 + 180.0) / 360.0 * side)
    x1 = int((lon1 + 180.0) / 360.0 * side)
    return (slice(max(0, y0), min(side, max(y1, y0 + 1))),
            slice(max(0, x0), min(side, max(x1, x0 + 1))))


def crop_rows(row_weight, side, pad, keep=0.999):
    """Trim the empty polar bands so the frame is filled by map, not by ground.

    Weighted, not a bare "any lit pixel" test: a single polar flight is enough
    to make every row non-empty, which kept 40% of the frame as dead ground.
    So rows are dropped from each end while doing so costs less than
    `1 - keep` of the total density.
    """
    tot = row_weight.sum()
    if tot <= 0:
        return slice(0, side)
    budget = (1.0 - keep) * tot / 2.0
    lo, acc = 0, 0.0
    while lo < side - 1 and acc + row_weight[lo] <= budget:
        acc += row_weight[lo]; lo += 1
    hi, acc = side, 0.0
    while hi > lo + 1 and acc + row_weight[hi - 1] <= budget:
        acc += row_weight[hi - 1]; hi -= 1
    return slice(max(0, lo - pad), min(side, hi + pad))


# --- png ------------------------------------------------------------------
def write_png(img, path):
    h, w = img.shape[:2]
    raw = np.zeros((h, w * 3 + 1), np.uint8)
    raw[:, 1:] = img.reshape(h, w * 3)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
                + chunk(b"IEND", b""))


# A 5x7 bitmap font, so a date can be burned into the frame without dragging in
# a font stack or a rasteriser. Only the glyphs a date and a label need.
GLYPHS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "-": ("00000", "00000", "00000", "01110", "00000", "00000", "00000"),
    " ": ("00000",) * 7,
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10011", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00110", "01100"),
    ":": ("00000", "00110", "00110", "00000", "00110", "00110", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "%": ("11001", "11010", "00010", "00100", "01000", "01011", "10011"),
}


def stamp(img, text, x, y, scale, rgb):
    """Draw `text` at (x, y) in the 5x7 font. Folded to upper case -- there is
    one case in the font, and a missing glyph advances without drawing rather
    than raising, so a caption silently loses characters if this is skipped."""
    for ch in text.upper():
        g = GLYPHS.get(ch)
        if g is None:
            x += 6 * scale
            continue
        for ry, row in enumerate(g):
            for rx, bit in enumerate(row):
                if bit != "1":
                    continue
                y0, x0 = y + ry * scale, x + rx * scale
                if 0 <= y0 < img.shape[0] - scale and 0 <= x0 < img.shape[1] - scale:
                    img[y0:y0 + scale, x0:x0 + scale] = rgb
        x += 6 * scale
    return x


# --- pipeline -------------------------------------------------------------
def load_days(src, start=None, end=None):
    """Read every YYYY-MM-DD.npz in `src` into one (days, side, side) stack."""
    files = []
    for name in sorted(os.listdir(src)):
        m = DATE_RE.search(name)
        if not name.endswith(".npz") or not m:
            continue
        d = m.group(1)
        if (start and d < start) or (end and d > end):
            continue
        files.append((d, os.path.join(src, name)))
    if not files:
        sys.exit(f"no YYYY-MM-DD.npz grids in {src}")
    dates, stack, side, producers = [], [], None, {}
    for d, path in files:
        g, _, _ = hex_raster.load_grid(path)
        if side is None:
            side = g.shape[0]
        elif g.shape[0] != side:
            sys.exit(f"{path}: side {g.shape[0]} != {side}; all days must share a --grid-zoom")
        producers.setdefault(hex_raster.grid_producer(path), []).append(d)
        dates.append(d)
        stack.append(g)
    return dates, np.stack(stack), producers


def fill_missing(dates, stack):
    """Insert a linearly interpolated frame for every absent calendar day, so
    the animation runs at a constant time rate. A gap left as a cut reads as an
    event, which is the one thing this must not invent."""
    import datetime as dt
    have = {d: i for i, d in enumerate(dates)}
    d0 = dt.date.fromisoformat(dates[0])
    d1 = dt.date.fromisoformat(dates[-1])
    span = [(d0 + dt.timedelta(days=k)).isoformat()
            for k in range((d1 - d0).days + 1)]
    out = np.empty((len(span),) + stack.shape[1:], np.float32)
    filled = []
    known = [i for i, d in enumerate(span) if d in have]
    for i, d in enumerate(span):
        if d in have:
            out[i] = stack[have[d]]
            continue
        lo = max([k for k in known if k < i], default=known[0])
        hi = min([k for k in known if k > i], default=known[-1])
        w = 0.5 if hi == lo else (i - lo) / (hi - lo)
        out[i] = (1 - w) * out[lo] if hi == lo else \
                 (1 - w) * stack[have[span[lo]]] + w * stack[have[span[hi]]]
        filled.append(d)
    return span, out, filled


def partial_days(dates, stack, frac, win=29):
    """Days whose total is far below the local rolling median.

    These are almost always an incomplete pipeline run, not a quiet day, and
    they are the single most dangerous input to an animation: a short day
    renders as a dark frame, a dark frame reads as an event, and the viewer has
    no way to tell it from a real one. Measured on the 30 days in R2, 3 of them
    were partial -- at that rate a year of frames would carry dozens of
    fabricated events. So they are treated as MISSING and interpolated over,
    which is honest about knowing nothing rather than inventing a collapse.
    """
    tot = np.array([g.sum() for g in stack], np.float64)
    half = max(1, win // 2)
    med = np.array([np.median(tot[max(0, i - half):i + half + 1])
                    for i in range(len(tot))])
    bad = tot < frac * med
    return [(dates[i], tot[i] / med[i]) for i in np.nonzero(bad)[0]]


def rolling_mean(stack, win):
    """Centred rolling mean over axis 0, shrinking the window at the edges so
    the first and last frames are not darkened by padding that isn't there."""
    if win <= 1:
        return stack
    n = len(stack)
    half = win // 2
    cs = np.concatenate([np.zeros((1,) + stack.shape[1:], np.float64),
                         np.cumsum(stack, axis=0, dtype=np.float64)])
    out = np.empty_like(stack)
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out[i] = ((cs[b] - cs[a]) / (b - a)).astype(np.float32)
    return out


def coverage_scale(stack, side, region, win=28):
    """Per-day multiplier that flattens SLOW drift in the reference region.

    The subtlety that matters: divide by the region's *daily* total and you also
    delete the real global day-to-day signal -- the weekly cycle, and any event
    big enough to move the reference itself. Feeder growth is a months-scale
    trend, so only the trend should be divided out. So the divisor is a rolling
    median of the reference total, and everything faster than `win` survives.
    """
    sl = box_slice(side, *REGIONS[region])
    tot = stack[:, sl[0], sl[1]].sum(axis=(1, 2))
    if not np.all(tot > 0):
        sys.exit(f"reference region {region!r} is empty on some days; pick another")
    half = max(1, win // 2)
    trend = np.array([np.median(tot[max(0, i - half):i + half + 1])
                      for i in range(len(tot))])
    return float(np.median(trend)) / trend


def trailing_baseline(stack, win):
    """Mean of the `win` days before each frame (the first frames use what
    exists). Trailing, not centred: a centred baseline would let an event leak
    backwards and blunt its own onset."""
    n = len(stack)
    cs = np.concatenate([np.zeros((1,) + stack.shape[1:], np.float64),
                         np.cumsum(stack, axis=0, dtype=np.float64)])
    out = np.empty_like(stack)
    for i in range(n):
        a = max(0, i - win)
        b = max(a + 1, i)
        out[i] = ((cs[b] - cs[a]) / (b - a)).astype(np.float32)
    return out


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--grids", required=True,
                   help="directory of YYYY-MM-DD.npz written by --grid-out")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--mode", choices=("absolute", "anomaly"), default="anomaly")
    p.add_argument("--palette", default=None,
                   help=f"absolute: {'/'.join(SEQUENTIAL)}; "
                        f"anomaly: {'/'.join(DIVERGING)}")
    p.add_argument("--start"), p.add_argument("--end")
    p.add_argument("--smooth", type=int, default=7,
                   help="centred rolling-mean window in days; 1 disables it. "
                        "Below 7 the weekday/weekend cycle comes through.")
    p.add_argument("--baseline", type=int, default=28,
                   help="anomaly mode: trailing baseline window in days")
    p.add_argument("--gamma", type=float, default=2.0,
                   help="ramp compression, as in the raster; 1 = linear")
    p.add_argument("--clip", type=float, default=99.5,
                   help="absolute mode: percentile of the WHOLE run that maps "
                        "to the top of the palette. One divisor for every "
                        "frame -- that is the point")
    p.add_argument("--anomaly-clip", type=float, default=2.0,
                   help="anomaly mode: ratio at the ends of the diverging "
                        "palette, e.g. 2 = 2x the baseline saturates")
    p.add_argument("--anomaly-floor", type=float, default=75.0,
                   help="anomaly mode: percentile of lit density at which a "
                        "pixel's change is shown at full strength; fainter "
                        "pixels fade toward neutral so noise cannot shout as "
                        "loudly as signal. 0 disables the weighting")
    p.add_argument("--coverage-ref", default="w-europe", choices=sorted(REGIONS),
                   help="region whose slow trend is flattened to absorb feeder "
                        "coverage drift")
    p.add_argument("--coverage-win", type=int, default=28,
                   help="window for the coverage trend; anything faster than "
                        "this is kept as signal")
    p.add_argument("--no-coverage-fix", action="store_true")
    p.add_argument("--min-day-frac", type=float, default=0.85,
                   help="days totalling below this fraction of the local median "
                        "are treated as incomplete runs and interpolated over; "
                        "0 keeps every day as-is")
    p.add_argument("--hold", type=int, default=2, help="frames per day")
    p.add_argument("--fps", type=int, default=24)
    p.add_argument("--no-encode", action="store_true",
                   help="write the PNG frames but don't call ffmpeg")
    p.add_argument("--no-stamp", action="store_true")
    p.add_argument("--allow-mixed-producers", action="store_true",
                   help="render a stack whose grids came from different code "
                        "paths; refused by default, see the message it prints")
    a = p.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    dates, stack, producers = load_days(a.grids, a.start, a.end)
    side = stack.shape[1]
    print(f"{len(dates)} days, {side}x{side}, {dates[0]} .. {dates[-1]}")

    # build_grid.py reads ~5x higher than build_hexes.py for the same pixel
    # (measured; see hex_raster.grid_producer). Either is fine alone. Spliced
    # into one run, the factor steps at the join -- and a step in a traffic
    # animation is indistinguishable from an event, which is the one failure
    # this tool exists to avoid.
    if len(producers) > 1:
        detail = "; ".join(f"{k}: {len(v)} day(s) ({v[0]}..{v[-1]})"
                           for k, v in sorted(producers.items()))
        msg = (f"grids come from {len(producers)} different producers -- {detail}. "
               "They do not share a scale, so the join would render as a step "
               "change that looks like an event. Rebuild the odd days with the "
               "same path, or pass --allow-mixed-producers if you know why "
               "that is safe here.")
        if not a.allow_mixed_producers:
            sys.exit("REFUSING: " + msg)
        print("WARNING: " + msg)
    else:
        print(f"producer: {next(iter(producers))}")

    if a.min_day_frac > 0:
        bad = partial_days(dates, stack, a.min_day_frac)
        if bad:
            print("DROPPED as incomplete runs (interpolated over): "
                  + ", ".join(f"{d} ({r:.2f}x median)" for d, r in bad))
            drop = {d for d, _ in bad}
            keep = [i for i, d in enumerate(dates) if d not in drop]
            if len(keep) < 2:
                sys.exit("--min-day-frac rejected almost everything; raise it")
            dates = [dates[i] for i in keep]
            stack = stack[keep]

    dates, stack, filled = fill_missing(dates, stack)
    if filled:
        print(f"interpolated {len(filled)} absent day(s): {', '.join(filled)}")

    if not a.no_coverage_fix:
        sc = coverage_scale(stack, side, a.coverage_ref, a.coverage_win)
        print(f"coverage fix vs {a.coverage_ref} trend "
              f"({a.coverage_win}d): x{sc.min():.3f} .. x{sc.max():.3f} "
              f"(drift {100*(sc.max()/sc.min()-1):.1f}% over the run)")
        stack *= sc[:, None, None]

    stack = rolling_mean(stack, a.smooth)

    # Everything below the poles is empty on every day; crop once, using the
    # union so the frame never changes size mid-animation.
    rows = crop_rows(stack.mean(axis=0).sum(axis=1), side, side // 128)
    stack = stack[:, rows, :]

    if a.mode == "absolute":
        lut = ramp(SEQUENTIAL[a.palette or "inferno"])
        lit = stack[stack > 0]
        top = max(float(np.percentile(lit, a.clip)), 1e-9)
        print(f"absolute: one divisor for the run, p{a.clip} = {top:.1f}")
        norm = np.power(np.clip(stack / top, 0, 1), 1.0 / a.gamma)
        frames = (norm * 255.0 + 0.5).astype(np.uint8)
    else:
        lut = ramp(DIVERGING[a.palette or "cyan-orange"])
        base = trailing_baseline(stack, a.baseline)
        # Ratio in log space so a halving and a doubling are equal, opposite
        # excursions. Pixels empty in both are pinned to the midpoint rather
        # than counted as a change.
        eps = max(float(np.percentile(stack[stack > 0], 5)), 1e-6)
        r = np.log2((stack + eps) / (base + eps))
        k = math.log2(max(a.anomaly_clip, 1.0001))
        t = np.clip(r / k, -1, 1)
        dead = (stack <= 0) & (base <= 0)
        t[dead] = 0.0
        floor = 0.0
        if a.anomaly_floor > 0:
            # Weight each pixel's excursion by how much traffic is actually
            # involved, or a near-empty pixel doubling from nothing to nothing
            # reads as loud as a closed corridor. Against max(day, baseline),
            # NOT the day: a closure drives the day to zero, and weighting by
            # the day alone would fade out precisely the event being looked for.
            floor = max(float(np.percentile(stack[stack > 0], a.anomaly_floor)),
                        1e-9)
            t *= np.clip(np.maximum(stack, base) / floor, 0.0, 1.0)
        print(f"anomaly: {a.baseline}-day trailing baseline, "
              f"+/-{a.anomaly_clip}x saturates, eps {eps:.3f}, "
              f"full-strength floor p{a.anomaly_floor} = {floor:.2f}")
        frames = ((t * 0.5 + 0.5) * 255.0 + 0.5).astype(np.uint8)

    h = frames.shape[1]
    scale = max(1, min(3, side // 512))
    paths = []
    for i, d in enumerate(dates):
        img = lut[frames[i]]
        if not a.no_stamp:
            stamp(img, d, 12 * scale, h - 14 * scale, scale, np.uint8([235, 235, 235]))
        path = os.path.join(a.out_dir, f"f{i:05d}.png")
        write_png(img, path)
        paths.append(path)
    print(f"{len(paths)} frames -> {a.out_dir}  ({img.shape[1]}x{img.shape[0]})")

    rank = os.path.join(a.out_dir, "ranking.csv")
    write_ranking(rank, dates, stack, side, rows, a.baseline)
    print(f"shot list -> {rank}")

    if a.no_encode:
        return
    mp4 = os.path.join(a.out_dir, f"traffic-{a.mode}.mp4")
    # yuv420p + even dimensions, or the file won't play in a browser
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-framerate", str(a.fps / max(1, a.hold)),
           "-i", os.path.join(a.out_dir, "f%05d.png"),
           "-vf", f"fps={a.fps},scale=trunc(iw/2)*2:trunc(ih/2)*2",
           "-c:v", "libx264", "-preset", "slow", "-crf", "18",
           "-pix_fmt", "yuv420p", mp4]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print("ffmpeg not found -- frames are written, encode them yourself:\n  "
              + " ".join(cmd))
        return
    print(f"video -> {mp4} ({os.path.getsize(mp4)/1e6:.1f} MB)")


def write_ranking(path, dates, stack, side, rows, baseline):
    """Per-region daily totals against their own trailing baseline, biggest
    deviation first. This is the shot list: it nominates what to look at."""
    out = []
    for name, box in REGIONS.items():
        sl = box_slice(side, *box)
        # the stack is already row-cropped; shift the region's rows to match
        r = slice(max(0, sl[0].start - rows.start), max(0, sl[0].stop - rows.start))
        if r.stop <= r.start:
            continue
        series = stack[:, r, sl[1]].sum(axis=(1, 2))
        if series.max() <= 0:
            continue
        for i in range(len(dates)):
            a0 = max(0, i - baseline)
            if i - a0 < 3:
                continue
            b = series[a0:i].mean()
            if b <= 0:
                continue
            out.append((name, dates[i], series[i], b, series[i] / b - 1.0))
    out.sort(key=lambda r: -abs(r[4]))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["region", "date", "total", "baseline", "pct_change"])
        for name, d, tot, b, pc in out:
            w.writerow([name, d, f"{tot:.1f}", f"{b:.1f}", f"{100*pc:+.1f}"])
    return out


if __name__ == "__main__":
    main()
