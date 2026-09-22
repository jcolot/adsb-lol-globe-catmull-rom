# The day bundle

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

## `legs.parquet` — one row per leg, shipped whole

`lid`, `icao`, `leg_id`, `reg`, `type`, `dep`, `arr`, `t0`, `t1`, `n_nodes`, the
bounding box (`min_lat`…`max_lon`, degrees × 1e5), `min_alt`/`max_alt` in feet,
and `off`/`len` — the byte range of that leg's record in `tracks.bin`.

`lid` is this file's row index and means nothing outside it; `leg_id` is the
stable identity that joins to `flights.parquet`, the per-airport partitions and
`spliced.parquet`, and from schema 4 on it carries its own date.

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

## `cells.bin` — H3 × time inverted index, shipped whole

```
offset   size               field
0        8                  magic "ADSBIDX1"
8        1                  version    4
9        1                  res        H3 resolution (default 4)
10       1                  n_buckets  time buckets, DERIVED from the span
11       1                  — padding —
12       4                  n_cells    uint32
16       4                  n_legs     uint32
20       4                  n_groups   uint32, distinct (cell, bucket) pairs
24       4                  bucket_ds  uint32, bucket WIDTH in deciseconds
28       4                  n_res0     uint32, res-0 cells with traffic
32       8 x n_res0         dir_cell[] uint64, ascending res-0 H3 index
…        4 x (n_res0+1)     dir_coff[] uint32 prefix index into cell[]
…        8 x n_cells        cell[]     uint64, ascending H3 index
…        4 x (n_cells+1)    goff[]     uint32 prefix offsets into bucket[]
…        4 x (n_cells+1)    coff[]     uint32 prefix BYTE offset into post[]
…        1 x n_groups       bucket[]   uint8, ascending within a cell
…        —                  post[]     per group: varint n_pairs, then n_pairs
                                       gap-coded ascending lids
```

A **group** is one `(cell, time bucket)`. Bucket *b* covers
`[b * bucket_ds, (b+1) * bucket_ds)` deciseconds from `meta.t_epoch`. Lookup is
a binary search over `cell[]` for the cell, then a scan of its `goff` run keeping
the groups whose `bucket[]` falls in the window, prefix-summing `glen[]` from
`coff[]` to find each one's slice of `post[]`.

**The width is the parameter (`--bucket-minutes`, default 60); the count is
derived from the data span.** Getting this backwards is a trap. "24 buckets per
day" looks equivalent and breaks the moment the input spans more than one UTC
day — which is what `splice_legs.py` produces, since a flight airborne at 00:00Z
is cut in half by the archive boundary, and what a *local* day is for most of the
world: Los Angeles runs 07:00Z to 07:00Z. With a per-day count every hour past
the 24th clamps into the last bucket; measured on a two-day stitch, bucket 23
held **30% of all groups** against ~750 for its neighbours — a bin holding 25
hours of traffic, with no false negatives and no resolution either. Deriving the
count gives that same stitch 48 real hourly buckets, with its last bucket at 810
groups against an 861 average. `verify_bundle.py` checks both that the count
covers the span and that the last bucket is not a dumping ground.

`n_buckets` is a `uint8`, so hourly reaches 255 hours ≈ 10 days; past that the
build fails and tells you to widen the bucket. A reader must take `bucket_ds`
and `n_buckets` from the header and never derive them from 86,400.

## The res-0 directory — range-read a viewport, don't fetch the world

Because cells are sorted by H3 index, every **res-0** cell's descendants form
exactly one contiguous run of `cell[]`. `dir_cell[]` names the res-0 cells that
carry traffic and the two prefix arrays give each one's slice, so a client
binary-searches the directory and range-reads only the region it is showing.

Measured on 2026-09-20: **120 of the 122** res-0 cells carry traffic, the
directory costs **1.9 KB** of a 15.34 MB index (0.012%), and reading the res-0
cell containing Brussels takes **1.38 MB in 7 range requests — 9.2% of the
file** — returning answers identical to the whole-file reader on all 2,399 of
its res-4 cells, all-day and for a 15-minute window.

Partitioning this way costs **nothing**: every res-4 cell has exactly one res-0
parent, so `cell[]`, `goff[]` and `coff[]` divide with no overlap. A split by
hour cannot do that — it duplicates the cell table, and measured at 1.58×.
Traffic is very unevenly spread, which helps here: largest partition 1.41 MB,
median **14 KB**, top 3 res-0 cells holding 29% of all postings.

This is the same shape as PMTiles — one object, directory in the header,
spatially clustered payload, HTTP range reads — but keyed by H3 rather than by
Mercator tiles, because the two grids do not nest: a res-4 hexagon straddles
tile edges, so a tile-keyed index would duplicate posting lists and fragment
the gap coding that gets them to 1.36 B/pair.

Note what this does **not** solve. One res-0 partition still references 16–21%
of all legs (Brussels' holds 20,787 of 113,522), and `legs.parquet` is ordered
by `(dep, t0)` rather than geographically, so those rows are scattered through
it. A regional view is therefore ~1.4 MB of index plus whatever it takes to get
leg metadata — the whole 4.6 MB today.

Cells stay sorted by H3 index because a parent's descendants form exactly **one**
contiguous run in that order, so "everything under this cell" is a single slice
and the client can pick its covering resolution by zoom.

**Every group states its own length**, as a varint pair count in front of its
gaps, so a cell's byte range from `coff[]` is entirely self-describing. A side
table of lengths — which v3 had — is a varint *stream*: readable, but not
indexable, because entry *g* does not sit at `4*g`. Reaching a cell's lengths
meant decoding every length before it. Measured on 2026-09-20, one click on the
Brussels cell walked **43,833 bytes** of lengths belonging to other cells to
reach the **33 bytes** it wanted — a 1359× waste. A flat `uint32` per group
would be indexable but costs 4.55 MB against that table's 1.14 MB.

Folding the length into the payload is free: a pair count and a byte length are
both ~1 byte per group, so `post[]` grows by exactly what the table gave up.
Measured, v4 came out **1,268 bytes smaller** than v3 on the same day.

What it buys, for the click-a-cell interaction:

| | v3 | v4 |
|---|---|---|
| first click in a region | 85.5 KB | **22.5 KB** |
| each further click | 44 KB | **2.4 KB** |

`coff[]` is a byte offset per **cell**, not per group, for the same reason: a
`uint32` per group would be 4.55 MB against 0.64 MB, and per-cell is the
granularity a client asks at — it clicks a cell, reads that cell's range, and
walks the self-delimiting groups inside.

**Why the time dimension.** Without it a cell query can only be narrowed by each
leg's `[t0, t1]`, and that span is the whole flight while its time inside one
res-4 cell is minutes. Measured on 2026-09-20 for "who was in this cell during
this window", against the exact per-visit answer:

| cell, window | span prune alone | hourly index | exact |
|---|---|---|---|
| Brussels, 15 min | 388 (8.1×) | **89 (1.9×)** | 48 |
| Heathrow, 15 min | 760 (7.2×) | **182 (1.7×)** | 106 |
| enroute over the Alps, 15 min | 169 (**24.1×**) | **19 (2.7×)** | 7 |
| Brussels, 1 h | 452 (4.2×) | **176 (1.6×)** | 108 |
| Heathrow, 3 h | 887 (1.7×) | **593 (1.2×)** | 508 |

Those multiples are `tracks.bin` range reads the client no longer makes — 4–9×
fewer — and the worst case was an enroute cell, which is where the question is
most interesting.

**A sample claims the buckets out to its neighbours in time, not just its own.**
This is the one subtle part of the build and it is not optional. The densifier
samples the curve by **distance**, so a stationary aircraft is barely sampled in
time at all: bucketing each sample on its own timestamp put a leg parked in the
Brussels cell from 06:25 to 21:05 into buckets 6, 20 and 21 only — a fourteen
hour hole in which every query missed it. The same defect at the other extreme
is one sample interval wide: a leg whose last in-cell sample sat at 11:59:28 was
absent from the 12:00 bucket though it was still in the cell at 12:00:42.
Claiming out to each neighbour fixes both with one rule and no threshold, since
between consecutive samples the curve moves at most `step_km` and cannot leave a
~45 km cell and return. Where the neighbour lies outside the cell the claim
over-reaches by at most one sample interval — a false positive, never a false
negative. It costs 4.6% more pairs and 0.41 MB.

`verify_bundle.py` asserts the invariant directly: two consecutive nodes in the
same cell bracket an interval the aircraft provably spent there, so every bucket
between them must be posted.

**Size.** Hourly is not free, and the honest numbers are:

| `--bucket-minutes` | cells.bin | of which postings | B/pair | shipped whole |
|---|---|---|---|---|
| 0 (no time dimension) | 9.23 MB | 6.36 MB | 1.41 | 13.8 MB |
| 360 (6 h) | 11.05 MB | 7.69 MB | 1.62 | 15.6 MB |
| 180 (3 h) | 12.34 MB | 8.58 MB | 1.75 | 16.9 MB |
| **60 (1 h, default)** | **15.34 MB** | 10.51 MB | 1.98 | **19.9 MB** |
| 30 | 18.24 MB | 12.36 MB | 2.12 | 22.8 MB |

Hourly costs **+6.1 MB** over no time dimension, and a pair count alone does not
predict that. Pairs grow only 1.18×; what actually costs is that splitting a
cell's postings by hour leaves each list sparser over the same `lid` range, so
gap coding degrades from 1.41 to 1.98 bytes per pair, plus 1.95 MB of group
tables. If 19.9 MB resident is too much, `--bucket-minutes 180` keeps most of the
precision win for 3.0 MB less (`IDX_BUCKET_MIN` in `run_pipeline.sh`).

Note that `--bucket-minutes 0` is **not** byte-identical to a v1 index (9.23 vs
8.26 MB): it carries the v2 group tables for a single group per cell. Use it to
turn the time dimension off, not to reproduce the old file.

## `tracks.bin` — per-leg records, range-read

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

## Frontend: answering "all flights through this box"

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

## Frontend: all flights at one airport, for a local day

A local day is a UTC window, and that window is **not** a `date=` partition. Get
that wrong and the error is not a rounding one: it is the wrong flights.

```js
// 1. local midnight -> UTC, from the ROOT airports_utc.parquet (spans the
//    retained window; the per-date copies only speak for their own few days)
const off  = offMin(apt[icao], meta, Date.UTC(y, m - 1, d));          // minutes
const off1 = offMin(apt[icao], meta, Date.UTC(y, m - 1, d + 1));      // DST days
const w0 = Date.UTC(y, m - 1, d)     / 1000 - off  * 60;
const w1 = Date.UTC(y, m - 1, d + 1) / 1000 - off1 * 60;   // 23, 24 or 25 h

// 2. the UTC dates that window touches -- one or two, never three
const partitions = [...new Set([w0, w1 - 1].map(
  s => new Date(s * 1000).toISOString().slice(0, 10)))];

// 3. one request per partition -- the per-airport file carries the leg's
//    base_ts / t_off / t_on / ground flags alongside every point, so nothing
//    else has to be fetched to know which flights these are
const legs = new Map();                        // `${date}/${leg_id}` -> points
for (const date of partitions) {
  if (!manifest.dates.includes(date)) markIncomplete(date);   // see below
  for (const r of await parquet(
        `legs/date=${date}/airports/airport=${icao}/data_0.parquet`)) {
    const t = r.dep === icao ? r.t_off : r.t_on;              // wheels, not ramp
    const utc = r.base_ts + t / 10;
    if (utc < w0 || utc >= w1) continue;
    push(legs, `${date}/${r.leg_id}`, r);      // key by DATE + leg_id, see below
  }
}
```

An eastern airport reaches **backwards** (EBBR at +02:00 opens its day at 22:00Z
the previous date), a western one reaches **forwards** (KLAX at −07:00 closes at
07:00Z the next), which is why the second partition is sometimes one that does
not exist yet.

**Filter on `t_off`/`t_on`, never `t_start`/`t_end`.** The envelope carries taxi
and ramp time: measured on 2026-09-09 over legs with a ground fix at both ends,
`t_off - t_start` is 8.0 min at p50 and 21.5 at p90, and `t_end - t_on` is 4.5
and 12.4. A flight that pushes back at 23:55 local and lifts off at 00:06 is
tomorrow's departure, and the envelope puts it in today.

Measured for EBBR, local day 2026-09-09 (`+02:00`, so 2026-09-08 22:00Z →
2026-09-09 22:00Z), against both partitions:

| | departures | arrivals |
|---|---|---|
| from `date=2026-09-09` | 305 | 290 |
| from `date=2026-09-08` | **6** | **15** |
| local day, total | 311 | 305 |
| reading `date=2026-09-09` alone | 311 | 313 |

The departure counts matching is a coincidence — the *sets* differ by 6 at each
end of the day. That is the error a naive one-partition read makes: not a small
count, but the wrong flights, and always the same ones (the late-evening bank,
which is exactly what an airport-day view is usually asked about).

Three things the query has to handle, none of them optional:

- **`leg_id` is globally unique — but only from `flights_schema` 4 on.** It is
  `{date}_{icao}_{k}`, so the id alone is a key across partitions. It used to be
  `{icao}_{k}` with `k` restarting every day: 78,148 ids were shared between
  2026-09-08 and 2026-09-09, meaning different flights in each, and a client
  spanning a local midnight silently merged them. Partitions already in the
  bucket still carry the old form, so check `dates.json`'s `days[date]
  .flights_schema` and key by `(date, leg_id)` for anything below 4.
- **The neighbouring partition is often missing.** The manifest has real holes
  (six of the 36 days before 2026-09-18), the newest day's successor does not
  exist until the next run, and retention eventually eats the predecessor. Check
  `manifest.dates` for *both* partitions and show a partial day as partial —
  a western airport's local day is never complete on the day itself.
- **The retained window is not schema-homogeneous.** Partitions built before
  `t_off`/`t_on`/`base_ts` existed cannot be put on an absolute clock at all.
  `dates.json`'s `days` map carries each date's `flights_schema` (see
  `legs_meta.json`), and a date absent from it predates the map — feature-detect
  its columns, or leave it out of a local-time picker rather than dating it
  wrong.

Cost, for EBBR: the root offset table (160 KB, cached across every query) plus
one 0.73 MB request per partition. The day-wide `flights.parquet` is **not** on
this path — carrying `base_ts`/`t_off`/`t_on`/`dep_gnd`/`arr_gnd` into the
per-airport file costs 1.41% of it and saves fetching 3.6 MB to read five
values per leg.

## What this replaces, and what it doesn't

`points_legs.parquet` is no longer uploaded — nothing reads it, and `tracks.bin`
supersedes it at a third less size (measured on 2026-09-01: 161 MB vs ~106 MB for
the same 14.4 M nodes).

`legs/airports/airport=<ICAO>/data_0.parquet` is **still built and uploaded, and
should stay that way**. It carries each leg's `base_ts`, `t_off`, `t_on`,
`dep_gnd` and `arr_gnd` alongside the points, so one request answers both "which
flights" and "draw them" without the day-wide index.

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
