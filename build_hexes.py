#!/usr/bin/env python3
"""
build_hexes.py - points_legs.parquet -> traffic.pmtiles (H3 traffic-density tiles).

A global *overview* layer, complementary to the per-airport spline partitions:
hexbins can't be animated or drawn as flight paths, so `legs/airports/` stays the
detail layer and this is what the frontend shows at world zoom.

Metric: DISTINCT FLIGHTS through each cell -- "how many aircraft crossed here
today". Deliberately NOT a count of spline nodes: the fitter places nodes densely
in turns and near the ground and sparsely at cruise, so a node histogram would
mostly render the tolerance ramp instead of air traffic.

Pipeline (all in DuckDB, so the expansion never materialises in Python):

  1. densify   Reconstruct each leg's centripetal Catmull-Rom curve -- the exact
               curve the frontend draws, broken at `cusp` nodes -- and sample it
               every --step-km. Chord interpolation is NOT good enough: a wide
               turn held by few nodes bows several km away from its chord, more
               than a res-6 hex is wide.
  2. hex       h3_latlng_to_cell at --max-res, reduced immediately to DISTINCT
               (cell, leg) pairs. Legs are bucketed by hash so each bucket's
               hash table stays small, and because a leg lives in exactly one
               bucket the per-bucket DISTINCT is globally distinct.
  3. rollup    Build the pyramid by h3_cell_to_parent, level by level: the
               distinct-pair set for res r-1 is derived from res r, shrinking ~7x
               each step. One flight crossing many children must count ONCE in
               the parent, so this rolls up pairs (then counts), never counts.
  4. geojson   One .geojsonl per resolution, hexes as real polygons.
  5. tiles     tippecanoe per resolution pinned to its zoom, tile-join'd into one
               .pmtiles archive. Levels are built finest-first and each is tiled
               and freed before the next, so peak disk stays near one level.

Zoom mapping is res = z - 2, one H3 resolution per zoom level, so a hexagon holds
a constant on-screen size. Above the top zoom MapLibre overzooms: hexes just grow,
which is the natural visual handover to the spline layer.

Two archives, and the RASTER is the one to draw:

  --raster-out  traffic-raster.pmtiles   PNG pyramid rendered from the FINEST hex
                                         level, halved down to z0. Keeps the route
                                         network legible at low zoom, which the
                                         vector one structurally cannot. See
                                         hex_raster.py.
  --out         traffic.pmtiles          vector hexbins, one H3 resolution per
                                         zoom. OPTIONAL. Only worth building if
                                         something needs per-cell values (a
                                         tooltip); omitting it skips the entire
                                         H3 roll-up and tippecanoe with it.

Vector styling: the tiles carry data, not looks -- `n` (distinct flights through
the cell), `dens` (mean flights per finest-resolution cell, an area-average), `dn`
(0-255 linear ramp of `dens`) and `a` (mean altitude, ft). Ramp opacity off `dn`,
not `n`: raw counts aren't comparable across resolutions (a res-0
cell swallows ~117x the area of a res-2 cell), so a single ramp on `n` blows out
at world zoom and vanishes when you zoom in.
"""
import argparse, json, os, shutil, subprocess, sys, time

import duckdb

Q_POS = 1e5             # points_legs lat/lon are degrees * 1e5 (fit_spline Q_POS)
DEG_KM = 111.32         # km per degree of latitude

# ---------------------------------------------------------------------------
# centripetal Catmull-Rom, in SQL. Mirrors _cr() in fit_spline.py: knots spaced
# by sqrt(chord), three nested lerps. Chords are measured in a local
# equal-ish-area plane (lon scaled by cos(lat)) because the PARAMETERISATION has
# to be metric; the lerps themselves are affine per axis so they run in raw
# degrees. Degenerate knots (coincident nodes) fall back to a straight lerp,
# exactly as the Python does.
# ---------------------------------------------------------------------------
CR_MACROS = """
CREATE OR REPLACE MACRO cr_chord(qx0, qy0, qx1, qy1, qkx) AS
    greatest(sqrt(((qx1-qx0)*qkx)*((qx1-qx0)*qkx) + (qy1-qy0)*(qy1-qy0)), 1e-12);

-- knot values t1,t2,t3 (t0 = 0), spaced by sqrt(chord) -> "centripetal"
CREATE OR REPLACE MACRO cr_t1(qx0, qy0, qx1, qy1, qkx) AS
    sqrt(cr_chord(qx0, qy0, qx1, qy1, qkx));

-- one axis of the curve at parameter u in [0,1) on the span p1->p2
CREATE OR REPLACE MACRO cr_axis(v0,v1,v2,v3, t1,t2,t3, u) AS (
    WITH k AS (SELECT t1 + (t2-t1)*u AS t)
    SELECT CASE WHEN t1 <= 0 OR t2 <= t1 OR t3 <= t2 THEN v1 + (v2-v1)*u ELSE
        -- B1 -> B2 lerp over [t1,t2]
        ( ( (v0 + (v1-v0)*(k.t-0)/(t1-0))                                    -- A1 (over [0,t1])
            + ((v1 + (v2-v1)*(k.t-t1)/(t2-t1))                               -- A2 (over [t1,t2])
               - (v0 + (v1-v0)*(k.t-0)/(t1-0))) * (k.t-0)/(t2-0) )           -- B1 (over [0,t2])
          + ( ( (v1 + (v2-v1)*(k.t-t1)/(t2-t1))
                + ((v2 + (v3-v2)*(k.t-t2)/(t3-t2))                           -- A3 (over [t2,t3])
                   - (v1 + (v2-v1)*(k.t-t1)/(t2-t1))) * (k.t-t1)/(t3-t1) )   -- B2 (over [t1,t3])
              - ( (v0 + (v1-v0)*(k.t-0)/(t1-0))
                  + ((v1 + (v2-v1)*(k.t-t1)/(t2-t1))
                     - (v0 + (v1-v0)*(k.t-0)/(t1-0))) * (k.t-0)/(t2-0) ) )
            * (k.t-t1)/(t2-t1)
        ) END
    FROM k
);
"""


def densify_sql(points, has_cusp, step_km, max_sub, bucket, buckets, limit_legs):
    """Sample every leg's reconstructed CR curve at ~step_km spacing.

    p0/p3 collapse onto p1/p2 at leg ends AND at cusp nodes, so the curve breaks
    where the frontend breaks it. A cusp node legitimately appears as p0 of the
    span after next (it is that segment's start), which the CASEs below allow."""
    cusp = "cusp" if has_cusp else "false"
    # ORDER BY, or each bucket's LIMIT picks a different subset and the buckets
    # stop being a partition of one consistent set of legs.
    legfilter = (f"AND leg_id IN (SELECT DISTINCT leg_id FROM '{points}' "
                 f"ORDER BY leg_id LIMIT {limit_legs})") if limit_legs else ""
    return f"""
WITH src AS (
    SELECT leg_id, t, lat::DOUBLE / {Q_POS:.1f}::DOUBLE AS y,
           lon::DOUBLE / {Q_POS:.1f}::DOUBLE AS x, alt::DOUBLE AS alt,
           {cusp} AS cusp
    FROM '{points}'
    WHERE hash(leg_id) % {buckets} = {bucket} {legfilter}
), w AS (
    SELECT leg_id, y, x, alt, cusp,
           lag(y, 1)  OVER q AS y_1, lag(x, 1)  OVER q AS x_1,
           lead(y, 1) OVER q AS y1,  lead(x, 1) OVER q AS x1,
           lead(y, 2) OVER q AS y2,  lead(x, 2) OVER q AS x2,
           lead(alt, 1) OVER q AS alt1, lead(cusp, 1) OVER q AS cusp1
    FROM src WINDOW q AS (PARTITION BY leg_id ORDER BY t)
), span AS (
    -- one row per drawn span p1->p2, with its four control points resolved
    SELECT leg_id, alt, alt1,
           x AS x1c, y AS y1c, x1 AS x2c, y1 AS y2c,
           CASE WHEN cusp OR x_1 IS NULL THEN x  ELSE x_1 END AS x0c,
           CASE WHEN cusp OR y_1 IS NULL THEN y  ELSE y_1 END AS y0c,
           CASE WHEN coalesce(cusp1, true) OR x2 IS NULL THEN x1 ELSE x2 END AS x3c,
           CASE WHEN coalesce(cusp1, true) OR y2 IS NULL THEN y1 ELSE y2 END AS y3c,
           cos(radians(y)) AS kx
    FROM w WHERE y1 IS NOT NULL
), knot AS (
    SELECT *, cr_t1(x0c, y0c, x1c, y1c, kx) AS t1,
           cr_t1(x0c, y0c, x1c, y1c, kx)
             + sqrt(cr_chord(x1c, y1c, x2c, y2c, kx)) AS t2,
           cr_t1(x0c, y0c, x1c, y1c, kx)
             + sqrt(cr_chord(x1c, y1c, x2c, y2c, kx))
             + sqrt(cr_chord(x2c, y2c, x3c, y3c, kx)) AS t3,
           least({max_sub}, greatest(1, ceil(
               cr_chord(x1c, y1c, x2c, y2c, kx) * {DEG_KM}::DOUBLE / {step_km}::DOUBLE
           )::BIGINT)) AS nsub
    FROM span
)
SELECT leg_id,
       cr_axis(y0c, y1c, y2c, y3c, t1, t2, t3, i::DOUBLE / nsub) AS lat,
       cr_axis(x0c, x1c, x2c, x3c, t1, t2, t3, i::DOUBLE / nsub) AS lon,
       (alt + (alt1 - alt) * i::DOUBLE / nsub) AS alt
FROM knot, LATERAL range(nsub) g(i)
UNION ALL
SELECT leg_id, y AS lat, x AS lon, alt FROM w WHERE y1 IS NULL   -- each leg's last node
"""


def write_stats(a, n_legs, layers, rstats, path, el):
    """Sidecar next to the archive, not in tmp: tmp is deleted on success and
    the per-level cell counts are the thing worth watching day over day."""
    with open(path, "w") as f:
        json.dump(dict(legs=n_legs, step_km=a.step_km,
                       zoom_offset=a.zoom_offset, max_res=a.max_res,
                       levels=[{k: v for k, v in l.items() if k != "path"}
                               for l in layers],
                       raster=rstats), f, indent=1)
    print(f"{el()} stats: {path}")
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--points", required=True,
                   help="points_legs.parquet from build_legs.py")
    p.add_argument("--out", default=None,
                   help="vector hexbin .pmtiles path. OPTIONAL: omit it and only "
                        "the raster is built, which skips the whole H3 roll-up "
                        "(the raster reads the finest level and derives its own "
                        "pyramid by halving the image) as well as tippecanoe")
    p.add_argument("--tmp", default=None, help="scratch dir (default <out>.tmp)")
    p.add_argument("--max-res", type=int, default=6,
                   help="finest H3 resolution (6 = 3.2 km edge, drawn at z8)")
    p.add_argument("--min-res", type=int, default=0)
    p.add_argument("--zoom-offset", type=int, default=2, help="zoom = res + this")
    p.add_argument("--raster-out", default=None,
                   help="raster overview .pmtiles path -- this is the archive a "
                        "map should draw; see hex_raster.py for why")
    p.add_argument("--raster-max-zoom", type=int, default=4,
                   help="zoom the raster grid is built at; at 512px tiles z4 is "
                        "4.9 km/px, which matches res 6")
    p.add_argument("--raster-tile-size", type=int, default=512)
    p.add_argument("--grid-out", default=None,
                   help="also dump the raw density grid as a sparse .npz -- "
                        "unnormalised, so a stack of days is comparable; this "
                        "is what render_video.py consumes")
    p.add_argument("--grid-zoom", type=int, default=2,
                   help="zoom level to dump for --grid-out (default 2 = 2048px "
                        "world, enough for 1080p)")
    p.add_argument("--raster-gamma", type=float, default=2.0,
                   help="ramp compression for the raster: alpha = (v/p99) ** "
                        "(1/gamma). 1 = linear, which measured as leaving most "
                        "of the network invisible; see hex_raster.py")
    p.add_argument("--step-km", type=float, default=1.5,
                   help="curve sample spacing; keep <= half the finest hex edge")
    p.add_argument("--max-sub", type=int, default=4096,
                   help="cap on samples per span (bounds the lateral expansion)")
    p.add_argument("--buckets", type=int, default=16,
                   help="leg hash buckets; more = smaller hash tables, more scans")
    p.add_argument("--threads", type=int, default=0, help="0 = DuckDB default")
    p.add_argument("--memory-limit", default=None, help="e.g. 6GB")
    p.add_argument("--limit-legs", type=int, default=0, help="smoke-test subset")
    p.add_argument("--keep-tmp", action="store_true")
    p.add_argument("--no-tiles", action="store_true",
                   help="stop after the .geojsonl files (skip tippecanoe)")
    a = p.parse_args()

    if a.min_res > a.max_res:
        sys.exit("--min-res must be <= --max-res")
    if not a.out and not a.raster_out:
        sys.exit("nothing to do: pass --raster-out and/or --out")
    want_vector = a.out is not None
    # fail before the aggregation, not after an hour of it -- and only when the
    # vector archive is actually being built, since nothing else needs these
    if want_vector and not a.no_tiles:
        for tool in ("tippecanoe", "tile-join"):
            if not shutil.which(tool):
                sys.exit(f"{tool} not found on PATH. Build tippecanoe >= 2.17 "
                         f"(https://github.com/felt/tippecanoe), or rerun with "
                         f"--no-tiles to stop after the .geojsonl files.")
    tmp = a.tmp or ((a.out or a.raster_out) + ".tmp")
    os.makedirs(tmp, exist_ok=True)
    t0 = time.time()

    def el():
        return f"[{time.time()-t0:7.1f}s]"

    db = os.path.join(tmp, "hex.duckdb")
    if os.path.exists(db):
        os.remove(db)
    con = duckdb.connect(db)
    try:
        con.execute("INSTALL h3 FROM community")
        con.execute("LOAD h3")
    except Exception as e:
        sys.exit(f"could not load DuckDB's h3 community extension "
                 f"(duckdb {duckdb.__version__}): {e}\n"
                 f"needs network access on first run; if the extension has no build "
                 f"for this duckdb version, pin an older duckdb in requirements.txt")
    con.execute(f"SET temp_directory = '{os.path.join(tmp, 'spill')}'")
    if a.threads:
        con.execute(f"SET threads TO {a.threads}")
    if a.memory_limit:
        con.execute(f"SET memory_limit = '{a.memory_limit}'")
    con.execute(CR_MACROS)

    has_cusp = "cusp" in {r[0] for r in
                          con.execute(f"DESCRIBE SELECT * FROM '{a.points}'").fetchall()}
    if not has_cusp:
        print("WARNING: no `cusp` column -- curve will not break at taxi corners")

    # ---- leg id -> dense int (80M+ pair rows; 4-byte ids beat dictionary strings)
    con.execute(f"""
        CREATE TABLE leg AS
        SELECT leg_id, (row_number() OVER (ORDER BY leg_id))::INTEGER AS lid
        FROM (SELECT DISTINCT leg_id FROM '{a.points}')
    """)
    n_legs = con.execute("SELECT count(*) FROM leg").fetchone()[0]
    print(f"{el()} {n_legs} legs")

    # --max-sub bounds the lateral expansion, but a span longer than
    # max_sub * step_km would then be under-sampled and could skip cells. Say so
    # rather than truncating quietly.
    reach = a.max_sub * a.step_km
    capped = con.execute(f"""
        WITH w AS (
            SELECT lat::DOUBLE / {Q_POS:.1f}::DOUBLE AS y,
                   lon::DOUBLE / {Q_POS:.1f}::DOUBLE AS x,
                   lead(lat::DOUBLE / {Q_POS:.1f}::DOUBLE) OVER q AS y1,
                   lead(lon::DOUBLE / {Q_POS:.1f}::DOUBLE) OVER q AS x1
            FROM '{a.points}' WINDOW q AS (PARTITION BY leg_id ORDER BY t)
        )
        SELECT count(*) FROM w WHERE y1 IS NOT NULL
          AND sqrt(((x1-x)*cos(radians(y)))*((x1-x)*cos(radians(y)))
                   + (y1-y)*(y1-y)) * {DEG_KM}::DOUBLE > {reach}::DOUBLE
    """).fetchone()[0]
    if capped:
        print(f"WARNING: {capped} span(s) longer than {reach:.0f} km hit --max-sub "
              f"({a.max_sub}) and are sampled coarser than --step-km")

    # ---- 1+2. densify -> hex -> DISTINCT (cell, lid), per leg-hash bucket.
    # A leg hashes to exactly one bucket, so per-bucket DISTINCT is already
    # globally distinct and the final count is count(*), not count(DISTINCT).
    R = a.max_res
    con.execute(f"CREATE TABLE pair_{R} (cell UBIGINT, lid INTEGER)")
    # `a`/`amin` are vector feature properties; the raster carries density only
    if want_vector:
        con.execute(f"CREATE TABLE alt_{R} "
                    f"(cell UBIGINT, asum DOUBLE, acnt BIGINT, amin INTEGER)")
    for b in range(a.buckets):
        con.execute(f"CREATE OR REPLACE TEMP VIEW s AS "
                    f"SELECT * FROM ("
                    f"{densify_sql(a.points, has_cusp, a.step_km, a.max_sub, b, a.buckets, a.limit_legs)}"
                    f")")
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE cells AS
            SELECT h3_latlng_to_cell(lat, lon, {R}) AS cell, l.lid AS lid, s.alt AS alt
            FROM s JOIN leg l USING (leg_id)
        """)
        con.execute(f"INSERT INTO pair_{R} SELECT DISTINCT cell, lid FROM cells")
        if want_vector:
            con.execute(f"""
                INSERT INTO alt_{R}
                SELECT cell, sum(alt), count(*), min(alt)::INTEGER
                FROM cells GROUP BY cell
            """)
        ns, np_ = con.execute("SELECT count(*) FROM cells").fetchone()[0], \
            con.execute(f"SELECT count(*) FROM pair_{R}").fetchone()[0]
        print(f"{el()} bucket {b+1}/{a.buckets}: {ns} samples -> {np_} pairs so far")
        con.execute("DROP TABLE cells")

    # buckets each aggregated altitude independently; merge the partials
    if want_vector:
        con.execute(f"""
            CREATE OR REPLACE TABLE alt_{R} AS
            SELECT cell, sum(asum) AS asum, sum(acnt) AS acnt, min(amin) AS amin
            FROM alt_{R} GROUP BY cell
        """)

    # The density accumulator, and the reason the coarse zooms read at all.
    #
    # `n` rolls up as a SET UNION -- distinct flights through the cell -- which is
    # the honest answer to "how many flights crossed here" but grows with cell
    # area, so every coarse cell converges on "lots" and the level saturates. On a
    # real day, union-counting put western Europe's res-2 cells in the global top
    # 2% with almost no spread left between them.
    #
    # `w` instead accumulates the SUM of the finest level's per-cell counts, which
    # divided by the descendant count is an area-average: mean flights per
    # finest-resolution cell. That is what a raster overview pyramid computes when
    # it averages 2x2 pixels into their parent (Mapterhorn's downsampling stage
    # does exactly this), and it is scale-invariant in the way a union-count
    # cannot be -- so one linear ramp reads correctly at every zoom. Measured on
    # the same day, it widens western Europe's own spread from 50%% to 65%% of the
    # global range and moves its median off the ceiling.
    if want_vector:
        con.execute(f"""
            CREATE OR REPLACE TABLE w_{R} AS
            SELECT cell, count(*)::DOUBLE AS w FROM pair_{R} GROUP BY cell
        """)

    # ---- 3. The raster, off the FINEST level and nothing else. It derives its
    # own pyramid by halving the image, so it never needs the H3 roll-up below --
    # which is why --raster-out alone skips everything after this.
    rstats = None
    if a.raster_out:
        import hex_raster
        pts = con.execute(f"""
            SELECT h3_cell_to_lat(cell) AS lat, h3_cell_to_lng(cell) AS lon,
                   count(*)::DOUBLE AS n
            FROM pair_{R} GROUP BY cell
        """).fetchnumpy()
        if len(pts["n"]):
            rstats = hex_raster.build(
                pts["lat"], pts["lon"], pts["n"], a.raster_out,
                max_zoom=a.raster_max_zoom, tile_size=a.raster_tile_size,
                gamma=a.raster_gamma,
                grid_out=a.grid_out, grid_zoom=a.grid_zoom,
                log=lambda m: print(f"{el()} {m}", flush=True))
            print(f"{el()} raster DONE: {a.raster_out} "
                  f"({os.path.getsize(a.raster_out)/1e6:.1f} MB)")
        else:
            print(f"{el()} raster: no cells, skipped")
        del pts

    if not want_vector:
        write_stats(a, n_legs, [], rstats, a.raster_out + ".stats.json", el)
        con.close()
        if not a.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        return

    # ---- 4+5+6. Walk the pyramid finest-first, and finish each level completely
    # (aggregate -> geojsonl -> tiles -> free) before deriving the next. The
    # alternative -- build every level, then write every geojsonl, then tile --
    # holds all of it on disk at once, and at res 6 that is several GB more than
    # a CI runner has spare next to the extracted traces.
    layers = []
    for r in range(R, a.min_res - 1, -1):
        z = r + a.zoom_offset
        # The coarsest level also fills every zoom below its own. MapLibre does
        # not underzoom -- coveringTiles() drops any tile below the source's
        # minzoom rather than stretching a parent -- so an archive starting at
        # z2 renders NOTHING from z0 to z1.9, which is exactly the globe view
        # this layer exists for. 21 extra tiles buys that back.
        z_lo = 0 if r == a.min_res else z
        # 7 children per cell per level (6 under the 12 pentagons, which is a
        # rounding error at this scale), so this is the descendant count that
        # turns the accumulated w back into a per-finest-cell average. Counting
        # missing descendants as zero is the point: a parent holding one busy
        # child and six empty ones is not as dense as one busy throughout.
        descendants = 7 ** (R - r)
        con.execute(f"""
            CREATE OR REPLACE TABLE agg_{r} AS
            SELECT p.cell AS cell, count(*)::BIGINT AS n,
                   max(w.w) / {descendants}::DOUBLE AS dens,
                   round(max(al.asum) / max(al.acnt))::INTEGER AS a,
                   max(al.amin) AS amin
            FROM pair_{r} p JOIN alt_{r} al USING (cell) JOIN w_{r} w USING (cell)
            GROUP BY p.cell
        """)
        ncell, nmax, nmed, dp99, dmax = con.execute(f"""
            SELECT count(*), max(n), coalesce(median(n), 1),
                   coalesce(quantile_cont(dens, 0.99), 1), coalesce(max(dens), 1)
            FROM agg_{r}
        """).fetchone()

        if ncell:
            dp99 = max(float(dp99), 1e-9)
            gj = os.path.join(tmp, f"res{r}.geojsonl")
            # dn = dens on a LINEAR 0-255 ramp against this resolution's p99.
            #
            # Linear, not log and not a percentile rank. Both of those were tried
            # against a real day and both flattened the busy regions: they spend
            # the range making the empty 90%% of the planet visible, which leaves
            # nothing for the corridors. Linear on an area-average keeps the
            # contrast where the traffic is, at the cost of central Africa reading
            # as genuinely near-empty -- which it is, at ~100x less traffic. That
            # is an editorial choice and this is the honest side of it.
            #
            # Clipped at the p99 rather than the max so a handful of extreme cells
            # (a single airport can be 10x its own neighbourhood) don't compress
            # everything else into the bottom of the ramp.
            dexpr = f"least(255, round(255 * dens / {dp99}::DOUBLE))"
            con.execute(f"""
                COPY (
                  WITH g AS (
                    SELECT n, round(dens, 2) AS dens, a, amin,
                           {dexpr}::INTEGER AS dn,
                           list_transform(h3_cell_to_vertexes(cell),
                               v -> [h3_vertex_to_lng(v), h3_vertex_to_lat(v)]) AS ring
                    FROM agg_{r}
                  ), u AS (
                    -- unwrap each ring relative to its first vertex so an
                    -- antimeridian cell stays a small polygon instead of
                    -- smearing right across the globe
                    SELECT n, dens, a, amin, dn, list_transform(ring, q ->
                        [CASE WHEN q[1] - ring[1][1] >  180 THEN q[1] - 360
                              WHEN q[1] - ring[1][1] < -180 THEN q[1] + 360
                              ELSE q[1] END, q[2]]) AS ring
                    FROM g
                  ), s AS (
                    -- a cell now poking past +-180 is emitted twice, shifted, so
                    -- tippecanoe clips each copy to the world and both halves draw
                    SELECT n, dens, a, amin, dn, ring, unnest(
                        CASE WHEN list_max(list_transform(ring, q -> q[1])) >  180
                                  THEN [0.0, -360.0]
                             WHEN list_min(list_transform(ring, q -> q[1])) < -180
                                  THEN [0.0,  360.0]
                             ELSE [0.0] END) AS sh
                    FROM u
                  )
                  SELECT '{{"type":"Feature","properties":{{"n":' || n
                      || ',"dens":' || dens || ',"dn":' || dn
                      || ',"a":' || a || ',"amin":' || amin
                      || '}},"geometry":{{"type":"Polygon","coordinates":[['
                      || array_to_string(list_transform(ring,
                             q -> '[' || round(q[1] + sh, 5) || ','
                                      || round(q[2], 5) || ']'), ',')
                      || ',[' || round(ring[1][1] + sh, 5) || ','
                      || round(ring[1][2], 5) || ']]]}}}}'
                  FROM s
                ) TO '{gj}' (FORMAT csv, HEADER false, DELIMITER E'\\x1f', QUOTE E'\\x01')
            """)
            zlabel = f"z{z}" if z_lo == z else f"z{z_lo}-{z}"
            print(f"{el()} res {r} -> {zlabel}: {ncell} cells, "
                  f"median {nmed:.0f} / max {nmax} flights, "
                  f"dens p99 {dp99:.1f} / max {dmax:.1f}"
                  f"  ({os.path.getsize(gj)/1e6:.1f} MB geojsonl)")
            lay = dict(res=r, zoom=z, zoom_lo=z_lo, cells=ncell,
                       nmax=nmax, nmedian=float(nmed),
                       dens_p99=float(dp99), dens_max=float(dmax),
                       dn="linear(dens / dens_p99)")
            if a.no_tiles:
                lay["path"] = gj
            else:
                # -pf/-pk/-ps/-pt: the bins ARE the intended geometry at this
                # zoom, so drop the feature limit, the tile size limit,
                # simplification and tiny-polygon reduction. tippecanoe must not
                # second-guess them.
                part = os.path.join(tmp, f"res{r}.pmtiles")
                subprocess.run(["tippecanoe", "-o", part, "--force",
                                "-Z", str(z_lo), "-z", str(z), "-l", f"h{r}",
                                "-pf", "-pk", "-ps", "-pt", "--quiet", gj],
                               check=True)
                os.remove(gj)               # ~10x the tiles; don't keep both
                lay["path"] = part
                print(f"{el()} tiled res {r} ({os.path.getsize(part)/1e6:.1f} MB)")
            layers.append(lay)
        else:
            print(f"{el()} res {r}: empty, skipped")

        # derive the next-coarser level, then free this one. Rolling up PAIRS
        # (not counts) is what makes one flight crossing many children count
        # ONCE in the parent.
        if r > a.min_res:
            con.execute(f"""
                CREATE TABLE pair_{r-1} AS
                SELECT DISTINCT h3_cell_to_parent(cell, {r-1}) AS cell, lid FROM pair_{r}
            """)
            con.execute(f"""
                CREATE TABLE alt_{r-1} AS
                SELECT h3_cell_to_parent(cell, {r-1}) AS cell,
                       sum(asum) AS asum, sum(acnt) AS acnt, min(amin) AS amin
                FROM alt_{r} GROUP BY 1
            """)
            # SUM, deliberately -- w is an accumulated total that only becomes an
            # average when divided by the descendant count above. Averaging here
            # instead would silently drop the empty descendants from the divisor.
            con.execute(f"""
                CREATE TABLE w_{r-1} AS
                SELECT h3_cell_to_parent(cell, {r-1}) AS cell, sum(w) AS w
                FROM w_{r} GROUP BY 1
            """)
        for tbl in (f"pair_{r}", f"alt_{r}", f"agg_{r}", f"w_{r}"):
            con.execute(f"DROP TABLE IF EXISTS {tbl}")

    layers.reverse()                        # coarsest first, for tile-join
    stats = write_stats(a, n_legs, layers, rstats, a.out + ".stats.json", el)
    con.close()

    if a.no_tiles:
        print(f"{el()} DONE (no tiles): {tmp}/res*.geojsonl")
        return
    if not layers:
        sys.exit("no non-empty resolutions -- nothing to tile")

    # tile-join prints "mismatched maxzooms" here -- expected and harmless: the
    # inputs deliberately cover one zoom each and the output takes their union.
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    subprocess.run(["tile-join", "-o", a.out, "--force", "-pk",
                    "-n", "adsb traffic density",
                    *[l["path"] for l in layers]], check=True)
    print(f"{el()} DONE: {a.out} ({os.path.getsize(a.out)/1e6:.1f} MB), layers "
          f"h{layers[0]['res']}..h{layers[-1]['res']} at "
          f"z{layers[0]['zoom_lo']}..z{layers[-1]['zoom']}\n  {stats}")
    if not a.keep_tmp:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
