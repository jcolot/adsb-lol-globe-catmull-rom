#!/usr/bin/env python3
"""
splice_legs.py - rejoin flights that the daily archive boundary cut in half.

adsb.lol publishes one archive per UTC day, so a flight airborne at 00:00Z is
split across two of them and each half loses an endpoint: the far end is a
cruise fix hundreds of km from any airport, so build_legs' 10 km resolver
returns NULL for it. Both halves are then dropped by any consumer that needs
dep AND arr -- including overnight.py, whose whole subject is flights that
cross midnight somewhere.

The loss is not uniform. It scales with flight length (~duration/24, so 31% of
JFK-LHR and 58% of LAX-SYD), and 00:00Z is 20:00 in New York and 17:00 in Los
Angeles -- the departure peak for exactly the transatlantic and transpacific
red-eyes that are canonically overnight.

Matching needs no time tolerance, because "the archive cut this leg" is exact:

  tail  the leg is the aircraft's LAST in day D, its arr is NULL, and its
        airborne run reaches the leg's final point (t_on == t_end) -- i.e. the
        aircraft was still flying when the data stopped. A real landing leaves
        ground or descent fixes after t_on, so t_on < t_end.
  head  the mirror image in day D+1: first leg, dep NULL, t_off == t_start.

An aircraft has at most one tail and one head per boundary, so (icao, boundary)
is a unique key. A flight under 24 h contains at most one 00:00Z, so a leg is
never cut into three.

What the timestamps CANNOT do is confirm the match, which is why they are not
used for it. Over the North Atlantic the median node gap at cruise is 2.7 h
(README), so the last fix before midnight can sit hours short of it and the
first fix after can be hours past. Two independent checks instead:

  --max-gap-h    the unobserved stretch across the boundary, default 3.0 h,
                 sized against that 2.7 h median rather than the cut itself;
  speed          gc(dep, arr) / airborne time must land in a plausible band,
                 which catches a tail spliced onto an unrelated head without
                 needing any position data.

TIME: the two halves come from different archives with different per-aircraft
base_ts, so a spliced leg has no single relative frame. The output therefore
carries ABSOLUTE deciseconds in t_off/t_on with base_ts = 0 -- chosen so the
usual `base_ts + t/10` rebase still yields absolute UTC seconds and downstream
readers need no special case.

Usage:
  ./splice_legs.py --root legs --out legs_spliced.parquet
  ./splice_legs.py 2026-08-01=a/flights.parquet 2026-08-02=b/flights.parquet \
                   --out legs_spliced.parquet
"""
import argparse, datetime as dt, glob, os, re, sys
import duckdb, pyarrow as pa, pyarrow.parquet as pq

from overnight import load_airports, gc_km

MAX_GAP_H = 3.0
MIN_KMH, MAX_KMH = 100.0, 1100.0   # plausible block-average ground speed
# The longest scheduled nonstop is ~19 h, so a spliced leg claiming more than
# this is wrong no matter what distance it covers. The speed band alone does NOT
# catch these: on real data a 45.6 h KDSM->VHHH splice worked out to 254 km/h,
# comfortably inside the band. They are collapse-bug legs -- an aircraft that
# never emits a surface message has its whole day made one leg, and two of those
# joined span two days.
MAX_AIR_H = 20.0
DATE_RE = re.compile(r"date=(\d{4}-\d{2}-\d{2})")

COLS = ["leg_id", "icao", "reg", "type", "dep", "arr",
        "t_off", "t_on", "dep_gnd", "arr_gnd", "base_ts", "n_points"]


def discover(root):
    out = []
    for p in sorted(glob.glob(os.path.join(root, "date=*", "flights.parquet"))):
        m = DATE_RE.search(p)
        if m:
            out.append((m.group(1), p))
    return out


def read_day(con, date, path, max_gap_h=MAX_GAP_H):
    """One day's legs with absolute-UTC deciseconds, plus the cut markers.

    `t_on == t_end` alone is NOT a midnight cut. It says only "still airborne at
    the last fix", which is equally true of an aircraft that flew out of
    receiver coverage -- and measured on 2026-09-08, 42% of the legs matching it
    had their last fix more than SIX HOURS before the boundary, so they are
    coverage dropouts, not archive cuts. Requiring the last fix to be within
    max_gap_h of the boundary separates the two.

    This does not change which pairs are accepted: a tail far from the boundary
    whose head is just past it already fails the gap check by construction. It
    shrinks the candidate pool and makes the rejection counts mean what they
    say.

    A half must also carry the endpoint the splice exists to recover -- a tail
    needs a `dep`, a head needs an `arr`. Without that, splicing a dep-less tail
    onto an arr-less head yields a leg with NEITHER endpoint, which nothing
    downstream can use and which silently skips the speed check, having no
    airports to measure between. On real data these were the aircraft that never
    emit a surface message: build_legs collapses their whole day into a single
    leg, and two of those spliced together came out as 48 HOURS airborne.
    """
    d = dt.date.fromisoformat(date)
    # 00:00Z that ENDS this day, and the one that begins it, in deciseconds
    end_ds = int(dt.datetime(d.year, d.month, d.day,
                             tzinfo=dt.timezone.utc).timestamp() + 86400) * 10
    beg_ds = end_ds - 86400 * 10
    win = int(max_gap_h * 3600 * 10)
    return con.execute(f"""
        WITH l AS (
            SELECT leg_id, icao, reg, type, dep, arr, dep_gnd, arr_gnd, n_points,
                   base_ts * 10 + t_off AS off_ds,
                   base_ts * 10 + t_on  AS on_ds,
                   t_off, t_on, t_start, t_end,
                   row_number() OVER (PARTITION BY icao ORDER BY t_off)      AS seq,
                   count(*)     OVER (PARTITION BY icao)                    AS n_legs
            FROM '{path}' WHERE base_ts IS NOT NULL
        )
        SELECT leg_id, icao, reg, type, dep, arr, dep_gnd, arr_gnd, n_points,
               off_ds, on_ds,
               -- still airborne when this archive ended, AND close enough to
               -- the boundary that the archive is a plausible reason for it,
               -- AND carrying the endpoint the splice is supposed to recover
               (arr IS NULL AND dep IS NOT NULL
                AND t_on = t_end AND seq = n_legs
                AND {end_ds} - on_ds <= {win})                    AS is_tail,
               -- already airborne when the next archive began
               (dep IS NULL AND arr IS NOT NULL
                AND t_off = t_start AND seq = 1
                AND off_ds - {beg_ds} <= {win})                   AS is_head,
               -- airborne at the last fix but nowhere near the boundary: the
               -- aircraft left coverage, which is a different thing entirely
               (arr IS NULL AND t_on = t_end AND seq = n_legs
                AND {end_ds} - on_ds > {win})                     AS is_dropout
        FROM l
    """).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="*", metavar="DATE=PATH")
    ap.add_argument("--root", help="directory of date=YYYY-MM-DD/flights.parquet")
    ap.add_argument("--out", required=True)
    ap.add_argument("--airports-tz", default="airport_tz.csv")
    ap.add_argument("--max-gap-h", type=float, default=MAX_GAP_H)
    ap.add_argument("--max-air-h", type=float, default=MAX_AIR_H,
                    help="reject a splice claiming more airborne time than "
                         "this (default %(default)s h)")
    ap.add_argument("--dep-date", metavar="YYYY-MM-DD",
                    help="keep only legs whose wheels-off falls in this UTC "
                         "date. Splicing needs two days of input, so without "
                         "this the output spans both and consecutive daily "
                         "tables would double-count the overlap")
    ap.add_argument("--keep-halves", action="store_true",
                    help="also emit unmatched halves (dep or arr still NULL); "
                         "off by default because a half-leg's t_on is the "
                         "truncation instant, not a wheels event, and averaging "
                         "it into a block time is worse than dropping it")
    a = ap.parse_args()

    days = discover(a.root) if a.root else []
    for spec in a.inputs:
        if "=" not in spec:
            sys.exit(f"expected DATE=PATH, got {spec!r}")
        d, p = spec.split("=", 1)
        days.append((d, p))
    days = sorted(set(days))
    if not days:
        sys.exit("no inputs -- pass --root or DATE=PATH arguments")

    airports = load_airports(a.airports_tz)
    con = duckdb.connect()
    per_day = {d: read_day(con, d, p, a.max_gap_h) for d, p in days}
    for d, p in days:
        print(f"  {d}: {len(per_day[d])} legs  ({p})")

    complete, spliced = [], []
    matched_heads = set()          # head leg_ids consumed, counted once
    tails_left = 0
    rej_gap = rej_speed = rej_nopair = rej_air = 0
    dropouts = sum(1 for rows in per_day.values() for r in rows if r[13])

    dates = [d for d, _ in days]
    for i, d in enumerate(dates):
        rows = per_day[d]
        nxt = per_day[dates[i + 1]] if i + 1 < len(dates) else []
        # is the following archive actually the next calendar day? a gap in
        # retention means the boundary was never observed, so nothing to splice
        contiguous = bool(nxt) and (
            dt.date.fromisoformat(dates[i + 1]) - dt.date.fromisoformat(d)
        ).days == 1
        heads = {}
        if contiguous:
            for r in nxt:
                if r[12]:                      # is_head
                    heads.setdefault(r[1], r)  # icao -> head row
        for r in rows:
            (leg_id, icao, reg, typ, dep, arr, dep_gnd, arr_gnd, npts,
             off_ds, on_ds, is_tail, is_head, _is_dropout) = r
            if dep is not None and arr is not None:
                complete.append((leg_id, icao, reg, typ, dep, arr,
                                 off_ds, on_ds, dep_gnd, arr_gnd, 0, npts))
                continue
            if not is_tail:
                continue
            h = heads.get(icao)
            if h is None:
                rej_nopair += 1; tails_left += 1; continue
            gap_h = (h[9] - on_ds) / 10.0 / 3600.0
            if not (0 <= gap_h <= a.max_gap_h):
                rej_gap += 1; tails_left += 1; continue
            air_h = (h[10] - off_ds) / 10.0 / 3600.0
            if air_h > a.max_air_h:
                rej_air += 1; tails_left += 1; continue
            # is_tail/is_head guarantee both endpoints, so this now always runs
            # unless the tz table is missing the airport
            if dep in airports and h[5] in airports and air_h > 0:
                d1, o1, _ = airports[dep]; d2, o2, _ = airports[h[5]]
                kmh = gc_km(d1, o1, d2, o2) / air_h
                if not (MIN_KMH <= kmh <= MAX_KMH):
                    rej_speed += 1; tails_left += 1; continue
            spliced.append((f"{leg_id}+{h[0]}", icao, reg, typ, dep, h[5],
                            off_ds, h[10], dep_gnd, h[7], 0,
                            (npts or 0) + (h[8] or 0)))
            matched_heads.add(h[0])
            heads.pop(icao)

    # a head is unmatched only if no boundary ever consumed it
    heads_left = sum(1 for d in dates for r in per_day[d]
                     if r[12] and r[0] not in matched_heads)

    rows = complete + spliced
    if a.keep_halves:
        for d in dates:
            for r in per_day[d]:
                if (r[4] is None) != (r[5] is None):
                    rows.append((r[0], r[1], r[2], r[3], r[4], r[5],
                                 r[9], r[10], r[6], r[7], 0, r[8]))
    if a.dep_date:
        d = dt.date.fromisoformat(a.dep_date)
        lo = int(dt.datetime(d.year, d.month, d.day,
                             tzinfo=dt.timezone.utc).timestamp()) * 10
        hi = lo + 86400 * 10
        before = len(rows)
        rows = [r for r in rows if lo <= r[6] < hi]
        print(f"  --dep-date {a.dep_date}: kept {len(rows)} of {before} "
              f"legs departing that UTC day")
    spliced_ids = {s[0] for s in spliced}
    rows.sort(key=lambda x: (x[4] or "", x[6]))

    t = pa.table({
        "leg_id": [r[0] for r in rows], "icao": [r[1] for r in rows],
        "reg": [r[2] for r in rows], "type": [r[3] for r in rows],
        "dep": [r[4] for r in rows], "arr": [r[5] for r in rows],
        "t_off": pa.array([r[6] for r in rows], pa.int64()),
        "t_on": pa.array([r[7] for r in rows], pa.int64()),
        "dep_gnd": [r[8] for r in rows], "arr_gnd": [r[9] for r in rows],
        "base_ts": pa.array([r[10] for r in rows], pa.int64()),
        "n_points": pa.array([r[11] for r in rows], pa.int32()),
        "spliced": [r[0] in spliced_ids for r in rows],
    })
    pq.write_table(t, a.out, compression="zstd",
                   use_dictionary=["dep", "arr", "type", "reg"])

    print(f"\n{len(rows)} legs -> {a.out} ({os.path.getsize(a.out)/1e6:.2f} MB)")
    print(f"  {len(complete)} already complete")
    print(f"  {len(spliced)} spliced across a midnight boundary")
    print(f"  rejected: {rej_nopair} no pair, {rej_gap} gap > {a.max_gap_h} h, "
          f"{rej_air} airborne > {a.max_air_h} h, {rej_speed} implausible speed")
    print(f"  {dropouts} legs airborne at their last fix but > {a.max_gap_h} h "
          f"from the boundary -- left coverage, never splice candidates")
    print(f"  {tails_left} tails and {heads_left} heads left unmatched"
          f"{' (emitted)' if a.keep_halves else ' (dropped)'}")
    if dates:
        print(f"  NOTE: {dates[-1]} is the newest day, so its own tails cannot "
              f"be spliced until {(dt.date.fromisoformat(dates[-1]) + dt.timedelta(days=1))} "
              f"is processed -- the empirical layer is inherently one day lagged.")


if __name__ == "__main__":
    main()
