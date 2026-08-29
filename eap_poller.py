#!/usr/bin/env python3
"""
EAP RWY15 departure-compliance poller
=====================================

Continuously polls community ADS-B (adsb.lol / adsb.fi) *server-side* (no browser,
no CORS), classifies south-departures off EuroAirport runway 15 against the
prescribed corridor axis, and logs everything to disk.

Zero dependencies — standard library only. Python 3.8+.

Usage
-----
    python3 eap_poller.py                 # start polling (Ctrl+C to stop cleanly)
    python3 eap_poller.py --source fi     # use adsb.fi instead of adsb.lol
    python3 eap_poller.py --axis axis.json# load a calibrated axis (see --help)
    python3 eap_poller.py --report        # print aggregate stats from flights.csv

Outputs (in ./eap_data/)
------------------------
    raw_YYYYMMDD.jsonl   every accepted position sample (full fidelity, append-only)
    flights.csv          one row per finalized flight
    session.json         rolling snapshot in the HTML tool's *Import* format

Calibrate once, share geometry: edit the AXIS in the map, Export, then pass that
file with --axis so the script measures against exactly the same corridor.
"""
import argparse, json, math, os, ssl, time, signal, urllib.request, urllib.error
from datetime import datetime, timezone
from corridor_dispersion import analyse_flight, TrackPoint, summarise

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# ----------------------------------------------------------------------------- geography
AIRPORT   = (47.5896, 7.5299)
ALLSCHWIL = (47.5519, 7.5328)        # confirmed town centre
DEFAULT_AXIS = [                     # matches CORRIDOR_15 in corridor_dispersion.py
    (47.6033, 7.5158),               # RWY15 threshold — confirmed
    (47.5885, 7.5256),               # turn point — map-derived placeholder
    (47.5503, 7.5006),               # post-turn — map-derived placeholder, replace with ELBEG
]

# ----------------------------------------------------------------------------- geo math (mirrors the JS)
R = 6371000.0
def _hav(a, b):
    dla = math.radians(b[0]-a[0]); dlo = math.radians(b[1]-a[1])
    la1 = math.radians(a[0]); la2 = math.radians(b[0])
    h = math.sin(dla/2)**2 + math.cos(la1)*math.cos(la2)*math.sin(dlo/2)**2
    return 2*R*math.asin(min(1.0, math.sqrt(h)))
def _brng(a, b):
    la1 = math.radians(a[0]); la2 = math.radians(b[0]); dlo = math.radians(b[1]-a[1])
    y = math.sin(dlo)*math.cos(la2)
    x = math.cos(la1)*math.sin(la2) - math.sin(la1)*math.cos(la2)*math.cos(dlo)
    return math.atan2(y, x)
def _cross_track(A, B, P):
    d13 = _hav(A, P)/R; t13 = _brng(A, P); t12 = _brng(A, B)
    xt = math.asin(max(-1.0, min(1.0, math.sin(d13)*math.sin(t13-t12)))) * R
    at = math.acos(max(-1.0, min(1.0, math.cos(d13)/math.cos(xt/R)))) * R
    return xt, at, _hav(A, B)

def classify_point(P, axis, half_width):
    """Return (status, dist_m): 'ok' on corridor, 'south' overshoot toward Allschwil,
    'north' off-axis the other way."""
    best = None
    for i in range(len(axis)-1):
        A, B = axis[i], axis[i+1]
        xt, at, seglen = _cross_track(A, B, P)
        if at < 0:           dist = _hav(A, P)
        elif at > seglen:    dist = _hav(B, P)
        else:                dist = abs(xt)
        if best is None or dist < best[0]:
            aref_xt, _, _ = _cross_track(A, B, ALLSCHWIL)
            best = (dist, _sign(xt), _sign(aref_xt))
    if best is None:
        return ("none", 0.0)
    dist, side, all_side = best
    if dist <= half_width/2:        return ("ok", dist)
    if side == all_side:            return ("south", dist)
    return ("north", dist)
def _sign(x): return (x > 0) - (x < 0)

# ----------------------------------------------------------------------------- feed
def api_url(cfg):
    la, lo = AIRPORT
    if cfg["source"] == "lol":
        return f"https://api.adsb.lol/v2/lat/{la}/lon/{lo}/dist/{cfg['range']}"
    return f"https://opendata.adsb.fi/api/v3/lat/{la}/lon/{lo}/dist/{cfg['range']}"

def fetch(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "eap-departure-poller/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("ac") or data.get("aircraft") or []

def parse_alt(v):
    if v is None or v == "ground": return None
    try: return float(v)
    except (TypeError, ValueError): return None

# ----------------------------------------------------------------------------- track model
RANK = {"none": 0, "ok": 1, "north": 2, "south": 3}
class Track:
    __slots__ = ("hex","cs","pts","worst","is_dep","max_south","min_alt","first","last","ac_type","reg","disp_result")
    def __init__(self, hexid, cs):
        self.hex = hexid; self.cs = cs; self.pts = []
        self.worst = "none"; self.is_dep = False
        self.max_south = 0.0; self.min_alt = math.inf
        self.first = self.last = time.time()
        self.ac_type = None; self.reg = None; self.disp_result = None

def ingest(aircraft, tracks, cfg, raw_fp):
    now = time.time()
    for a in aircraft:
        lat, lon = a.get("lat"), a.get("lon")
        hexid = a.get("hex") or a.get("icao")
        if lat is None or lon is None or not hexid:
            continue
        alt  = parse_alt(a.get("alt_baro"))
        rate = a.get("baro_rate"); rate = a.get("geom_rate") if rate is None else rate
        cs   = (a.get("flight") or "").strip()
        t = tracks.get(hexid)
        if t is None:
            t = Track(hexid, cs or hexid); tracks[hexid] = t
        if cs: t.cs = cs
        if a.get("t"): t.ac_type = a["t"]
        if a.get("r"): t.reg = a["r"]
        last = t.pts[-1] if t.pts else None
        if last and last["lat"] == lat and last["lon"] == lon:
            t.last = now; continue                       # stationary update
        climbing = (rate > 100) if rate is not None else (
            bool(last) and alt is not None and last["alt"] is not None and alt > last["alt"]+30)
        dist_km = _hav((lat, lon), AIRPORT)/1000.0
        dep = (alt is not None and alt <= cfg["alt"] and dist_km <= cfg["range"]*1.852)
        if cfg["dep_only"]:
            dep = dep and climbing
        status, d = classify_point((lat, lon), cfg["axis"], cfg["width"]) if dep else ("none", 0.0)
        pt = {"t": now, "lat": lat, "lon": lon, "alt": alt,
              "trk": a.get("track"), "gs": a.get("gs"), "status": status}
        t.pts.append(pt); t.last = now
        if len(t.pts) > cfg["max_pts"]: t.pts.pop(0)
        if dep:
            t.is_dep = True
            if alt < t.min_alt: t.min_alt = alt
            if status == "south" and d > t.max_south: t.max_south = d
            if RANK[status] > RANK[t.worst]: t.worst = status
        # full-fidelity raw line
        raw_fp.write(json.dumps({"t": round(now,1), "hex": hexid, "cs": t.cs,
            "lat": lat, "lon": lon, "alt": alt, "gs": a.get("gs"),
            "trk": a.get("track"), "dep": dep, "status": status,
            "dist_m": round(d,1)}) + "\n")
    raw_fp.flush()

# ----------------------------------------------------------------------------- persistence
def iso(ts): return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")

def finalize(t, csv_fp, corridor):
    points = [TrackPoint(lat=p["lat"], lon=p["lon"], alt_ft=p["alt"]) for p in t.pts]
    t.disp_result = analyse_flight(t.hex, t.cs, points, corridor=corridor)
    dr = t.disp_result
    csv_fp.write(",".join([
        iso(t.last), t.cs.replace(",", " "), t.hex,
        "1" if t.is_dep else "0", t.worst,
        str(round(t.max_south)), str(int(t.min_alt) if math.isfinite(t.min_alt) else ""),
        str(len(t.pts)), iso(t.first),
        str(round(dr.max_abs_offset_m)) if dr else "",
        str(round(dr.mean_abs_offset_m)) if dr else "",
        (dr.side.replace(",", " ")) if dr else "",
        ("1" if dr.overflew_allschwil else "0") if dr else "",
        str(round(dr.min_dist_allschwil_m)) if dr else "",
    ]) + "\n")
    csv_fp.flush()

def _track_dict(t):
    d = {
        "callsign": t.cs, "hex": t.hex, "departure": t.is_dep, "result": t.worst,
        "maxSouthOvershoot_m": round(t.max_south),
        "minAlt_ft": int(t.min_alt) if math.isfinite(t.min_alt) else None,
        "acType": t.ac_type, "reg": t.reg,
        "track": [[round(p["t"]*1000), p["lat"], p["lon"], p["alt"], p["status"]]
                  for p in t.pts],
    }
    dr = t.disp_result
    if dr is not None:
        d["dispersion"] = {
            "maxAbsOffset_m": round(dr.max_abs_offset_m),
            "meanAbsOffset_m": round(dr.mean_abs_offset_m),
            "side": dr.side,
            "overflew_allschwil": dr.overflew_allschwil,
            "minDistAllschwil_m": round(dr.min_dist_allschwil_m),
        }
    return d

def _write_json(path, flights, cfg):
    out = {"airport": "LFSB/BSL", "exported": iso(time.time()),
           "axis": [list(p) for p in cfg["axis"]],
           "rule": {"corridorWidth_m": cfg["width"], "evalBelow_ft": cfg["alt"]},
           "flights": flights}
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(out, f)
    os.replace(tmp, path)

def write_session(path, tracks, finalized, cfg):
    fin_dicts = [t if isinstance(t, dict) else _track_dict(t) for t in finalized]
    act_dicts = [_track_dict(t) for t in tracks.values()]
    flights = fin_dicts[-cfg["max_flights"]:] + act_dicts
    _write_json(path, flights, cfg)

def load_cumulative(path):
    if not os.path.exists(path): return []
    try:
        with open(path) as f: return json.load(f).get("flights", [])
    except Exception: return []

def write_cumulative(path, cum_flights, cfg):
    _write_json(path, cum_flights, cfg)

# ----------------------------------------------------------------------------- report mode
def report(data_dir):
    path = os.path.join(data_dir, "flights.csv")
    if not os.path.exists(path):
        print("No flights.csv yet — run the poller first."); return
    rows = [l.rstrip("\n").split(",") for l in open(path) if l.strip() and not l.startswith("finalized")]
    deps = [r for r in rows if len(r) > 4 and r[3] == "1"]
    n = len(deps)
    if not n:
        print("No departures recorded yet."); return
    ok    = sum(1 for r in deps if r[4] == "ok")
    south = sum(1 for r in deps if r[4] == "south")
    north = sum(1 for r in deps if r[4] == "north")
    worst = sorted((int(r[5]) for r in deps if r[4] == "south"), reverse=True)[:5]
    print(f"\n  EuroAirport RWY15 departures logged : {n}")
    print(f"  On corridor                         : {ok:>4}  ({100*ok//n}%)")
    print(f"  South overshoot (Allschwil)         : {south:>4}  ({100*south//n}%)")
    print(f"  Off-axis north                      : {north:>4}  ({100*north//n}%)")
    if worst: print(f"  Worst south overshoots (m)          : {', '.join(map(str, worst))}")

    # Dispersion summary (only rows that have the extended columns)
    from corridor_dispersion import FlightResult
    disp_rows = [r for r in deps if len(r) >= 14 and r[9]]
    if disp_rows:
        def _fr(r):
            side = r[11]
            signed = float(r[9]) if side.startswith("right") else -float(r[9])
            return FlightResult(
                icao=r[2], callsign=r[1], n_points=int(r[7]),
                max_abs_offset_m=float(r[9]), mean_abs_offset_m=float(r[10]),
                signed_offset_at_max_m=signed,
                overflew_allschwil=(r[12] == "1"),
                min_dist_allschwil_m=float(r[13]),
            )
        summary = summarise(_fr(r) for r in disp_rows)
        print(f"\n  --- Lateral dispersion (corridor_dispersion) ---")
        print(f"  {summary}")
    print()

# ----------------------------------------------------------------------------- main loop
def load_axis(path):
    raw = json.load(open(path))
    seq = raw.get("axis", raw) if isinstance(raw, dict) else raw
    return [tuple(p) for p in seq]

def main():
    ap = argparse.ArgumentParser(description="EuroAirport RWY15 departure compliance poller")
    ap.add_argument("--source", choices=["lol", "fi"], default="lol")
    ap.add_argument("--range", type=int, default=20, help="search radius, nm")
    ap.add_argument("--poll",  type=float, default=3.0, help="seconds between polls (>=1)")
    ap.add_argument("--width", type=int, default=800, help="corridor full width, m")
    ap.add_argument("--alt",   type=int, default=6000, help="only evaluate below this alt, ft")
    ap.add_argument("--all-traffic", action="store_true", help="don't require climbing")
    ap.add_argument("--axis", help="JSON file: [[lat,lon],...] or an exported session.json")
    ap.add_argument("--data-dir", default="eap_data")
    ap.add_argument("--finalize-after", type=int, default=120, help="flight ends after N s unseen")
    ap.add_argument("--report", action="store_true", help="print stats and exit")
    args = ap.parse_args()

    if args.report:
        report(args.data_dir); return

    cfg = {"source": args.source, "range": args.range, "width": args.width, "alt": args.alt,
           "dep_only": not args.all_traffic, "axis": DEFAULT_AXIS, "max_pts": 600,
           "max_flights": 800}
    if args.axis:
        cfg["axis"] = load_axis(args.axis)
        print(f"Loaded calibrated axis ({len(cfg['axis'])} points) from {args.axis}")

    os.makedirs(args.data_dir, exist_ok=True)
    sess_path = os.path.join(args.data_dir, "session.json")
    cum_path  = os.path.join(args.data_dir, "cumulative.json")
    csv_path  = os.path.join(args.data_dir, "flights.csv")
    new_csv   = not os.path.exists(csv_path)
    csv_fp = open(csv_path, "a")
    if new_csv:
        csv_fp.write("finalized,callsign,hex,departure,result,max_south_m,min_alt_ft,points,first_seen,"
                     "max_abs_offset_m,mean_abs_offset_m,side,overflew_allschwil,min_dist_allschwil_m\n")
        csv_fp.flush()
    raw_fp = open(os.path.join(args.data_dir, f"raw_{datetime.now():%Y%m%d}.jsonl"), "a")

    tracks = {}
    finalized = load_cumulative(sess_path)   # restore previous session's flights on restart
    cum_flights = load_cumulative(cum_path)
    print(f"Resuming: {len(finalized)} flights from previous session, {len(cum_flights)} cumulative")
    interval = max(1.0, args.poll)
    running = {"go": True}
    def stop(*_): running["go"] = False
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)

    print(f"Polling {api_url(cfg)}  every {interval:g}s — Ctrl+C to stop. Data → {args.data_dir}/")
    last_session = 0.0
    while running["go"]:
        t0 = time.time()
        try:
            ac = fetch(api_url(cfg))
            ingest(ac, tracks, cfg, raw_fp)
            print(f"\r{datetime.now():%H:%M:%S}  aircraft:{len(ac):<3} tracking:{len(tracks):<3}"
                  f" dep:{sum(t.is_dep for t in tracks.values()):<3}"
                  f" south:{sum(t.is_dep and t.worst=='south' for t in tracks.values())}   ",
                  end="", flush=True)
        except urllib.error.HTTPError as e:
            print(f"\n[{datetime.now():%H:%M:%S}] HTTP {e.code} (rate limit? back off).")
        except Exception as e:
            print(f"\n[{datetime.now():%H:%M:%S}] feed error: {e}")

        now = time.time()
        for hexid in [h for h, t in tracks.items() if now - t.last > args.finalize_after]:
            t = tracks.pop(hexid)
            if t.is_dep:
                finalize(t, csv_fp, cfg["axis"]); finalized.append(t)
                finalized[:] = finalized[-cfg["max_flights"]:]
                cum_flights.append(_track_dict(t))
        if now - last_session >= 5:
            write_session(sess_path, tracks, finalized, cfg)
            write_cumulative(cum_path, cum_flights, cfg)
            last_session = now

        time.sleep(max(0.0, interval - (time.time() - t0)))

    # clean shutdown: finalize everything, flush snapshot
    for t in tracks.values():
        if t.is_dep:
            finalize(t, csv_fp, cfg["axis"]); finalized.append(t)
            cum_flights.append(_track_dict(t))
    write_session(sess_path, {}, finalized, cfg)
    write_cumulative(cum_path, cum_flights, cfg)
    csv_fp.close(); raw_fp.close()
    print("\nStopped. session.json + flights.csv written. Import session.json into the map to review.")

if __name__ == "__main__":
    main()
