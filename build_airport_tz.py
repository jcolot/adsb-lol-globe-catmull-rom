#!/usr/bin/env python3
"""
build_airport_tz.py - resolve an IANA timezone for every airport that carries
scheduled service, and write airport_tz.csv.

Why this is a separate, committed artifact rather than a runtime lookup:

  * airports.csv (OurAirports) has NO timezone column, and overnight
    classification is decided in LOCAL dates at both ends -- so a tz per airport
    is the one input the repo does not already have.
  * a fixed UTC offset is not enough. The classifier's whole job is to decide
    which side of local midnight a departure falls on, so being an hour out over
    a DST boundary flips exactly the cases that are already marginal. We store
    the IANA ZONE NAME and let zoneinfo apply the rules for the date in
    question.
  * lat/lon -> zone needs a polygon database (timezonefinder, ~50 MB). Resolving
    6.4k airports once into a ~200 KB CSV keeps that dependency at generate time
    only, so the pipeline itself stays on numpy/pyarrow/duckdb.
  * longitude/15 is NOT an acceptable substitute: China spans five geographic
    hours in one zone, India is +5:30, and neither is recoverable from a
    meridian. There is deliberately no such fallback here.

Regenerate only when airports.csv is updated or tzdata shifts a zone.

Usage:  ./build_airport_tz.py [--airports CSV] [--out CSV] [--all]
Needs:  pip install timezonefinder   (generate time only)
"""
import argparse, csv, sys

MAJOR_TYPES = ("large_airport", "medium_airport")


def is_major(row):
    """Scheduled service OR a large/medium field.

    The union matters: 896 airports typed `small_airport` do carry scheduled
    service, and filtering on `type` alone silently drops every one of them.
    """
    return row.get("scheduled_service") == "yes" or row.get("type") in MAJOR_TYPES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--airports", default="airports.csv")
    ap.add_argument("--out", default="airport_tz.csv")
    ap.add_argument("--all", action="store_true",
                    help="every airport, not just the ones with scheduled "
                         "service (85k rows, ~2.5 MB, minutes to resolve)")
    a = ap.parse_args()

    try:
        from timezonefinder import TimezoneFinder
    except ImportError:
        sys.exit("need timezonefinder at generate time: pip install timezonefinder")
    tf = TimezoneFinder()

    rows, unresolved = [], []
    for r in csv.DictReader(open(a.airports, newline="")):
        if not (a.all or is_major(r)):
            continue
        ident = r["ident"]
        try:
            lat = float(r["latitude_deg"]); lon = float(r["longitude_deg"])
        except (KeyError, ValueError):
            continue
        if not ident:
            continue
        # timezone_at returns None only for a point the polygon set does not
        # cover (mid-ocean platforms, a few disputed strips); timezone_at_land
        # is stricter still, so fall FORWARD to the looser answer, not back to a
        # meridian guess, and report whatever is left over.
        tz = tf.timezone_at(lat=lat, lng=lon)
        if tz is None:
            unresolved.append(ident)
            continue
        rows.append((ident, r.get("type", ""), r.get("iso_country", ""),
                     f"{lat:.6f}", f"{lon:.6f}", tz))

    rows.sort()
    with open(a.out, "w", newline="") as f:
        # csv's default dialect terminates lines with CRLF, which git then
        # normalises on the next touch -- churning a committed file. Pin LF.
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["ident", "type", "iso_country", "lat", "lon", "tz"])
        w.writerows(rows)

    zones = len({r[5] for r in rows})
    print(f"{len(rows)} airports -> {a.out}  ({zones} distinct zones)")
    if unresolved:
        print(f"  {len(unresolved)} unresolved (no zone polygon covers them): "
              f"{', '.join(unresolved[:10])}"
              f"{' ...' if len(unresolved) > 10 else ''}")


if __name__ == "__main__":
    main()
