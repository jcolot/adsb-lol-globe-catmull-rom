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
import argparse, importlib.util, json, os, random, struct, sys

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
    search over cell[] and a slice of post[]."""

    def __init__(self, blob):
        if blob[:8] != MAGIC:
            raise ValueError("bad magic")
        self.version, self.res, _, self.n_cells, self.n_legs = \
            struct.unpack_from("<BBHII", blob, 8)
        o = 20
        self.cells = list(struct.unpack_from(f"<{self.n_cells}Q", blob, o))
        o += 8 * self.n_cells
        self.off = list(struct.unpack_from(f"<{self.n_cells + 1}I", blob, o))
        o += 4 * (self.n_cells + 1)
        self.post = blob[o:]

    def get(self, cell):
        """Ascending lid list for one cell, or [] if the cell has no traffic."""
        lo, hi = 0, self.n_cells - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.cells[mid] == cell:
                break
            if self.cells[mid] < cell:
                lo = mid + 1
            else:
                hi = mid - 1
        else:
            return []
        i, end = self.off[mid], self.off[mid + 1]
        out, cur = [], 0
        while i < end:
            g, i = read_varint(self.post, i)
            cur += g
            out.append(cur)
        return out

    def query(self, cells):
        s = set()
        for c in cells:
            s.update(self.get(c))
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
    check("cells.bin magic + version", idx.version == 1 and idx.res == meta["index_res"])
    check("cells.bin n_legs matches", idx.n_legs == meta["n_legs"])
    check("cells ascending, no duplicates",
          all(idx.cells[i] < idx.cells[i + 1] for i in range(len(idx.cells) - 1)))
    check("posting offsets monotonic and cover post[]",
          all(idx.off[i] <= idx.off[i + 1] for i in range(len(idx.off) - 1))
          and idx.off[-1] == len(idx.post))
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
            ORDER BY s.leg_id, s.tds""").fetchall():
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

    # a leg's own cells must contain its nodes' cells
    sample = rng.sample(range(len(legs)), min(400, len(legs)))
    con.execute("CREATE TABLE pick2(lid INTEGER)")
    con.executemany("INSERT INTO pick2 VALUES (?)", [(int(i),) for i in sample])
    pairs = con.execute(f"""
        SELECT DISTINCT l.lid,
               h3_latlng_to_cell(p.lat/{qpos}.0, p.lon/{qpos}.0, {res}) AS cell
        FROM '{a.points}' p JOIN leg l USING (leg_id) SEMI JOIN pick2 USING (lid)
    """).fetchall()
    miss = sum(1 for lid, cell in pairs if lid not in idx.get(cell))
    check(f"every node's own cell posts its leg "
          f"({len(sample)} legs, {len(pairs)} node cells)", miss == 0,
          f"{miss} missing")

    print()
    if fail:
        print(f"FAILED: {len(fail)} check(s): {', '.join(fail)}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
