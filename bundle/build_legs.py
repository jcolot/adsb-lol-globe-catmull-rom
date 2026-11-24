#!/usr/bin/env python3
"""
build_legs.py - segment each aircraft's day into flight LEGS (with departure and
arrival airports) and write per-airport Parquet files a browser (hyparquet) can
fetch one at a time.

Pipeline:
  1. read the decimated points (prod-0-parquet-single/traces.parquet), grouped by
     icao and ordered by time;
  2. find airborne runs that actually go somewhere (>2 km horizontal), cutting a
     run wherever a landing fits in a gap between two airborne nodes (see
     GAP_SLACK_MIN), and split the day at the temporal midpoint between
     consecutive flights -> one leg each, taxi-out + airborne + taxi-in;
  3. departure = nearest airport to the leg's first end-window anchor, arrival =
     nearest to its last (airports.csv, <=10 km, closed fields excluded). The anchor is a ground fix when
     the window has one, else the window's lowest fix, so airports with thin
     surface coverage still resolve -- except an airborne end that the 00:00Z
     archive boundary cut (BOUNDARY_CUT_S), which is left NULL for
     overnight/splice_legs.py to rejoin;
  4. stamp the callsign (callsigns.parquet) onto each leg: readsb reports `flight`
     only when it CHANGES, so prefer the first change inside the leg (crews set it
     at the stand) and otherwise carry forward the last one set before landing;
  5. write points_legs.parquet (one row/point, with dep/arr/leg_id),
     flights.parquet (one row/leg = the index, sorted by dep),
     airports/airport=XXXX/*.parquet (each leg written under BOTH its dep and
     arr airport, carrying that leg's row from the index), and legs_meta.json
     (what schema this day was written with).

flights.parquet times, in deciseconds past that leg's own base_ts:
  t_start/t_end  the LEG envelope -- includes half of each adjacent turnaround
                 plus all ramp time on the day's first and last leg. NOT a
                 flight time; do not subtract them to get one.
  t_off/t_on     the airborne run: wheels-off / wheels-on.
  dep_gnd/arr_gnd  whether that end had a real surface message. False means the
                 anchor was a climb-out/approach fix, so both the airport and
                 the t_off/t_on at that end are approximations.
  base_ts        absolute UTC seconds = base_ts + t/10. Needed because t is
                 per-aircraft relative and NOT comparable between aircraft.

leg_id is "<date>_<icao>_<k>" and is GLOBALLY unique. It used to be
"<icao>_<k>", with k restarting in every daily archive, so the same string
named a different flight in each partition -- measured between 2026-09-08 and
2026-09-09, 78,148 ids collided. Anything holding more than one day (a client
spanning a local midnight, splice_legs matching a tail to a head, a cache keyed
by id) had to carry the date alongside it and was silently wrong if it did not.
The date is now IN the id, so the id alone is the key.

The per-airport files carry base_ts, t_off, t_on, dep_gnd and arr_gnd from the
leg index, so ONE request answers "which flights were at this airport between
these two instants, and where did they fly?" -- absolute UTC is base_ts + t/10,
and the wheels times are what a day-boundary filter has to cut on. Without them
that question needed the 3.6 MB day-wide index as well, only to read five
per-leg values. Denormalising them costs 1.41% (measured on a real EBBR
partition: 715,536 -> 725,642 bytes over 69,308 rows), because each is constant
across a leg's run of rows and RLE says so in a few bytes.

legs_meta.json records flights_schema (bump it whenever a column is ADDED or
its meaning changes) so a client reading a retained older partition can tell
what it is looking at instead of feature-detecting column by column. It also
records whether every aircraft shared one base_ts and what it was; today
fit_spline gives the whole day a single epoch, but nothing here depends on
that, and a client should read base_ts rather than assume it.

Usage:  ./build_legs.py [--limit-aircraft N] [--traces PATH] [--meta PATH]
                        [--airports CSV] [--out-dir DIR]
"""
import argparse, datetime as dt, importlib.util, json, math, os, csv as csvmod
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

# Bumped whenever flights.parquet / the per-airport files gain a column or one
# changes meaning, and written into legs_meta.json so a client can tell which
# contract a retained partition was built under.
#   1  leg_id, icao, reg, type, dep, arr, t_start, t_end, n_points
#   2  + t_off, t_on, dep_gnd, arr_gnd, base_ts  (flights.parquet only)
#   3  + those five in the per-airport files too, + legs_meta.json
#   4  leg_id is "<date>_<icao>_<k>" -- globally unique, not just within a day
#   5  + flight, the callsign, on both (NULLable: readsb reports it only on
#      change, so absence is not evidence of a callsign-less flight)
#   6  dep/arr may be FILLED where the trace never saw that end (routes.py):
#      + dep_src, arr_src on both ('ground' | 'air' observed, 'chain' | 'route'
#      filled, NULL unknown) and + route on flights.parquet. A filled end has
#      no timing of its own -- measure flight times on observed ends only.
#      Legs are also cut at unseen turnarounds (GAP_SLACK_MIN).
FLIGHTS_SCHEMA = 6

# What each per-airport row carries beyond the point itself. All five are
# constant across a leg's run of rows, so RLE reduces them to a few bytes per
# leg (+1.41% on a real EBBR partition) -- and they are exactly what turns the
# file from "the geometry" into "the answer", with no second fetch of the
# day-wide index to find out when the wheels came up.
LEG_COLS = "p.*, l.base_ts, l.t_off, l.t_on, l.dep_gnd, l.arr_gnd"
# dep_src / arr_src ride along in points_legs (p.*) already, so the per-airport
# files carry them without a join; `route` stays in flights.parquet only.

FLIGHT_MIN_KM = 2.0     # an airborne run must span this to count as a flight
# An airborne run is also CUT where a landing fits in a gap between two
# consecutive airborne nodes: the gap's time minus the time to fly its distance
# at GAP_KMH is at least GAP_SLACK_MIN. Without this the run is only broken by
# an on_ground fix, and a field with no surface coverage never produces one, so
# the flight in and the flight out fuse into one leg. Measured on 2026-11-15:
# SunExpress EBBR->LTAC->EBBR came out as one 7-8 h leg from EBBR to a German
# strip, and LTAC had no legs at all.
#
# Both values come from the airborne node gaps of 2026-11-15 (10.3M nodes,
# 92k legs). Gaps up to 20 min are overwhelmingly at cruise speed (normal
# spline spacing on a straight track); from 40 min they are overwhelmingly
# under 150 km/h (the aircraft went nowhere). Slack at 700 km/h falls off a
# cliff in altitude past ~20 min: median lower end 22,400 ft at 0-10 min,
# 4,800 at 20-30, ~1,700-2,750 from 30 min on -- lost on the approach,
# reacquired on the climb. 30 min keeps holding and approach shuffles in poor
# coverage intact and still catches a 25-minute low-cost turn, since the gap
# then also spans the last minutes of descent and first of climb. 700 km/h is
# deliberately SLOW for a jet: a lower speed charges more flying time to the
# gap and so splits less, which keeps a long tailwind crossing of a coverage
# hole (slack < 0) whole. The rule splits 26,986 of that day's 92,330 legs.
GAP_SLACK_MIN = 30.0
GAP_KMH = 700.0
NEAR_KM = 10.0          # max distance from a fix to call it "at" an airport
# An AIRBORNE anchor (no surface fix at that end) may name a field only if it is
# at most this far above the field's elevation. The anchor is the lowest fix in
# the end-window, and nothing used to check that it was low: on 2026-11-15,
# 3,535 ends were resolved from fixes at 15,000 ft or more -- an overflight of
# whatever strip lay within NEAR_KM. The gap split above makes many more ends
# that stop at cruise, which took that to 5,913. Within 10 km of a runway an
# arrival is ~3,000 ft up and even a light jet's climb-out is ~6,000, so 8,000
# keeps every real approach and departure. Relative to elevation, not MSL, or
# La Paz (13,300 ft) and Lhasa (11,700) would lose every airborne anchor.
ANCHOR_MAX_AGL_FT = 8000.0
# The archive is cut at 00:00Z, so an aircraft still flying then has a day-D
# leg that ends airborne on its last fix and a day-D+1 leg that starts airborne
# on its first. Neither end is a takeoff or a landing, but the airborne-anchor
# fallback resolves it anyway whenever the cut lands within NEAR_KM and
# ANCHOR_MAX_AGL_FT of a field: a departure still climbing out at 23:59:59 got
# an ARRIVAL at the airport it had just left. On 2026-11-15, 2,119 day-final
# legs ended airborne on an air-resolved arr; 635 were self-loops (KMIA->KMIA,
# 0.4 min airborne, ending 23:59:59), and the mirror on 2026-11-15's first legs
# was 1,984 and 594. splice_legs.py only recognises a cut end by its NULL, so
# these were never rejoined either. The window is the cliff in that data: 1,573
# of the 2,119 last fixes sit within 30 s of midnight, the rate is back to the
# ~40/min background of genuine coverage dropouts by 2 min. A real landing in
# that window with no surface fix loses its observed end too; the chain/route
# fill and the splice can still supply it, a fabricated one they cannot fix.
BOUNDARY_CUT_S = 120.0
# OurAirports keeps closed fields in the file (13k of 86k rows), and nearest-
# match happily picked them: on 2026-08-16, 7,716 of 117,884 legs ended at one,
# often a closed heliport or seaplane base inside a live airport's 10 km. A
# balloonport is not somewhere a transponder-equipped flight departs from.
SKIP_AIRPORT_TYPES = {"closed", "balloonport"}
# Fields that share a site with a scheduled airport ("shadow" fields): Melsbroek
# Air Base (EBMB) sits on Brussels Airport's (EBBR) own runways 1.4 km from its
# reference point, and heliports ring KIAH, KLAX, KSFO, WMKK, RPLL within 1-3
# km. Nearest-reference-point matching handed them airliner ends: on
# 2026-11-15, 9,078 ends were at a field within 5 km of a scheduled large or
# medium airport, 3,915 of them by airliner types, and they caused 1,561 of
# the 6,764 route contradictions in routes.py. Two rules, both needing to know
# WHAT is flying (the aircraft's species from VRS model-type, and whether the
# callsign is an airline's):
#   - a landplane (or gyroplane) cannot end at a heliport or a seaplane base,
#     an amphibian not at a heliport. Helicopters and tiltrotors can use anything.
#   - an AIRLINE callsign -- its VRS airline row has an IATA code, which no
#     air force has (BAF, CNV, ACW are in the same table without one) -- takes
#     the scheduled large/medium airport over a non-scheduled field when the
#     two are co-sited: the scheduled one within SITE_KM of the nearest
#     candidate. So BEL lands at EBBR, while a Belgian Air Force C-130 at the
#     same spot still resolves to EBMB.
#   - (AIR_ANCHOR) an AIRLINE end with a VRS route, anchored IN THE AIR -- the lowest approach or
#     climb-out fix, no surface message -- resolves to scheduled airports
#     only (still within NEAR_KM), and to nothing when none is. The lowest
#     fix of an approach lost in a coverage hole can be anywhere on the last
#     30 km, and a strip under it won: on 2026-11-15 the schedule of EBBR's
#     own flights caught US-12876 for Miami, IT-0219 for Florence, TR-0034 for
#     Eskisehir and ES-0151 for Vitoria, 6-34 km from the real field. Left
#     unknown, the end goes to routes.py, which fills it from the callsign --
#     so the rule applies only when the callsign HAS a VRS route. Without one
#     (NetJets EJA into Naples or Miami Executive, Kalitta into Willow Run)
#     the non-scheduled field is likely the real one and nothing would replace
#     it. A GROUND anchor keeps the rules above: an airliner on a strip's
#     surface was there.
SITE_KM = 5.0
NO_FIELD_FOR = {"L": {"heliport", "seaplane_base"},     # landplane
                "G": {"heliport", "seaplane_base"},     # gyroplane
                "A": {"heliport"}}                      # amphibian
SCHEDULED_TYPES = {"large_airport", "medium_airport"}
ENDPOINT_FRAC = 0.25    # search this much of the leg at each end for an anchor
ENDPOINT_MIN_PTS = 5    # ...but always look at at least this many points


def resolve_date(given, meta):
    """The UTC date this run covers, which goes into every leg_id.

    Taken from --date when given, else derived from base_ts -- fit_spline puts
    the whole day on one epoch, so the aircraft table already knows the date.
    Deriving it is refused when the epochs disagree, because then there is no
    single date and a leg_id built from the wrong one is worse than a stopped
    pipeline: it would collide with a real day's ids.
    """
    if given:
        return dt.date.fromisoformat(given).isoformat()     # also validates
    days = {dt.datetime.fromtimestamp(b, dt.timezone.utc).date().isoformat()
            for _, _, b in meta.values() if b is not None}
    if len(days) == 1:
        return days.pop()
    raise SystemExit(
        f"cannot derive the data date from base_ts ({len(days)} distinct "
        f"date(s) in the aircraft table) -- pass --date YYYY-MM-DD")


def build_airport_index(path):
    grid = {}
    with open(path, newline="") as f:
        for row in csvmod.DictReader(f):
            try:
                lat = float(row["latitude_deg"]); lon = float(row["longitude_deg"])
                ident = row["ident"]
            except (KeyError, ValueError):
                continue
            if not ident or row.get("type") in SKIP_AIRPORT_TYPES:
                continue
            try:
                elev = float(row.get("elevation_ft") or 0.0)
            except ValueError:
                elev = 0.0              # unknown: sea level, i.e. the strict side
            sched = (row.get("scheduled_service") == "yes"
                     and row.get("type") in SCHEDULED_TYPES)
            grid.setdefault((round(lat), round(lon)), []).append(
                (lat, lon, ident, elev, row.get("type"), sched))
    return grid


def make_resolver(grid, max_km=NEAR_KM, max_agl_ft=ANCHOR_MAX_AGL_FT):
    cache = {}
    def resolve(lat, lon, alt=None, species=None, airline=False, sched_only=False):
        """Nearest airport within max_km. `alt` (ft) is given for an AIRBORNE
        anchor only, and then a field the fix is more than max_agl_ft above is
        not a candidate -- see ANCHOR_MAX_AGL_FT. Ground anchors pass None.
        `species` (VRS SpeciesCode) and `airline` apply the shadow-field rules
        at SITE_KM, and with `alt` given `sched_only` keeps only scheduled
        airports (AIR_ANCHOR); all default to plain nearest-match."""
        # keyed on the EXACT fix. It used to be rounded to 0.01 deg (~1 km), so
        # the first fix to land in a square decided the airport for every later
        # one there, so a leg's airport depended on processing order and could
        # be the one nearest some OTHER fix: resolving 2026-08-16 exactly
        # changes dep or arr on 4,708 of its 117,884 legs.
        # Ground fixes repeat exactly (parked, stationary-snapped), so the
        # exact key keeps most of the hits.
        ck = (lat, lon, alt, species, airline, sched_only)
        if ck in cache:
            return cache[ck]
        excl = NO_FIELD_FOR.get(species, ())
        cand = []
        rl, ro = round(lat), round(lon)
        for dla in (-1, 0, 1):
            for dlo in (-1, 0, 1):
                for (alat, alon, ident, elev, atype, sched) in grid.get((rl + dla, ro + dlo), ()):
                    if alt is not None and alt - elev > max_agl_ft:
                        continue
                    if atype in excl:
                        continue
                    d = _km(lat, lon, alat, alon)
                    if d < max_km:
                        cand.append((d, ident, sched, alat, alon))
        if sched_only and alt is not None:
            cand = [c for c in cand if c[2]]    # see AIR_ANCHOR rule above
        best = None
        if cand:
            cand.sort()
            best = cand[0][1]
            if airline and not cand[0][2]:
                n_la, n_lo = cand[0][3], cand[0][4]
                site = [c for c in cand if c[2] and _km(n_la, n_lo, c[3], c[4]) <= SITE_KM]
                if site:
                    best = site[0][1]           # nearest co-sited scheduled airport
        cache[ck] = best
        return best
    return resolve


def _km(la1, lo1, la2, lo2):
    return math.hypot((la2 - la1) * 111.32,
                      (lo2 - lo1) * 111.32 * math.cos(math.radians(la1)))


TRACK_KM = 30.0         # an end's track is measured over at least this far...
TRACK_DS = 6000         # ...or this long (deciseconds = 10 min), whichever first


def _bearing(la1, lo1, la2, lo2):
    """Initial great-circle bearing, degrees clockwise from north."""
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    return math.degrees(math.atan2(
        math.sin(dl) * math.cos(p2),
        math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360


def end_track(ts, la, lo, al, s, e, at_end):
    """(track, alt, dalt) of the aircraft at one end of its airborne run s..e:
    the direction of TRAVEL (deg) over the last (at_end) or first TRACK_KM /
    TRACK_DS of the run, the altitude (ft) at the end fix, and the altitude
    change over that window in the direction of travel (positive = climbing).
    track is None when the run never gets TRACK_KM from the end fix, since a
    shorter chord is mostly turn and noise; alt/dalt are None without altitude.
    routes.py uses these to refuse a route fill the seen track points away from."""
    i = e if at_end else s
    step = -1 if at_end else 1
    j = i
    while s <= j + step <= e:
        j += step
        if (_km(la[i] / 1e5, lo[i] / 1e5, la[j] / 1e5, lo[j] / 1e5) >= TRACK_KM
                or abs(ts[i] - ts[j]) >= TRACK_DS):
            break
    a, b = (j, i) if at_end else (i, j)         # a -> b is the direction of travel
    trk = None
    if _km(la[a] / 1e5, lo[a] / 1e5, la[b] / 1e5, lo[b] / 1e5) >= TRACK_KM:
        trk = _bearing(la[a] / 1e5, lo[a] / 1e5, la[b] / 1e5, lo[b] / 1e5)
    alt = al[i]
    dalt = (al[b] - al[a]) if (al[a] is not None and al[b] is not None and a != b) else None
    return trk, alt, dalt


def segment(ts, la, lo, alt, gnd):
    """Return (leg_of_point[list], legs[(i0,i1)], airs[(s,e)]) for one aircraft.

    Points are assigned to the nearest flight in time; taxi/parked points fall to
    the adjacent leg. Returns ([], [], []) if the aircraft never really flew.

    `airs[k]` is the AIRBORNE run inside `legs[k]` -- the two are aligned by
    construction, since leg boundaries are cut at the midpoints between
    consecutive airborne runs. Callers want both: legs[k] is the envelope to
    draw, airs[k] is the span to time."""
    n = len(ts)
    # airborne runs (on_ground == False), cut again where a landing fits in a gap
    runs = []
    i = 0
    while i < n:
        if not gnd[i]:
            j = i
            while j < n and not gnd[j]:
                j += 1
            runs.extend(_split_at_gaps(ts, la, lo, i, j - 1))
            i = j
        else:
            i += 1
    # keep only runs that travel > FLIGHT_MIN_KM horizontally
    flights = []
    for (s, e) in runs:
        lats = la[s:e + 1]; lons = lo[s:e + 1]
        span = _km(min(lats) / 1e5, min(lons) / 1e5, max(lats) / 1e5, max(lons) / 1e5)
        if span > FLIGHT_MIN_KM:
            flights.append((s, e))
    if not flights:
        return [], [], []
    # leg boundaries: split at temporal midpoint between consecutive flights
    bounds = [0]
    for k in range(len(flights) - 1):
        e_k = flights[k][1]; s_k1 = flights[k + 1][0]
        mid_t = (ts[e_k] + ts[s_k1]) / 2
        b = e_k
        while b < s_k1 and ts[b] < mid_t:
            b += 1
        bounds.append(b)
    bounds.append(n)
    leg_of = [0] * n
    legs = []
    for k in range(len(flights)):
        i0, i1 = bounds[k], bounds[k + 1] - 1
        for idx in range(i0, i1 + 1):
            leg_of[idx] = k
        legs.append((i0, i1))
    return leg_of, legs, flights


def _landing_fits(ts, la, lo, a, b):
    """Could the aircraft have landed and taken off again between airborne
    nodes a and b? `ts` is in deciseconds, la/lo are degrees * 1e5. See
    GAP_SLACK_MIN for where the numbers come from."""
    dt_min = (ts[b] - ts[a]) / 600.0
    if dt_min < GAP_SLACK_MIN:
        return False                    # cheap exit: most node pairs
    d_km = _km(la[a] / 1e5, lo[a] / 1e5, la[b] / 1e5, lo[b] / 1e5)
    return dt_min - 60.0 * d_km / GAP_KMH >= GAP_SLACK_MIN


def _split_at_gaps(ts, la, lo, s, e):
    """Airborne run [s, e] cut wherever a landing fits between two nodes.

    Each piece is still filtered by FLIGHT_MIN_KM afterwards, and the leg
    boundary then falls at the temporal midpoint of the gap like any other
    turnaround -- so neither leg inherits a straight line across the hole. The
    two ends either side of a cut never had a surface message, so dep_gnd /
    arr_gnd come out False there and t_on / t_off are the last and first SEEN
    airborne fixes, not wheels events."""
    out = []
    start = s
    for k in range(s, e):
        if _landing_fits(ts, la, lo, k, k + 1):
            out.append((start, k))
            start = k + 1
    out.append((start, e))
    return out


def _anchor(al, gnd, idxs, head):
    """(index, was_ground) of the best airport-anchor within one end-window.

    on_ground is the least reliably received bit in the feed: surface position
    messages are low-power and need a receiver with near line-of-sight to the
    field, so half the aircraft in a day's traces never produce one at all, and
    airports with thin local coverage produce almost none. Prefer a ground fix
    when the window has one, but fall back to the window's LOWEST fix -- the
    climb-out or the approach -- instead of giving up, so a field can still be
    resolved from the air.
    """
    grounds = [i for i in idxs if gnd[i]]
    if grounds:
        return (grounds[0] if head else grounds[-1]), True
    return min(idxs, key=lambda i: al[i]), False


def endpoint_airport(la, lo, al, gnd, i0, i1, resolve, species=None, airline=False,
                     sched_only=False):
    """Nearest airport to each TEMPORAL end of the leg.

    The two windows are disjoint by construction, and that is what keeps a leg
    from being labelled X->X: scanning the WHOLE leg for "first ground fix" and
    "last ground fix" puts both of them on whichever end happened to have
    surface coverage, so a flight out of a thinly-covered field was recorded as
    departing from its own destination -- losing it for the real origin and
    inventing a self-loop at the destination.
    """
    n = i1 - i0 + 1
    w = max(ENDPOINT_MIN_PTS, int(n * ENDPOINT_FRAC))
    w = min(w, max(1, n // 2))              # never let head and tail overlap
    dep_i, dep_gnd = _anchor(al, gnd, range(i0, i0 + w), True)
    arr_i, arr_gnd = _anchor(al, gnd, range(i1 - w + 1, i1 + 1), False)
    dep = resolve(la[dep_i] / 1e5, lo[dep_i] / 1e5,
                  None if dep_gnd else al[dep_i], species, airline, sched_only)
    arr = resolve(la[arr_i] / 1e5, lo[arr_i] / 1e5,
                  None if arr_gnd else al[arr_i], species, airline, sched_only)
    return dep, arr, dep_gnd, arr_gnd


def _epoch_s(con, path, col):
    """SQL that yields `col` as float epoch SECONDS. Absolute times are stored as
    TIMESTAMP(ms, UTC), but the OOOI arithmetic below is plain float seconds -- so
    unwrap once here rather than at every comparison. Pre-timestamp files (plain
    integer seconds) still read correctly, instead of silently degrading to null."""
    types = {c[0]: c[1].upper() for c in
             con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()}
    return f"epoch_ms({col}) / 1000.0" if "TIMESTAMP" in types.get(col, "") else col


def load_callsigns(con, path):
    """callsigns.parquet -> {icao: [(t, flight)]}, time-ordered."""
    out = {}
    if not path:
        return out
    try:
        t = _epoch_s(con, path, "t")
        rows = con.execute(f"SELECT icao, {t}, flight FROM '{path}' "
                           f"ORDER BY icao, t").fetchall()
    except duckdb.Error as e:
        print(f"  no callsigns ({e}); flight will be null")
        return out
    for icao, tv, f in rows:
        out.setdefault(icao, []).append((float(tv), f))
    return out


def flight_for_leg(changes, a0, a1, on):
    """Callsign in effect for this leg. readsb reports `flight` only when it
    CHANGES, so prefer the first change falling inside the leg (crews set it at
    the stand, during taxi-out) and otherwise carry forward the last one set
    before the aircraft landed."""
    inside = [f for (t, f) in changes if a0 <= t <= a1 and f]
    if inside:
        return inside[0]
    lim = on if on is not None else a1
    carried = None
    for (t, f) in changes:
        if t > lim:
            break
        if f:
            carried = f
    return carried


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", default="prod-0-parquet-single/traces.parquet")
    ap.add_argument("--meta", default="prod-0-parquet-single/aircraft.parquet")
    ap.add_argument("--callsigns", default=None,
                    help="callsigns.parquet (or glob) from fit_spline.py; "
                         "without it the `flight` column stays NULL")
    ap.add_argument("--airports", default="airports.csv")
    ap.add_argument("--out-dir", default="airport_ds")
    ap.add_argument("--date", metavar="YYYY-MM-DD",
                    help="the UTC date this archive covers; it goes into every "
                         "leg_id. Derived from the aircraft table's base_ts "
                         "when omitted")
    ap.add_argument("--routes", default=None, metavar="DIR",
                    help="VRS standing-data checkout (routes/ + airlines/, and a "
                         "COMMIT file naming its revision) to fill unseen leg "
                         "ends from callsign routes; without it only the "
                         "same-aircraft chain fill runs. See routes.py")
    ap.add_argument("--limit-aircraft", type=int, default=0)
    ap.add_argument("--airport-radius-km", dest="airport_radius", type=float,
                    default=0.0, help="if > 0, per-airport files keep only the "
                    "leg's points within this radius of the airport (local taxi + "
                    "approach/departure). Default 0 = keep the FULL leg path in "
                    "each airport partition (legs are already split by leg_id, so "
                    "the frontend draws each as one continuous dep->arr track)")
    a = ap.parse_args()

    con = duckdb.connect()
    # base_ts is what makes leg times comparable BETWEEN aircraft: traces.parquet
    # stores t as deciseconds past each aircraft's own base_ts, so a raw t_off
    # from one aircraft cannot be compared with another's. Carry base_ts into the
    # leg index so any consumer can rebase (absolute UTC = base_ts + t/10).
    mcols = {c[0] for c in con.execute(
        f"DESCRIBE SELECT * FROM '{a.meta}'").fetchall()}
    if "base_ts" in mcols:
        meta = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
            f"SELECT icao, reg, type, base_ts FROM '{a.meta}'").fetchall()}
    else:
        print(f"WARNING: {a.meta} has no base_ts column -- leg times will stay "
              f"per-aircraft relative and base_ts will be NULL. Re-run "
              f"fit_spline.py to get one.")
        meta = {r[0]: (r[1], r[2], None) for r in con.execute(
            f"SELECT icao, reg, type FROM '{a.meta}'").fetchall()}
    day = resolve_date(a.date, meta)
    grid = build_airport_index(a.airports)
    resolve = make_resolver(grid)
    coords = {ident: (c[0], c[1]) for cell in grid.values() for c in cell
              for ident in (c[2],)}
    os.makedirs(a.out_dir, exist_ok=True)
    cs_by = load_callsigns(con, a.callsigns)
    rspec = importlib.util.spec_from_file_location(
        "routes", os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes.py"))
    rt = importlib.util.module_from_spec(rspec); rspec.loader.exec_module(rt)
    routes = rt.Routes(a.routes) if a.routes else None
    if routes:
        print(f"  routes: {len(routes.by_cs):,} callsigns from {a.routes} "
              f"(commit {routes.commit or 'unknown'})")
    fill_st = rt.new_stats()
    # 00:00Z that begins and ends this archive, in absolute UTC seconds
    day_beg = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp()
    day_end = day_beg + 86400
    cut_st = {"dep": 0, "arr": 0}     # resolved ends dropped as boundary cuts

    # carry the cusp bit through if the source has one (CR-fit output); the
    # frontend breaks its Catmull-Rom spline at cusp nodes for sharp corners
    src_cols = {c[0] for c in con.execute(
        f"DESCRIBE SELECT * FROM '{a.traces}'").fetchall()}
    has_cusp = "cusp" in src_cols

    pcols = ["icao", "t", "lat", "lon", "alt", "on_ground", "leg_id", "dep", "arr",
             "reg", "type", "flight", "dep_src", "arr_src"] + (["cusp"] if has_cusp else [])
    pbuf = {c: [] for c in pcols}
    legs_rows = []
    pw = [None]
    pfields = [("icao", pa.string()), ("t", pa.int32()), ("lat", pa.int32()),
               ("lon", pa.int32()), ("alt", pa.int32()), ("on_ground", pa.bool_()),
               ("leg_id", pa.string()), ("dep", pa.string()), ("arr", pa.string()),
               ("reg", pa.string()), ("type", pa.string()),
               ("flight", pa.string()),
               ("dep_src", pa.string()), ("arr_src", pa.string())]
    if has_cusp:
        pfields.append(("cusp", pa.bool_()))
    pschema = pa.schema(pfields)
    pl_path = os.path.join(a.out_dir, "points_legs.parquet")

    def flush_points():
        if not pbuf["t"]:
            return
        if pw[0] is None:
            pw[0] = pq.ParquetWriter(pl_path, pschema, compression="zstd",
                                     use_dictionary=["icao", "leg_id", "dep", "arr",
                                                     "reg", "type", "flight",
                                                     "dep_src", "arr_src"])
        pw[0].write_table(pa.table(pbuf, schema=pschema))
        for c in pcols:
            pbuf[c] = []

    # stream points grouped by icao
    lim = f"WHERE icao IN (SELECT DISTINCT icao FROM '{a.traces}' LIMIT {a.limit_aircraft})" \
          if a.limit_aircraft else ""
    sel = "icao,t,lat,lon,alt,on_ground" + (",cusp" if has_cusp else "")
    # duckdb 2.0 removed fetch_record_batch (every nightly from 2026-10-22 died
    # here, since requirements.txt does not pin it); to_arrow_reader replaces it.
    rel = con.sql(f"SELECT {sel} FROM '{a.traces}' {lim} ORDER BY icao, t")
    reader = (rel.to_arrow_reader(1_000_000) if hasattr(rel, "to_arrow_reader")
              else rel.fetch_record_batch(1_000_000))

    cur = None
    ts = la = lo = al = gd = cu = None
    ac = 0

    def finish(icao, ts, la, lo, al, gd, cu):
        leg_of, legs, airs = segment(ts, la, lo, al, gd)
        if not legs:
            return 0
        reg, typ, base = meta.get(icao, (None, None, None))
        recs = []
        species = routes.species.get(typ) if routes and typ else None
        for k, (i0, i1) in enumerate(legs):
            # t_start/t_end bracket the LEG (half of each adjacent turnaround,
            # and all of the ramp time on the day's first and last leg), so
            # t_end - t_start is not a flight time. t_off/t_on bracket the
            # airborne run instead. When dep_gnd/arr_gnd is false that end never
            # produced a surface message, so its t_off/t_on is a climb-out or
            # approach fix rather than a real wheels event -- filter on the flags.
            s_air, e_air = airs[k]
            t_off, t_on = ts[s_air], ts[e_air]
            # callsigns.parquet carries ABSOLUTE instants, while `ts` here is
            # deciseconds past this aircraft's own base_ts, so the comparison
            # has to happen in absolute seconds: base_ts + t/10. Without a
            # base_ts column there is no common timeline and `flight` stays
            # NULL rather than being matched against the wrong day.
            changes = cs_by.get(icao, ()) if base is not None else ()
            flight = (flight_for_leg(changes, base + ts[i0] / 10.0,
                                     base + ts[i1] / 10.0, base + t_on / 10.0)
                      if changes else None)
            # resolved AFTER the callsign: who is flying decides which of two
            # co-sited fields an end belongs to (SITE_KM)
            airline = bool(routes and routes.is_airline(flight))
            dep, arr, dep_gnd, arr_gnd = endpoint_airport(
                la, lo, al, gd, i0, i1, resolve, species, airline,
                airline and routes.lookup(flight) is not None)
            # an end the 00:00Z archive boundary cut is not an airport event:
            # airborne on the trace's very first/last fix, within BOUNDARY_CUT_S
            # of the boundary. NULL it so splice_legs.py sees the cut.
            if base is not None:
                if (k == 0 and i0 == 0 and s_air == i0 and not dep_gnd
                        and base + ts[i0] / 10.0 - day_beg <= BOUNDARY_CUT_S):
                    if dep is not None:
                        cut_st["dep"] += 1
                    dep = None
                if (k == len(legs) - 1 and i1 == len(ts) - 1 and e_air == i1
                        and not arr_gnd
                        and day_end - (base + ts[i1] / 10.0) <= BOUNDARY_CUT_S):
                    if arr is not None:
                        cut_st["arr"] += 1
                    arr = None
            # off_s/on_s only need to be comparable WITHIN this aircraft, so a
            # missing base_ts can fall back to 0 for the fill's reachability test
            b0 = base or 0
            trk0, alt0, dalt0 = end_track(ts, la, lo, al, s_air, e_air, False)
            trk1, alt1, dalt1 = end_track(ts, la, lo, al, s_air, e_air, True)
            recs.append(dict(k=k, i0=i0, i1=i1, dep=dep, arr=arr, dep_gnd=dep_gnd,
                             arr_gnd=arr_gnd, flight=flight, t_off=t_off, t_on=t_on,
                             off_s=b0 + t_off / 10.0, on_s=b0 + t_on / 10.0,
                             f0=(la[s_air] / 1e5, lo[s_air] / 1e5),
                             f1=(la[e_air] / 1e5, lo[e_air] / 1e5),
                             trk0=trk0, alt0=alt0, dalt0=dalt0,
                             trk1=trk1, alt1=alt1, dalt1=dalt1))
        # unseen ends: same-aircraft chain, then callsign routes -- routes.py
        rt.fill_aircraft(recs, coords, routes, fill_st)
        for r in recs:
            i0, i1 = r["i0"], r["i1"]
            leg_id = f"{day}_{icao}_{r['k']}"
            for idx in range(i0, i1 + 1):
                pbuf["icao"].append(icao); pbuf["t"].append(ts[idx])
                pbuf["lat"].append(la[idx]); pbuf["lon"].append(lo[idx])
                pbuf["alt"].append(al[idx]); pbuf["on_ground"].append(gd[idx])
                pbuf["leg_id"].append(leg_id)
                pbuf["dep"].append(r["dep"]); pbuf["arr"].append(r["arr"])
                pbuf["reg"].append(reg); pbuf["type"].append(typ)
                pbuf["flight"].append(r["flight"])
                pbuf["dep_src"].append(r["dep_src"]); pbuf["arr_src"].append(r["arr_src"])
                if has_cusp:
                    pbuf["cusp"].append(cu[idx])
            legs_rows.append((leg_id, icao, reg, typ, r["flight"], r["dep"], r["arr"],
                              ts[i0], ts[i1], r["t_off"], r["t_on"],
                              r["dep_gnd"], r["arr_gnd"], base, i1 - i0 + 1,
                              r["dep_src"], r["arr_src"], r["route"]))
        return len(legs)

    for batch in reader:
        d = batch.to_pydict()
        ic = d["icao"]; T = d["t"]; LA = d["lat"]; LO = d["lon"]; AL = d["alt"]; GD = d["on_ground"]
        CU = d["cusp"] if has_cusp else None
        for r in range(len(ic)):
            if ic[r] != cur:
                if cur is not None:
                    finish(cur, ts, la, lo, al, gd, cu)
                    ac += 1
                    if ac % 10000 == 0:
                        print(f"  ... {ac} aircraft, {len(legs_rows)} legs", flush=True)
                    if len(pbuf["t"]) >= 800_000:
                        flush_points()
                cur = ic[r]; ts = []; la = []; lo = []; al = []; gd = []; cu = []
            ts.append(T[r]); la.append(LA[r]); lo.append(LO[r])
            al.append(AL[r] if AL[r] is not None else 0); gd.append(GD[r])
            if has_cusp:
                cu.append(CU[r])
    if cur is not None:
        # the loop only counts an aircraft when the NEXT one starts, so the last
        # one in the stream needs counting here -- without this a single-aircraft
        # run reported "DONE: 0 aircraft"
        finish(cur, ts, la, lo, al, gd, cu)
        ac += 1
    flush_points()
    if pw[0] is not None:
        pw[0].close()

    # flights index (one row per leg), sorted by dep
    lt = pa.table({
        "leg_id": [x[0] for x in legs_rows], "icao": [x[1] for x in legs_rows],
        "reg": [x[2] for x in legs_rows], "type": [x[3] for x in legs_rows],
        "flight": [x[4] for x in legs_rows],
        "dep": [x[5] for x in legs_rows], "arr": [x[6] for x in legs_rows],
        "t_start": [x[7] for x in legs_rows], "t_end": [x[8] for x in legs_rows],
        "t_off": [x[9] for x in legs_rows], "t_on": [x[10] for x in legs_rows],
        "dep_gnd": [x[11] for x in legs_rows], "arr_gnd": [x[12] for x in legs_rows],
        "base_ts": [x[13] for x in legs_rows],
        "n_points": [x[14] for x in legs_rows],
        "dep_src": pa.array([x[15] for x in legs_rows], pa.string()),
        "arr_src": pa.array([x[16] for x in legs_rows], pa.string()),
        "route": pa.array([x[17] for x in legs_rows], pa.string()),
    }).sort_by("dep")
    pq.write_table(lt, os.path.join(a.out_dir, "flights.parquet"),
                   compression="zstd", use_dictionary=["dep", "arr", "type", "reg", "flight",
                                                       "dep_src", "arr_src", "route"])
    # joined into every per-airport file below, so one fetch carries both the
    # geometry and the five per-leg values a time query needs
    con.register("legidx", lt)

    # per-airport files: each leg under BOTH dep and arr (hive-partitioned). With
    # --airport-radius-km > 0 the copy is CLIPPED to the airport's vicinity; by
    # default the FULL leg path is kept in each partition (legs are split by
    # leg_id and drawn per-leg, so a full dep->arr track is continuous, not a
    # teleport between fields).
    apath = os.path.join(a.out_dir, "airports")
    if a.airport_radius and a.airport_radius > 0:
        con.execute("""
            CREATE OR REPLACE TEMP MACRO km(la1, lo1, la2, lo2) AS
              6371 * 2 * asin(sqrt(
                power(sin(radians(la2 - la1) / 2), 2) +
                cos(radians(la1)) * cos(radians(la2)) *
                power(sin(radians(lo2 - lo1) / 2), 2)))
        """)
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE ap AS
            SELECT ident AS code, latitude_deg AS lat, longitude_deg AS lon
            FROM read_csv_auto('{a.airports}')
            WHERE ident IS NOT NULL AND latitude_deg IS NOT NULL
        """)
        dep_sel = (f"SELECT {LEG_COLS}, p.dep AS airport FROM '{pl_path}' p "
                   f"JOIN legidx l USING (leg_id) "
                   f"JOIN ap d ON p.dep = d.code WHERE p.dep IS NOT NULL "
                   f"AND km(d.lat, d.lon, p.lat / 1e5, p.lon / 1e5) < {a.airport_radius}")
        arr_sel = (f"SELECT {LEG_COLS}, p.arr AS airport FROM '{pl_path}' p "
                   f"JOIN legidx l USING (leg_id) "
                   f"JOIN ap r ON p.arr = r.code WHERE p.arr IS NOT NULL "
                   f"AND p.arr <> p.dep "
                   f"AND km(r.lat, r.lon, p.lat / 1e5, p.lon / 1e5) < {a.airport_radius}")
    else:
        dep_sel = (f"SELECT {LEG_COLS}, p.dep AS airport FROM '{pl_path}' p "
                   f"JOIN legidx l USING (leg_id) WHERE p.dep IS NOT NULL")
        arr_sel = (f"SELECT {LEG_COLS}, p.arr AS airport FROM '{pl_path}' p "
                   f"JOIN legidx l USING (leg_id) "
                   f"WHERE p.arr IS NOT NULL AND p.arr <> p.dep")
    # Single-thread this one write so every partition gets exactly ONE file
    # (data_0.parquet). DuckDB's parallel partitioned write otherwise has each
    # thread flush its own data_N.parquet per partition, and a browser can't list
    # a directory over HTTP to discover data_1/data_2 -- so the frontend contract
    # must be one predictable file per airport. This write is cheap vs the fit.
    con.execute("SET threads TO 1")
    con.execute(f"""
        COPY (
          {dep_sel}
          UNION ALL
          {arr_sel}
          ORDER BY airport, icao, t
        ) TO '{apath}' (FORMAT parquet, PARTITION_BY (airport),
                        OVERWRITE_OR_IGNORE, COMPRESSION zstd)
    """)
    con.execute("RESET threads")

    n_air = con.execute(f"SELECT count(DISTINCT airport) FROM (SELECT dep AS airport "
                        f"FROM '{pl_path}' WHERE dep IS NOT NULL UNION SELECT arr "
                        f"FROM '{pl_path}' WHERE arr IS NOT NULL)").fetchone()[0]

    # legs_meta.json: what this partition IS, so a client reading a retained
    # older day can tell without probing every file's footer. flights_schema
    # goes up whenever a column is added or changes meaning -- days written
    # before this file existed have no legs_meta.json at all, which reads as
    # schema 1 (no t_off/t_on, no ground flags, no base_ts).
    # x[13], not x[12]: [12] is arr_gnd, which made this {True, False} -- every
    # day said base_ts_uniform false, and an all-ground-arrival day would have
    # reported t_epoch = 1 (1970)
    bases = {x[13] for x in legs_rows}
    uniform = len(bases) == 1 and None not in bases
    epoch = next(iter(bases)) if uniform else None
    meta_out = {
        "flights_schema": FLIGHTS_SCHEMA,
        "date": day,
        # leg_id carries its date, so it is a key on its own across partitions
        "leg_id_global": True,
        "n_legs": len(legs_rows), "n_aircraft": ac, "n_airports": int(n_air),
        "base_ts_uniform": uniform,
        "t_epoch": epoch,
        "t_epoch_iso": (dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
                        .isoformat().replace("+00:00", "Z")) if epoch else None,
        "has_cusp": has_cusp,
        # where filled dep/arr came from, and how many of each kind
        "routes_commit": routes.commit if routes else None,
        "fill": fill_st,
        # airborne ends at the 00:00Z cut that resolved to a field and were
        # dropped (BOUNDARY_CUT_S) -- before the chain/route fill
        "boundary_cut": cut_st,
    }
    with open(os.path.join(a.out_dir, "legs_meta.json"), "w") as f:
        json.dump(meta_out, f, indent=1)

    print(f"\nDONE: {ac} aircraft, {len(legs_rows)} legs, {n_air} airports")
    print(f"  {pl_path}")
    print(f"  {os.path.join(a.out_dir,'flights.parquet')}  (leg index, sorted by dep)")
    print(f"  {apath}/airport=XXXX/*.parquet  (per-airport, fetch one at a time)")
    print(f"  {os.path.join(a.out_dir,'legs_meta.json')}  (schema {FLIGHTS_SCHEMA}, "
          f"base_ts {'uniform' if uniform else 'per-aircraft'})")


if __name__ == "__main__":
    main()
