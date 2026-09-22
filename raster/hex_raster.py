"""hex_raster.py - raster overview tiles for the H3 traffic layer.

Why a raster at all, when there are already vector hexes: a vector tile has to
keep its feature count sane, so the pyramid coarsens the hexagons as it zooms
out -- and by z2 that means H3 res 0, cells 1100 km across. At that size the
route network is not simplified, it is *gone*: every cell holds a bit of
everything and the level is a flat wash. Meanwhile a 512 px tile at z2 has ~20 km
pixels, which is finer than a res-5 cell. The structure fits in the image even
though it cannot fit in the hexagons.

So the low zooms are rendered from the FINEST H3 level into one pixel grid and
then halved repeatedly, which is how a terrain or imagery pyramid is built --
Mapterhorn's downsampling stage is "half the size to 512 by 512 using 2 by 2
averaging". Averaging with the empty pixels included is the point: it is an
area-average, so a corridor stays bright against empty airspace instead of being
diluted into it.

The value is baked into the PNG's ALPHA channel against a single flat colour, so
the client just adds a raster layer and gets the same composite the vector fill
gave it. Restyling means rebuilding -- MapLibre cannot colour-ramp a raster
source client-side.
"""
import math
import struct
import zlib

import numpy as np

# One flat colour, alpha carries the data. Matches TRAFFIC_COLOR in the
# frontend's trafficLayer.ts -- change both together.
RGB = (255, 158, 61)


def _png(rgba):
    """Encode an (h, w, 4) uint8 array as a PNG. Hand-rolled to keep the
    pipeline's dependencies to numpy + the pmtiles writer."""
    h, w = rgba.shape[:2]
    raw = np.zeros((h, w * 4 + 1), np.uint8)      # +1 for the per-row filter byte
    raw[:, 1:] = rgba.reshape(h, w * 4)           # filter 0 (None) on every row
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
            + chunk(b"IEND", b""))


def _project(lat, lon, n):
    """Web-mercator to pixel coordinates on an n x n grid."""
    x = (lon + 180.0) / 360.0 * n
    lat = np.clip(lat, -85.051129, 85.051129)
    s = np.sin(np.radians(lat))
    y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def save_grid(grid, zoom, tile_size, path, producer="unknown"):
    """Dump one level's RAW density as a sparse npz -- no normalisation, so a
    stack of these is comparable across days. The archive's alpha is not: its
    p99 divisor is per-day, so a quiet day normalises up to look like a busy
    one, which is exactly the signal a multi-day animation is after.

    Sparse because a level is mostly empty: at z2 about 18% of pixels are lit,
    so index+value beats a dense float32 array and stays exact.
    """
    flat = grid.reshape(-1)
    idx = np.nonzero(flat)[0]
    np.savez_compressed(path, idx=idx.astype(np.int64),
                        val=flat[idx].astype(np.float32),
                        side=np.int32(grid.shape[0]), zoom=np.int32(zoom),
                        tile_size=np.int32(tile_size),
                        producer=np.array(producer))
    return len(idx)


def grid_producer(path):
    """Which code path wrote this grid.

    Worth recording because the two producers do NOT agree on scale: measured
    on one full day, build_grid.py reads about 5x higher than build_hexes.py
    for the same pixel (it counts whole traces crossing a 20 km pixel; the
    other averages H3 res-6 cell counts over a 4x4 block of finer pixels).
    Relative density agrees closely -- r=0.94, and the ratio is flat at 4.3-5.7
    across everything but the near-empty fringe -- so either producer makes a
    sound animation on its own. Splicing them into ONE run does not: the factor
    steps at the join, and a step in a traffic animation reads as an event.
    """
    d = np.load(path)
    return str(d["producer"]) if "producer" in d.files else "unknown"


def load_grid(path):
    """Inverse of save_grid. Returns (grid, zoom, tile_size)."""
    d = np.load(path)
    side = int(d["side"])
    g = np.zeros(side * side, np.float32)
    g[d["idx"]] = d["val"]
    return g.reshape(side, side), int(d["zoom"]), int(d["tile_size"])


def build(lat, lon, value, out_path, max_zoom=4, tile_size=512,
          max_alpha=0.85, gamma=2.0, grid_out=None, grid_zoom=2, log=print):
    """Render per-cell `value` at (lat, lon) into a raster PMTiles pyramid.

    Returns a per-level stats list. `max_zoom` is where the grid is built and
    should put roughly one pixel on one source cell -- at tile_size 512, z4 is
    4.9 km/px, which matches H3 res 6 (~6.4 km across).

    `gamma` compresses the ramp: alpha = (v / p99) ** (1 / gamma). 1.0 is linear.

    `grid_out` additionally dumps the raw `grid_zoom` level via save_grid, for
    consumers that need days to be comparable to each other -- see there.
    """
    from pmtiles.tile import Compression, TileType, zxy_to_tileid
    from pmtiles.writer import Writer

    n = tile_size << max_zoom
    x, y = _project(np.asarray(lat, float), np.asarray(lon, float), n)
    xi = np.clip(x.astype(np.int64), 0, n - 1)
    yi = np.clip(y.astype(np.int64), 0, n - 1)

    # Mean per pixel, not sum: cells and pixels are the same size here, so a
    # pixel that happens to catch two centroids is not twice as busy.
    tot = np.zeros((n, n), np.float32)
    cnt = np.zeros((n, n), np.int32)
    np.add.at(tot, (yi, xi), np.asarray(value, np.float32))
    np.add.at(cnt, (yi, xi), 1)
    grid = np.divide(tot, cnt, out=np.zeros_like(tot), where=cnt > 0)
    del tot, cnt
    log(f"raster: {len(xi)} cells -> {n}x{n} grid, "
        f"{int((grid > 0).sum())} pixels lit")

    # Build every level first: PMTiles wants tiles written in ascending tile id,
    # which means z0 before z1, and the coarse levels are derived from the fine.
    levels = [grid]
    for _ in range(max_zoom):
        g = levels[-1]
        # 2x2 mean, zeros included -- this is the area-average
        levels.append(g.reshape(g.shape[0] // 2, 2, g.shape[1] // 2, 2)
                      .mean(axis=(1, 3)))
    levels.reverse()                                   # levels[z] is zoom z

    if grid_out:
        gz = min(max(grid_zoom, 0), max_zoom)
        lit = save_grid(levels[gz], gz, tile_size, grid_out,
                        producer="build_hexes")
        log(f"grid: z{gz} {tile_size << gz}px, {lit} lit px -> {grid_out}")

    rgb = np.array(RGB, np.uint8)
    stats, written = [], 0
    with open(out_path, "wb") as f:
        w = Writer(f)
        for z, g in enumerate(levels):
            lit = g[g > 0]
            # Each level normalises against its own p99: halving the grid halves
            # the peaks too, so one shared divisor would fade the coarse levels
            # out. Clipped at p99 rather than max so a couple of extreme pixels
            # don't push everything else to the bottom of the ramp.
            p99 = float(np.quantile(lit, 0.99)) if lit.size else 1.0
            p99 = max(p99, 1e-9)
            # ...and then compressed, because air traffic is not linear. Measured
            # on a full day, a linear ramp left 58-73% of the lit pixels under
            # alpha 8/255 -- present in the data, invisible on screen, which is
            # exactly the sparse ocean and polar routes the overview exists to
            # show. gamma 2 (a square root) drops that to ~0% past z1 while the
            # hubs still saturate. adsb.exposed does the same thing with a 1/5
            # power, but against the level's MAX; that fails here because our
            # max/p99 ratio grows with zoom (4.8x at z0, 18x at z4), so a
            # max-normalised ramp would get dimmer the further in you go.
            alpha = np.power(np.clip(g / p99, 0.0, 1.0), 1.0 / gamma) * max_alpha
            a8 = (alpha * 255.0 + 0.5).astype(np.uint8)
            tiles = 1 << z
            for ty in range(tiles):
                for tx in range(tiles):
                    a = a8[ty * tile_size:(ty + 1) * tile_size,
                           tx * tile_size:(tx + 1) * tile_size]
                    if not a.any():
                        continue                        # leave it absent
                    px = np.empty(a.shape + (4,), np.uint8)
                    px[..., :3] = rgb
                    px[..., 3] = a
                    w.write_tile(zxy_to_tileid(z, tx, ty), _png(px))
                    written += 1
            stats.append(dict(zoom=z, p99=p99, gamma=gamma,
                              max=float(lit.max()) if lit.size else 0.0,
                              lit_px=int(lit.size)))
            log(f"raster z{z}: {lit.size} lit px, p99 {p99:.1f}, "
                f"max {float(lit.max()) if lit.size else 0:.1f}")
        w.finalize(
            dict(tile_type=TileType.PNG, tile_compression=Compression.NONE,
                 min_zoom=0, max_zoom=max_zoom, clustered=True,
                 internal_compression=Compression.GZIP,
                 min_lon_e7=-1800000000, min_lat_e7=-850511290,
                 max_lon_e7=1800000000, max_lat_e7=850511290,
                 center_zoom=0, center_lon_e7=0, center_lat_e7=0),
            dict(name="adsb traffic density (raster)", type="overlay",
                 tile_size=tile_size, levels=stats))
    log(f"raster: {written} tiles written")
    return stats
