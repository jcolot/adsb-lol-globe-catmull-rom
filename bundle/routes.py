"""routes.py - fill leg ends the trace never saw, for build_legs.py.

A leg end is unknown when the aircraft was out of coverage at that end: the
field has no receiver with line of sight, or the trace stops in a hole. Two
sources fill it, in this order, and each filled end says where it came from in
dep_src / arr_src:

  ground  seen on the surface                       (observed, from build_legs)
  air     resolved from an approach/climb-out fix   (observed, from build_legs)
  chain   the same aircraft's adjacent leg: it departs where it last landed.
          Only an OBSERVED neighbour end is used.
  route   the VRS standing-data route for the leg's callsign
          (github.com/vradarserver/standing-data, CC0). With one end known,
          that end must be one of the route's airports and the filled end is
          its neighbour on the route; with neither known, the seen track must
          run from A towards B.

Every fill must also be physically possible:
  - the seen track got closer to the filled airport (or further from the
    filled origin) by no less than -SLACK_KM;
  - the HEADING at the seen end points there: the direction of travel over the
    last (first) ~30 km is within MAX_TURN_DEG of the great-circle bearing to
    (from) the airport, unless the airport is within NEAR_TURN_KM, where
    vectors and holds turn the aircraft any way. Getting closer alone let
    QR8099, lost over Algeria descending at Algiers, be filled to Doha 86 deg
    off its nose; the schedule caught five such fills out of EBBR on 2026-11-15;
  - an end seen low (below LOW_FT) and descending (or climbing, at a
    departure) is close to its airport: at most SLACK_KM + LOW_KM_PER_FT * alt,
    about three times a 3-degree glide path;
  - the aircraft can reach it at KMH between its last seen fix and the next
    leg's first.
A route fill that then disagrees with the aircraft's adjacent leg is withdrawn.
Observed ends are never overwritten.

VRS routes are typed in by volunteers, one per callsign, with no date: they go
stale (SXS6YH was EDDL-LTBS in VRS while the trace showed it leaving EBBR).
That is why a route is only used where the trace agrees with it. Measured on
2026-11-15, hiding one end of the 24,610 legs seen on the ground at BOTH ends,
the route put it back correctly 95.5% (arr) / 95.9% (dep) of the time when it
made a fill, before the reachability checks. Those legs are at well-covered
fields, so the rate where routes matter most (LTAC and the like) is unmeasured.

Filled ends carry NO timing: t_off/t_on at a chain or route end are still the
last/first SEEN airborne fix. Anything that measures flight time must use only
observed ends -- see observed_sql().
"""
import csv, glob, math, os, re

KMH = 950.0                 # generous jet ground speed for reachability
SLACK_KM = 50.0
MAX_TURN_DEG = 45.0         # heading at the seen end vs bearing to the airport
NEAR_TURN_KM = 100.0        # ...not checked this close in
LOW_FT = 20000.0            # an end seen below this, descending/climbing,
LOW_KM_PER_FT = 0.01        # ...is within SLACK_KM + this * alt of its airport
LEVEL_FT = 1000.0           # altitude change that counts as descending/climbing
OBSERVED = ("ground", "air")


def observed_sql(cols, end):
    """SQL for the OBSERVED value of `end` ('dep' or 'arr') in a flights table
    with column set `cols`: filled values masked to NULL. Pre-schema-6 files
    have no *_src column and every non-null end was observed."""
    if f"{end}_src" in cols:
        return f"(CASE WHEN {end}_src IN ('ground', 'air') THEN {end} END)"
    return end


def _gc(a, b):
    p1, l1, p2, l2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    return 6371 * 2 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))


def _bearing(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    return math.degrees(math.atan2(
        math.sin(dl) * math.cos(p2),
        math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360


def _turn(h1, h2):
    return abs((h1 - h2 + 180) % 360 - 180)


def heading_ok(trk, fix, apt, d, inbound):
    """Does the travel direction `trk` at `fix` agree with airport `apt` at
    distance d? inbound: flying TO it (an arrival); else FROM it (a departure:
    the airport lies behind, so the bearing to it is trk + 180)."""
    if trk is None or d <= NEAR_TURN_KM:
        return True
    want = trk if inbound else (trk + 180) % 360
    return _turn(want, _bearing(fix, apt)) <= MAX_TURN_DEG


def low_ok(alt, dalt, d, inbound):
    """An end seen low and descending (arrival) / climbing (departure) is near."""
    if alt is None or dalt is None or alt >= LOW_FT:
        return True
    if (-dalt if inbound else dalt) < LEVEL_FT:
        return True
    return d <= SLACK_KM + LOW_KM_PER_FT * max(alt, 0)


class Routes:
    """VRS routes keyed by normalised callsign, from a standing-data checkout
    (routes/schema-01 and airlines/schema-01 are all that is read)."""

    def __init__(self, root):
        self.root = root
        self.commit = None
        cf = os.path.join(root, "COMMIT")
        if os.path.exists(cf):
            self.commit = open(cf).read().strip() or None
        self.iata2icao = {}
        self.airlines = set()       # ICAO codes of airlines WITH an IATA code
        amb = set()
        with open(os.path.join(root, "airlines/schema-01/airlines.csv"),
                  newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ia, ic = r.get("IATA"), r.get("ICAO")
                if ia and ic:
                    # air forces are in this table too (BAF, CNV, ACW...) but
                    # never with an IATA code, so this is the airline test
                    self.airlines.add(ic)
                    if ia in self.iata2icao and self.iata2icao[ia] != ic:
                        amb.add(ia)
                    self.iata2icao[ia] = ic
        for ia in amb:                      # an IATA code shared by airlines
            del self.iata2icao[ia]          # cannot be swapped safely
        self.by_cs = {}
        for p in glob.glob(os.path.join(root, "routes/schema-01/*/*.csv")):
            with open(p, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    cs, codes = r.get("Callsign"), r.get("AirportCodes")
                    if cs and codes:
                        self.by_cs[cs] = codes.split("-")
        if not self.by_cs:
            raise SystemExit(f"no routes under {root}/routes/schema-01")
        # ICAO type designator -> SpeciesCode (L landplane, H helicopter,
        # A amphibian, S seaplane, G gyroplane, T tiltrotor). Optional: a
        # checkout without model-type/ just leaves every type unknown.
        self.species = {}
        for p in glob.glob(os.path.join(root, "model-type/schema-01/*.csv")):
            with open(p, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    t, sp = r.get("ICAO"), r.get("SpeciesCode")
                    if t and sp and (t not in self.species or r.get("IsActive") == "1"):
                        self.species[t] = sp

    _CS = re.compile(r"^(?P<code>[A-Z]{2,3}|[A-Z][0-9]|[0-9][A-Z])(?P<num>\d[A-Z0-9]*)$")
    _NUM = re.compile(r"^\d+[A-Z]{0,2}$")
    # A US registration flown as the callsign (N912AB, N35KP) has the shape of
    # an IATA-prefixed flight number (N9 + 12AB): on 2026-11-15 it turned
    # 9,000 Cessna and Piper legs into "airline" flights NVR12AB, VOS5KP... An
    # ADS-B airline callsign carries the 3-letter ICAO code, so an N-number is
    # never read as an IATA one.
    _NREG = re.compile(r"^N[1-9][0-9]{0,4}[A-Z]{0,2}$")

    def normalise(self, cs):
        """The VRS rule (routes/schema-01/README.md): ICAO airline code, then the
        number without leading zeros, at most four characters of which at most
        the last two are letters. An IATA prefix is swapped for its ICAO code."""
        if not cs:
            return None
        cs = cs.strip().upper()
        m = self._CS.match(cs)
        if not m or (len(m["code"]) == 2 and self._NREG.match(cs)):
            return None
        code, num = m["code"], m["num"]
        if len(code) == 2:
            code = self.iata2icao.get(code)
            if not code:
                return None
        num = num.lstrip("0")
        if not num or num.isalpha():
            num = "0" + num
        if len(num) > 4 or not self._NUM.match(num):
            return None
        return code + num

    def is_airline(self, cs):
        """Callsign of a scheduled/cargo airline (see self.airlines)."""
        k = self.normalise(cs)
        return bool(k) and k[:3] in self.airlines

    def lookup(self, cs):
        k = self.normalise(cs)
        return self.by_cs.get(k) if k else None


def new_stats():
    return dict(chain_arr=0, chain_dep=0, route_arr=0, route_dep=0, route_both=0,
                route_contradicts_observed=0, route_implausible=0,
                no_callsign=0, callsign_not_in_routes=0, withdrawn_conflict=0,
                heading_rejected=0, low_rejected=0)


def fill_aircraft(legs, coords, routes, st):
    """Fill one aircraft's legs IN PLACE. `legs` is time-ordered; each is a dict
    with dep, arr, dep_gnd, arr_gnd, flight, off_s / on_s (absolute seconds of
    the first / last seen airborne fix) and f0 / f1 ((lat, lon) of those
    fixes), and optionally trk0/alt0/dalt0, trk1/alt1/dalt1 (build_legs.end_track;
    missing or None skips that check). Adds dep_src, arr_src and route. `coords` maps ident -> (lat, lon);
    `routes` may be None (chain only)."""
    n = len(legs)
    for l in legs:
        l["dep_src"] = ("ground" if l["dep_gnd"] else "air") if l["dep"] else None
        l["arr_src"] = ("ground" if l["arr_gnd"] else "air") if l["arr"] else None
        l["route"] = None
    obs_dep = [l["dep"] for l in legs]
    obs_arr = [l["arr"] for l in legs]

    def arr_ok(i, x):
        l = legs[i]
        if x not in coords:
            return False
        d1 = _gc(l["f1"], coords[x])
        if _gc(l["f0"], coords[x]) + SLACK_KM < d1:      # moving away from x
            return False
        if not heading_ok(l.get("trk1"), l["f1"], coords[x], d1, True):
            st["heading_rejected"] += 1
            return False
        if not low_ok(l.get("alt1"), l.get("dalt1"), d1, True):
            st["low_rejected"] += 1
            return False
        if i + 1 < n:
            nx = legs[i + 1]
            h = (nx["off_s"] - l["on_s"]) / 3600.0
            if d1 > KMH * h + SLACK_KM or _gc(coords[x], nx["f0"]) > KMH * h + SLACK_KM:
                return False
        return True

    def dep_ok(i, x):
        l = legs[i]
        if x not in coords:
            return False
        d0 = _gc(l["f0"], coords[x])
        if _gc(l["f1"], coords[x]) + SLACK_KM < d0:      # track came from elsewhere
            return False
        if not heading_ok(l.get("trk0"), l["f0"], coords[x], d0, False):
            st["heading_rejected"] += 1
            return False
        if not low_ok(l.get("alt0"), l.get("dalt0"), d0, False):
            st["low_rejected"] += 1
            return False
        if i > 0:
            pv = legs[i - 1]
            h = (l["off_s"] - pv["on_s"]) / 3600.0
            if d0 > KMH * h + SLACK_KM or _gc(pv["f1"], coords[x]) > KMH * h + SLACK_KM:
                return False
        return True

    # 1. chain, from OBSERVED neighbours only
    for i, l in enumerate(legs):
        if l["arr"] is None and i + 1 < n and obs_dep[i + 1] and arr_ok(i, obs_dep[i + 1]):
            l["arr"], l["arr_src"] = obs_dep[i + 1], "chain"; st["chain_arr"] += 1
        if l["dep"] is None and i > 0 and obs_arr[i - 1] and dep_ok(i, obs_arr[i - 1]):
            l["dep"], l["dep_src"] = obs_arr[i - 1], "chain"; st["chain_dep"] += 1

    # 2. routes
    if routes is not None:
        for i, l in enumerate(legs):
            if l["dep"] and l["arr"]:
                continue
            if not routes.normalise(l["flight"]):
                st["no_callsign"] += 1; continue
            r = routes.lookup(l["flight"])
            if not r:
                st["callsign_not_in_routes"] += 1; continue
            pairs = list(zip(r, r[1:]))
            if l["dep"] and not l["arr"]:
                cand = [b for (x, b) in pairs if x == l["dep"]]
                if not cand:
                    st["route_contradicts_observed"] += 1
                elif arr_ok(i, cand[0]):
                    l["arr"], l["arr_src"], l["route"] = cand[0], "route", "-".join(r)
                    st["route_arr"] += 1
                else:
                    st["route_implausible"] += 1
            elif l["arr"] and not l["dep"]:
                cand = [x for (x, b) in pairs if b == l["arr"]]
                if not cand:
                    st["route_contradicts_observed"] += 1
                elif dep_ok(i, cand[0]):
                    l["dep"], l["dep_src"], l["route"] = cand[0], "route", "-".join(r)
                    st["route_dep"] += 1
                else:
                    st["route_implausible"] += 1
            else:
                ok = [(x, b) for (x, b) in pairs if dep_ok(i, x) and arr_ok(i, b)]
                if len(ok) == 1:
                    l["dep"], l["arr"] = ok[0]
                    l["dep_src"] = l["arr_src"] = "route"
                    l["route"] = "-".join(r); st["route_both"] += 1
                else:
                    st["route_implausible"] += 1

    # 3. a turnaround has ONE station: withdraw a route fill its neighbour contradicts
    for i in range(n - 1):
        a, b = legs[i], legs[i + 1]
        if a["arr"] and b["dep"] and a["arr"] != b["dep"]:
            for leg, end in ((a, "arr"), (b, "dep")):
                if leg[f"{end}_src"] == "route":
                    leg[end] = None; leg[f"{end}_src"] = None
                    st["withdrawn_conflict"] += 1
                    if "route" not in (leg["dep_src"], leg["arr_src"]):
                        leg["route"] = None
