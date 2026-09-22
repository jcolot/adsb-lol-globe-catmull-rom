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
about 20 MB of index shipped up front that makes every flight findable by
callsign/registration/hex/route and answers "which flights went through this box, in this
hour?" with **zero further requests**, then one HTTP range read per track drawn.

### Stages

1. **`fit_spline.py`** — `traces/ → nodes.parquet` + `aircraft.parquet`, plus
   `callsigns.parquet` and `events.parquet`. Per-second decimation (mean position
   **and** time), stationary-gate snapping, ground-elevation reference, greedy
   CR-node placement. **`events.parquet` has no consumer yet** — `build_legs.py`
   reads `callsigns.parquet` but not the gate dwells and airborne runs, so that
   file is written and unread.
2. **`build_legs.py`** — `nodes.parquet → legs/` (per-airport partitions +
   `flights.parquet` index), stamping the callsign from `callsigns.parquet` onto
   each leg.
3. **`build_hexes.py`** — `points_legs.parquet → traffic-raster.pmtiles` (H3
   traffic density as a raster overview; `hex_raster.py` renders it). The vector
   hexbin archive is optional and **off by default** — see [docs/h3-tiles.md](docs/h3-tiles.md).
4. **`build_bundle.py`** — `points_legs.parquet → legs.parquet + cells.bin +
   tracks.bin + meta.json` (the queryable **day bundle** — see [docs/bundle-format.md](docs/bundle-format.md)).
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
| `leg_id` | string | `{date}_{icao}_{k}` — unique across partitions, not just within one (schema 4+; older days are `{icao}_{k}`) |
| `dep`, `arr`, `reg`, `type` | | leg / aircraft metadata |
| `base_ts` | int64 | **absolute UTC seconds = `base_ts + t/10`.** `t` alone is relative and not comparable between aircraft |
| `t_off`, `t_on` | int64 | that leg's wheels-off / wheels-on, same units as `t`. Filter time on these, not on the first and last node — the node span includes taxi and ramp |
| `dep_gnd`, `arr_gnd` | bool | false means that end never emitted a surface message, so the airport and the wheels time there are approximations |

**Frontend:** draw a centripetal Catmull-Rom through consecutive nodes, starting a
new curve at every `cusp` node. The last five columns are constant within a leg
and cost 1.41% of the file; they are there so this one file answers "which
flights were here between two instants" without also fetching the day index —
see [Frontend: all flights at one airport, for a local day](docs/bundle-format.md#frontend-all-flights-at-one-airport-for-a-local-day).

## Run locally

```bash
pip install -r requirements.txt
python3 fit_spline.py path/to/traces --ground-elevation \
    --parquet nodes --tol-ground 2 --tol-cruise 150 --corner 35
python3 build_legs.py --traces nodes/nodes.parquet \
    --meta nodes/aircraft.parquet --callsigns nodes/callsigns.parquet \
    --out-dir out/legs
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

## Documentation

The design notes, format specs and ops guides live in [`docs/`](docs/):

| document | what it covers |
|---|---|
| [H3 traffic-density tiles](docs/h3-tiles.md) | why the overview draws a raster and not hexagons, what a hexagon means, zoom ↔ resolution, feature properties |
| [Animating a span of days](docs/video.md) | the daily density grid, the four corrections it needs, backfilling with `build_grid.py`, rendering on Actions, region framing, coverage gaps |
| [The day bundle](docs/bundle-format.md) | byte layout of `legs.parquet`, `cells.bin`, `tracks.bin` and the res-0 directory, plus the two frontend query recipes |
| [Cell query brief](docs/frontend-cell-query.md) | worked frontend implementation of "which planes crossed this H3 cell between T0 and T1?" |
| [Overnight classification](docs/overnight.md) | the date-offset question, why the margin matters more than the block time, `airport_tz.csv`, the daily splice, BTS validation, the published artifacts |
| [Daily automation](docs/deployment.md) | the GitHub Actions schedule, configuration, R2 read URLs and CORS |
