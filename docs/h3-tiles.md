# H3 traffic-density tiles

A **second, complementary layer**, not a replacement: hexbins can't be animated or
drawn as flight paths, so `legs/airports/` stays the detail layer and this is what
the frontend shows at world zoom, where fetching per-airport partitions makes no
sense. One file per day, range-fetched — no directory listing needed.

## Draw the raster, not the hexagons

`build_hexes.py` can write two archives, and **only the raster is built by
default** — `HEX_VECTOR=1` adds the vector one. The reason the raster is what you
draw is a hard limit of vector tiles: a tile has to keep its feature
count sane, so the pyramid coarsens the hexagons as it zooms out — and by z2 that
means H3 res 0, cells 1,100 km across. At that size the route network isn't
simplified, it's *gone*: every cell holds a bit of everything and the level is a
flat wash. No ramp fixes it, because the structure is no longer in the data.

A 512 px tile at z2 has ~20 km pixels — finer than a res-5 cell. The structure
fits in an *image* even though it cannot fit in the hexagons. So `hex_raster.py`
renders the **finest** hex level into one pixel grid and halves it repeatedly,
which is how a terrain or imagery pyramid is built —
[Mapterhorn's downsampling stage](https://github.com/mapterhorn/mapterhorn/tree/main/pipelines)
is *"half the size to 512 by 512 using 2 by 2 averaging"*. Averaging **with the
empty pixels included** is the point: it's an area-average, so a corridor stays
bright against empty airspace instead of being diluted into it.

Measured on 2026-09-01: 3.24 M res-6 cells → **8.2 MB** of PNG tiles for z0–z4,
242 tiles, ~40 s. At z2 the airway network, the North Atlantic track bundle and
the hub structure are all legible; the vector level at the same zoom is a wash.

| | `traffic-raster.pmtiles` | `traffic.pmtiles` |
|---|---|---|
| built | **always** | only with `HEX_VECTOR=1` |
| what it is | PNG pyramid, z0–z4 | vector hexbins, one H3 res per zoom |
| built from | the finest hex level, downsampled | the H3 roll-up |
| size / day | ~8 MB | ~115 MB at `--max-res 6` (85 of it res 6 alone) |
| use | **what you draw** | per-cell values for tooltips and queries |

Leaving the vector archive off skips the whole H3 roll-up, every `.geojsonl`, and
tippecanoe with them: the raster only ever reads the finest level and derives its
own pyramid by halving the image. Measured on a real day that is **185 s → 52 s**
and 890 MB → 35 MB of peak scratch, with a byte-identical raster.

Do **not** reach for a lower `--max-res` to save space. It is the raster's source
resolution: at res 6 the cells are ~6.4 km against 4.9 km pixels, roughly one per
pixel, so corridors come out continuous; at res 4 they are 22.6 km, 4.6× the
pixel, and the network degrades to a dotted lattice (the archive drops 8.2 → 1.4
MB, which is the structure going missing, not a saving).

The value is baked into the PNG's **alpha** channel against a flat colour
(`RGB` in `hex_raster.py`), because MapLibre can't colour-ramp a raster source
client-side; restyling means rebuilding. Each level normalises against its own
p99, since halving the grid halves the peaks too, and then **compresses the ramp**:
`alpha = (v / p99) ** (1 / HEX_RASTER_GAMMA)`, gamma 2 by default.

The gamma is not cosmetic. Air traffic density is not linear, and on a full day a
linear ramp left **58–73 % of the lit pixels under alpha 8/255** — in the data,
invisible on screen, and it was precisely the sparse ocean and polar routes the
overview exists to show. Gamma 2 takes that to ~0 % from z2 up while the hubs
still saturate, for +3.8 % archive size (8.19 → 8.50 MB). `HEX_RASTER_GAMMA=1`
restores the linear ramp exactly, pixel-for-pixel.

| gamma | invisible px (alpha < 8) z0 / z2 / z4 | median alpha z2 | archive |
|---|---|---|---|
| 1 (linear) | 73 % / 68 % / 58 % | 3 | 8.19 MB |
| **2 (default)** | **15 % / 0 % / 0 %** | **27** | **8.50 MB** |
| 3 | 0 % / 0 % / 0 % | 53 | 8.55 MB |

Gamma against the level's **max** — which is what
[adsb.exposed](https://github.com/ClickHouse/adsb.exposed) does, with a 1/5 power
— was measurably worse here: our max/p99 ratio *grows* with zoom (4.8× at z0,
18× at z4), so a max-normalised ramp gets dimmer the further in you go. They can
divide by max because they pick per-zoom sampling rates to hold the scale; a
static archive has no such knob, so the clip stays at p99 and only the curve is
borrowed.

```js
map.addSource("traffic", {
  type: "raster", url: "pmtiles://" + BASE + "/date=" + date + "/traffic-raster.pmtiles",
  tileSize: 512, maxzoom: 4,
});
map.addLayer({
  id: "traffic-density", type: "raster", source: "traffic",
  maxzoom: 7.5,                        // past here nothing is in range, so no fetches
  paint: {
    "raster-opacity": ["interpolate", ["linear"], ["zoom"], 0, 1, 4, 1, 6, 0.55, 7.2, 0],
    "raster-resampling": "linear",     // "nearest" shows the source grid as squares
  },
}, firstSymbolLayerId);                // over any night shading, under the labels
```

## What a hexagon means

## Two values, and you almost certainly want the second

`n` = **distinct flights that crossed the cell** that day. Deliberately *not* a
count of spline nodes: the fitter places nodes densely in turns and near the ground
and sparsely at cruise, so a node histogram would mostly render the tolerance ramp
instead of air traffic. `build_hexes.py` reconstructs each leg's centripetal
Catmull-Rom curve — the exact curve the frontend draws, broken at `cusp` nodes —
samples it every `--step-km`, and counts distinct legs per cell. Chord interpolation
between nodes is *not* good enough: a wide turn held by few nodes bows several km
away from its chord, more than a res-6 hex is wide.

Roll-up to coarser resolutions goes through `h3_cell_to_parent` on the
**(cell, flight) pairs**, then counts — never by summing child counts, which would
count one flight once per child cell it crossed.

But **do not ramp on `n`**. It is a set union, so it grows with cell area: the
coarser the cell, the more flights cross it, until every coarse cell converges on
"lots" and the level is flat. On a real day, union-counting put western Europe's
res-2 cells in the global top 2% with almost nothing between them — and no
normalisation fixes that, because it is the metric, not the scale. Neither a
log-vs-p99 scale nor a percentile rank helps; both flatten the busy regions.

`dens` is the aggregation that works: **mean distinct flights per finest-resolution
cell**, i.e. the accumulated total divided by the descendant count, with missing
descendants counted as zero. That is what a raster overview pyramid computes when
it averages 2×2 pixels into their parent — [Mapterhorn's downsampling
stage](https://github.com/mapterhorn/mapterhorn/tree/main/pipelines) *"half the
size to 512 by 512 using 2 by 2 averaging"* — and it is scale-invariant the way a
union-count cannot be. Measured on the same day it widens western Europe's own
spread from 50% to 65% of the global range; the coarsening maximum settles
(2584 → 300) instead of climbing (2584 → 8631).

`dn` is `dens` on a **linear** 0–255 ramp against that resolution's p99. Linear
because the alternatives spend the range making the empty 90% of the planet
visible; the honest cost is that genuinely quiet airspace reads as quiet
(central Africa lands at ~0.3% of the range against western Europe's 65%).

## Zoom ↔ resolution

One H3 resolution per zoom level (`res = z - 2`), so a hexagon holds a roughly
constant on-screen size:

| source zoom | layer | H3 res | hex edge |
|---|---|---|---|
| 0–2 | `h0` | 0 | 1108 km |
| 3 | `h1` | 1 | 419 km |
| 4 | `h2` | 2 | 158 km |
| 5 | `h3` | 3 | 59.8 km |
| 6 | `h4` | 4 | 22.6 km |
| 7 | `h5` | 5 | 8.5 km |
| 8 | `h6` | 6 | 3.2 km |

The coarsest level also fills every zoom below its own, which is why `h0` covers
z0–2 rather than just z2. MapLibre never *under*zooms — `coveringTiles()` drops
any tile below the source's minzoom instead of stretching a parent — so an
archive starting at z2 renders nothing at all from z0 to z1.9, which is exactly
the globe view this layer is for. It costs 21 extra tiles.

Above z8 MapLibre overzooms the z8 tiles — hexes just grow, which is the natural
visual handover to the spline layer. `--max-res` raises the ceiling (res 7 ≈ 1.2 km,
res 8 ≈ 460 m) at a steep cost in cells; keep `--step-km` at or below half the
finest hex edge.

## Feature properties

| property | meaning |
|---|---|
| `n` | distinct flights through the cell — **for tooltips, not for styling** |
| `dens` | mean distinct flights per finest-resolution cell (area-average) |
| `dn` | **0–255, `dens` on a linear ramp against that resolution's p99** |
| `a` | mean altitude in the cell, feet |
| `amin` | minimum altitude in the cell, feet |

**Ramp opacity off `dn`.** See above for why `n` cannot carry a ramp across
zooms.

## Frontend

Each resolution lives at exactly **one** source zoom, which tempts you to leave
the fill layers unbounded: a given map zoom fetches one source zoom, whose tile
contains exactly one `h*` layer, so surely only that layer can draw. **Give each
layer its own one-zoom-wide window anyway.** Measured at z6, MapLibre drew `h3`
and `h4` at once — when a z6 tile hasn't arrived it keeps the z5 parent to fill
the gap, that parent carries `h3`, and the two fills composite, doubling the
opacity and washing the basemap. A momentary gap is better than a wash.

```js
maplibregl.addProtocol("pmtiles", new pmtiles.Protocol().tile);
const src = "pmtiles://" + BASE + "/date=" + date + "/traffic.pmtiles";
map.addSource("traffic", {type: "vector", url: src});
for (let r = 0; r <= 6; r++) map.addLayer({
  id: "h" + r, type: "fill", source: "traffic", "source-layer": "h" + r,
  minzoom: r + 2, maxzoom: r + 3,     // one zoom each -- see above
  paint: {
    // hue by mean altitude: ground amber -> cruise cyan
    "fill-color": ["interpolate", ["linear"], ["get", "a"],
                   0, "#ffb347", 8000, "#ff5e7a", 20000, "#a855f7", 36000, "#22d3ee"],
    // opacity off the area-averaged density, NOT off `n`
    "fill-opacity": ["interpolate", ["linear"], ["get", "dn"], 0, 0, 255, 0.5],
    "fill-antialias": false,   // no strokes: tippecanoe clips hexes at tile
  },                           // edges, so outlines would show seams
});
```

Two things that snippet is deliberate about. `fill-opacity` puts **`["zoom"]` as
the input of the outermost `interpolate`** — MapLibre rejects a `["zoom"]` nested
anywhere else, and rejects it by *firing an error event rather than throwing*, so
multiplying a zoom curve by a density curve produces a layer that is silently
never added. And a single `maxzoom` on every layer (not a per-resolution window,
which would fight the pyramid) means that once the map is past the handover no
layer is in range, MapLibre marks the source unused, and the tiles stop being
requested — so the layer costs nothing on a page that opens zoomed into one
airport.

Two more things worth knowing, both found by looking at it rather than reasoning
about it. Draw the hexes **over** any terminator/night shading, not under: under
it, a whole day's traffic gets dimmed by wherever the terminator happens to be at
the current second, which is both hard to see and wrong. And in a busy region
essentially every cell has traffic, so from about z5 the layer is a near-uniform
fill covering the viewport — hand over to whatever detail layer you have by then
rather than holding on to the finest levels.

`build_hexes.py` also writes `traffic.pmtiles.stats.json` next to the archive
(cell count, `n` median/max, and the `dens` p99 and max, per resolution) — worth watching
day over day, and enough for the frontend to renormalise `n` itself if it wants a
ramp other than `d`.

Known limitation: cells straddling the antimeridian are emitted twice (shifted
±360°) so tippecanoe clips each copy to the world and both halves draw.
