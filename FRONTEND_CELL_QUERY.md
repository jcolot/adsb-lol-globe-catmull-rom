# Brief: "which planes crossed this H3 cell between T0 and T1?"

For the frontend agent. Everything below is measured against the live bucket,
data date **2026-09-20** (113,522 legs, 12.46 M nodes).

> **Reads `cells.bin` v1 and v4.** v4 keys the index by `(cell, hour)` rather
> than `(cell)`, which is what makes this query cheap; carries a **res-0
> directory** so you can range-read one region instead of the whole file; and
> lets each group state its own length, so **one cell click costs ~2.4 KB**.
>
> **The bucket will hold both versions for a while.** The retained days on R2
> were built at v1 and are not rewritten; only days built from the next pipeline
> run onward are v4, so the day picker spans a mix until v1 days age out of
> `RETENTION_DAYS`. The reader below handles both and exposes `timeIndexed`,
> which is **false** on a v1 day — there `get()` ignores your time window and
> you must fall back to the leg-span prune or the geometry pass. Both paths are
> tested against the real live v1 file and a real v4 build; see **Version
> transition** below.

## The one-paragraph answer

The day bundle answers this with **zero network requests**: `cells.bin` turns a
(cell, time window) pair into a candidate leg list, and `legs.parquet` — already
resident — gives you each candidate's identity, route, times and bbox. You only
range-read `tracks.bin` for the legs you actually intend to *draw*.

The index is exact to the **hour** and approximate inside it. For a 15-minute
window it over-reports by about **1.7×–2.7×** against the exact per-visit
answer; an hour-aligned window is tighter still (1.2×–1.6×). If you need exact
sub-hour times, refine the survivors with the stage-3 geometry pass below, but
that is now an optional polish on a short list rather than the only way to get a
sane answer. For contrast, pruning on each leg's `[t0,t1]` span alone — the only
option before v2 — over-reports by **4×–24×**, because a leg's span is the whole
flight (median 2 h, p90 10 h) while its time *inside* one res-4 cell is minutes
(median 2.2 min enroute, 7 min over an airport).

## What you are reading

Base: `https://pub-135f2252a0074f0b9761b0dc93a75fa5.r2.dev/legs`

```
/dates.json                     {"dates":[...], "latest":"2026-09-20"}
/date=<DATE>/meta.json          units, counts, t_epoch, index_res, buckets
/date=<DATE>/legs.parquet       4.6 MB  one row per leg    -- load whole
/date=<DATE>/cells.bin         15.3 MB  (H3, hour) -> legs -- either (see below)
/date=<DATE>/tracks.bin        94.0 MB  node geometry      -- RANGE-READ ONLY
```

`tracks.bin` is the only file you must never fetch whole. `legs.parquet` you do
fetch whole — `lid` indexes it directly and the rows you need are scattered
through it. **`cells.bin` is your choice**, and the tradeoff is real:

| | load whole | range-read per res-0 cell |
|---|---|---|
| up front | 15.3 MB (v1 day: 8.3 MB) | nothing |
| first click in a region | 0 requests | 22.5 KiB, 6 requests |
| each further click there | 0 requests, instant | 2.4 KiB, 4 requests |
| a click in a new region | 0 requests | another 20.2 KiB of scaffolding |
| whole-world view | already have it | would fetch most of the file anyway |

**Load whole if the view is the globe, or if the user will roam.** One fetch and
then every cell, every hour, every region answers locally with no request and no
latency — which for a click-to-explore interaction is a better experience than
paying a round trip per click, and 15.3 MB is one cacheable object.

**Range-read if the view opens on a region** and you want the first answer before
15 MB has landed. The res-0 directory is what makes that possible: every res-0
cell's descendants are one contiguous run, so a region is a handful of ranges.
See **Version transition** — a v1 day has no directory, so it is load-whole only.

With `meta.json` + `legs.parquet` + `cells.bin` resident that is **19.9 MB for a
v4 day** and **12.8 MB for a v1 day**, after which every cell query is local
until you need geometry.

Key facts from `meta.json`, all of which you should read rather than hardcode:

| field | 2026-09-20 | meaning |
|---|---|---|
| `index_res` | `4` | the **only** resolution in `cells.bin` (~45 km across) |
| `t_epoch` | `1789862400` | UTC midnight of the data date, unix **seconds** |
| `t_unit` | `"ds"` | all `t`/`t0`/`t1` are **deciseconds** from `t_epoch` |
| `q_pos` | `100000` | lat/lon are degrees × 1e5, as int32 |
| `step_km` | `11.305` | spacing the index sampled the curve at |
| `index_buckets` | `24` | time buckets per day in `cells.bin` |
| `bucket_ds` | `36000` | bucket width in deciseconds (36,000 ds = 1 h) |
| `index_groups` | `1137849` | distinct `(cell, bucket)` pairs |
| `index_res0` | `120` | res-0 cells with traffic (of 122) |

`lid` **is the row index** in `legs.parquet` — verified true on this day — so a
posting list indexes the leg table directly with no map.

## The query, staged, with real numbers

Measured on 2026-09-20, window 12:00–12:15 UTC unless noted:

| | EBBR cell | LHR cell | Alps cell (enroute) | EBBR, 1 h window |
|---|---|---|---|---|
| 1. `cells.bin` postings | 1,286 | 2,342 | 369 | 1,286 |
| 2. + leg span overlaps window | 388 | 760 | 169 | 452 |
| 3. + a **visit** overlaps window | **48** | **106** | **7** | **108** |
| stage 2 over-report | **8.1×** | **7.2×** | **24.1×** | 4.2× |
| stage 3 cost | 388 reads / 577 KiB | 760 / 1,260 KiB | 169 / 304 KiB | 452 / 644 KiB |
| matching visit dwell, median | 6.9 min | 17.8 min | 2.2 min | 3.7 min |

Two things to read off that table. The over-report is **worst exactly where the
question is most interesting** — an enroute cell, where the aircraft is present
for 2 minutes out of a 10-hour leg. And stage 3 is affordable: a few hundred
range reads over 304–1,278 KiB, not a re-download.

## Stage 3 is per-*visit*, not per-leg min/max

The first version of this measurement bracketed each leg's in-cell time as
`min(t)…max(t)` over its in-cell samples, and got a median dwell of **250
minutes** at EBBR. That number is an artifact: an aircraft parked at Brussels is
in that cell at 04:00 and again at 16:00, so its min/max spans every window in
between. Legs hit the same cell up to **26 separate times** on this day (median
1). You must group in-cell samples into **contiguous runs** and test each run
against the window. Using min/max instead inflated the EBBR 15-min answer from
48 legs to 79.

## Code

### Loading

```js
import { parquetRead } from 'hyparquet';
import * as h3 from 'h3-js';

const BASE = 'https://pub-135f2252a0074f0b9761b0dc93a75fa5.r2.dev/legs';

export async function loadDay(date) {
  const D = `${BASE}/date=${date}`;
  const [meta, cellsBuf] = await Promise.all([
    fetch(`${D}/meta.json`).then(r => r.json()),
    fetch(`${D}/cells.bin`).then(r => r.arrayBuffer()),
  ]);
  const legs = {};
  await parquetRead({
    file: await fetch(`${D}/legs.parquet`).then(r => r.arrayBuffer()),
    rowFormat: 'object',
    onComplete: rows => {
      const n = rows.length;
      legs.t0 = new Uint32Array(n); legs.t1 = new Uint32Array(n);
      legs.off = new Float64Array(n); legs.len = new Uint32Array(n);
      legs.icao = new Array(n); legs.dep = new Array(n); legs.arr = new Array(n);
      legs.reg = new Array(n); legs.type = new Array(n);
      legs.flight = new Array(n);          // callsign, nullable
      for (let i = 0; i < n; i++) {
        const r = rows[i];
        // lid IS the row index; don't build a map, but do assert it once
        if (r.lid !== i) throw new Error(`legs.parquet: lid ${r.lid} at row ${i}`);
        legs.t0[i] = r.t0; legs.t1[i] = r.t1;
        legs.off[i] = Number(r.off);   // uint64 -> BigInt from hyparquet
        legs.len[i] = r.len;
        legs.icao[i] = r.icao; legs.dep[i] = r.dep; legs.arr[i] = r.arr;
        legs.reg[i] = r.reg; legs.type[i] = r.type;
        // absent entirely on days built before the callsign existed
        legs.flight[i] = r.flight ?? null;
      }
    },
  });
  return { meta, legs, idx: new CellIndex(cellsBuf), tracks: `${D}/tracks.bin` };
}
```

### `cells.bin` reader

**The header moved.** v2 added `n_buckets`, `n_groups` and `bucket_ds`; v3 added
`n_res0` plus a res-0 directory, so the header is 32 bytes and the directory sits
between it and `cell[]`; v4 dropped the length table. An old reader pointed at a
v4 file returns plausible garbage rather than failing loudly, so check `version`
first.

The reader below loads the file whole, which is the simple path and what you
should start with. For the click-a-cell interaction you do not have to: `coff[]`
gives each cell's byte range in `post[]` and every group inside announces its own
pair count, so a click needs `goff[j..j+1]`, `coff[j..j+1]`, its `bucket[]` bytes
and its postings — measured at **22.5 KiB in 6 ranges for the first click in a
region and 2.4 KiB in 4 for each one after**, against a 15.34 MB file. The res-0 directory is how
you find the region. See the README for the byte layout.

**Do not assume 24 buckets.** The bucket *width* is fixed; the *count* follows
the data span. A single UTC day gives 24 hourly buckets, but a bundle stitched
across the UTC boundary — which is what a local day at LAX or SYD is — gives 48.
Read `bucket_ds` and `n_buckets` from the header, never derive them from 86400.

```js
export class CellIndex {
  constructor(buf) {
    const u8 = new Uint8Array(buf), dv = new DataView(buf);
    if (new TextDecoder().decode(u8.subarray(0, 8)) !== 'ADSBIDX1')
      throw new Error('cells.bin: bad magic');
    this.version = u8[8];
    this.res     = u8[9];

    if (this.version === 4) {
      this.nBuckets = u8[10];
      this.nCells   = dv.getUint32(12, true);
      this.nLegs    = dv.getUint32(16, true);
      this.nGroups  = dv.getUint32(20, true);
      this.bucketDs = dv.getUint32(24, true);   // bucket WIDTH in deciseconds
      this.nRes0    = dv.getUint32(28, true);
      let o = 32;
      // The res-0 directory. Skip it for a whole-file read; use it to
      // range-read one region instead (see the README).
      // GOTCHA: these offsets are not 8-byte aligned, so
      // `new BigUint64Array(buf, o, n)` throws. slice() copies into a fresh,
      // aligned buffer -- ~1.3 MB, once per day.
      this.dirCell = new BigUint64Array(buf.slice(o, o + 8 * this.nRes0));
      o += 8 * this.nRes0;
      this.dirCoff = new Uint32Array(buf.slice(o, o + 4 * (this.nRes0 + 1)));
      o += 4 * (this.nRes0 + 1);
      this.cells = new BigUint64Array(buf.slice(o, o + 8 * this.nCells));
      o += 8 * this.nCells;
      this.goff = new Uint32Array(buf.slice(o, o + 4 * (this.nCells + 1)));
      o += 4 * (this.nCells + 1);
      this.coff = new Uint32Array(buf.slice(o, o + 4 * (this.nCells + 1)));
      o += 4 * (this.nCells + 1);
      this.bkt = u8.subarray(o, o + this.nGroups);
      o += this.nGroups;
      this.post = u8.subarray(o);

    } else if (this.version === 1) {
      // Retained days built before the index gained a time dimension. One
      // posting list per cell, no buckets, no per-group counts, no directory.
      this.nCells   = dv.getUint32(12, true);
      this.nLegs    = dv.getUint32(16, true);
      this.nBuckets = 1;
      this.nGroups  = this.nCells;
      this.bucketDs = 864000;                   // the whole day, one bucket
      this.nRes0    = 0;
      let o = 20;
      this.cells = new BigUint64Array(buf.slice(o, o + 8 * this.nCells));
      o += 8 * this.nCells;
      this.coff = new Uint32Array(buf.slice(o, o + 4 * (this.nCells + 1)));
      o += 4 * (this.nCells + 1);
      this.post = u8.subarray(o);

    } else {
      throw new Error(`cells.bin v${this.version}: reader handles 1 and 4`);
    }
    // Whether get() can honour a time window at all. FALSE on v1 days -- the
    // caller must fall back to the leg-span prune and accept 4-24x over-report,
    // or refine with the geometry pass.
    this.timeIndexed = this.version >= 2;
  }

  _cellIndex(cellHex) {
    const cell = BigInt('0x' + cellHex);
    let lo = 0, hi = this.nCells - 1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1, c = this.cells[m];
      if (c === cell) return m;
      if (c < cell) lo = m + 1; else hi = m - 1;
    }
    return -1;
  }

  // `* 2**sh` not `<< sh`: shifts are 32-bit in JS.
  _varint(i) {
    let x = 0, sh = 0, b;
    do { b = this.post[i++]; x += (b & 0x7f) * 2 ** sh; sh += 7; } while (b & 0x80);
    return [x, i];
  }

  /** lids in `cellHex` during [ds0, ds1] deciseconds from t_epoch.
   *  Omit ds0/ds1 for the whole day. On a v1 day the window is IGNORED --
   *  check `timeIndexed` so you know which you got. Clamp exactly as the
   *  builder does, or an out-of-day query silently drops the edge buckets. */
  get(cellHex, ds0, ds1) {
    const at = this._cellIndex(cellHex);
    if (at < 0) return [];                     // no traffic in that cell, ever
    const out = new Set();
    let i = this.coff[at];
    const end = this.coff[at + 1];

    if (this.version === 1) {                  // one flat gap-coded list
      let cur = 0;
      while (i < end) {
        let x; [x, i] = this._varint(i);
        cur += x;
        out.add(cur);
      }
      return [...out].sort((x, y) => x - y);
    }

    const clamp = d => Math.min(this.nBuckets - 1,
                                Math.max(0, Math.floor(d / this.bucketDs)));
    const b0 = ds0 === undefined ? 0 : clamp(ds0);
    const b1 = ds1 === undefined ? this.nBuckets - 1 : clamp(ds1);
    for (let g = this.goff[at]; g < this.goff[at + 1]; g++) {
      let n; [n, i] = this._varint(i);         // each group states its own size
      const want = this.bkt[g] >= b0 && this.bkt[g] <= b1;
      let cur = 0;
      for (let k = 0; k < n; k++) {
        let x; [x, i] = this._varint(i);
        cur += x;                              // gap-coded, ascending
        if (want) out.add(cur);
      }
    }
    if (i !== end)
      throw new Error(`cells.bin: cell ${at} groups end at ${i}, coff says ${end}`);
    return [...out].sort((x, y) => x - y);
  }
}
```

### `tracks.bin` record decoder

Mirrors `decode_track()` in `verify_bundle.py`, which is the reference. Both
decoders below were run under Node against the live 2026-09-20 files and agree
with the Python reference node-for-node on the records checked (including a
4,289-node one), and `CellIndex.get` returns the same 1,286 postings for the
EBBR cell as the Python `CellIndex` does.

```js
export function decodeTrack(buf) {       // Uint8Array of exactly one record
  let i = 0;
  const uv = () => { let x = 0, s = 0, b;
    do { b = buf[i++]; x += (b & 0x7f) * 2 ** s; s += 7; } while (b & 0x80);
    return x; };
  const sv = () => { const x = uv(); return (x % 2) ? -(x + 1) / 2 : x / 2; };

  const n = uv(), t0 = uv();
  let t = t0, lat = 0, lon = 0, alt = 0;
  const out = new Array(n);
  for (let k = 0; k < n; k++) {
    t += uv(); lat += sv(); lon += sv(); alt += sv();
    out[k] = { t, lat, lon, alt, onGround: false, cusp: false };
  }
  const nb = (n + 7) >> 3;
  const gnd = buf.subarray(i, i + nb), cusp = buf.subarray(i + nb, i + 2 * nb);
  if (cusp.length !== nb) throw new Error('record truncated in the bitplanes');
  for (let k = 0; k < n; k++) {
    out[k].onGround = !!((gnd[k >> 3] >> (k & 7)) & 1);
    out[k].cusp     = !!((cusp[k >> 3] >> (k & 7)) & 1);
  }
  if (i + 2 * nb !== buf.length)
    throw new Error(`record has ${buf.length - i - 2 * nb} trailing bytes`);
  return out;                            // lat/lon are degrees * 1e5
}
```

### The query

```js
/** Legs present in `cellHex` at some point inside [startMs, endMs).
 *  `exact` = false (default) answers from resident data with no requests. */
export async function planesInCell(day, cellHex, startMs, endMs,
                                   exact = !day.idx.timeIndexed) {
  const { meta, legs, idx, tracks } = day;
  const res = meta.index_res;
  // `exact` defaults to TRUE on a day whose index has no time dimension: there
  // the cheap answer IS the 4-24x over-report, and only the geometry pass
  // narrows it. On a time-indexed day it defaults to false, because the hour
  // buckets already did that work.

  // the index holds ONE resolution; bring the caller's cell to it
  const cellRes = h3.getResolution(cellHex);
  if (cellRes > res) cellHex = h3.cellToParent(cellHex, res);
  const keys = cellRes < res ? h3.cellToChildren(cellHex, res) : [cellHex];

  const w0 = Math.round((startMs - meta.t_epoch * 1000) / 100);   // -> ds
  const w1 = Math.round((endMs   - meta.t_epoch * 1000) / 100);

  // 1 + 2: zero requests. On a time-indexed day the index has already filtered
  // to the hour and the leg-span test only trims the residue inside the edge
  // buckets. On a v1 day get() IGNORES the window, so this prune is the only
  // time filter there -- which is what the `exact` default above compensates.
  const cand = new Set();
  for (const k of keys) for (const l of idx.get(k, w0, w1)) cand.add(l);
  const maybe = [...cand].filter(l => legs.t0[l] <= w1 && legs.t1[l] >= w0);

  // THIS IS ALREADY A GOOD ANSWER -- ~1.7-2.7x the exact count for a 15-minute
  // window, and it cost no requests. Render it, then refine if you need exact
  // sub-hour entry/exit times. `exact: false` below skips stage 3 entirely.
  if (!exact) return maybe.map(l => ({
    lid: l, icao: legs.icao[l], reg: legs.reg[l], type: legs.type[l],
    dep: legs.dep[l], arr: legs.arr[l], flight: legs.flight[l],
  }));

  // 3 (optional): range-read the survivors and test the geometry against the
  // window, which gets you the actual in-cell enter/exit times.
  const hits = [];
  const want = new Set(keys);
  for (const { lid, rec } of await fetchRecords(tracks, legs, maybe)) {
    const nodes = decodeTrack(rec);
    const v = cellVisits(nodes, want, res).find(v => v.enter <= w1 && v.exit >= w0);
    if (v) hits.push({
      lid, icao: legs.icao[lid], reg: legs.reg[lid], type: legs.type[lid],
      dep: legs.dep[lid], arr: legs.arr[lid],
      enterMs: (meta.t_epoch * 1000) + v.enter * 100,
      exitMs:  (meta.t_epoch * 1000) + v.exit * 100,
      nodes,                            // already decoded -- draw it for free
    });
  }
  return hits;
}

/** Contiguous runs where the track is inside `want`. Linear between nodes:
 *  at res 4 (45 km across) the chord-vs-Catmull-Rom difference is far below
 *  the cell, and this runs per candidate. */
function cellVisits(nodes, want, res, stepKm = 5) {
  const vis = [];
  let cur = null;
  const test = (la, lo, ts) => {
    if (want.has(h3.latLngToCell(la, lo, res))) {
      if (cur) cur.exit = ts; else cur = { enter: ts, exit: ts };
    } else if (cur) { vis.push(cur); cur = null; }
  };
  for (let i = 0; i < nodes.length - 1; i++) {
    const a = nodes[i], b = nodes[i + 1];
    const la0 = a.lat / 1e5, lo0 = a.lon / 1e5;
    const la1 = b.lat / 1e5, lo1 = b.lon / 1e5;
    const kx = Math.cos(((la0 + la1) / 2) * Math.PI / 180);
    const km = Math.hypot((la1 - la0) * 111.32, (lo1 - lo0) * 111.32 * kx);
    const n = Math.min(512, Math.max(1, Math.ceil(km / stepKm)));
    for (let k = 0; k < n; k++) {       // half-open; b opens the next span
      const f = k / n;
      test(la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f, a.t + (b.t - a.t) * f);
    }
  }
  const z = nodes[nodes.length - 1];
  test(z.lat / 1e5, z.lon / 1e5, z.t);
  if (cur) vis.push(cur);
  return vis;                            // times are deciseconds from t_epoch
}
```

### Coalesced range reads

Leg rows are ordered `(dep, t0)`, so one cell's candidates are **scattered**
through `tracks.bin`. Measured on the EBBR cell (388 candidates, 577 KiB of
payload):

| gap tolerance | requests | bytes | amplification |
|---|---|---|---|
| exact ranges | 388 | 577 KiB | 1.0× |
| **4 KiB** | **229** | **715 KiB** | **1.2×** |
| 16 KiB | 162 | 1.26 MiB | 2.2× |
| 64 KiB | 94 | 3.49 MiB | 6.2× |
| 1 MiB | 21 | 22.4 MiB | 39.8× |
| one span | 1 | 85.5 MiB | 152× |

**4 KiB is the setting.** It removes 40% of the requests for 20% more bytes;
everything above 16 KiB trades badly. Do not "optimise" this into one big
request — the candidates span 89.6 of the file's 94 MB.

```js
async function fetchRecords(url, legs, lids, gap = 4096, conc = 6) {
  const runs = lids.map(l => ({ l, o: legs.off[l], n: legs.len[l] }))
                   .sort((a, b) => a.o - b.o);
  const groups = [];
  for (const r of runs) {
    const g = groups[groups.length - 1];
    if (g && r.o - g.end <= gap) { g.end = Math.max(g.end, r.o + r.n); g.items.push(r); }
    else groups.push({ start: r.o, end: r.o + r.n, items: [r] });
  }
  const out = [];
  for (let i = 0; i < groups.length; i += conc) {
    const batch = await Promise.all(groups.slice(i, i + conc).map(async g => {
      const r = await fetch(url, { headers: { Range: `bytes=${g.start}-${g.end - 1}` } });
      if (r.status !== 206) throw new Error(`expected 206, got ${r.status}`);
      const b = new Uint8Array(await r.arrayBuffer());
      return g.items.map(({ l, o, n }) =>
        ({ lid: l, rec: b.subarray(o - g.start, o - g.start + n) }));
    }));
    for (const g of batch) out.push(...g);
  }
  return out;
}
```

## Six things that will bite you

1. **Ring-expand a box, always.** If the user draws a rectangle rather than
   clicking a hexagon, `h3.polygonToCells` returns cells whose *centroid* is
   inside the polygon, so a track just inside your box can sit in a cell whose
   centroid is outside it. Take `gridDisk(c, 1)` of every result. Measured over
   120 random boxes: `polygonToCells` alone missed 0.1% of matching legs, the
   ring-expanded set missed none.

2. **The index is exact at cell granularity, approximate below it.** Res 4 is
   ~45 km across. `verify_bundle.py` measured **14%** false positives over 24
   random boxes of 1°–25° on 2026-09-20; the README reports **17–22%** for a box
   tighter than a cell, which is the harder case and the one you will hit. The
   share grows as the box shrinks, so refine against the leg bbox and then the
   geometry. There are **never**
   false negatives — that is the property `verify_bundle.py` asserts.

3. **A leg's span is not its dwell — but the index now knows that.** Filtering
   on `t0`/`t1` alone over-reports 4–24×; that was the reason stage 3 used to be
   mandatory. Pass the window to `idx.get()` and the hour buckets do the work.
   Do *not* skip passing it and then filter on the leg span — you would be back
   to the old over-report with a bigger file.

4. **Sample the curve, not the nodes.** Nodes are sparse Catmull-Rom control
   points, ~20 km apart at cruise over Europe. Testing only node containment
   will miss legs that cross a 45 km cell entirely between two nodes — a false
   negative, the one error class the index itself does not have. The index was
   built from the *densified* curve at 11.3 km spacing (`meta.step_km`) for
   exactly this reason; your stage 3 must resample too. `cellVisits` above uses
   5 km linear steps.

5. **Some of the geometry is invented, and the bundle cannot tell you which.**
   `fit_spline.insert_gc_nodes` fills any node gap over 200 km with
   great-circle nodes, because adsb.lol is volunteer-fed and large areas have no
   receivers (median cruise node gap: 20.6 km over continental Europe, **2,621
   km over the open North Atlantic**). Those synthetic nodes carry interpolated
   timestamps and go into `cells.bin` like any other. So an oceanic cell query
   returns aircraft *inferred* to have crossed it, at inferred times — and the
   `gc` flag is set internally in `fit_spline.py` but **is not written to
   `nodes.parquet`**, so nothing downstream can distinguish it. My mid-Atlantic
   test cell returned 31 legs for the whole day and 3 for a 15-minute window;
   treat anything oceanic as modelled, not observed. Practical client-side
   heuristic: a node-to-node gap over ~150 km means that span is interpolated
   (land p90 is 55–116 km). If you need this properly, ask for the flag to be
   plumbed through.

6. **The callsign is nullable, and absent from older days.** `legs.parquet`
   carries `flight` alongside `icao`, `reg` and `type`, but readsb reports the
   callsign only when it *changes*, so a leg never seen carrying one reads NULL.
   Days built before the column existed have no `flight` field at all — the
   loader above reads `r.flight ?? null` for exactly that reason. Label by
   `flight` when present and fall back to `reg` (tail), then the `icao` hex;
   never assume a flight number exists.

Two smaller ones: `dep`/`arr` can be `null` (aircraft never seen on the ground)
and can be **non-ICAO identifiers** from `airports.csv` such as `BE-0065` —
don't assume a 4-letter code. And `dep == arr` is common and legitimate
(circuits, training, parked aircraft observed all day).

## What the hour buckets do and do not give you

The index resolves time to the hour and space to a res-4 cell, so a tight query
still returns extra. Measured against the exact per-visit answer:

| cell, window | span prune (v1) | hourly index (v2) | exact |
|---|---|---|---|
| Brussels, 15 min | 388 | **89** | 48 |
| Heathrow, 15 min | 760 | **182** | 106 |
| Alps enroute, 15 min | 169 | **19** | 7 |
| Brussels, 1 h aligned | 452 | **176** | 108 |
| Heathrow, 3 h aligned | 887 | **593** | 508 |

Still **no false negatives** — that is the property `verify_bundle.py` asserts,
and it now asserts it for time as well as space. Two consequences worth knowing:

- **Clamp your window the way the builder does.** Buckets cover the nominal day
  only; a stitched multi-release input can hold nodes outside it, and those are
  clamped into the first/last bucket. The reader above clamps identically. Query
  an out-of-day range without clamping and you will drop the edge buckets.
- **A bucket is claimed generously on purpose.** A sample claims every bucket
  out to its neighbours in time, because the curve is sampled by *distance* — a
  parked aircraft is barely sampled in time at all, and bucketing each sample on
  its own timestamp left holes hours wide. So a leg may be listed for an hour in
  which it was only near the cell, by up to one sample interval. That is a false
  positive by design; refine with stage 3 if it matters.

## Version transition

What a v1 day cannot do, and what the reader does about it:

| | v1 (retained days) | v4 (new days) |
|---|---|---|
| `idx.timeIndexed` | `false` | `true` |
| `get(cell, ds0, ds1)` | window **ignored**, returns the whole day | honours the window to the hour |
| Brussels cell, 12:00–12:15 | 1,286 legs | **109 legs** |
| res-0 directory | absent (`nRes0 === 0`) | 120 entries, range-readable |
| cost of one cell click | whole file | ~2.4 KiB |
| `flight` in `legs.parquet` | column absent | present, nullable |
| `index_version` in `meta.json` | absent | `4` |

Both numbers above are measured on the same data — the published v1 index for
2026-09-20 and a v4 rebuild of the same day — and both readers agree with
`verify_bundle.py`'s reference decoder on every query tried.

Practical consequences:

- **Take the version from `cells.bin`'s header, not `meta.json`.** The manifest
  only gained `index_version` at v4, so on a retained day the key is missing
  rather than `1`.
- **`planesInCell` defaults `exact` to `!timeIndexed`.** On a v1 day it goes
  straight to the geometry pass, because the cheap answer there is the 4–24×
  over-report. Left alone, the same call gives a correct answer on both.
- **Do not cache a decoded index across days** without keying the cache by
  version; a v1 and a v4 index of the same date are different objects.
- **If you would rather not carry two paths**, the alternative is to wait until
  every v1 day has aged out of the retention window and then delete the v1
  branch of the reader. Nothing else in the file depends on it.

## Quick reference

```
absolute ms -> ds:   ds = (ms - meta.t_epoch * 1000) / 100
ds -> absolute ms:   ms = meta.t_epoch * 1000 + ds * 100
lat/lon:             degrees = int32 / meta.q_pos      (1e5)
alt:                 feet, already plain
drawing:             centripetal Catmull-Rom through consecutive nodes,
                     NEW CURVE at every node with cusp === true
```
