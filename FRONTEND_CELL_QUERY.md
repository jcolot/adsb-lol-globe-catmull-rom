# Brief: "which planes crossed this H3 cell between T0 and T1?"

For the frontend agent. Everything below is measured against the live bucket,
data date **2026-09-20** (113,522 legs, 12.46 M nodes).

> **Updated for `cells.bin` v2.** The index is now keyed by `(cell, hour)`, not
> just `(cell)`, which is what makes this query cheap. Read **The header moved**
> below before porting a v1 reader — the layout changed and so did the offsets.

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
/date=<DATE>/meta.json          units, counts, t_epoch, index_res
/date=<DATE>/legs.parquet       4.6 MB  one row per leg   -- LOAD WHOLE
/date=<DATE>/cells.bin         15.3 MB  (H3, hour) -> legs     -- LOAD WHOLE
/date=<DATE>/tracks.bin        94.0 MB  node geometry     -- RANGE-READ ONLY
```

Load `meta.json` + `legs.parquet` + `cells.bin` once per day: **12.9 MB
resident**, and after that every cell query is local until you need geometry.

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
| `index_groups` | `1137857` | distinct `(cell, bucket)` pairs |

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
| stage 3 cost | 388 reads / 577 KiB | 760 / 1.26 MiB | 169 / 304 KiB | 452 / 644 KiB |
| matching visit dwell, median | 7.1 min | 15.4 min | 2.2 min | 4.2 min |

Two things to read off that table. The over-report is **worst exactly where the
question is most interesting** — an enroute cell, where the aircraft is present
for 2 minutes out of a 10-hour leg. And stage 3 is affordable: a few hundred
range reads over ~0.5–1.3 MiB, not a re-download.

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
      for (let i = 0; i < n; i++) {
        const r = rows[i];
        // lid IS the row index; don't build a map, but do assert it once
        if (r.lid !== i) throw new Error(`legs.parquet: lid ${r.lid} at row ${i}`);
        legs.t0[i] = r.t0; legs.t1[i] = r.t1;
        legs.off[i] = Number(r.off);   // uint64 -> BigInt from hyparquet
        legs.len[i] = r.len;
        legs.icao[i] = r.icao; legs.dep[i] = r.dep; legs.arr[i] = r.arr;
        legs.reg[i] = r.reg; legs.type[i] = r.type;
      }
    },
  });
  return { meta, legs, idx: new CellIndex(cellsBuf), tracks: `${D}/tracks.bin` };
}
```

### `cells.bin` reader

**The header moved.** v2 added `n_buckets`, `n_groups` and `bucket_ds`, and the
tables now start at **offset 28**, not 20. A v1 reader pointed at a v2 file
returns plausible garbage rather than failing loudly, so check `version` first.

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
    if (this.version !== 2)
      throw new Error(`cells.bin v${this.version}: this reader wants v2`);
    this.res       = u8[9];
    this.nBuckets  = u8[10];
    this.nCells    = dv.getUint32(12, true);
    this.nLegs     = dv.getUint32(16, true);
    this.nGroups   = dv.getUint32(20, true);
    this.bucketDs  = dv.getUint32(24, true);   // bucket WIDTH in deciseconds
    let o = 28;
    // GOTCHA: 28 is not 8-byte aligned, so `new BigUint64Array(buf, 28, n)`
    // throws "start offset of BigUint64Array should be a multiple of 8" unless
    // the ArrayBuffer itself starts 8-aligned. slice() copies into a fresh,
    // aligned buffer -- 1.3 MB, once per day. Do the same for safety.
    this.cells = new BigUint64Array(buf.slice(o, o + 8 * this.nCells));
    o += 8 * this.nCells;
    this.goff = new Uint32Array(buf.slice(o, o + 4 * (this.nCells + 1)));
    o += 4 * (this.nCells + 1);
    this.coff = new Uint32Array(buf.slice(o, o + 4 * (this.nCells + 1)));
    o += 4 * (this.nCells + 1);
    this.bkt = u8.subarray(o, o + this.nGroups);
    o += this.nGroups;
    // Per-group posting LENGTHS, varint on the wire. Expand once into a flat
    // prefix table: it costs 4 B/group in memory and saves 2.7 MB of download,
    // which is the trade the format is making.
    this.poff = new Uint32Array(this.nGroups + 1);
    {
      let i = o, g = 0;
      const len = new Uint32Array(this.nGroups);
      for (; g < this.nGroups; g++) {
        let x = 0, sh = 0, b;
        do { b = u8[i++]; x += (b & 0x7f) * 2 ** sh; sh += 7; } while (b & 0x80);
        len[g] = x;
      }
      o = i;                                   // post[] starts here
      for (let c = 0; c < this.nCells; c++) {
        let pos = this.coff[c];
        for (let gg = this.goff[c]; gg < this.goff[c + 1]; gg++) {
          this.poff[gg] = pos;
          pos += len[gg];
        }
        this.poff[this.goff[c + 1]] = pos;
      }
    }
    this.post = u8.subarray(o);
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

  _decode(g) {
    const out = [];
    let i = this.poff[g], cur = 0;
    const end = this.poff[g + 1];
    while (i < end) {
      let x = 0, sh = 0, b;
      // `* 2**sh` not `<< sh`: shifts are 32-bit in JS.
      do { b = this.post[i++]; x += (b & 0x7f) * 2 ** sh; sh += 7; } while (b & 0x80);
      cur += x;                                // gap-coded, ascending
      out.push(cur);
    }
    return out;
  }

  /** lids in `cellHex` during [ds0, ds1] deciseconds from t_epoch.
   *  Omit ds0/ds1 for the whole day. Clamp exactly as the builder does, or an
   *  out-of-day query silently drops the edge buckets. */
  get(cellHex, ds0, ds1) {
    const at = this._cellIndex(cellHex);
    if (at < 0) return [];                     // no traffic in that cell, ever
    const g0 = this.goff[at], g1 = this.goff[at + 1];
    const clamp = d => Math.min(this.nBuckets - 1,
                                Math.max(0, Math.floor(d / this.bucketDs)));
    const b0 = ds0 === undefined ? 0 : clamp(ds0);
    const b1 = ds1 === undefined ? this.nBuckets - 1 : clamp(ds1);
    const out = new Set();
    for (let g = g0; g < g1; g++)
      if (this.bkt[g] >= b0 && this.bkt[g] <= b1)
        for (const l of this._decode(g)) out.add(l);
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
export async function planesInCell(day, cellHex, startMs, endMs, exact = false) {
  const { meta, legs, idx, tracks } = day;
  const res = meta.index_res;

  // the index holds ONE resolution; bring the caller's cell to it
  const cellRes = h3.getResolution(cellHex);
  if (cellRes > res) cellHex = h3.cellToParent(cellHex, res);
  const keys = cellRes < res ? h3.cellToChildren(cellHex, res) : [cellHex];

  const w0 = Math.round((startMs - meta.t_epoch * 1000) / 100);   // -> ds
  const w1 = Math.round((endMs   - meta.t_epoch * 1000) / 100);

  // 1 + 2: zero requests. The index is already time-filtered to the hour, so
  // the leg-span test only trims the residue inside the edge buckets.
  const cand = new Set();
  for (const k of keys) for (const l of idx.get(k, w0, w1)) cand.add(l);
  const maybe = [...cand].filter(l => legs.t0[l] <= w1 && legs.t1[l] >= w0);

  // THIS IS ALREADY A GOOD ANSWER -- ~1.7-2.7x the exact count for a 15-minute
  // window, and it cost no requests. Render it, then refine if you need exact
  // sub-hour entry/exit times. `exact: false` below skips stage 3 entirely.
  if (!exact) return maybe.map(l => ({
    lid: l, icao: legs.icao[l], reg: legs.reg[l], type: legs.type[l],
    dep: legs.dep[l], arr: legs.arr[l],
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
request — the candidates span 85 of the file's 94 MB.

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
   ~45 km across. For a box tighter than a cell, expect 17–22% false positives
   and refine against the leg bbox and then the geometry. There are **never**
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
   test cell returned 31 legs from the index and 0 for a 15-minute window;
   treat anything oceanic as modelled, not observed. Practical client-side
   heuristic: a node-to-node gap over ~150 km means that span is interpolated
   (land p90 is 55–116 km). If you need this properly, ask for the flag to be
   plumbed through.

6. **There is no callsign in the deployed data.** I checked the live files:
   `legs.parquet` has `icao, reg, type, dep, arr` and `flights.parquet` has no
   `flight` column either. The README's bundle section claims "callsign, route,
   times, bbox, all local" — **that line is wrong** on this branch;
   `fit_spline.py` here emits no `callsigns.parquet` at all. Label aircraft by
   `reg` (tail) or `type`, falling back to the `icao` hex, and do not build UI
   that needs a flight number until the pipeline provides one.

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

## Quick reference

```
absolute ms -> ds:   ds = (ms - meta.t_epoch * 1000) / 100
ds -> absolute ms:   ms = meta.t_epoch * 1000 + ds * 100
lat/lon:             degrees = int32 / meta.q_pos      (1e5)
alt:                 feet, already plain
drawing:             centripetal Catmull-Rom through consecutive nodes,
                     NEW CURVE at every node with cusp === true
```
