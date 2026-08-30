#!/usr/bin/env python3
"""
Build a "today only" eap_data/session.json for the public GitHub Pages demo.

Sourced from eap_data/cumulative.json (the uncapped, append-only record of
every finalized flight) rather than session.json, since session.json is a
capped rolling buffer that the local poller keeps overwriting live — reading
straight from it risks a torn read if the poller is writing at the same
moment. cumulative.json is untouched by the poller's live view and is not
committed to git, so this script never mutates it.

Run this right before committing/pushing a snapshot for the public demo —
it keeps the published file small and shows only "today's" traffic instead
of the full rolling multi-day buffer.

Usage
-----
    python3 publish_today.py              # write today-only eap_data/session.json
    python3 publish_today.py --dry-run    # just report what would change
    python3 publish_today.py --tz UTC     # use a different timezone boundary
"""
import argparse, json, os
from datetime import datetime
from zoneinfo import ZoneInfo


def flight_date(flight, tz):
    track = flight.get("track")
    if not track:
        return None
    return datetime.fromtimestamp(track[0][0] / 1000, tz).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="eap_data")
    ap.add_argument("--tz", default="Europe/Zurich", help="timezone used to define 'today' (default: airport local time)")
    ap.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = ap.parse_args()

    tz = ZoneInfo(args.tz)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    cum_path = os.path.join(args.data_dir, "cumulative.json")
    out_path = os.path.join(args.data_dir, "session.json")

    with open(cum_path) as f:
        cum = json.load(f)

    before = cum["flights"]
    after = [f for f in before if flight_date(f, tz) == today]

    print(f"'today' = {today} ({args.tz})")
    print(f"flights (cumulative -> today-only): {len(before)} -> {len(after)}")

    if args.dry_run:
        return

    out = {
        "airport": cum.get("airport", "LFSB/BSL"),
        "exported": datetime.now(tz).isoformat(timespec="seconds"),
        "axis": cum["axis"],
        "rule": cum["rule"],
        "flights": after,
    }
    payload = json.dumps(out)

    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)

    # verify what actually landed on disk before trusting it
    with open(out_path) as f:
        written = f.read()
    if written != payload:
        raise SystemExit(
            f"Integrity check failed: {out_path} on disk does not match what "
            f"was just written ({len(written)} vs {len(payload)} bytes). "
            "Something else is touching this file (editor autosave? sync client?) "
            "-- close anything with it open and re-run."
        )
    print(f"Wrote and verified {out_path} ({len(payload)} bytes, {len(after)} flights)")


if __name__ == "__main__":
    main()
