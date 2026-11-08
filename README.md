# EAP RWY15 Departure Compliance

Monitors south departures off EuroAirport (LFSB) runway 15 using community ADS-B
data, and measures how far each flight sits from the expected corridor.

**Live site:** https://elmuchotomato.github.io/flightracker/

This is a transparency and monitoring tool, not a penalty calculator. LFSB has no
formally defined lateral protection volume, so its "violation" flag means "outside
the expected corridor". The default corridor axis is an estimate: confirm it against
the published procedure before trusting absolute offsets. See the notes in
`corridor_dispersion.py`.

## Layout

```
index.html               redirects to viewer.html
viewer.html              map and compliance view (the published site)
eap_poller.py            polls adsb.lol or adsb.fi and logs flights to eap_data/
corridor_dispersion.py   lateral dispersion and Allschwil overflight analysis
publish_today.py         builds a today-only eap_data/session.json for the site
eap_data/session.json    the only data file committed; everything else there is ignored
```

## Running

Needs Python 3.9 or newer and nothing else, since the scripts only use the standard
library. Run them from the repo root.

```bash
python3 eap_poller.py            # poll until Ctrl+C
python3 eap_poller.py --report   # print stats from flights.csv
python3 publish_today.py         # refresh the published snapshot before committing
```
