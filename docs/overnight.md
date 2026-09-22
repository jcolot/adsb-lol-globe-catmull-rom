# Overnight classification

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

## The margin, not the block time

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

## `airport_tz.csv` — the one input the repo lacked

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

## The empirical table

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

## Two filters that are not optional

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

## `splice_legs.py` — the daily cut, and why it biases the wrong way

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

A *tail* is the aircraft's last leg of day D with `arr IS NULL` whose airborne
run reaches the leg's final point (`t_on == t_end`), meaning it was still flying
when the data stopped — a real landing leaves descent or ground fixes after
`t_on`. A *head* is the mirror in day D+1. One of each per aircraft per boundary
makes the key unique, and since any flight under 24 h contains at most one
00:00Z, a leg is never cut into three.

⚠️ **`t_on == t_end` is not by itself a midnight cut**, which an earlier version
of this section wrongly claimed. It says only "still airborne at the last fix",
and that is equally true of an aircraft that flew out of receiver coverage.
Measured on 2026-09-08: of 11,768 legs matching it, only 31.8% had their last
fix within an hour of the boundary and **42.4% were more than six hours from
it** — coverage dropouts, not archive cuts. So a half must also be within
`--max-gap-h` of the boundary. That does not change which pairs are accepted (a
distant tail already fails the gap check), but it cut the misleading "no pair"
count from 18,361 to 7,372 and moved 12,897 legs into a category that says what
they are.

Two more guards, both of which real data was needed to find:

- **A half must carry the endpoint the splice recovers** — a tail needs a `dep`,
  a head an `arr`. Splicing a dep-less tail onto an arr-less head yields a leg
  with *neither* endpoint, useless downstream, and it silently skips the speed
  check for want of airports to measure between. Those were the collapse-bug
  aircraft: 472 of an apparent 1,312 splices, some spanning **48 hours**.
- **`--max-air-h` (default 20).** The longest scheduled nonstop is ~19 h, so a
  splice claiming more is wrong whatever distance it covers — and the speed band
  does not catch them: a 45.6 h `KDSM->VHHH` splice worked out to 254 km/h,
  comfortably inside it. This removed a further 152.

Timestamps still do not *confirm* a match: over the North Atlantic the median
cruise node gap is 2.7 h, so `--max-gap-h` is sized against that rather than
against the cut, and a great-circle speed band checks the result without needing
position data.

| fixture | outcome |
|---|---|
| still airborne at 23:59Z, head next day | spliced |
| complete leg | passes through |
| tail whose aircraft never reappears | rejected, no pair |
| last fix 20:30Z (3.5 h out) | left coverage, not a candidate |
| EGLL tail joined to an EHAM head after 8 h | rejected, 46 km/h |
| a two-day collapsed leg pair | rejected, airborne > 20 h |
| dates two apart (retention gap) | nothing spliced |

**On one real pair of days** (2026-09-08 against 2026-09-09, 126,514 and 128,697
legs): 763 legs spliced across the boundary, 12,897 set aside as coverage
dropouts, and 7,372 tails that genuinely found no partner. The recovered legs
look right — `VTBS->EHAM` 691 min, `CYYZ->CYVR` 281 min, `KDFW->KMIA` 171 min.
Fitting the block model on the result gives `52.2 + 60*d/872` with a residual
median of 8.7 min and p90 of 18.2 min, and the empirical table lands at 8,611
directed routes: 7,900 same-day, 698 `+1`, 13 `+2`, in **70 KB of Parquet** —
which is the sparse-matrix argument settled by measurement rather than
estimate.

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

## Reading the splice

`spliced.parquet` in `date=D` is that day's legs with the midnight cut repaired:
one row per leg *departing* in D's UTC day, `dep`/`arr` both non-NULL, `t_off`
and `t_on` in **absolute** deciseconds with `base_ts = 0` (so the usual
`base_ts + t/10` still yields UTC), and a `spliced` flag.

It is published because the repair was previously computed and thrown away. A
client reading the partitions sees a flight airborne at 00:00Z as two half-legs
with a NULL endpoint each — 46 of them touch EBBR's local day 2026-09-09 alone —
and nothing it could fetch said they were one flight. A spliced row's `leg_id` is
`<tail>+<head>`, and each half now carries its own date, so splitting on `+`
maps straight back to the partition holding that half's geometry:

```js
const [tail, head] = row.leg_id.split('+');
// "2026-09-08_4ca123_7" and "2026-09-09_4ca123_0" -- the date is in the id
```

Two consequences of how it is built. It lands **a day late** — D's tails cannot
be matched until D+1 exists — so `date=D/spliced.parquet` appears during D+1's
run, the same lag `overnight.parquet` has and for the same reason. And unmatched
halves are **dropped, not carried**: a half's `t_on` is the truncation instant,
not a wheels event, and the table exists to be trusted. If you need the halves
themselves, they are still in `flights.parquet`, NULL endpoint and all.

## Validated against BTS

US DOT's [Reporting Carrier On-Time Performance](https://www.bts.gov/topics/airlines-and-airports/number-14-time-reporting)
publishes `WheelsOff` and `WheelsOn` as first-class fields, which is exactly
what `t_off`/`t_on` estimate — so it is a direct comparison, not a proxy. It also
carries `TaxiOut`/`TaxiIn`, `AirTime`, and `Tail_Number`, and `build_legs`
already records `reg`, so the join needs no new plumbing. `airports.csv` has both
`ident` and `iata_code`, which is the ICAO↔IATA bridge BTS needs.

⚠️ **BTS runs ~3 months behind** — in September 2026 the newest month published
is June — so there is no date overlap with any day this pipeline has processed
under the current schema. Per-flight matching by tail number is therefore not
possible yet. What *is* valid is a per-route comparison, because a route's
airborne time is stable month to month. Against June 2026 (607,577 flights) and
2026-09-08/09:

| | |
|---|---|
| matched routes (n ≥ 5 both sides) | **1,217** |
| ADS-B airborne − BTS `AirTime`, median | **−0.9 min** |
| \|difference\|, median / p90 / p99 | **2.7 / 9.5 / 22.8 min** |
| within 10 min | **91.0%** |

The residual bias is short and grows with length — −0.2 min under an hour,
−3.7 min at 4–6 h — which is what losing the ends of a flight to coverage gaps
looks like: a climb-out received late puts `t_off` late, a descent lost early
puts `t_on` early, and both bias the same way.

BTS also settles the taxi constant, the one number here that was frankly a
guess. June route medians are **15.0 min out and 6.0 min in**, so `TAXI_MIN` is
now 21 rather than 25 — treat it as a floor outside the US, since US hubs taxi
long.

**What this can and cannot establish.** BTS is *actuals*, and the classifier is
queried with a *scheduled* arrival, so this validates the mechanism — wheels-off,
wheels-on, taxi, block time — and not `day_offset` itself. BTS reaches `+1` only
implicitly (`ArrTime < DepTime` domestically) and can never reach `+2`. For the
day offset as ground truth you need a schedule source: OAG, Cirium, or an SSIM
file with its Date Variation field.

### What it found: merged legs

The comparison immediately surfaced a defect no synthetic fixture had. On
IAD–EWR, which BTS puts at 44 min, the raw legs split into two clean
populations — **36–71 min and 226–1000 min** — and *every one of them* had
`dep_gnd` and `arr_gnd` true.

So the ground-fix filter does **not** protect against the collapse described
above, contrary to what an earlier version of this section implied. It catches
only the aircraft that emit *no* surface message at all. An aircraft that emits
them at the ends of its day but not at intermediate turnarounds has its
consecutive flights fused into one leg carrying genuine ground fixes at both
ends — and on a shuttle route that is roughly half the legs.

`MERGE_FACTOR` (default 2.0) drops a leg whose airborne time exceeds twice the
distance model's expectation. The effect is exactly the right shape — it removes
garbage without disturbing good data:

| | median \|diff\| | p99 | max |
|---|---|---|---|
| unfiltered | 2.8 min | 41.7 min | 320 min |
| 2× filter | 2.7 min | **22.8 min** | **84 min** |

Only 7 of 1,254 routes moved by more than 10 minutes, but those were the badly
wrong ones: IAD–EWR 242 → 46 (BTS 44), DEN–MDW 227 → 110 (BTS 110). The
per-route *median* was already absorbing most of the contamination, which is why
the headline barely moves — the fix is for the tail.

It also mattered for the fit: dropping merged legs took the block-model residual
from a median of 8.7 min to **5.0**, because those legs were inflating the
intercept by ~11 min. The threshold uses the fitted coefficients and is
therefore mildly self-referential, but 2× is loose and one iteration converges
(41.0/856 → 40.8/855).

## Static artifacts for external clients

There is no server: clients range-read static files from R2. So the classifier
ships as **data**, in three files per day.

| file | partition | what |
|---|---|---|
| `airports_utc.parquet` | **bucket root** | the same table spanning the whole retained window — one URL, one fetch, every date |
| `airports_utc.parquet` | `date=D` | 6,372 airports: `ident`, `lat`/`lon` (degrees x 1e5), `tz`, and `off_dp1`/`off_d0`/`off_dm1`/`off_dm2` — UTC offset in **minutes** for D+1, D, D-1, D-2 |
| `overnight.parquet` | `date=D-1` | the settled tier-1 lookup, `(dep, arr, arr_local_hour) -> day_offset` |
| `spliced.parquet` | `date=D-1` | the repaired legs the table was fitted on — see *Reading the splice* |
| `overnight_meta.json` | `date=D`, and the root | tier-2 model coefficients, `tz_columns`/`tz_dates`, and whether a settled table exists |

**Two copies of the offset table, deliberately.** The per-date one is part of a
self-contained partition and resolves only its own D+1…D-2. The root one is
built with `--tz-back $RETENTION_DAYS`, lives outside `date=` where the prune
never reaches, and is rewritten every run — so a client asking about a local day
three weeks back has somewhere to get that day's offset. Without it the question
was unanswerable: the oldest partitions predate the artifact entirely, and the
ones that have it only speak for their own three days. Widening it is cheap —
measured 89 KB over 4 dates, 160 KB over 32, i.e. ~2.5 KB a date against a
fixed ~80 KB of idents and zone names.

**`off_dp1` exists for the closing midnight.** A local day ends at midnight of
*L+1*, resolved with *L+1*'s offset, and on a DST-transition date that is not
`off_d0` — the local day is 23 or 25 hours long. Without the column the newest
partition could not close its own local day.

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

⚠️ **Check which dates a copy of the table covers, and throw outside them.**
`overnight_meta.json` carries `tz_columns` and the `tz_dates` they resolve, in
the same order, so a client maps a date to a column by lookup instead of
reproducing the `d0`/`dm1` arithmetic. A per-date copy covers D+1…D-2, which is
exactly enough for the overnight question: arrival on D, and a departure up to
two days earlier for a westbound date-line crossing. Reading a D-2 arrival out
of *today's* file needs D-3 and the decoder throws — that guard is deliberate.
Use the root copy when you need a date outside the partition's own window.

Two passes are needed on the departure side: its local date is not known until
it is computed, and its offset depends on that date. One correction is enough,
since a DST step is at most an hour.

```js
const DAY = 1440;

// meta.tz_dates[i] is the date meta.tz_columns[i] resolves; both come from
// overnight_meta.json and are ordered newest-first (off_dp1, off_d0, off_dm1…)
function offMin(rec, meta, dateMs) {
  const iso = new Date(dateMs).toISOString().slice(0, 10);
  const i = meta.tz_dates.indexOf(iso);
  if (i < 0) throw new Error(
    `${iso} outside this table: ${meta.tz_dates.at(-1)}..${meta.tz_dates[0]}`);
  return rec[meta.tz_columns[i]];
}

// arrLocal: {y, m, d, hh, mm} wall clock at the ARRIVAL airport.
export function classify(dep, arr, arrLocal, apt, meta, blockMin) {
  const { y, m, d, hh, mm } = arrLocal;
  const arrDateMs  = Date.UTC(y, m - 1, d);
  const arrLocalMin = Date.UTC(y, m - 1, d, hh, mm) / 60000;
  const arrUtcMin = arrLocalMin - offMin(apt[arr], meta, arrDateMs);
  const depUtcMin = arrUtcMin - blockMin;

  let off = offMin(apt[dep], meta, arrDateMs);
  let depLocalMin = depUtcMin + off;
  const depDateMs = Math.floor(depLocalMin / DAY) * DAY * 60000;
  if (depDateMs !== arrDateMs) {
    const off2 = offMin(apt[dep], meta, depDateMs);
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

## `overnight_client.py` — the same contract, in Python

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
