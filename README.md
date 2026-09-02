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
3. **`build_hexes.py`** — `points_legs.parquet → traffic.pmtiles` (H3
   traffic-density vector tiles; a global overview layer, see below).
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

## H3 traffic-density tiles (`traffic.pmtiles`)

A **second, complementary layer**, not a replacement: hexbins can't be animated or
drawn as flight paths, so `legs/airports/` stays the detail layer and this is what
the frontend shows at world zoom, where fetching per-airport partitions makes no
sense. One file per day, range-fetched — no directory listing needed.

### What a hexagon means

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

### Zoom ↔ resolution

One H3 resolution per zoom level (`res = z - 2`), so a hexagon holds a roughly
constant on-screen size:

| source zoom | layer | H3 res | hex edge |
|---|---|---|---|
| 2 | `h0` | 0 | 1108 km |
| 3 | `h1` | 1 | 419 km |
| 4 | `h2` | 2 | 158 km |
| 5 | `h3` | 3 | 59.8 km |
| 6 | `h4` | 4 | 22.6 km |
| 7 | `h5` | 5 | 8.5 km |
| 8 | `h6` | 6 | 3.2 km |

Above z8 MapLibre overzooms the z8 tiles — hexes just grow, which is the natural
visual handover to the spline layer. `--max-res` raises the ceiling (res 7 ≈ 1.2 km,
res 8 ≈ 460 m) at a steep cost in cells; keep `--step-km` at or below half the
finest hex edge.

### Feature properties

| property | meaning |
|---|---|
| `n` | distinct flights through the cell |
| `d` | **0–255, `log(n)` normalised against that resolution's p99** |
| `a` | mean altitude in the cell, feet |
| `amin` | minimum altitude in the cell, feet |

**Ramp opacity off `d`, not `n`.** Raw counts aren't comparable across resolutions
(a res-0 cell swallows ~117× the area of a res-2 cell), so a single ramp on `n`
blows out at world zoom and vanishes when you zoom in. `n` is for tooltips.

### Frontend

Each resolution lives at exactly **one** source zoom, so a given map zoom fetches
exactly one source zoom whose tile contains exactly one of the `h*` layers. That
means the fill layers need **no `minzoom`/`maxzoom` at all** — only the layer
actually present in the fetched tile draws:

```js
maplibregl.addProtocol("pmtiles", new pmtiles.Protocol().tile);
const src = "pmtiles://" + BASE + "/date=" + date + "/traffic.pmtiles";
map.addSource("traffic", {type: "vector", url: src});
for (let r = 0; r <= 6; r++) map.addLayer({
  id: "h" + r, type: "fill", source: "traffic", "source-layer": "h" + r,
  paint: {
    // hue by mean altitude: ground amber -> cruise cyan
    "fill-color": ["interpolate", ["linear"], ["get", "a"],
                   0, "#ffb347", 8000, "#ff5e7a", 20000, "#a855f7", 36000, "#22d3ee"],
    // opacity off the per-resolution normalised density
    "fill-opacity": ["interpolate", ["linear"], ["get", "d"], 0, 0.05, 255, 0.9],
    "fill-antialias": false,   // no strokes: tippecanoe clips hexes at tile
  },                           // edges, so outlines would show seams
});
```

`build_hexes.py` also writes `traffic.pmtiles.stats.json` next to the archive
(cell count, max `n` and the p99 used for `d`, per resolution) — worth watching
day over day, and enough for the frontend to renormalise `n` itself if it wants a
ramp other than `d`.

Known limitation: cells straddling the antimeridian are emitted twice (shifted
±360°) so tippecanoe clips each copy to the world and both halves draw.

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

### What this replaces

`points_legs.parquet` is no longer uploaded — `tracks.bin` supersedes it, and
keeping both would roughly double per-day storage.

`legs/airports/airport=<ICAO>/data_0.parquet` is **still built and uploaded** so
the current frontend keeps working, but the bundle makes it redundant: `dep`/`arr`
are columns you can filter in memory. Retiring it is a follow-up once the frontend
moves over — worth doing, because `build_legs.py` currently writes every leg's
nodes **twice** (once under its departure partition, once under its arrival).

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
