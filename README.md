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

It also builds a derived overview layer over the same day: **H3 traffic-density
vector tiles** (`traffic.pmtiles`), one file per day, for the zoom levels where
fetching per-airport partitions makes no sense.

### Stages

1. **`fit_spline.py`** — `traces/ → nodes.parquet` + `aircraft.parquet`.
   Per-second decimation (mean position **and** time), stationary-gate snapping,
   ground-elevation reference, greedy CR-node placement.
2. **`build_legs.py`** — `nodes.parquet → legs/` (per-airport partitions +
   `flights.parquet` index).
3. **`build_hexes.py`** — `points_legs.parquet → traffic.pmtiles` (H3
   traffic-density vector tiles; a global overview layer, see below).

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
```

`build_hexes.py` needs [tippecanoe](https://github.com/felt/tippecanoe) ≥ 2.17 on
`PATH` (`brew install tippecanoe`) and installs DuckDB's `h3` **community**
extension on first run, so that step needs network access. `--no-tiles` stops
after the per-resolution `.geojsonl` files if you only want the aggregation.

`run_pipeline.sh` does the whole daily job end-to-end (resolve latest release →
stream-extract → fit → legs → hexes → upload to R2). It streams the ~4 GB
split-tar
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

## Daily automation

`.github/workflows/daily.yml` runs at **04:00 UTC** (after the ~03:26 UTC
`prod-0` release drops) and uploads `legs/` to Cloudflare R2 via `rclone`. It
builds tippecanoe from source (cached by `TIPPECANOE_REF`) for the hexes step.

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
