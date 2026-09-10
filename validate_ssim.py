#!/usr/bin/env python3
"""
validate_ssim.py - check overnight.py's day_offset against SSIM ground truth.

This is the validation BTS cannot do. BTS publishes ACTUALS, so it validates the
mechanism -- wheels-off, wheels-on, taxi, block time -- but says nothing about
the day offset, which it can only reach implicitly for domestic +1. SSIM carries
the offset explicitly, as the Date Variation field, which makes it the only
ground truth for the question this repo is actually asking.

SSIM Chapter 7 is a 200-byte fixed-width record set. The fields used here, all
verified against a real AF/KL/TO/HV file rather than taken from a spec:

     1      record type ('3' = flight leg)
     3-5    airline
    15-21   period of operation, from (DDMMMYY) -- the flight's REFERENCE date
    37-39   departure station (IATA)
    44-47   scheduled time of aircraft departure (local hhmm)
    48-52   departure UTC/local time variation
    55-57   arrival station (IATA)
    58-61   scheduled time of aircraft arrival (local hhmm)
    66-70   arrival UTC/local time variation
   193-194  DATE VARIATION: one char for departure, one for arrival
   195-200  record serial number

Date Variation alphabet, as observed across 397,992 real leg records:
`0` `1` `2` and `A`, where **A is minus one day**. Values seen were 00, 01, 11,
AA, A0, 12, 22.

The offset is `arr_var - dep_var`, NOT the arrival variation alone. Both are
measured from the ITINERARY's first-leg departure, so a second leg legitimately
reads 11 or 22 while being a same-day flight -- e.g. LAX 01:50 -> CDG 12:35 is
`22`, offset 0. Reading only the arrival char would call that +2.

What this does NOT validate: SSIM times are SCHEDULED, and a scheduled block
carries padding an ADS-B-measured block does not. That padding biases the
comparison toward under-calling +1, which is visible in the confusion matrix.
Do not "fix" it by tuning BLOCK_FIXED_MIN against this file -- that is fitting
to the validation set, and it is worth only a few tenths of a point anyway.

Usage:  ./validate_ssim.py --ssim path/to/file.ssim [--legs spliced.parquet]
"""
import argparse, collections, csv, datetime as dt, statistics, sys

from overnight import (load_airports, classify, load_legs, drop_merged,
                       pair_blocks, gc_km, BLOCK_FIXED_MIN, BLOCK_KMH, TAXI_MIN)

DV = {"0": 0, "1": 1, "2": 2, "A": -1, "B": -2}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
BUCKETS = [(30, "< 30m"), (60, "30-60m"), (120, "60-120m"),
           (240, "120-240m"), (10 ** 9, ">= 240m")]
MAJOR = ("large_airport", "medium_airport")
MAX_ELAPSED_H = 20.0     # longest scheduled nonstop is ~19 h
MIN_KMH, MAX_KMH = 100.0, 1100.0   # physical bounds, not a fitted model


def _mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[2:])


def _utcvar(s):
    """'-0400' / '+0200' -> minutes east of UTC."""
    return (-1 if s[0] == "-" else 1) * (int(s[1:3]) * 60 + int(s[3:5]))


def elapsed_min(std, sta, dvar, avar, dv):
    """Scheduled elapsed minutes implied by the record's OWN fields.

    Uses only SSIM's times, UTC variations and date variations, so it is
    independent of anything in this repo -- which is what lets it gate the
    ground truth without circularity.
    """
    dep = DV[dv[0]] * 1440 + _mins(std) - _utcvar(dvar)
    arr = DV[dv[1]] * 1440 + _mins(sta) - _utcvar(avar)
    return arr - dep


def iata_to_icao(path, known):
    """IATA -> ICAO, restricted to majors we have a timezone for."""
    out = {}
    for r in csv.DictReader(open(path, newline="")):
        code = (r.get("iata_code") or "").strip()
        if code and r["ident"] in known and r.get("type") in MAJOR:
            out.setdefault(code, r["ident"])
    return out


def parse(path, i2icao, airports):
    """Unique (dep, arr, STD, STA, DateVariation) schedule patterns.

    Deduplicated because one pattern repeats across every date range it
    operates, and counting it thousands of times would weight the score by how
    long a route runs rather than by how many distinct schedules exist.
    """
    seen, rows, skip = set(), [], collections.Counter()
    types, dvs = collections.Counter(), collections.Counter()
    for line in open(path, encoding="latin-1"):
        types[line[:1]] += 1
        if not line.startswith("3"):
            continue
        dep_i, arr_i = line[36:39], line[54:57]
        std, sta, dv = line[43:47], line[57:61], line[192:194]
        dvar, avar = line[47:52], line[65:70]
        dvs[dv] += 1
        key = (dep_i, arr_i, std, sta, dv)
        if key in seen:
            continue
        seen.add(key)
        if dv[0] not in DV or dv[1] not in DV:
            skip["unknown date variation"] += 1; continue
        # Gate the ground truth on its own internal consistency. Measured on a
        # real AF/KL/TO/HV file, 335 of 67,593 patterns (0.50%) imply a
        # NEGATIVE or absurd elapsed time -- and 269 of those 335 carry 4-digit
        # codeshare flight numbers, i.e. marketing records whose date variation
        # the publisher left at 00. They cluster on exactly the trans-Pacific
        # westbound date-line crossings where the offset is hardest, so scoring
        # against them would penalise correct answers.
        try:
            el = elapsed_min(std, sta, dvar, avar, dv)
        except (ValueError, IndexError):
            skip["unparseable times"] += 1; continue
        if not 0 < el <= MAX_ELAPSED_H * 60:
            skip["ssim elapsed <= 0 or > 20 h"] += 1; continue
        dep, arr = i2icao.get(dep_i), i2icao.get(arr_i)
        if not dep or not arr:
            skip["station not a major with a tz"] += 1; continue
        if dep == arr:
            skip["dep == arr"] += 1; continue
        d = line[14:21]
        try:
            ref = dt.date(2000 + int(d[5:7]), MONTHS[d[2:5]], int(d[0:2]))
        except (KeyError, ValueError):
            skip["bad period date"] += 1; continue
        # ...and on physics. A record can imply a positive elapsed time that is
        # still impossible for the distance: LAS-AMS at 00:05 -> 10:15 with
        # DV 00 implies 70 minutes for 8,600 km, i.e. Mach 6. The bounds here
        # are physical limits on an airliner, NOT the fitted block model, so
        # this stays independent of what is being validated.
        km = gc_km(airports[dep][0], airports[dep][1],
                   airports[arr][0], airports[arr][1])
        kmh = km / (el / 60.0)
        if km > 100 and not MIN_KMH <= kmh <= MAX_KMH:
            skip["ssim implies impossible speed"] += 1; continue

        arr_date = ref + dt.timedelta(days=DV[dv[1]])
        try:
            arr_local = dt.datetime(arr_date.year, arr_date.month, arr_date.day,
                                    int(sta[:2]), int(sta[2:]))
        except ValueError:
            skip["bad STA"] += 1; continue
        rows.append((dep, arr, arr_local, DV[dv[1]] - DV[dv[0]],
                     dep_i, arr_i, std, sta, dv, line[2:9].strip()))
    return rows, skip, types, dvs


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ssim", required=True)
    p.add_argument("--airports-tz", default="airport_tz.csv")
    p.add_argument("--airports", default="airports.csv")
    p.add_argument("--legs", help="spliced legs, to use observed block medians "
                                  "for pairs that have them (tier 1)")
    p.add_argument("--errors", type=int, default=10, help="sample errors to show")
    a = p.parse_args()

    ap = load_airports(a.airports_tz)
    rows, skip, types, dvs = parse(a.ssim, iata_to_icao(a.airports, ap), ap)
    print(f"record types: {dict(sorted(types.items()))}")
    print(f"date variation values: {dict(dvs.most_common())}")
    print(f"unique schedule patterns: {len(rows):,}")
    if skip:
        print(f"skipped: {dict(skip)}")

    blocks = None
    if a.legs:
        legs = drop_merged(load_legs(a.legs), ap)
        blocks = pair_blocks(legs)
        print(f"observed block medians available for {len(blocks):,} pairs")

    ok, errs = 0, []
    conf = collections.Counter()
    bk = collections.defaultdict(lambda: [0, 0])
    for dep, arr, arr_local, truth, di, ai, std, sta, dv, flt in rows:
        try:
            r = classify(dep, arr, arr_local, ap, blocks)
        except (KeyError, ValueError):
            skip["classify failed"] += 1; continue
        good = r["offset"] == truth
        ok += good
        conf[(truth, r["offset"])] += 1
        for lim, name in BUCKETS:
            if r["margin_min"] < lim:
                bk[name][0] += good; bk[name][1] += 1
                break
        if not good:
            errs.append((flt, di, ai, std, sta, dv, truth, r["offset"],
                         r["margin_min"], r["block_min"]))
    n = ok + len(errs)
    if not n:
        sys.exit("nothing comparable")
    print(f"\nblock model: {BLOCK_FIXED_MIN} + 60*d/{BLOCK_KMH}, taxi {TAXI_MIN}")
    print(f"compared {n:,}:  {ok:,} correct ({100.0*ok/n:.2f}%), {len(errs):,} wrong")

    print("\nconfusion (SSIM truth -> predicted):")
    for (t, pr), c in sorted(conf.items()):
        print(f"  {t:+d} -> {pr:+d}: {c:7,}{'   <-- error' if t != pr else ''}")

    # The point of the whole design: the margin, not the block time, says
    # whether an answer is trustworthy. This table is that claim, measured.
    print("\naccuracy by margin from local midnight:")
    print(f"  {'bucket':>11}{'n':>9}{'accuracy':>10}{'share':>8}")
    for _, name in BUCKETS:
        g, c = bk[name]
        if c:
            print(f"  {name:>11}{c:>9,}{100.0*g/c:>9.2f}%{100.0*c/n:>7.1f}%")

    if errs and a.errors:
        em = [e[8] for e in errs]
        print(f"\nerror margins: median {statistics.median(em):.0f} min, "
              f"{100.0*sum(1 for x in em if x < 60)/len(em):.0f}% under 60 min")
        print(f"\nsample errors:")
        print(f"  {'flight':>9}{'route':>9}{'STD':>6}{'STA':>6}{'DV':>4}"
              f"{'truth':>7}{'pred':>6}{'margin':>8}{'block':>7}")
        for e in sorted(errs, key=lambda x: -x[8])[:a.errors]:
            print(f"  {e[0]:>9}{e[1]+'-'+e[2]:>9}{e[3]:>6}{e[4]:>6}{e[5]:>4}"
                  f"{e[6]:>+7d}{e[7]:>+6d}{e[8]:>7.0f}m{e[9]:>6.0f}m")


if __name__ == "__main__":
    main()
