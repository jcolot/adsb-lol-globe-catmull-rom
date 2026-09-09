# adsb-lol-globe-catmull-rom

Daily pipeline that turns the [adsblol `globe_history`](https://github.com/adsblol/globe_history_2026/releases)
ADS-B trace dump into compact, smooth per-airport flight tracks for a browser 3-D
viewer (THREE.js + [hyparquet](https://github.com/hyparam/hyparquet)).

## What it does

For each aircraft's raw readsb trace it fits a sparse set of **centripetal
Catmull-Rom control points** ("nodes") whose reconstruction — the exact curve the
frontend draws — stays within an **altitude-graduated tolerance** (2 m on the
ground → 150 m at cruise). It then splits each aircraft into flight **legs** and
writes them **hive-partitioned per airport** so the browser fetches only the
airport it's showing.

Result: ~37× smaller than the source, no overshoot, sharp taxi corners (cusps),
stable ground altitude, and no parked-at-gate "scribbles".

It also builds two derived layers over the same day: **H3 traffic-density vector
tiles** (`traffic.pmtiles`) for the world-zoom overview, and a **day bundle** —
about 8 MB of index shipped up front that makes every flight findable by
callsign/registration/hex/route and answers "which flights went through this
box?" with **zero further requests**, then one HTTP range read per track drawn.

### Stages

1. **`fit_spline.py`** — `traces/ → nodes.parquet` + `aircraft.parquet`.
   Per-second decimation (mean position **and** time), stationary-gate snapping,
   ground-elevation reference, greedy CR-node placement.
2. **`build_legs.py`** — `nodes.parquet → legs/` (per-airport partitions +
   `flights.parquet` index).
3. **`build_hexes.py`** — `points_legs.parquet → traffic-raster.pmtiles` (H3
   traffic density as a raster overview; `hex_raster.py` renders it). The vector
   hexbin archive is optional and **off by default** — see below.
4. **`build_bundle.py`** — `points_legs.parquet → legs.parquet + cells.bin +
   tracks.bin + meta.json` (the queryable **day bundle**, see below).
   `verify_bundle.py` is its correctness gate and the reference decoder.

`smooth_trace.py`, `compress_trace.py`, `validate_recon.py` are supporting /
diagnostic modules (`validate_recon.py` measures reconstruction error vs raw).

## Output schema

`legs/airports/airport=<ICAO>/data_0.parquet`, one row per node:

| column | type | notes |
|---|---|---|
| `icao` | string | aircraft hex |
| `t` | int32 | **deciseconds** (0.1 s) — divide by 10 for seconds |
| `lat`, `lon` | int32 | scaled fixed-point |
| `alt` | int32 | feet |
| `on_ground` | bool | |
| `cusp` | bool | **break the spline here** (taxi corner / ground↔air) |
| `leg_id`, `dep`, `arr`, `reg`, `type` | | leg / aircraft metadata |

**Frontend:** draw a centripetal Catmull-Rom through consecutive nodes, starting a
new curve at every `cusp` node.

## Run locally

```bash
pip install -r requirements.txt
python3 fit_spline.py path/to/traces --ground-elevation \
    --parquet nodes --tol-ground 2 --tol-cruise 150 --corner 35
python3 build_legs.py --traces nodes/nodes.parquet \
    --meta nodes/aircraft.parquet --out-dir out/legs
python3 build_hexes.py --points out/legs/points_legs.parquet \
    --out out/legs/traffic.pmtiles
python3 build_bundle.py --points out/legs/points_legs.parquet \
    --meta nodes/aircraft.parquet --out-dir out/legs
python3 verify_bundle.py --bundle out/legs \
    --points out/legs/points_legs.parquet --meta nodes/aircraft.parquet
```

`build_hexes.py` needs [tippecanoe](https://github.com/felt/tippecanoe) ≥ 2.17 on
`PATH` (`brew install tippecanoe`) and installs DuckDB's `h3` **community**
extension on first run, so that step needs network access. `--no-tiles` stops
after the per-resolution `.geojsonl` files if you only want the aggregation.

`run_pipeline.sh` does the whole daily job end-to-end (resolve latest release →
stream-extract → fit → legs → hexes → bundle → upload to R2). It streams the ~4 GB split-tar
download straight into `tar`, so peak disk is just the ~2.9 GB extracted tree.

## H3 traffic-density tiles

A **second, complementary layer**, not a replacement: hexbins can't be animated or
drawn as flight paths, so `legs/airports/` stays the detail layer and this is what
the frontend shows at world zoom, where fetching per-airport partitions makes no
sense. One file per day, range-fetched — no directory listing needed.

### Draw the raster, not the hexagons

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

### What a hexagon means

### Two values, and you almost certainly want the second

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
normalisation fixes that, because it is the metric, not the scale. Log-vs-p99 and
a percentile rank were both tried and both flattened the busy regions.

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

### Zoom ↔ resolution

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

### Feature properties

| property | meaning |
|---|---|
| `n` | distinct flights through the cell — **for tooltips, not for styling** |
| `dens` | mean distinct flights per finest-resolution cell (area-average) |
| `dn` | **0–255, `dens` on a linear ramp against that resolution's p99** |
| `a` | mean altitude in the cell, feet |
| `amin` | minimum altitude in the cell, feet |

**Ramp opacity off `dn`.** See above for why `n` cannot carry a ramp across
zooms.

### Frontend

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

## Animating a span of days

`render_video.py` turns a stack of daily grids into an mp4. Two modes: `absolute`
(one fixed divisor for the whole run, sequential palette) and `anomaly` (each day
against its own trailing baseline, diverging palette). Use `anomaly` to find
events — a closure is a hole, a reroute is a bright corridor beside a dark one.

### The grid, and why it is not the raster

The animation **cannot** be built from `traffic-raster.pmtiles`. Its alpha is
normalised against each day's own p99, so a day where a region's traffic
collapses is scaled back up to look like every other day — the animation would
normalise away the thing it exists to show.

So `build_hexes.py --grid-out` also writes `traffic-grid.npz`: the raw z2 density
grid, sparse, ~2 MB/day, no normalisation. `run_pipeline.sh` writes it by
default (`HEX_GRID_ZOOM`), and `upload()` puts it at **`$base/grids/$date.npz`**,
*outside* the `date=` prefix — deliberately, because the retention prune only
walks `date=` partitions. The grids therefore outlive the 30-day window, which
is the whole point: they are the only per-day artifact comparable across days,
and an animation has to reach back past retention. 0.7 GB/year.

### Four corrections, none of them optional

Each of these, left out, manufactures events that are not there:

| | why |
|---|---|
| Fixed normalisation | see above |
| Drop incomplete days (`--min-day-frac`) | a half-ingested run renders as a dark frame indistinguishable from a real event. **3 of the first 30 days in R2 were partial** (0.65×/0.72×/0.76× of local median, neighbours at 0.99×) — the common case, not a corner |
| 7-day rolling mean (`--smooth`) | weekday/weekend swing otherwise strobes under any slower signal |
| Coverage trend (`--coverage-ref`) | adsb.lol is volunteer-fed, so feeder growth looks exactly like traffic growth. Only the *slow* trend of a reference region is divided out — dividing by its daily total would also delete the weekly cycle and any event big enough to move the reference |

`anomaly` additionally weights each pixel's excursion by magnitude
(`--anomaly-floor`) against `max(day, baseline)` — not the day alone, or a
closure would fade out exactly where it matters. Without it a pixel going 0.1 →
0.2 shouts as loudly as a closed corridor.

Every run also writes `ranking.csv`: per-region deviation from trailing
baseline, biggest first. It is a shot list — it nominates the days and places
worth looking at instead of requiring you to guess them.

### Backfilling: `build_grid.py`

For a long span, the normal pipeline is mostly wasted work. The grid is coarse —
a z2 pixel is ~20 km — and spline fitting, leg splitting and the H3 roll-up all
exist to hold metre-scale tolerance that does not survive being binned into a
20 km pixel. So `build_grid.py` goes straight from raw traces to the grid:

```
normal   fetch -> fit_spline -> build_legs -> build_hexes (DuckDB + H3) -> grid
shortcut fetch -> build_grid                                            -> grid
```

It streams the release tar from stdin and stages nothing:

```sh
urls=$(gh api "repos/adsblol/globe_history_2026/releases/tags/$TAG" \
        --jq '.assets[].browser_download_url' | sort | tr '\n' ' ')
curl -fsSL $urls | python3 build_grid.py --tar-stream --out grids/$DATE.npz
```

It does **not** bin raw fixes. Fix density is a map of the feeder network and of
what the aircraft was doing — a hold over a well-covered field emits far more
fixes per km than an ocean cruise leg. Each trace instead contributes at most 1
to any pixel, which is the pixel-resolution equivalent of the `DISTINCT
(cell, leg)` count `build_hexes.py` does in SQL. `build_grid.py --compare
SHORTCUT REFERENCE` reports the agreement between the two paths.

Measured on one full day (2026-09-02, 3.9 GB, **231 s**, 76,890 traces →
**124,163 legs** against the pipeline's 127,303 — 97.5%):

| | |
|---|---|
| correlation on shared pixels | **r = 0.94** (log r = 0.90) |
| ratio to the H3 path | **4.4–5.8×** across the top three density quintiles; 12× in the lowest, where the reference is mostly averaged-in zeros |
| pixels the H3 path lights and this does not | 30%, but median density 0.19 vs 0.50 overall, and half of them adjacent to a lit pixel |

So the two paths agree on *relative* density, which is all either mode needs,
and disagree on absolute scale by a near-constant ~5×. The extra pixels the H3
path lights are its own dilation artifact: res-6 cells are ~6.4 km across, so a
leg's cell set fattens the track, and averaging down from z4 spreads single hits
into neighbouring z2 pixels. The same dilation is why the ratio drifts with
latitude (4.8× at the equator, 9.6× at 60–70°N): a high-latitude z2 pixel covers
less ground, so more of its z4 sub-pixels are empty and the reference is diluted
harder. Neither grid is area-uniform, and correcting for it is pointless in
`anomaly` mode, where any time-constant per-pixel factor divides out — measured,
the region percentages move by ≤0.5 pp and the shot list does not reorder.

Counting whole traces instead of legs was this script's first version. It is
wrong, but subtly: it changes only 16% of lit pixels, so the headline
correlation barely moves (r 0.937 → 0.943). Those 16% are the ones that matter —
median reference density 7.9 against 0.50 overall, undercounted by ~6 — i.e. the
hubs and busy corridors, which is exactly where a frequency change shows up.

**Do not splice the two into one run.** A 5× step at the join renders as exactly
the kind of jump this tool exists to distinguish from a real event. Every grid
records which path wrote it (`hex_raster.grid_producer`), and `render_video.py`
**refuses** a mixed stack unless `--allow-mixed-producers` is given.

The floor cost is the ~4 GB/day download, which no shortcut removes: about
1.4 TB for a year. That is free and fast on Actions, and it is the blocker
locally — where a targeted window of 30–60 days (120–240 GB, and near-zero disk
because nothing is staged) is the practical option.

### Doing it on Actions instead of locally

Two workflows split the work along its natural seam, because the two halves are
nothing alike. Colour is the LAST step, so re-rendering is ~30 ms a frame; only
building the grids costs anything.

`backfill-grids.yml` — the expensive half. `workflow_dispatch` with `start`,
`end`, `days_per_job`, `grid_zoom`, `gap_mode`. A `plan` job resolves every
`prod-0` release once (60-odd jobs each paginating the API would get
rate-limited) and drops days with no release upstream — 2026-05-06 has none.
Each `build` job streams `days_per_job` days concurrently through
`build_grid.py --tar-stream`; the runner has 4 cores and the build is
single-threaded, so 4 is the natural batch. At `max-parallel: 20` the 245 days
of 2026 take roughly half an hour of wall clock.

Grids come back as an **artifact**, not via R2, for two reasons: the `grids/`
prefix in R2 currently returns `AccessDenied`, and 245 jobs is the wrong place
to discover a credential problem; and artifacts are free on a public repo, where
245 × 1.5 MB is ~370 MB. `collect` merges them into one `grids-all` download.

`render.yml` — the cheap half, seconds of compute. Takes `mode`, `palette`,
`start`, `end`, `smooth`, `min_day_frac`, an `extra_args` escape hatch, and
either a `grids_run_id` to pull a backfill run's artifact or nothing, in which
case it fetches the grids from R2. It writes the frames, the mp4 and
`ranking.csv` as an artifact and puts the shot list in the run summary. Run the
backfill once, then run this as often as you like.

Both route dispatch inputs through `env:` rather than interpolating them into
`run:` blocks, so a crafted input cannot inject shell.

The floor cost is the download, which no shortcut removes — but on Actions the
release assets never leave GitHub's own network, so a year is free and fast
there while being the binding constraint locally.

**Render memory.** The stack is held whole, so a 245-day run at `--grid-zoom 2`
is 3.8 GB, and `rolling_mean` and `trailing_baseline` each need a second array
of the same shape: ~7.7 GB peak against a runner's 16 GB. Those two used to
build a float64 cumulative sum over the whole stack — and because
`np.concatenate` holds both its input and its result, that was 15.4 GB of
float64 on top of the stack, or 23 GB total, which simply did not run. They now
slide a running total instead, which is bit-identical and needs one
`(side, side)` accumulator. If a longer span ever runs out of memory, build the
grids at `--grid-zoom 1` (1024 px, a quarter of it) rather than trimming the
render.

### Crossing a coverage gap

adsb.lol is fed by volunteer ground receivers, so large parts of the world are
simply unwatched. Measured on 2026-09-02, consecutive spline-node gaps at cruise
run **2,621 km median (2.7 h) over the open North Atlantic** against **20.6 km
(93 s) over continental Europe** — 127×. The largest are 6,264 km over Siberia,
5,974 km mid-Atlantic and 4,998 km over Greenland. `--tol-cruise` is 150 m, so
the fitter places a node wherever a sample exists; a huge node gap therefore
means no data, not a straight flight.

Whatever is drawn across such a gap is invented, and the only question is which
invention. Three things were wrong with the obvious choices:

- A **straight line in pixel space** is a rhumb line — constant bearing — which
  no aircraft flies. Over 5,500 km it shares one pixel in 855 with the great
  circle between the same two fixes.
- A **Catmull-Rom span** between nodes thousands of km apart is unconstrained
  and free to overshoot.
- A **great circle** is much closer, and is what `fit_spline.insert_gc_nodes`
  now puts in the archive: any node gap over `GC_MAX_GAP_KM` (200 km) is filled
  with great-circle nodes, so the frontend's spline, the hexbins, the raster and
  the bundle all inherit one fix instead of four. Cost is +7.5% nodes on
  long-range traces and nothing on short-haul, which has no gaps.

But a great circle is still a hairline asserting ~2 px of precision where the
real uncertainty is ~70. Over the North Atlantic aircraft fly an organised track
structure spanning ~780 km of latitude, chosen daily from the jet stream —
information this data does not contain. So `build_grid.py --gap-mode` offers:

| mode | what it draws | when |
|---|---|---|
| `arc` | the plain great circle | baseline |
| `band` | one vote spread over a tapered Gaussian band | when the ocean should *look* as uncertain as it is |
| **`best`** | the candidate route with the most **observed** support, as one crisp line | default |

`best` is a two-pass estimate. Pass one accumulates only short, covered
segments. Pass two scores ~25 candidate routes per gap — all pinned to the same
two observed fixes, differing only mid-gap — against that observed field, and
draws the winner. On 2026-09-02, **19,433 of 36,771 gaps were moved by the
evidence** and 17,338 found nothing nearby and kept the great circle, so roughly
half the ocean remains a geometric guess; the build log prints the split for
exactly that reason.

Three details it depends on:

- **The scoring field contains observed segments only.** Score against a field
  that already holds interpolated paths and every gap snaps onto the pipeline's
  own guesses, which then look like corroboration.
- **It is MAP, not maximum likelihood.** Support is multiplied by a Gaussian
  prior on lateral offset, so thin evidence cannot drag a route far. Measured on
  a synthetic gap: a corridor 2.4° off pulled the route 1.53°, and one 6.4° off
  (outside ±2σ) was ignored.
- **Support is scored on 4×4 blocks, log-compressed**, so a candidate is
  rewarded for passing *near* observed traffic rather than exactly through it,
  and a busy corridor outranks a single bright hub pixel.

`band` conserves mass rather than adding it — weights sum to one across the
band, members of one gap sum, and different gaps combine by max, so no pixel
exceeds one vote for one leg. It costs 4× the archive (6.09 MB vs 1.50 MB) since
a diffuse field compresses badly, and it inflates the `n-atlantic` and `red-sea`
region totals as neighbouring uncertainty spills into those boxes.


## The day bundle

Four files per day that make every flight findable and every "what flew through
this box?" answerable. Built by `build_bundle.py`; `verify_bundle.py` gates it.

The design turns on one observation: **the two queries want different structures,
and only one of them needs the geometry.** "Which flights went through this box?"
is set membership, answerable from an index small enough to hold in memory.
"Draw flight X" is a byte-range question.

| question | answered by | cost |
|---|---|---|
| how much traffic is here? | `traffic.pmtiles` | tile reads |
| which flights went through this box? | `cells.bin` + `legs.parquet`, both resident | **zero requests** |
| find and draw flight X | `legs.parquet` → `tracks.bin` | one range read |

Because the box query never touches the payload, `tracks.bin` is free to be
clustered for whole-flight retrieval instead — which dissolves the tension that
would otherwise force two copies of the geometry.

### `legs.parquet` — one row per leg, shipped whole

`lid`, `icao`, `reg`, `type`, `dep`, `arr`, `t0`, `t1`, `n_nodes`, the bounding
box (`min_lat`…`max_lon`, degrees × 1e5), `min_alt`/`max_alt` in feet, and
`off`/`len` — the byte range of that leg's record in `tracks.bin`.

**`lid` is the row index**, so a posting list from `cells.bin` indexes this table
directly with no lookup map.

Row order is `(dep, t0)`, which means **every departure from one airport is a
single contiguous byte range** in `tracks.bin` — the property the per-airport
partitions existed to provide, now without needing an airport directory. Legs
with no `dep` go in a tail bucket ordered by H3 anchor cell.

`t0`/`t1` are **deciseconds from `meta.json`/`t_epoch`** (UTC midnight of the data
date). This matters: `nodes.parquet` stores `t` relative to each *aircraft's*
`base_ts`, so raw `t` is not comparable between flights. `build_bundle.py` joins
`aircraft.parquet` to rebase everything on one epoch — which is why it needs
`--meta`.

### `cells.bin` — H3 inverted index, shipped whole

```
offset   size             field
0        8                magic "ADSBIDX1"
8        1                version    1
9        1                res        H3 resolution (default 4)
10       2                — padding —
12       4                n_cells    uint32
16       4                n_legs     uint32
20       8 x n_cells      cell[]     uint64, ascending H3 index
…        4 x (n_cells+1)  off[]      uint32 prefix offsets into post[]
…        —                post[]     per cell: varint gap-coded ascending lids
```

Lookup is a binary search over `cell[]` and a slice of `post[off[i]..off[i+1]]`.
Sorted by H3 index because a parent's descendants form exactly **one** contiguous
run in that order, so "everything under this cell" is a single slice and the
client can pick its covering resolution by zoom.

Gap-coded posting lists cost ~1.5 bytes per (cell, leg) pair.

### `tracks.bin` — per-leg records, range-read

```
varint            n           node count
varint            t0          deciseconds from t_epoch

n x {
  varint          dt          t[i] - t[i-1],  dt[0] = 0
  svarint         dlat        delta, degrees x 1e5   (first is absolute)
  svarint         dlon        delta, degrees x 1e5
  svarint         dalt        delta, feet
}

ceil(n/8) B       on_ground   bitplane, LSB-first
ceil(n/8) B       cusp        bitplane, LSB-first
```

`svarint` is zigzag + LEB128. Bit *k* of a bitplane is
`byte[k >> 3] >> (k & 7) & 1`. Flags live in bitplanes at the tail rather than
interleaved per node: an interleaved flag byte costs a byte per node, two
bitplanes cost two bits.

`verify_bundle.py` contains the reference decoder in plain Python — the frontend
decoder should read like `decode_track()` and `CellIndex`.

### Frontend: answering "all flights through this box"

⚠️ **The covering cell set must be ring-expanded.** `polygonToCells` returns cells
whose *centroid* falls inside the polygon, so a node just inside your box can sit
in a cell whose centroid is outside it. Take `gridDisk(cell, 1)` of the result or
you will silently miss flights — measured over 120 random boxes, `polygonToCells`
alone missed 0.1% of matching legs while the ring-expanded set missed none. Rare
enough to survive testing, common enough to be a real bug:

```js
const cover = new Set();
for (const c of h3.polygonToCells(boxRing, meta.index_res))
  for (const n of h3.gridDisk(c, 1)) cover.add(n);

const hits = new Set();
for (const c of cover) for (const lid of idx.get(c)) hits.add(lid);
// hits now index legs.parquet directly -- callsign, route, times, bbox, all local
```

The index is **exact at cell granularity, approximate below it**: res 4 cells are
~45 km across, so a smaller box returns false positives (measured 17–22% on test
data). There are never false negatives — that is the property `verify_bundle.py`
asserts. Refine against the leg bbox, then against real geometry, for boxes
tighter than a cell.

### What this replaces, and what it doesn't

`points_legs.parquet` is no longer uploaded — nothing reads it, and `tracks.bin`
supersedes it at a third less size (measured on 2026-09-01: 161 MB vs ~106 MB for
the same 14.4 M nodes).

`legs/airports/airport=<ICAO>/data_0.parquet` is **still built and uploaded, and
should stay that way.** An earlier draft of this section claimed the bundle made
it redundant; measuring the read pattern showed that's only half true, and the
half it gets wrong is the expensive half.

The `(dep, t0)` sort puts every *departure* from one airport in a single byte
range, so those cost one request. *Arrivals* into that airport are scattered —
each sits in its own departure airport's run — and coalescing adjacent ranges
trades requests for bandwidth on terrible terms (2026-09-01, EBBR, 308 arrivals
totalling 0.30 MB of payload):

| gap tolerance | requests | bytes fetched |
|---|---|---|
| exact ranges | 238 | 0.30 MB |
| 64 KB | 92 | 2.96 MB |
| 256 KB | 32 | 11.16 MB |
| 1 MB | 8 | 22.16 MB |

The per-airport partition delivers the same 555 legs in **one** 0.71 MB request.
So the two layouts are not competing: `build_legs.py`'s double-write **is** the
arrival-clustered copy, which is the thing the bundle's single ordering
structurally cannot also be. Keeping it costs far less than a second
`(arr, t0)`-ordered copy of `tracks.bin` would.

What the bundle uniquely buys, then, is not the airport fan but the queries the
partitions can't answer at all: box containment, any-airport-without-a-prebuild,
and finding one flight by identity across the whole day.

## Overnight classification

Given a flight *arriving* at a known scheduled local time, did it depart its
origin on the same local date or the day before? `overnight.py` answers it, and
the answer is a signed **date offset**, not a boolean:

```
arr_utc   = arr_local @ tz(arr) -> UTC
dep_utc   = arr_utc - block_time
dep_local = dep_utc -> tz(dep)
offset    = arr_local.date() - dep_local.date()
```

Crossing the date line westbound makes that `+2` (LAX→SYD) and eastbound makes
it `0` even on a 14-hour flight (SYD→LAX), so collapsing to a bit too early
loses two real cases. "Overnight" is `offset >= 1`.

### The margin, not the block time

**The timezone conversion carries the classification; the block time only has to
avoid walking the computed departure across local midnight.** So the number to
report is the *margin* — how far the computed departure sits from the nearest
local midnight — because that, not the block-time residual, is what says whether
an answer is trustworthy. Checked against eight published schedules, all eight
offsets were right and every one held under ±90 minutes of injected block-time
error, on margins of 82–588 minutes. A four-hour margin is immune to any
plausible error; a twenty-minute margin is a coin toss however good the estimate
is. Precision in the block time is worth buying only for the marginal cases.

This is why the estimator is deliberately cheap, and why the "large sparse
matrix" a block-time table seems to need never has to exist:

| block time source | when | note |
|---|---|---|
| observed median for the directed pair | `--legs`, n ≥ 3 | carries the wind asymmetry |
| `45 min + d / 800 km/h` | any pair, never observed | ±15 min from CDG–LHR to SIN–LHR |

Direction matters and a symmetric model gets one side wrong: the distance model
puts NRT→LAX 1h40 early, because eastbound rides the jet stream. The per-pair
median is per-direction and absorbs it.

### `airport_tz.csv` — the one input the repo lacked

`airports.csv` has no timezone column, and this question is decided in local
dates at both ends. `build_airport_tz.py` resolves 6,372 airports (scheduled
service **or** large/medium — 896 airports typed `small_airport` do carry
scheduled service, so filtering on `type` alone drops every one of them) into a
374 KB CSV of IANA **zone names**, 383 distinct.

Zone names, not fixed offsets: being an hour out over a DST boundary flips
exactly the cases that are already marginal, so `zoneinfo` applies the rules for
the date in question. There is deliberately no `longitude / 15` fallback — China
spans five geographic hours in one zone and India is `+05:30`, neither
recoverable from a meridian. `timezonefinder` is a **generate-time** dependency
only; the CSV is committed and the pipeline stays on numpy/pyarrow/duckdb.

### The empirical table

`--build-table` writes one row per `(dep, arr, arrival local hour)` with the
modal offset, the agreement fraction and `n`. Keyed on arrival hour because a
route can run both a daytime and a red-eye service with different offsets, and
the caller already knows the scheduled arrival — a free conditioning variable.
Actual arrivals scatter across adjacent hours, so a route-level rollup at
`arr_local_hour = -1` is emitted as the fallback for a thin bucket: look up
`(dep, arr, hour)` first, then `(dep, arr, -1)`.

The column is `day_offset`, not `offset`, which is a reserved word in
DuckDB/Postgres and would make a bare `SELECT` a parser error downstream.

This is the whole of the "sparse matrix": an edge list of a few tens of
thousands of rows. A dense matrix over all 85,734 airports would be 7.35 × 10⁹
cells (14.7 GB of `uint16`); over the 6,372 that matter it is 56 MB, and over
observed pairs only, ~5 MB of Parquet. The distance model covers everything
never observed, so nothing needs a matrix layout.

### Two filters that are not optional

- **`dep_gnd AND arr_gnd`.** `build_legs.py` splits the day on the `on_ground`
  bit alone, so an aircraft that never emits a surface message — about half of
  them — has its entire day collapsed into a single leg whose `t_off`/`t_on`
  span every flight it made. `--all-legs` disables the filter; it is there for
  diagnosis, not for use.
- **`t_off`/`t_on`, never `t_end - t_start`.** The leg envelope carries half of
  each adjacent turnaround plus all ramp time on the day's first and last leg.
  Measured on a synthetic EGLL→LFPG→EGLL pair: 209-minute envelope against 74
  minutes airborne, the difference being 90 minutes of ramp and 45 of half-
  turnaround. It is not a flight time and the gap is structured, not noise.

Both filters are necessary and **neither is sufficient**: together they also
discard every flight the archive boundary cut in half, which is what
`splice_legs.py` exists to repair. Run the splicer first and point `--legs` at
its output.

### `splice_legs.py` — the daily cut, and why it biases the wrong way

adsb.lol publishes one archive per UTC day, so a flight airborne at 00:00Z is
split across two of them. Each half loses an endpoint — the far end is a cruise
fix hundreds of km from any airport, and `build_legs`' 10 km resolver returns
`NULL` — so both halves fail `dep IS NOT NULL AND arr IS NOT NULL` and vanish.
Measured on a synthetic EGLL→KJFK departing 22:00Z:

```
day D    dep=EGLL  arr=NULL   arr_gnd=false   22:00Z -> 23:59Z   "airborne" 119 min
day D+1  dep=NULL  arr=KJFK   dep_gnd=false   00:00Z -> 05:59Z   "airborne" 359 min
overnight.py on either half -> 0 usable legs
```

**The loss is concentrated exactly where the question is interesting.** It scales
as roughly `duration / 24` — 31% of JFK–LHR, 58% of LAX–SYD — and 00:00Z is
20:00 in New York and 17:00 in Los Angeles, the departure peak for the
transatlantic and transpacific red-eyes that are canonically overnight. Left
unrepaired, the empirical table is close to blind to `offset >= 1`.

Tier 2 still answers these routes correctly, since the distance model needs no
observed leg — but they then fall back to the model precisely where it is
weakest, because wind asymmetry is largest on long-haul.

Matching needs **no time tolerance**, because "the archive cut this leg" is
exact: a *tail* is the aircraft's last leg of day D with `arr IS NULL` whose
airborne run reaches the leg's final point (`t_on == t_end`), meaning it was
still flying when the data stopped — a real landing leaves descent or ground
fixes after `t_on`. A *head* is the mirror in day D+1. One tail and one head per
aircraft per boundary makes the key unique, and since any flight under 24 h
contains at most one 00:00Z, a leg is never cut into three.

What the timestamps *cannot* do is confirm the match, which is why they are not
used for it: over the North Atlantic the median cruise node gap is 2.7 h, so the
last fix before midnight can sit hours short of it. Two independent checks
instead — `--max-gap-h` (default 3.0, sized against that 2.7 h median) on the
unobserved stretch, and a great-circle speed band on the spliced result, which
catches a tail joined to an unrelated head without needing any position data.

| fixture | outcome |
|---|---|
| still airborne at 23:59Z, head next day | spliced |
| complete leg | passes through |
| tail whose aircraft never reappears | rejected, no pair |
| last fix 20:30Z, head at 02:00Z | rejected, gap 5.5 h |
| EGLL tail joined to an EHAM head after 8 h | rejected, 46 km/h |
| dates two apart (retention gap) | nothing spliced |

Two consequences to plan around. A spliced leg spans two `base_ts` frames, so it
has no single relative time frame: the output carries **absolute** deciseconds
in `t_off`/`t_on` with `base_ts = 0`, chosen so the usual `base_ts + t/10` rebase
still yields absolute UTC and readers need no special case. And day D's tails
cannot be completed until D+1 exists, so **the empirical layer is inherently one
day lagged** — clients need to know which days are settled, and the oldest
retained day has no predecessor, leaving its early-morning arrivals unresolved.

```bash
./build_airport_tz.py                     # once; needs timezonefinder
./splice_legs.py --root legs --out legs_spliced.parquet
./overnight.py --dep KJFK --arr EGLL --arr-local '2026-09-08 09:20'
./overnight.py --legs legs_spliced.parquet --fit
./overnight.py --legs legs_spliced.parquet --build-table overnight.parquet
```

### Static artifacts for external clients

There is no server: clients range-read static files from R2. So the classifier
ships as **data**, in three files per day.

| file | partition | what |
|---|---|---|
| `airports_utc.parquet` | `date=D` | 6,372 airports: `ident`, `lat`/`lon` (degrees x 1e5), `tz`, and `off_d0`/`off_dm1`/`off_dm2` — UTC offset in **minutes** for D, D-1, D-2 |
| `overnight.parquet` | `date=D-1` | the settled tier-1 lookup, `(dep, arr, arr_local_hour) -> day_offset` |
| `overnight_meta.json` | `date=D` | tier-2 model coefficients, and whether a settled table exists |

**Parquet, not a custom binary.** Measured: 86 KB against 137 KB raw for an
equivalent `.bin`. The binary is ~18 KB smaller *gzipped* (63 vs 81 KB), which
is not worth a format to decode, version and verify when hyparquet is already
loaded for `legs.parquet` — and the Parquet carries the IANA zone name too,
which dictionary-compresses to nearly nothing. `cells.bin` and `tracks.bin` are
binary because posting lists and byte-range records are things Parquet cannot
express; a flat 6k-row airport table is not one of those.

**Resolved offsets, not zone names**, because the conversion a client needs is
local-wall-clock → UTC — the direction `Intl.DateTimeFormat` does *not* do.
From a zone name that needs `Temporal` or an iterate-and-correct loop, including
the ambiguous DST hour. An integer offset makes both directions addition and
bakes DST in for the date.

⚠️ **Fetch the partition for the arrival date you are asking about.** The file
covers only D, D-1 and D-2, which is exactly enough: arrival on D, and a
departure up to two days earlier for a westbound date-line crossing. Reading a
D-2 arrival out of *today's* file needs D-3 and the decoder throws — that guard
is deliberate, and widening the window would hide the misuse without closing it.

Two passes are needed on the departure side: its local date is not known until
it is computed, and its offset depends on that date. One correction is enough,
since a DST step is at most an hour.

```js
const DAY = 1440;

function offMin(rec, d0Ms, dateMs) {
  const k = Math.round((d0Ms - dateMs) / 86400000);
  if (k < 0 || k > 2) throw new Error(`date outside the 3-day window (k=${k})`);
  return [rec.off_d0, rec.off_dm1, rec.off_dm2][k];
}

// arrLocal: {y, m, d, hh, mm} wall clock at the ARRIVAL airport.
export function classify(dep, arr, arrLocal, apt, d0Ms, blockMin) {
  const { y, m, d, hh, mm } = arrLocal;
  const arrDateMs  = Date.UTC(y, m - 1, d);
  const arrLocalMin = Date.UTC(y, m - 1, d, hh, mm) / 60000;
  const arrUtcMin = arrLocalMin - offMin(apt[arr], d0Ms, arrDateMs);
  const depUtcMin = arrUtcMin - blockMin;

  let off = offMin(apt[dep], d0Ms, arrDateMs);
  let depLocalMin = depUtcMin + off;
  const depDateMs = Math.floor(depLocalMin / DAY) * DAY * 60000;
  if (depDateMs !== arrDateMs) {
    const off2 = offMin(apt[dep], d0Ms, depDateMs);
    if (off2 !== off) depLocalMin = depUtcMin + off2;
  }

  const dayOffset = Math.floor(arrLocalMin / DAY) - Math.floor(depLocalMin / DAY);
  const into = ((depLocalMin % DAY) + DAY) % DAY;
  return { dayOffset, overnight: dayOffset >= 1,
           marginMin: Math.min(into, DAY - into) };
}

// tier 2, when overnight.parquet has no row for the pair
export function blockFromModel(a, b, meta) {
  const R = 6371.0088, r = Math.PI / 180;
  const p1 = a.lat / 1e5 * r, p2 = b.lat / 1e5 * r;
  const dp = p2 - p1, dl = (b.lon - a.lon) / 1e5 * r;
  const h = Math.sin(dp / 2) ** 2
          + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return meta.block_fixed_min
       + 60 * (2 * R * Math.asin(Math.sqrt(h))) / meta.block_kmh;
}
```

This is not illustrative code: it is cross-checked against `overnight.classify`
under node on the eight published schedules, and reproduces all eight offsets
and margins to within a minute of rounding.

**Client order of preference.** Look up `(dep, arr, arr_local_hour)` in
`overnight.parquet`; fall back to `(dep, arr, -1)` when the hour bucket is thin,
because a client queries with a *scheduled* arrival while the table is built
from *actual* times and delay moves flights between buckets; fall back to
`classify()` with `blockFromModel()` for a pair that never flew. Then read
`marginMin` — under ~60 it is close enough to local midnight not to trust.

### `overnight_client.py` — the same contract, in Python

`overnight_client.py` is a reference client that depends on **nothing in this
repo**. It reads only the three published files, so it exercises the same
contract a browser does and would keep working if the rest of the pipeline
vanished. No `zoneinfo`, no `tzdata`, no `timezonefinder`: the offsets arrive
already resolved per date, which is the point of shipping them that way, and
the only arithmetic is integer minutes.

```bash
# a local partition
./overnight_client.py --dir legs/date=2026-09-08 \
    --dep KJFK --arr EGLL --arr-local '2026-09-08 09:20'

# or straight off R2, fetching the three files itself
./overnight_client.py --base-url https://pub-XXXX.r2.dev/legs \
    --date 2026-09-08 --dep KJFK --arr EGLL --arr-local '2026-09-08 09:20'
```

```
KJFK -> EGLL, arriving 2026-08-15 07:30 local
  departed  2026-08-14 18:49 local (estimated)
  offset    +1 day  -> OVERNIGHT
  source    observed hour bucket, n=19, agreement 1.00
  block     460 min over 5540 km
  margin    311 min from local midnight
```

It reports `source` on every answer, so a caller can see which tier produced
the offset, and prints what the distance model alone *would* have said whenever
the observed table disagrees with it — the two differing is the interesting
case, not a warning.

Cross-checked against `overnight.classify` on the eight published schedules:
8/8 identical offsets and margins, reached without a timezone database. Both
transports are exercised, and a partition with no `overnight.parquet` — the
newest day, always — degrades to tier 2 with a note instead of failing.

**`table_settled` matters.** Day D's midnight-crossing legs cannot be spliced
until D+1 exists, so the settled table published during D's run is for **D-1**
and is copied into that partition (`rclone copyto`, never `sync` — a sync of a
previous date would delete the rest of it). `date=D` therefore has no
`overnight.parquet` until the following night, and
`overnight_meta.json.table_settled` says so. On the first run and after a
retention prune the previous day is simply absent; the phase logs it and skips
the settled table rather than failing.

## Daily automation

`.github/workflows/daily.yml` runs at **04:00 UTC** (after the ~03:26 UTC
`prod-0` release drops) and uploads `legs/` to Cloudflare R2 via `rclone`. It
builds tippecanoe from source (cached by `TIPPECANOE_REF`) for the hexes step.
The `bundle` phase runs `verify_bundle.py` before upload and fails the job on any
check — a bundle with wrong byte offsets is worse than no bundle.

### Configuration

Repo **variable**: `R2_BUCKET` — the R2 bucket name.

Repo **secrets**:

| secret | value |
|---|---|
| `R2_ACCESS_KEY_ID` | R2 API token access key |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` |

R2 has **no egress fees**, so the browser range-fetches partitions directly.

### Frontend read URL

Public bucket base (r2.dev dev URL — rate-limited, not CDN-cached, fine to start):

```
https://pub-135f2252a0074f0b9761b0dc93a75fa5.r2.dev/legs
```

**Data is partitioned by day.** Each day the pipeline processes lives under its own
`date=YYYY-MM-DD/` prefix (the newest `RETENTION_DAYS` days are kept, older pruned).
The date is the *data* date, taken from the release tag.

1. **Discover available days** — fetch the manifest (a browser can't list a bucket):
   ```
   .../legs/dates.json   ->   {"dates": ["2026-07-18", "2026-07-19"], "latest": "2026-07-19"}
   ```
   Default the day picker to `latest`.
2. **A partition** for a chosen `<DATE>`:
   `https://pub-135f2252a0074f0b9761b0dc93a75fa5.r2.dev/legs/date=<DATE>/airports/airport=<ICAO>/data_0.parquet`
3. **The leg index** for that day: `.../legs/date=<DATE>/flights.parquet`
4. **The traffic tiles** for that day: `.../legs/date=<DATE>/traffic.pmtiles`
   (plus `traffic.pmtiles.stats.json`)
5. **The day bundle** for that day: `.../legs/date=<DATE>/` →
   `meta.json`, `legs.parquet`, `cells.bin`, `tracks.bin`
6. **The overnight artifacts**: `.../legs/date=<DATE>/` →
   `airports_utc.parquet`, `overnight_meta.json`, and `overnight.parquet` —
   the last one lands a day later than the other two, since it is settled
   during the *following* night's run (see *Static artifacts for external
   clients*)

**Exactly one file per airport.** The per-airport write is single-threaded so each
partition is a single `data_0.parquet` (DuckDB's parallel partitioned write would
otherwise emit `data_0`, `data_1`, … per busy airport, which a browser can't
discover over HTTP since it can't list a directory). Fetch `data_0.parquet` and
you have the whole airport for that day.

To move to a CDN-cached custom domain later (e.g. `splines.<domain>`), connect it
in **R2 → bucket → Settings → Custom Domains**; only this base URL changes on the
frontend — the pipeline is unaffected.

### CORS

hyparquet and pmtiles.js both issue cross-origin **Range** requests, so set the
bucket CORS policy (**R2 → bucket → Settings → CORS**) to allow your frontend
origin:

```json
[{"AllowedOrigins":["https://timefli.es","http://localhost:4200"],
  "AllowedMethods":["GET","HEAD"],
  "AllowedHeaders":["range","content-type"],
  "ExposeHeaders":["content-length","content-range","accept-ranges","etag"],
  "MaxAgeSeconds":3600}]
```

(`etag` is exposed for pmtiles.js, which uses it to detect an archive changing
underneath a partially-read index.)

(Add `https://www.timefli.es` or other dev ports here if the frontend ever loads
from them — CORS origins must match scheme + host + port exactly.)
