#!/usr/bin/env python3
"""
build_bundle.py - points_legs.parquet -> the per-day "bundle": four files that
make every flight findable and every "what flew through this box?" answerable.

    meta.json      manifest: units, counts, file sizes
    legs.parquet   one row per leg -- SHIPPED WHOLE, held in memory
    cells.bin      H3 inverted index, cell -> legs -- SHIPPED WHOLE
    tracks.bin     per-leg delta-varint node records -- RANGE-READ

The design turns on one observation: the two queries want different structures,
and only one of them needs the geometry. "Which flights went through this box?"
is set membership, answerable from an index small enough to hold in memory.
"Draw flight X" is a byte-range question. Because the box query never touches
tracks.bin, tracks.bin is free to be clustered for whole-flight retrieval
instead -- which is what dissolves the tension that would otherwise force two
copies of the payload.

Row order is (dep, t0), so every departure from one airport is a single
contiguous byte range: the property the retired per-airport partitions existed
to provide. Legs with no dep go in a tail bucket ordered by H3 anchor cell.

`lid` is the row index into legs.parquet, so a posting list from cells.bin
indexes the leg table directly with no lookup map.

TIME: nodes.parquet stores `t` as deciseconds past that AIRCRAFT's base_ts, so
raw `t` is not comparable between flights. This joins aircraft.parquet to get
base_ts and rebases everything on one epoch (meta.json/t_epoch), after which
t0/t1 are directly comparable day-relative deciseconds.
"""
import argparse, datetime, importlib.util, json, os, struct, sys, time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))

# reuse the CR reconstruction + densify SQL rather than restating it
def _load(name, path):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m

bh = _load("bh", os.path.join(HERE, "build_hexes.py"))


def _arrow(rel):
    """to_arrow_table() on current duckdb, fetch_arrow_table() on older."""
    return (rel.to_arrow_table() if hasattr(rel, "to_arrow_table")
            else rel.fetch_arrow_table())


MAGIC = b"ADSBIDX1"
IDX_VERSION = 1
Q_POS = 1e5                     # points_legs lat/lon are degrees * 1e5

# H3 average edge length in km, by resolution -- the sample step must stay well
# under this or a leg could skip a cell entirely
H3_EDGE_KM = [1107.71, 418.68, 158.24, 59.81, 22.61, 8.54, 3.23, 1.22,
              0.461, 0.174, 0.0659, 0.0249, 0.00940, 0.00356, 0.00135, 0.00051]


# ---------------------------------------------------------------------------
# varint / zigzag, vectorised. These run over every node in the day, so they
# are numpy-wide rather than per-value: byte counts by threshold comparison,
# then one scatter per byte position (at most 10 passes).
# ---------------------------------------------------------------------------
def zigzag(v):
    """int64 -> uint64, so small negative deltas stay one byte."""
    v = np.asarray(v, dtype=np.int64)
    return ((v << np.int64(1)) ^ (v >> np.int64(63))).astype(np.uint64)


def varint_encode(vals):
    """uint64 array -> (uint8 buffer, per-value byte count). LEB128."""
    v = np.asarray(vals, dtype=np.uint64)
    n = len(v)
    nb = np.ones(n, dtype=np.int64)
    for k in range(1, 10):
        nb += (v >= np.uint64(1) << np.uint64(7 * k))
    total = int(nb.sum())
    out = np.zeros(total, dtype=np.uint8)
    if not n:
        return out, nb
    start = np.zeros(n, dtype=np.int64)
    np.cumsum(nb[:-1], out=start[1:])
    for k in range(int(nb.max())):
        m = nb > k
        b = ((v[m] >> np.uint64(7 * k)) & np.uint64(0x7F)).astype(np.uint8)
        b |= (nb[m] > k + 1).astype(np.uint8) << 7      # continuation bit
        out[start[m] + k] = b
    return out, nb


def varint_one(x):
    b = bytearray()
    x = int(x)
    while True:
        c = x & 0x7F
        x >>= 7
        b.append(c | (0x80 if x else 0))
        if not x:
            return bytes(b)


# ---------------------------------------------------------------------------
# 1. leg order + metadata
# ---------------------------------------------------------------------------
def build_leg_table(con, points, meta, index_res, epoch_ds):
    """Ordered leg table. lid is assigned IN FINAL ROW ORDER, so lid == row index
    in legs.parquet and a posting list indexes the leg table directly."""
    has_cusp = "cusp" in {r[0] for r in
                          con.execute(f"DESCRIBE SELECT * FROM '{points}'").fetchall()}
    mcols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{meta}'").fetchall()}
    if "base_ts" not in mcols:
        sys.exit(f"{meta} has no base_ts column -- need aircraft.parquet from fit_spline.py")

    con.execute(f"""
        CREATE OR REPLACE TABLE leg AS
        WITH n AS (
            SELECT p.leg_id, p.icao, p.dep, p.arr, p.reg, p.type,
                   p.lat, p.lon, p.alt, p.t,
                   -- rebase per-aircraft t onto one epoch
                   (p.t::BIGINT + m.base_ts::BIGINT * 10 - {epoch_ds}) AS tds
            FROM '{points}' p JOIN '{meta}' m USING (icao)
        ), agg AS (
            SELECT leg_id,
                   any_value(icao) AS icao, any_value(dep) AS dep,
                   any_value(arr) AS arr,  any_value(reg) AS reg,
                   any_value(type) AS type,
                   count(*)::INTEGER AS n_nodes,
                   min(tds) AS t0, max(tds) AS t1,
                   min(lat) AS min_lat, max(lat) AS max_lat,
                   min(lon) AS min_lon, max(lon) AS max_lon,
                   min(alt) AS min_alt, max(alt) AS max_alt,
                   -- anchor = first node's cell, only used to order dep-less legs
                   h3_latlng_to_cell(
                       arg_min(lat, tds) / {Q_POS:.1f}::DOUBLE,
                       arg_min(lon, tds) / {Q_POS:.1f}::DOUBLE, {index_res}) AS anchor
            FROM n GROUP BY leg_id
        )
        SELECT (row_number() OVER (
                    ORDER BY (dep IS NULL), coalesce(dep, ''),
                             CASE WHEN dep IS NULL THEN anchor ELSE 0::UBIGINT END,
                             t0, leg_id) - 1)::INTEGER AS lid,
               leg_id, icao, dep, arr, reg, type, n_nodes,
               t0::BIGINT AS t0, t1::BIGINT AS t1,
               min_lat, max_lat, min_lon, max_lon, min_alt, max_alt, anchor
        FROM agg
    """)
    n_legs, n_nodes = con.execute(
        "SELECT count(*), sum(n_nodes) FROM leg").fetchone()
    return has_cusp, int(n_legs), int(n_nodes or 0)


# ---------------------------------------------------------------------------
# 2. tracks.bin
# ---------------------------------------------------------------------------
def encode_chunk(lid, tds, lat, lon, alt, gnd, cusp, nb_lid):
    """Encode one run of complete legs. Returns (payload, off, length) arrays.

    Deltas reset at every leg boundary, so each record decodes standalone from
    prev = 0. The four per-node streams are interleaved into ONE value array
    before encoding, which is what lets the varint scatter stay vectorised.
    """
    # leg boundaries within the chunk
    bnd = np.flatnonzero(np.diff(lid)) + 1
    starts = np.concatenate(([0], bnd))
    counts = np.diff(np.concatenate((starts, [len(lid)])))

    first = np.zeros(len(lid), dtype=bool)
    first[starts] = True

    def dz(col):
        d = col.astype(np.int64)
        out = d - np.concatenate(([0], d[:-1]))
        out[first] = d[first]           # first node of a leg is absolute
        return zigzag(out)

    dt = tds.astype(np.int64) - np.concatenate(([0], tds[:-1].astype(np.int64)))
    dt[first] = 0                       # t0 carries the absolute; dt[0] = 0
    if (dt < 0).any():
        raise ValueError("node timestamps not monotonic within a leg")

    vals = np.empty(len(lid) * 4, dtype=np.uint64)
    vals[0::4] = dt.astype(np.uint64)
    vals[1::4] = dz(lat)
    vals[2::4] = dz(lon)
    vals[3::4] = dz(alt)
    buf, nb = varint_encode(vals)

    # bytes of node block per leg = sum of nb over that leg's 4*n values
    nb_cum = np.concatenate(([0], np.cumsum(nb)))
    blk_start = nb_cum[starts * 4]
    blk_end = nb_cum[(starts + counts) * 4]

    parts, offs, lens = [], [], []
    pos = 0
    for i in range(len(starts)):
        s, c = int(starts[i]), int(counts[i])
        rec = [varint_one(c), varint_one(int(tds[s])),
               buf[blk_start[i]:blk_end[i]].tobytes(),
               np.packbits(gnd[s:s + c], bitorder="little").tobytes(),
               np.packbits(cusp[s:s + c], bitorder="little").tobytes()]
        b = b"".join(rec)
        if c != nb_lid[i]:
            raise ValueError(f"leg {int(lid[s])}: {c} nodes, index says {nb_lid[i]}")
        parts.append(b)
        offs.append(pos)
        lens.append(len(b))
        pos += len(b)
    return b"".join(parts), np.array(offs, np.int64), np.array(lens, np.int64)


def write_tracks(con, points, meta, path, epoch_ds, n_legs, has_cusp,
                 chunk_nodes, log):
    """Stream nodes in lid order, encode per chunk of COMPLETE legs, append."""
    off = np.zeros(n_legs, dtype=np.uint64)
    ln = np.zeros(n_legs, dtype=np.uint32)
    nb_lid = con.execute(
        "SELECT n_nodes FROM leg ORDER BY lid").fetchnumpy()["n_nodes"]

    res = con.execute(f"""
        SELECT l.lid AS lid,
               (p.t::BIGINT + m.base_ts::BIGINT * 10 - {epoch_ds}) AS tds,
               p.lat, p.lon, p.alt,
               coalesce(p.on_ground, false) AS gnd,
               {"coalesce(p.cusp, false)" if has_cusp else "false"} AS cusp
        FROM '{points}' p
        JOIN '{meta}' m USING (icao)
        JOIN leg l USING (leg_id)
        ORDER BY l.lid, tds
    """)
    reader = (res.to_arrow_reader(1_000_000)
              if hasattr(res, "to_arrow_reader")
              else res.fetch_record_batch(1_000_000))

    cols = ("lid", "tds", "lat", "lon", "alt", "gnd", "cusp")
    buf = {c: [] for c in cols}
    held = 0
    base = 0            # byte offset of the next record in the file
    done = 0            # legs written
    t_start = time.time()

    def flush(final=False):
        nonlocal held, base, done
        if not held:
            return
        arr = {c: np.concatenate(buf[c]) for c in cols}
        for c in cols:
            buf[c] = []
        lid = arr["lid"]
        if not final:
            # defer the trailing (possibly incomplete) leg to the next batch
            last = lid[-1]
            keep = np.flatnonzero(lid == last)
            cut = int(keep[0])
            if cut == 0:                       # whole chunk is one leg; wait
                for c in cols:
                    buf[c] = [arr[c]]
                held = len(lid)
                return
            for c in cols:
                buf[c] = [arr[c][cut:]]
            held = len(lid) - cut
            arr = {c: arr[c][:cut] for c in cols}
            lid = arr["lid"]
        else:
            held = 0
        n_here = int(lid[-1]) - int(lid[0]) + 1
        payload, o, l = encode_chunk(
            lid, arr["tds"], arr["lat"], arr["lon"], arr["alt"],
            arr["gnd"].astype(bool), arr["cusp"].astype(bool),
            nb_lid[int(lid[0]):int(lid[0]) + n_here])
        fh.write(payload)
        sl = slice(int(lid[0]), int(lid[0]) + n_here)
        off[sl] = o + base
        ln[sl] = l
        base += len(payload)
        done += n_here
        log(f"tracks: {done}/{n_legs} legs, {base/1e6:.1f} MB")

    with open(path, "wb") as fh:
        for b in reader:
            d = b.to_pydict()
            for c in cols:
                buf[c].append(np.asarray(d[c]))
            held += b.num_rows
            if held >= chunk_nodes:
                flush()
        flush(final=True)

    if done != n_legs:
        raise ValueError(f"wrote {done} legs, expected {n_legs}")
    # the records must tile the file exactly -- no gaps, no overlaps
    expect = np.concatenate(([0], np.cumsum(ln[:-1], dtype=np.uint64)))
    if not np.array_equal(off, expect):
        raise ValueError("track records do not tile tracks.bin contiguously")
    log(f"tracks.bin: {base/1e6:.1f} MB, {base/max(nb_lid.sum(),1):.2f} B/node "
        f"({time.time()-t_start:.1f}s)")
    return off, ln


# ---------------------------------------------------------------------------
# 3. cells.bin
# ---------------------------------------------------------------------------
def write_cells(con, points, path, index_res, step_km, buckets, has_cusp, log):
    """H3 inverted index: cell -> gap-coded ascending lid list.

    Sorted ascending by H3 index, which is the right key here: a parent's
    descendants form exactly ONE contiguous run in that order, so "everything
    under this cell" is a single slice and the client can pick its covering
    resolution by zoom.
    """
    con.execute("CREATE OR REPLACE TABLE pair (cell UBIGINT, lid INTEGER)")
    for b in range(buckets):
        sql = bh.densify_sql(points, has_cusp, step_km, 4096, b, buckets, 0)
        con.execute(f"""
            INSERT INTO pair
            SELECT DISTINCT h3_latlng_to_cell(s.lat, s.lon, {index_res}) AS cell,
                            l.lid AS lid
            FROM ({sql}) s JOIN leg l USING (leg_id)
        """)
        log(f"cells: bucket {b+1}/{buckets}, "
            f"{con.execute('SELECT count(*) FROM pair').fetchone()[0]} pairs")

    t = _arrow(con.execute("SELECT cell, lid FROM pair ORDER BY cell, lid"))
    cell = t.column("cell").to_numpy(zero_copy_only=False).astype(np.uint64)
    lid = t.column("lid").to_numpy(zero_copy_only=False).astype(np.int64)
    if not len(cell):
        sys.exit("no (cell, leg) pairs -- nothing to index")

    starts = np.concatenate(([0], np.flatnonzero(np.diff(cell)) + 1))
    ucell = cell[starts]
    # gap-code ascending lids within each cell; first of each cell is absolute
    gap = lid - np.concatenate(([0], lid[:-1]))
    gap[starts] = lid[starts]
    if (gap < 0).any():
        raise ValueError("posting lists not ascending")
    post, nb = varint_encode(gap.astype(np.uint64))

    # per-cell byte length -> prefix offsets
    nb_cum = np.concatenate(([0], np.cumsum(nb)))
    off = nb_cum[np.concatenate((starts, [len(lid)]))].astype(np.uint32)
    if int(off[-1]) != len(post):
        raise ValueError("posting offsets disagree with buffer length")

    n_legs = con.execute("SELECT count(*) FROM leg").fetchone()[0]
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<BBHII", IDX_VERSION, index_res, 0,
                            len(ucell), n_legs))
        f.write(ucell.astype("<u8").tobytes())
        f.write(off.astype("<u4").tobytes())
        f.write(post.tobytes())
    log(f"cells.bin: {os.path.getsize(path)/1e6:.2f} MB, {len(ucell)} cells, "
        f"{len(lid)} pairs, {len(post)/len(lid):.2f} B/pair")
    return len(ucell), len(lid)


# ---------------------------------------------------------------------------
# 4. legs.parquet
# ---------------------------------------------------------------------------
def write_legs(con, path, off, ln):
    t = con.execute("""
        SELECT lid, icao, leg_id, reg, type, dep, arr,
               t0, t1, n_nodes,
               min_lat, max_lat, min_lon, max_lon, min_alt, max_alt
        FROM leg ORDER BY lid
    """)
    t = _arrow(t)
    t = t.append_column("off", pa.array(off, pa.uint64()))
    t = t.append_column("len", pa.array(ln, pa.uint32()))
    schema = pa.schema([
        ("lid", pa.uint32()), ("icao", pa.string()), ("leg_id", pa.string()),
        ("reg", pa.string()), ("type", pa.string()),
        ("dep", pa.string()), ("arr", pa.string()),
        ("t0", pa.uint32()), ("t1", pa.uint32()), ("n_nodes", pa.uint16()),
        ("min_lat", pa.int32()), ("max_lat", pa.int32()),
        ("min_lon", pa.int32()), ("max_lon", pa.int32()),
        ("min_alt", pa.int32()), ("max_alt", pa.int32()),
        ("off", pa.uint64()), ("len", pa.uint32())])
    t = t.cast(schema)
    pq.write_table(
        t, path, compression="zstd", version="2.6",
        use_dictionary=["icao", "reg", "type", "dep", "arr"],
        # off/t0 are monotonic in row order and the bboxes correlate with the
        # (dep, t0) sort, so delta packing earns its keep on all of these
        column_encoding={c: "DELTA_BINARY_PACKED" for c in
                         ("lid", "t0", "t1", "off", "len", "n_nodes",
                          "min_lat", "max_lat", "min_lon", "max_lon",
                          "min_alt", "max_alt")})
    return os.path.getsize(path)


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--points", required=True, help="points_legs.parquet")
    p.add_argument("--meta", required=True,
                   help="aircraft.parquet from fit_spline.py (for base_ts)")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--date", default=None,
                   help="data date YYYY-MM-DD; default: from the median base_ts")
    p.add_argument("--index-res", type=int, default=4,
                   help="H3 resolution of cells.bin (4 = ~45 km across)")
    p.add_argument("--step-km", type=float, default=None,
                   help="curve sample spacing; default half the index cell edge")
    p.add_argument("--buckets", type=int, default=8,
                   help="leg hash buckets for the densify pass")
    p.add_argument("--chunk-nodes", type=int, default=4_000_000,
                   help="nodes buffered before encoding a run of legs")
    p.add_argument("--threads", type=int, default=0)
    p.add_argument("--memory-limit", default=None)
    a = p.parse_args()

    if not 0 <= a.index_res <= 15:
        sys.exit("--index-res must be 0..15")
    step_km = a.step_km or H3_EDGE_KM[a.index_res] / 2.0
    os.makedirs(a.out_dir, exist_ok=True)
    t_start = time.time()

    def log(msg):
        print(f"[{time.time()-t_start:7.1f}s] {msg}", flush=True)

    con = duckdb.connect()
    try:
        con.execute("INSTALL h3 FROM community")
        con.execute("LOAD h3")
    except Exception as e:
        sys.exit(f"could not load DuckDB's h3 community extension "
                 f"(duckdb {duckdb.__version__}): {e}")
    if a.threads:
        con.execute(f"SET threads TO {a.threads}")
    if a.memory_limit:
        con.execute(f"SET memory_limit = '{a.memory_limit}'")
    con.execute(bh.CR_MACROS)

    # epoch = UTC midnight of the data date
    if a.date:
        day = datetime.date.fromisoformat(a.date)
    else:
        med = con.execute(
            f"SELECT median(base_ts) FROM '{a.meta}'").fetchone()[0]
        if med is None:
            sys.exit("could not derive a date -- pass --date")
        day = datetime.datetime.fromtimestamp(
            float(med), datetime.timezone.utc).date()
    epoch = int(datetime.datetime(day.year, day.month, day.day,
                                  tzinfo=datetime.timezone.utc).timestamp())
    epoch_ds = epoch * 10
    log(f"date {day.isoformat()} (epoch {epoch}), index res {a.index_res}, "
        f"step {step_km:.2f} km")

    has_cusp, n_legs, n_nodes = build_leg_table(
        con, a.points, a.meta, a.index_res, epoch_ds)
    if not has_cusp:
        print("WARNING: no `cusp` column -- curve will not break at taxi corners")
    log(f"{n_legs} legs, {n_nodes} nodes")
    if n_legs and n_legs > 4_000_000:
        print(f"WARNING: {n_legs} legs -- legs.parquet may be too large to ship "
              f"whole; consider an icao-sorted copy for range-read lookup")

    tracks = os.path.join(a.out_dir, "tracks.bin")
    off, ln = write_tracks(con, a.points, a.meta, tracks, epoch_ds, n_legs,
                           has_cusp, a.chunk_nodes, log)

    legs = os.path.join(a.out_dir, "legs.parquet")
    legs_bytes = write_legs(con, legs, off, ln)
    log(f"legs.parquet: {legs_bytes/1e6:.2f} MB "
        f"({legs_bytes/max(n_legs,1):.1f} B/leg)")

    cells = os.path.join(a.out_dir, "cells.bin")
    n_cells, n_pairs = write_cells(con, a.points, cells, a.index_res, step_km,
                                   a.buckets, has_cusp, log)

    meta = dict(
        date=day.isoformat(), version=1,
        n_legs=n_legs, n_nodes=n_nodes,
        index_res=a.index_res, index_cells=n_cells, index_pairs=n_pairs,
        q_pos=int(Q_POS), t_unit="ds", t_epoch=epoch,
        t_epoch_iso=datetime.datetime.fromtimestamp(
            epoch, datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        step_km=step_km,
        files={k: dict(path=os.path.basename(v), bytes=os.path.getsize(v))
               for k, v in (("legs", legs), ("cells", cells),
                            ("tracks", tracks))})
    mpath = os.path.join(a.out_dir, "meta.json")
    with open(mpath, "w") as f:
        json.dump(meta, f, indent=1)

    ship = legs_bytes + os.path.getsize(cells) + os.path.getsize(mpath)
    log(f"DONE: {a.out_dir}  shipped-whole {ship/1e6:.2f} MB "
        f"+ tracks.bin {os.path.getsize(tracks)/1e6:.1f} MB range-read")


if __name__ == "__main__":
    main()
