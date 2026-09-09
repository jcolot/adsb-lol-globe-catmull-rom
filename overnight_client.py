#!/usr/bin/env python3
"""
overnight_client.py - reference EXTERNAL client for the overnight artifacts.

Deliberately depends on NOTHING in this repo. It reads only the three published
files, so it exercises the same contract a browser does and would keep working
if the rest of the pipeline vanished:

  airports_utc.parquet   from the partition of the ARRIVAL date
  overnight_meta.json    from the same partition (tier-2 coefficients)
  overnight.parquet      the tier-1 lookup; optional, and any recent one will
                         do -- it is a per-route statistic, not a per-date
                         record, which is why it is not keyed by date

No zoneinfo, no tzdata, no timezonefinder: the UTC offsets in
airports_utc.parquet are already resolved per date, which is the whole point of
shipping them that way. The only arithmetic is integer minutes.

Mirrors the JS decoder in the README line for line, including the two-pass
departure offset and the 3-day window guard.

Usage:
  ./overnight_client.py --dir legs/date=2026-09-08 \\
      --dep KJFK --arr EGLL --arr-local '2026-09-08 09:20'

  ./overnight_client.py --base-url https://pub-XXXX.r2.dev/legs \\
      --date 2026-09-08 --dep KJFK --arr EGLL --arr-local '2026-09-08 09:20'
"""
import argparse, datetime as dt, json, math, os, sys, tempfile
import urllib.request

import pyarrow.parquet as pq

DAY = 1440                      # minutes
R_KM = 6371.0088
FILES = ("airports_utc.parquet", "overnight_meta.json", "overnight.parquet")


class OvernightClient:
    def __init__(self, airports, meta, table=None):
        self.apt = airports     # ident -> (lat_e5, lon_e5, off_d0, off_dm1, off_dm2)
        self.meta = meta
        self.table = table or {}
        self.d0 = dt.date.fromisoformat(meta["date"])

    # -- loading -----------------------------------------------------------
    @staticmethod
    def _read(dirpath):
        ap = pq.read_table(os.path.join(dirpath, "airports_utc.parquet")).to_pydict()
        airports = {
            ident: (lat, lon, d0, d1, d2)
            for ident, lat, lon, d0, d1, d2 in zip(
                ap["ident"], ap["lat"], ap["lon"],
                ap["off_d0"], ap["off_dm1"], ap["off_dm2"])
        }
        with open(os.path.join(dirpath, "overnight_meta.json")) as f:
            meta = json.load(f)
        table = {}
        tp = os.path.join(dirpath, "overnight.parquet")
        if os.path.exists(tp):
            t = pq.read_table(tp).to_pydict()
            for dep, arr, hh, off, agree, n in zip(
                    t["dep"], t["arr"], t["arr_local_hour"], t["day_offset"],
                    t["agreement"], t["n"]):
                table[(dep, arr, hh)] = (off, agree, n)
        return airports, meta, table

    @classmethod
    def from_dir(cls, dirpath):
        return cls(*cls._read(dirpath))

    @classmethod
    def from_url(cls, base, date, cache=None):
        """Fetch the partition's files over HTTP, exactly as a browser would."""
        d = cache or tempfile.mkdtemp(prefix="overnight-")
        for name in FILES:
            url = f"{base.rstrip('/')}/date={date}/{name}"
            try:
                with urllib.request.urlopen(url, timeout=30) as r, \
                        open(os.path.join(d, name), "wb") as f:
                    f.write(r.read())
            except Exception as e:
                # overnight.parquet is absent on the newest day by design, and
                # its absence must degrade to tier 2 rather than fail the load
                if name == "overnight.parquet":
                    print(f"note: no {name} for {date} ({e}); tier 2 only",
                          file=sys.stderr)
                    continue
                raise
        return cls.from_dir(d)

    # -- the contract ------------------------------------------------------
    def off_min(self, ident, target: dt.date):
        """UTC offset in minutes for `ident` on `target`.

        The partition carries only d0, d0-1 and d0-2. That is exactly enough
        when the partition IS the arrival date: a departure is at most two days
        earlier (a westbound date-line crossing is +2). Asking outside the
        window means the wrong partition was fetched, so it raises rather than
        silently returning a neighbouring day's offset.
        """
        rec = self.apt.get(ident)
        if rec is None:
            raise KeyError(f"{ident} not in airports_utc.parquet")
        k = (self.d0 - target).days
        if not 0 <= k <= 2:
            raise ValueError(
                f"{target} is outside the 3-day window of the {self.d0} "
                f"partition (k={k}) -- fetch date={target} instead")
        return rec[2 + k]

    def gc_km(self, dep, arr):
        a, b = self.apt[dep], self.apt[arr]
        p1, p2 = math.radians(a[0] / 1e5), math.radians(b[0] / 1e5)
        dp = p2 - p1
        dl = math.radians((b[1] - a[1]) / 1e5)
        h = (math.sin(dp / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
        return 2 * R_KM * math.asin(math.sqrt(h))

    def block_min(self, dep, arr):
        return (self.meta["block_fixed_min"]
                + 60.0 * self.gc_km(dep, arr) / self.meta["block_kmh"])

    def lookup(self, dep, arr, hour):
        """Tier 1: the hour bucket, then the route-level rollup at hour -1.

        The fallback is not cosmetic. The table is built from ACTUAL times while
        a caller queries a SCHEDULED arrival, and delay moves flights between
        buckets -- so a thin bucket beside a busy one is a sampling artefact,
        not a different service.
        """
        for key in ((dep, arr, hour), (dep, arr, -1)):
            hit = self.table.get(key)
            if hit and hit[2] >= self.meta.get("min_samples", 3):
                off, agree, n = hit
                where = "hour bucket" if key[2] == hour else "route rollup"
                return off, f"observed {where}, n={n}, agreement {agree:.2f}"
        return None, None

    def classify(self, dep, arr, arr_local: dt.datetime):
        """-> dict(day_offset, overnight, dep_local, margin_min, source)."""
        arr_date = arr_local.date()
        arr_local_min = int(arr_local.timestamp() // 60) if arr_local.tzinfo \
            else _naive_min(arr_local)

        observed, why = self.lookup(dep, arr, arr_local.hour)
        block = self.block_min(dep, arr)

        arr_utc_min = arr_local_min - self.off_min(arr, arr_date)
        dep_utc_min = arr_utc_min - block

        # two passes: the departure's local date is not known until it is
        # computed, and its offset depends on that date. One correction is
        # enough -- a DST step is at most an hour, far less than a day.
        off = self.off_min(dep, arr_date)
        dep_local_min = dep_utc_min + off
        dep_date = _date_of(dep_local_min)
        if dep_date != arr_date:
            try:
                off2 = self.off_min(dep, dep_date)
                if off2 != off:
                    dep_local_min = dep_utc_min + off2
            except ValueError:
                pass            # outside the window; keep the first-pass offset

        computed = _days(arr_local_min) - _days(dep_local_min)
        day_offset = observed if observed is not None else computed
        into = int(dep_local_min) % DAY
        return dict(
            day_offset=day_offset, overnight=day_offset >= 1,
            dep_local=_fmt(dep_local_min), block_min=block,
            dist_km=self.gc_km(dep, arr),
            margin_min=min(into, DAY - into),
            computed=computed,
            source=why or f"distance model ({self.gc_km(dep, arr):.0f} km)")


# -- minute arithmetic, so nothing depends on a tz database -----------------
_EPOCH = dt.datetime(1970, 1, 1)


def _naive_min(d):
    return int((d - _EPOCH).total_seconds() // 60)


def _days(m):
    return math.floor(m / DAY)


def _date_of(m):
    return (_EPOCH + dt.timedelta(days=_days(m))).date()


def _fmt(m):
    return (_EPOCH + dt.timedelta(minutes=int(m))).strftime("%Y-%m-%d %H:%M")


def main():
    p = argparse.ArgumentParser()
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", help="a local date=YYYY-MM-DD partition")
    src.add_argument("--base-url", help="e.g. https://pub-XXXX.r2.dev/legs")
    p.add_argument("--date", help="partition date, required with --base-url")
    p.add_argument("--dep", required=True)
    p.add_argument("--arr", required=True)
    p.add_argument("--arr-local", required=True,
                   help="scheduled arrival, local at --arr: 'YYYY-MM-DD HH:MM'")
    a = p.parse_args()

    if a.base_url:
        if not a.date:
            sys.exit("--base-url needs --date")
        c = OvernightClient.from_url(a.base_url, a.date)
    else:
        c = OvernightClient.from_dir(a.dir)

    arr_local = dt.datetime.strptime(a.arr_local, "%Y-%m-%d %H:%M")
    r = c.classify(a.dep, a.arr, arr_local)
    print(f"{a.dep} -> {a.arr}, arriving {arr_local:%Y-%m-%d %H:%M} local")
    print(f"  departed  {r['dep_local']} local (estimated)")
    print(f"  offset    {r['day_offset']:+d} day  -> "
          f"{'OVERNIGHT' if r['overnight'] else 'SAME DAY'}")
    print(f"  source    {r['source']}")
    if r["day_offset"] != r["computed"]:
        print(f"  note      the distance model alone would have said "
              f"{r['computed']:+d}")
    print(f"  block     {r['block_min']:.0f} min over {r['dist_km']:.0f} km")
    print(f"  margin    {r['margin_min']:.0f} min from local midnight"
          f"{'   <-- MARGINAL' if r['margin_min'] < 60 else ''}")


if __name__ == "__main__":
    main()
