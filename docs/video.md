# Animating a span of days

`render_video.py` turns a stack of daily grids into an mp4. Two modes: `absolute`
(one fixed divisor for the whole run, sequential palette) and `anomaly` (each day
against its own trailing baseline, diverging palette). Use `anomaly` to find
events — a closure is a hole, a reroute is a bright corridor beside a dark one.

## The grid, and why it is not the raster

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

## Four corrections, none of them optional

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

## Backfilling: `build_grid.py`

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

**Count legs, not whole traces.** The difference hides well: it changes only 16%
of lit pixels, so the headline correlation barely moves (r 0.937 → 0.943). Those
16% are the ones that matter — median reference density 7.9 against 0.50
overall, undercounted by ~6 — i.e. the hubs and busy corridors, which is exactly
where a frequency change shows up.

**Do not splice the two into one run.** A 5× step at the join renders as exactly
the kind of jump this tool exists to distinguish from a real event. Every grid
records which path wrote it (`hex_raster.grid_producer`), and `render_video.py`
**refuses** a mixed stack unless `--allow-mixed-producers` is given.

The floor cost is the ~4 GB/day download, which no shortcut removes: about
1.4 TB for a year. That is free and fast on Actions, and it is the blocker
locally — where a targeted window of 30–60 days (120–240 GB, and near-zero disk
because nothing is staged) is the practical option.

## Doing it on Actions instead of locally

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
of the same shape: ~7.7 GB peak against a runner's 16 GB. Both slide a running
total over the stack, needing one `(side, side)` accumulator. Do not replace
that with a float64 cumulative sum: it is bit-identical but, because
`np.concatenate` holds its input and its result at once, costs 15.4 GB on top of
the stack — 23 GB total, which does not run. If a longer span ever runs out of
memory, build the grids at `--grid-zoom 1` (1024 px, a quarter of it) rather
than trimming the render.

## Framing a region: aspect, palette, basemap

`--crop-region` takes a name or a literal `lat0,lat1,lon0,lon1`; `--aspect 16:9`
then grows it, centred, to that ratio. It grows rather than crops, because a
16:9 frame of a squarish region should add sea and sky around it, not cut the
routes off at the edges -- and latitude is solved in mercator, not degrees,
since that is the axis the raster uses. Beware that growing a square box to
16:9 adds a lot of longitude: `mideast` becomes Morocco-to-Vietnam. For a tight
frame, give a box that is already near the ratio.

`--palette paper` is light-grounded, for print and for when the faint structure
is the point: on white the low end is far more visible than on black. Label and
basemap colours flip automatically (`ink_for`), keyed off the luminance of the
palette's own ground.

`--basemap geo/coastline.json,geo/borders.json` draws Natural Earth 1:50m
coastlines and land borders (public domain, stripped of properties and rounded
to ~100 m, which is well under a pixel) UNDER the traffic. It is rasterised once
into a ground layer with bilinear splatting -- a hard one-pixel coastline
crawls once the frame moves -- and every frame is then composited over it using
its own value as opacity, so the outline shows through empty airspace and
traffic covers it where there is traffic. In `anomaly` mode the opacity is
`|t|`, not `t`, because there the neutral value is the middle of the ramp: a
pixel is transparent when it has not changed, whichever way it might have gone.

**Corrections stay global, and the ordering is the point.** `load_days` crops
each grid as it reads it and keeps only per-day scalars from the rest of the
world -- a global total for `partial_days`, the reference region's total for
`coverage_scale`, one total per region for the shot list. Cropping first would
feed `partial_days` a regional total, so a regional collapse would be
classified as an incomplete pipeline run and interpolated away; it would also
leave the coverage reference outside the frame. The scalar series are carried
through the day-dropping and gap-filling alongside the frames, then smoothed
and scaled the same way -- both corrections are linear, so summing a region
over the smoothed stack equals smoothing that region's summed series, which is
what lets the shot list still describe the frames.

That crop is also what makes a fine grid usable: 59 days at `--grid-zoom 4` is
15.8 GB held whole and `rolling_mean` would want twice that, against a region
window under 300 MB.

## Crossing a coverage gap

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
