#!/usr/bin/env python3
"""
verify_bundle.py - correctness gate for a day bundle built by build_bundle.py.

The decoders here are deliberately plain Python with no numpy tricks: they are
the reference implementation of the on-disk format, and the frontend decoder
should read like them.

Checks, in order of how much they'd hurt to get wrong:

  1. tiling      legs.parquet off/len tile tracks.bin exactly -- no gap, no
                 overlap, no byte unaccounted for.
  2. round-trip  every decoded node equals the source row exactly (t rebased to
                 the epoch, lat/lon/alt/on_ground/cusp identical).
  3. index       for random boxes, every leg with a node in the box appears in
                 the posting-list answer. NO FALSE NEGATIVES is the property
                 that matters; false positives are expected (the index resolves
                 to a cell, not to your box) and are reported, not failed.
  4. structure   cells.bin header, ascending cells, monotonic offsets,
                 ascending posting lists, lid == row index in legs.parquet.
"""
import argparse, bisect, importlib.util, json, os, random, struct, sys

import duckdb
import pyarrow.parquet as pq

MAGIC = b"ADSBIDX1"
HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    sp = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


# ---------------------------------------------------------------------------
# reference decoders
# ---------------------------------------------------------------------------
def read_varint(buf, i):
    x = s = 0
    while True:
        b = buf[i]
        i += 1
        x |= (b & 0x7F) << s
        if not b & 0x80:
            return x, i
        s += 7


def unzigzag(x):
    return (x >> 1) ^ -(x & 1)


def decode_track(buf):
    """One tracks.bin record -> list of (tds, lat, lon, alt, on_ground, cusp).

    lat/lon are degrees * q_pos, alt is feet, tds is deciseconds from t_epoch.
    """
    i = 0
    n, i = read_varint(buf, i)
    t0, i = read_varint(buf, i)
    t = t0
    lat = lon = alt = 0
    out = []
    for _ in range(n):
        dt, i = read_varint(buf, i)
        dla, i = read_varint(buf, i)
        dlo, i = read_varint(buf, i)
        dal, i = read_varint(buf, i)
        t += dt
        lat += unzigzag(dla)
        lon += unzigzag(dlo)
        alt += unzigzag(dal)
        out.append([t, lat, lon, alt, False, False])
    nb = (n + 7) // 8
    gnd, cusp = buf[i:i + nb], buf[i + nb:i + 2 * nb]
    if len(cusp) != nb:
        raise ValueError("record truncated in the bitplanes")
    for k in range(n):
        out[k][4] = bool(gnd[k >> 3] >> (k & 7) & 1)
        out[k][5] = bool(cusp[k >> 3] >> (k & 7) & 1)
    if i + 2 * nb != len(buf):
        raise ValueError(f"record has {len(buf) - i - 2*nb} trailing bytes")
    return out


class CellIndex:
    """cells.bin reader. The whole file is resident, so a lookup is a binary
    search over cell[] and a slice of post[].

    v3 added a res-0 directory: every res-0 cell's descendants are ONE contiguous
    run of cell[], so a client can binary-search dir_cell[] and range-read just
    the slice covering its viewport instead of the whole file.

    v2 added a time dimension: a cell owns a run of GROUPS, one per time bucket
    it saw traffic in, and each group has its own posting list. Bucket b covers
    [b * bucket_ds, (b+1) * bucket_ds) deciseconds from meta.t_epoch. The WIDTH
    is fixed and the COUNT follows the data span, so n_buckets is 24 for a single
    UTC day and larger for a stitched multi-day input -- do not assume a day.
    bucket_ds is in the header for exactly that reason. v1 files still read --
    they are treated as a single bucket spanning the day.
    """

    def __init__(self, blob):
        if blob[:8] != MAGIC:
            raise ValueError("bad magic")
        self.version = blob[8]
        if self.version == 1:
            self.res, _, self.n_cells, self.n_legs = \
                struct.unpack_from("<BHII", blob, 9)
            self.n_buckets, self.n_groups = 1, self.n_cells
            self.bucket_ds = 86400 * 10
            o = 20
            self.cells = list(struct.unpack_from(f"<{self.n_cells}Q", blob, o))
            o += 8 * self.n_cells
            # one group per cell, so the group table is the identity
            self.goff = list(range(self.n_cells + 1))
            self.bkt = [0] * self.n_cells
            self.poff = list(struct.unpack_from(f"<{self.n_cells + 1}I", blob, o))
            o += 4 * (self.n_cells + 1)
            self.coff = self.poff[:-1] + [self.poff[-1]]
            self.n_res0 = 0
            self.dir_cell, self.dir_coff, self.dir_glen = [], [0], [0]
        elif self.version == 3:
            self.res, self.n_buckets, _, self.n_cells, self.n_legs, \
                self.n_groups, self.bucket_ds, self.n_res0 = \
                struct.unpack_from("<BBBIIIII", blob, 9)
            # "<BBBBIIIII" after the 8-byte magic is 24 bytes with no padding,
            # so the res-0 directory starts at 32 and the tables after it.
            o = 32
            self.dir_cell = list(struct.unpack_from(f"<{self.n_res0}Q", blob, o))
            o += 8 * self.n_res0
            self.dir_coff = list(struct.unpack_from(f"<{self.n_res0 + 1}I", blob, o))
            o += 4 * (self.n_res0 + 1)
            self.dir_glen = list(struct.unpack_from(f"<{self.n_res0 + 1}I", blob, o))
            o += 4 * (self.n_res0 + 1)
            self.cells = list(struct.unpack_from(f"<{self.n_cells}Q", blob, o))
            o += 8 * self.n_cells
            self.goff = list(struct.unpack_from(f"<{self.n_cells + 1}I", blob, o))
            o += 4 * (self.n_cells + 1)
            self.coff = list(struct.unpack_from(f"<{self.n_cells + 1}I", blob, o))
            o += 4 * (self.n_cells + 1)
            self.bkt = list(struct.unpack_from(f"<{self.n_groups}B", blob, o))
            o += self.n_groups
            # per-group posting LENGTHS as varints; rebuild the flat prefix
            # offsets the old format stored outright. Each cell restarts from
            # its own coff[], so a corrupt length cannot walk off into the
            # next cell's postings unnoticed -- the structure check below
            # compares the two.
            glen_base = o
            glen = []
            for _g in range(self.n_groups):
                v, o = read_varint(blob, o)
                glen.append(v)
            self.glen_off = glen_base      # where glen[] starts, for the dir check
            base = o
            self.poff = [0] * (self.n_groups + 1)
            for i in range(self.n_cells):
                pos = self.coff[i]
                for g in range(self.goff[i], self.goff[i + 1]):
                    self.poff[g] = pos
                    pos += glen[g]
                self.poff[self.goff[i + 1]] = pos
            o = base
        else:
            raise ValueError(f"cells.bin version {self.version} not supported")
        self.post = blob[o:]
        self.post_blob = blob

    def _glen_byte_of(self, group):
        """Byte offset of `group`'s length varint inside glen[]. Only the
        verifier needs this -- a client reaches its groups through the res-0
        directory instead of walking from zero."""
        if not hasattr(self, "_glen_cum"):
            cum, o = [0], self.glen_off
            for _g in range(self.n_groups):
                _v, o = read_varint(self.post_blob, o)
                cum.append(o - self.glen_off)
            self._glen_cum = cum
        return self._glen_cum[group]

    def _find(self, cell):
        lo, hi = 0, self.n_cells - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.cells[mid] == cell:
                return mid
            if self.cells[mid] < cell:
                lo = mid + 1
            else:
                hi = mid - 1
        return -1

    def _decode(self, g):
        i, end = self.poff[g], self.poff[g + 1]
        out, cur = [], 0
        while i < end:
            gap, i = read_varint(self.post, i)
            cur += gap
            out.append(cur)
        return out

    def get(self, cell, ds0=None, ds1=None):
        """Ascending lids for one cell, or [] if the cell has no traffic.

        With ds0/ds1 (deciseconds from t_epoch, inclusive) only the buckets
        overlapping that window are read. The window is CLAMPED into the day the
        same way build_bundle clamps a node's bucket, so a leg parked outside the
        nominal day still answers for the edge buckets.
        """
        at = self._find(cell)
        if at < 0:
            return []
        g0, g1 = self.goff[at], self.goff[at + 1]
        if ds0 is None:
            out = set()
            for g in range(g0, g1):
                out.update(self._decode(g))
            return sorted(out)
        b0 = min(self.n_buckets - 1, max(0, int(ds0 // self.bucket_ds)))
        b1 = min(self.n_buckets - 1, max(0, int(ds1 // self.bucket_ds)))
        out = set()
        for g in range(g0, g1):
            if b0 <= self.bkt[g] <= b1:
                out.update(self._decode(g))
        return sorted(out)

    def buckets_of(self, cell):
        """(bucket, lids) for each time bucket this cell saw traffic in."""
        at = self._find(cell)
        if at < 0:
            return []
        return [(self.bkt[g], self._decode(g))
                for g in range(self.goff[at], self.goff[at + 1])]

    def query(self, cells, ds0=None, ds1=None):
        s = set()
        for c in cells:
            s.update(self.get(c, ds0, ds1))
        return s


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bundle", required=True, help="directory built by build_bundle.py")
    p.add_argument("--points", required=True, help="points_legs.parquet (source of truth)")
    p.add_argument("--meta", required=True, help="aircraft.parquet")
    p.add_argument("--sample-legs", type=int, default=2000,
                   help="legs to round-trip decode (0 = all)")
    p.add_argument("--boxes", type=int, default=40, help="random box queries to test")
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args()

    rng = random.Random(a.seed)
    fail = []
    def check(name, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
        if not ok:
            fail.append(name)

    bb = _load("bb", os.path.join(HERE, "build_bundle.py"))
    meta = json.load(open(os.path.join(a.bundle, "meta.json")))
    qpos = meta["q_pos"]
    epoch_ds = meta["t_epoch"] * 10
    legs_t = pq.read_table(os.path.join(a.bundle, "legs.parquet"))
    legs = legs_t.to_pylist()
    tracks_path = os.path.join(a.bundle, "tracks.bin")
    idx = CellIndex(open(os.path.join(a.bundle, "cells.bin"), "rb").read())

    con = duckdb.connect()
    con.execute("INSTALL h3 FROM community")
    con.execute("LOAD h3")
    con.register("leg_t", legs_t)
    con.execute("CREATE TABLE leg AS SELECT * FROM leg_t")

    print(f"\nbundle {a.bundle}")
    print(f"  date {meta['date']}  legs {meta['n_legs']}  nodes {meta['n_nodes']}  "
          f"index res {meta['index_res']}\n")

    # ---- 1. structure -----------------------------------------------------
    print("structure")
    check("legs.parquet row count matches meta",
          len(legs) == meta["n_legs"], f"{len(legs)}")
    check("lid == row index",
          all(r["lid"] == i for i, r in enumerate(legs)))
    check("cells.bin magic + version",
          idx.version == bb.IDX_VERSION and idx.res == meta["index_res"])
    check("cells.bin n_legs matches", idx.n_legs == meta["n_legs"])
    check("cells.bin bucket width + count match meta",
          idx.n_buckets == meta["index_buckets"]
          and idx.bucket_ds == meta["bucket_ds"],
          f"{idx.n_buckets} x {idx.bucket_ds/600:.0f} min")
    # the count must cover the span -- this is what a per-day count got wrong
    span = max(r["t1"] for r in legs) if legs else 0
    # >=, not >: the top edge is folded into the last bucket by the builder's
    # clamp, so a span of exactly 24 h is covered by exactly 24 hourly buckets.
    check("bucket count covers the data span",
          idx.n_buckets * idx.bucket_ds >= span,
          f"span {span/864000:.2f} days, buckets reach "
          f"{idx.n_buckets * idx.bucket_ds/864000:.2f}")
    check("the last bucket is not a dumping ground",
          idx.n_buckets == 1 or
          sum(1 for b in idx.bkt if b == idx.n_buckets - 1)
          <= 3 * max(1, idx.n_groups // idx.n_buckets),
          f"{sum(1 for b in idx.bkt if b == idx.n_buckets - 1)} groups in the "
          f"last bucket vs {idx.n_groups // max(idx.n_buckets,1)} average")
    check("cells.bin group count matches meta",
          idx.n_groups == meta["index_groups"], f"{idx.n_groups}")
    check("cells ascending, no duplicates",
          all(idx.cells[i] < idx.cells[i + 1] for i in range(len(idx.cells) - 1)))
    check("group offsets monotonic and cover every group",
          all(idx.goff[i] <= idx.goff[i + 1] for i in range(len(idx.goff) - 1))
          and idx.goff[-1] == idx.n_groups and idx.goff[0] == 0)
    check("every cell owns at least one group",
          all(idx.goff[i] < idx.goff[i + 1] for i in range(idx.n_cells)))
    check("buckets in range, ascending with no repeats inside a cell",
          all(all(0 <= idx.bkt[g] < idx.n_buckets for g in
                  range(idx.goff[i], idx.goff[i + 1]))
              and all(idx.bkt[g] < idx.bkt[g + 1] for g in
                      range(idx.goff[i], idx.goff[i + 1] - 1))
              for i in range(idx.n_cells)))
    check("posting offsets monotonic and cover post[]",
          all(idx.poff[i] <= idx.poff[i + 1] for i in range(len(idx.poff) - 1))
          and idx.poff[-1] == len(idx.post),
          f"{idx.poff[-1]} vs {len(idx.post)}")
    check("per-cell posting base agrees with its first group's offset",
          all(idx.coff[i] == idx.poff[idx.goff[i]] for i in range(idx.n_cells)))
    check("every posting list is non-empty and ascending",
          all(len(p) > 0 and all(p[k] < p[k + 1] for k in range(len(p) - 1))
              and p[-1] < idx.n_legs
              for p in (idx._decode(g) for g in range(idx.n_groups))))
    # ---- the res-0 directory -------------------------------------------
    # This is load-bearing: a client binary-searches it and slices, so a wrong
    # offset does not crash, it silently returns a subset of a region's traffic.
    if idx.version >= 3:
        check("res-0 directory count matches meta",
              idx.n_res0 == meta["index_res0"], f"{idx.n_res0} cells")
        check("res-0 cells ascending, no duplicates",
              all(idx.dir_cell[i] < idx.dir_cell[i + 1]
                  for i in range(idx.n_res0 - 1)))
        check("res-0 runs tile cell[] exactly",
              idx.dir_coff[0] == 0 and idx.dir_coff[-1] == idx.n_cells
              and all(idx.dir_coff[i] < idx.dir_coff[i + 1]
                      for i in range(idx.n_res0)))
        # every cell in a run must really be a child of that run's res-0 cell
        rows = con.execute(f"""
            SELECT h3_cell_to_parent(cell::UBIGINT, 0) FROM (VALUES {
                ','.join(f'({c})' for c in idx.cells[::max(1, idx.n_cells // 4000)])
            }) AS t(cell)""").fetchall()
        probe = list(range(0, idx.n_cells, max(1, idx.n_cells // 4000)))
        bad = 0
        for ci, (par,) in zip(probe, rows):
            k = bisect.bisect_right(idx.dir_coff, ci) - 1
            if not (0 <= k < idx.n_res0) or idx.dir_cell[k] != par:
                bad += 1
        check(f"each cell falls in its own res-0 run ({len(probe)} sampled)",
              bad == 0, f"{bad} misplaced")
        # glen offsets must land exactly on the partition's first group
        gl = idx.glen_off
        bad_g = 0
        for k in range(idx.n_res0):
            want = idx.goff[idx.dir_coff[k]]
            pos, g = gl + idx.dir_glen[k], 0
            # decode forward from byte 0 of glen[] only once, then compare
            bad_g += 0 if idx.dir_glen[k] == idx._glen_byte_of(want) else 1
        check("res-0 glen offsets point at the run's first group", bad_g == 0,
              f"{bad_g} wrong")

    check("file sizes match meta",
          all(os.path.getsize(os.path.join(a.bundle, v["path"])) == v["bytes"]
              for v in meta["files"].values()))

    # ---- 2. tiling --------------------------------------------------------
    print("\ntiling")
    total = os.path.getsize(tracks_path)
    pos, gaps, overlaps = 0, 0, 0
    for r in legs:
        if r["off"] != pos:
            if r["off"] > pos:
                gaps += 1
            else:
                overlaps += 1
        pos = max(pos, r["off"] + r["len"])
    check("records tile tracks.bin with no gaps", gaps == 0, f"{gaps} gaps")
    check("records do not overlap", overlaps == 0, f"{overlaps} overlaps")
    check("records cover tracks.bin exactly", pos == total,
          f"covered {pos} of {total} bytes")
    check("n_nodes sums to meta.n_nodes",
          sum(r["n_nodes"] for r in legs) == meta["n_nodes"])

    # ---- 2b. the property the (dep, t0) sort exists to provide ------------
    print("\nclustering")
    runs, worst_dep = [], None
    for dep in sorted({r["dep"] for r in legs if r["dep"] is not None}):
        rows = [i for i, r in enumerate(legs) if r["dep"] == dep]
        nrun = 1 + sum(1 for x, y in zip(rows, rows[1:]) if y != x + 1)
        runs.append(nrun)
        if nrun > 1 and worst_dep is None:
            worst_dep = dep
    check(f"each dep is one contiguous run of rows ({len(runs)} airports)",
          all(r == 1 for r in runs),
          f"worst: {worst_dep}" if worst_dep else "")
    # and therefore one contiguous byte range in tracks.bin
    span_ok = True
    for dep in sorted({r["dep"] for r in legs if r["dep"] is not None}):
        rows = [r for r in legs if r["dep"] == dep]
        lo = min(r["off"] for r in rows)
        hi = max(r["off"] + r["len"] for r in rows)
        if hi - lo != sum(r["len"] for r in rows):
            span_ok = False
    check("each dep is one contiguous byte range in tracks.bin", span_ok)
    ndep = len({r["dep"] for r in legs if r["dep"] is not None})
    nnull = sum(1 for r in legs if r["dep"] is None)
    print(f"        {ndep} departure airports, {nnull} legs with no dep "
          f"(tail bucket, ordered by anchor cell)")

    # ---- 3. round-trip ----------------------------------------------------
    print("\nround-trip")
    pick = list(range(len(legs)))
    if a.sample_legs and len(pick) > a.sample_legs:
        pick = sorted(rng.sample(pick, a.sample_legs))
    con.execute(f"""
        CREATE TABLE src AS
        SELECT p.leg_id,
               (p.t::BIGINT + m.base_ts::BIGINT * 10 - {epoch_ds}) AS tds,
               p.lat, p.lon, p.alt,
               coalesce(p.on_ground, false) AS gnd,
               coalesce(p.cusp, false) AS cusp
        FROM '{a.points}' p JOIN '{a.meta}' m USING (icao)
    """)
    # one query for every sampled leg, grouped in Python -- a query per leg would
    # be thousands of scans over a multi-GB parquet on a real day
    want_ids = [legs[i]["leg_id"] for i in pick]
    con.execute("CREATE TABLE pick(leg_id VARCHAR)")
    con.executemany("INSERT INTO pick VALUES (?)", [(x,) for x in want_ids])
    by_leg = {}
    for row in con.execute("""
            SELECT s.leg_id, s.tds, s.lat, s.lon, s.alt, s.gnd, s.cusp
            FROM src s SEMI JOIN pick USING (leg_id)
            -- must match build_bundle.py's node order exactly, including the
            -- tie-break: (leg_id, tds) alone lets two nodes sharing a tds come
            -- back in either order, and this comparison is positional
            ORDER BY s.leg_id, s.tds, s.lat, s.lon, s.alt, s.gnd, s.cusp""").fetchall():
        by_leg.setdefault(row[0], []).append(row[1:])

    bad_nodes = bad_count = bad_bbox = bad_t = 0
    checked = 0
    with open(tracks_path, "rb") as fh:
        for i in pick:
            r = legs[i]
            fh.seek(r["off"])
            got = decode_track(fh.read(r["len"]))
            want = by_leg.get(r["leg_id"], [])
            if len(got) != r["n_nodes"] or len(want) != len(got):
                bad_count += 1
                continue
            for g, w in zip(got, want):
                if (g[0] != w[0] or g[1] != w[1] or g[2] != w[2]
                        or g[3] != w[3] or g[4] != w[4] or g[5] != w[5]):
                    bad_nodes += 1
                    break
            lats = [g[1] for g in got]
            lons = [g[2] for g in got]
            if (min(lats) != r["min_lat"] or max(lats) != r["max_lat"]
                    or min(lons) != r["min_lon"] or max(lons) != r["max_lon"]):
                bad_bbox += 1
            if got[0][0] != r["t0"] or got[-1][0] != r["t1"]:
                bad_t += 1
            checked += 1
    check(f"decoded nodes identical to source ({checked} legs)", bad_nodes == 0,
          f"{bad_nodes} mismatched")
    check("decoded node counts match n_nodes", bad_count == 0, f"{bad_count} wrong")
    check("leg bbox matches decoded geometry", bad_bbox == 0, f"{bad_bbox} wrong")
    check("leg t0/t1 match decoded times", bad_t == 0, f"{bad_t} wrong")

    # ---- 4. index: no false negatives -------------------------------------
    print("\nindex")
    res = meta["index_res"]
    # A box's covering cell set is polygonToCells (centroid-in-polygon) RING-
    # EXPANDED by one: a node just inside the box can sit in a cell whose
    # centroid is outside it. The client must do the same, or it will miss legs.
    def cover(w, s, e, n):
        wkt = (f"POLYGON (({w} {s}, {e} {s}, {e} {n}, {w} {n}, {w} {s}))")
        rows = con.execute(f"""
            SELECT DISTINCT unnest(h3_grid_disk(c, 1)) FROM (
              SELECT unnest(h3_polygon_wkt_to_cells('{wkt}', {res})) AS c)
        """).fetchall()
        return [r[0] for r in rows]

    la0, la1, lo0, lo1 = con.execute(f"""
        SELECT min(lat)/{qpos}.0, max(lat)/{qpos}.0,
               min(lon)/{qpos}.0, max(lon)/{qpos}.0 FROM '{a.points}'
    """).fetchone()

    fn_total = fp_total = gt_total = ans_total = tested = 0
    worst = None
    for _ in range(a.boxes):
        span = rng.choice([1.0, 3.0, 10.0, 25.0])
        w = rng.uniform(lo0, max(lo0, lo1 - span))
        s = rng.uniform(la0, max(la0, la1 - span))
        e, n = w + span, s + span
        truth = {r[0] for r in con.execute(f"""
            SELECT DISTINCT l.lid
            FROM '{a.points}' p JOIN leg l USING (leg_id)
            WHERE p.lat BETWEEN {s * qpos} AND {n * qpos}
              AND p.lon BETWEEN {w * qpos} AND {e * qpos}
        """).fetchall()}
        if not truth:
            continue
        ans = idx.query(cover(w, s, e, n))
        missing = truth - ans
        tested += 1
        gt_total += len(truth)
        ans_total += len(ans)
        fn_total += len(missing)
        fp_total += len(ans - truth)
        if missing and worst is None:
            worst = (w, s, e, n, sorted(missing)[:5])
    check(f"no false negatives across {tested} boxes", fn_total == 0,
          f"{fn_total} legs missed" + (f" e.g. box {worst[:4]} legs {worst[4]}"
                                       if worst else ""))
    if tested:
        print(f"        ground truth {gt_total} legs, index returned {ans_total} "
              f"({fp_total} false positives, {100*fp_total/max(ans_total,1):.0f}% "
              f"-- expected: res {res} cells are ~"
              f"{2 * bb.H3_EDGE_KM[res]:.1f} km across)")

    # ---- 4b. the same, but with a TIME window ----------------------------
    # This is what the bucket dimension exists for, so it is also where a bug
    # in it would show up: a leg whose node is in the box during the window and
    # is NOT returned is a silent wrong answer in the frontend.
    if idx.n_buckets > 1:
        fn_t = ans_t = gt_t = span_t = tested_t = 0
        worst_t = None
        for _ in range(a.boxes):
            span = rng.choice([1.0, 3.0, 10.0, 25.0])
            w = rng.uniform(lo0, max(lo0, lo1 - span))
            sy = rng.uniform(la0, max(la0, la1 - span))
            e, n = w + span, sy + span
            # windows from 15 min to 3 h, aligned anywhere in the bundle's
            # span -- NOT anywhere in "the day". A stitched bundle is longer
            # than a day, and hardcoding 86400 s here silently stops testing
            # the second half of it.
            wid = rng.choice([9000, 36000, 108000])
            full = idx.n_buckets * idx.bucket_ds
            ds0 = rng.randrange(0, max(1, full - wid))
            ds1 = ds0 + wid
            truth = {r[0] for r in con.execute(f"""
                SELECT DISTINCT l.lid
                FROM src s JOIN leg l USING (leg_id)
                WHERE s.lat BETWEEN {sy * qpos} AND {n * qpos}
                  AND s.lon BETWEEN {w * qpos} AND {e * qpos}
                  AND s.tds BETWEEN {ds0} AND {ds1}
            """).fetchall()}
            if not truth:
                continue
            cover_cells = cover(w, sy, e, n)
            ans = idx.query(cover_cells, ds0, ds1)
            # what the caller would have had to accept WITHOUT the buckets:
            # every leg in those cells whose whole span overlaps the window
            span_ans = {l for l in idx.query(cover_cells)
                        if legs[l]["t0"] <= ds1 and legs[l]["t1"] >= ds0}
            missing = truth - ans
            tested_t += 1
            gt_t += len(truth)
            ans_t += len(ans)
            span_t += len(span_ans)
            fn_t += len(missing)
            if missing and worst_t is None:
                worst_t = (w, sy, e, n, ds0, ds1, sorted(missing)[:5])
        check(f"no false negatives across {tested_t} box+window queries",
              fn_t == 0,
              f"{fn_t} legs missed" + (f" e.g. {worst_t}" if worst_t else ""))
        if tested_t:
            print(f"        ground truth {gt_t} legs; bucketed index returned "
                  f"{ans_t}, leg-span prune alone would return {span_t} "
                  f"({span_t / max(ans_t, 1):.1f}x more to range-read)")

    # a leg's own cells must contain its nodes' cells
    sample = rng.sample(range(len(legs)), min(400, len(legs)))
    con.execute("CREATE TABLE pick2(lid INTEGER)")
    con.executemany("INSERT INTO pick2 VALUES (?)", [(int(i),) for i in sample])
    pairs = con.execute(f"""
        SELECT DISTINCT l.lid,
               h3_latlng_to_cell(s.lat/{qpos}.0, s.lon/{qpos}.0, {res}) AS cell,
               s.tds AS tds
        FROM src s JOIN leg l USING (leg_id) SEMI JOIN pick2 USING (lid)
    """).fetchall()
    miss = sum(1 for lid, cell, _ in pairs if lid not in idx.get(cell))
    check(f"every node's own cell posts its leg "
          f"({len(sample)} legs, {len(pairs)} node cells)", miss == 0,
          f"{miss} missing")
    # and in the bucket that node's own timestamp falls in -- the bucket
    # assignment has to agree with the timeline legs.parquet/meta.json publish
    miss_b = sum(1 for lid, cell, tds in pairs
                 if lid not in idx.get(cell, tds, tds))
    check("every node's own (cell, bucket) posts its leg", miss_b == 0,
          f"{miss_b} missing")

    # ---- 4c. no interior time holes --------------------------------------
    # Two CONSECUTIVE nodes in the same cell bracket an interval the aircraft
    # provably spent inside it, so every bucket in between must be posted. This
    # is the check that catches bucketing each sample on its own timestamp,
    # which is the obvious implementation and leaves holes HOURS wide: the
    # densifier samples by distance, so a parked aircraft is hardly sampled in
    # time at all. Measured before the fix, one leg sat in the Brussels cell
    # from 06:25 to 21:05 and was posted in buckets 6, 20 and 21 only.
    #
    # Nodes are used rather than a resampled curve on purpose -- a resample
    # would disagree with the builder's Catmull-Rom sampling over a cell edge
    # and make this check flaky about the thing it is not testing.
    if idx.n_buckets > 1:
        rows = con.execute(f"""
            SELECT l.lid, s.tds,
                   h3_latlng_to_cell(s.lat/{qpos}.0, s.lon/{qpos}.0, {res}) AS cell
            FROM src s JOIN leg l USING (leg_id) SEMI JOIN pick2 USING (lid)
            ORDER BY l.lid, s.tds
        """).fetchall()
        bmap = {}
        def posted(cell):
            if cell not in bmap:
                bmap[cell] = {b: set(ls) for b, ls in idx.buckets_of(cell)}
            return bmap[cell]
        def bkt(tds):
            return min(idx.n_buckets - 1, max(0, int(tds) // idx.bucket_ds))
        holes = spans = 0
        worst = None
        for (lid_a, t_a, c_a), (lid_b, t_b, c_b) in zip(rows, rows[1:]):
            if lid_a != lid_b or c_a != c_b:
                continue
            spans += 1
            pb = posted(c_a)
            for b in range(bkt(t_a), bkt(t_b) + 1):
                if lid_a not in pb.get(b, ()):
                    holes += 1
                    if worst is None:
                        worst = (lid_a, f"{c_a:#x}", b,
                                 f"{t_a/36000:.2f}-{t_b/36000:.2f}h")
                    break
        check(f"no bucket holes between same-cell consecutive nodes "
              f"({spans} node pairs)", holes == 0,
              f"{holes} holes" + (f" e.g. lid {worst[0]} cell {worst[1]} "
                                  f"bucket {worst[2]} over {worst[3]}"
                                  if worst else ""))

    print()
    if fail:
        print(f"FAILED: {len(fail)} check(s): {', '.join(fail)}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
