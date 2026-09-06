# F1 Commentator's Panel

A phone-friendly live F1 dashboard: running order, gaps, tyres, race
control, weather, and a "what if he pits this lap" position projector.

## How it works

```
F1's raw live timing feed
        │
        ▼
GitHub Action (poller.py, runs in the cloud -- you start it, it needs
   no laptop) polls the feed in a loop and publishes one combined
   state.json to this repo's gh-pages branch
        │
        ▼
GitHub Pages serves that file at
   https://<you>.github.io/<repo>/state.json
        │
        ▼
Django, running on your laptop (unchanged from before -- same views,
   same template, same pit-projection logic), fetches that URL every
   few seconds instead of talking to F1 or OpenF1 directly
        │
        ▼
Your phone, on the same wifi as your laptop
```

This replaces OpenF1 (which paywalls real-time access) with your own
free pipeline: the Action does the actual scraping/parsing of F1's feed,
your laptop just serves the polished dashboard from that.

**No live session running when you start the Action?** It automatically
falls back to replaying the most recently completed session from
OpenF1's free (historical) API instead of publishing an empty
dashboard -- see "Replay mode" below.

**Latency, honestly**: this is not the same as a direct live connection.
Each update has to go poll → write file → git commit/push → GitHub Pages
picks it up. In practice that's usually low single-digit seconds to
maybe ~30-60s behind live, not instant. The dashboard header shows you
exactly how stale the data is (`data Ns old`) so you're never guessing.

**IP risk**: GitHub Actions runners use cloud/datacenter IP ranges.
Some CDNs block those to deter scraping bots; F1's feed may or may not.
If the poller's fetches start failing (check the Action's logs), that's
the likely reason, and there's no fix from this end besides trying at a
different time or from a self-hosted runner.

**Bandwidth/CPU note**: the track map's car-position feed is F1's
highest-frequency data stream. The poller only decompresses lines it
hasn't seen yet (not the whole growing file every tick), so CPU cost
stays roughly flat through a session, but it still downloads that
growing file in full on every tick, so bandwidth use climbs as a session
goes on. Not a real problem for a single session on a GitHub-hosted
runner, just worth knowing.

**Session length**: GitHub caps any single job at 6 hours. The workflow
runs for ~5.5 hours per invocation, so start it manually a few minutes
before each session (Actions tab → "F1 Live Poller" → Run workflow --
you can do this from the GitHub mobile app, no laptop needed). It won't
run unattended all race weekend; you kick it off per-session, or add
your own `schedule:` cron entries in the workflow file if you want it
automatic (see the comments in `.github/workflows/poll.yml`).

## One-time setup

### 1. Push this to a GitHub repo

Create a new repo (public -- public repos get unlimited free Actions
minutes; private repos get a limited free monthly budget) and push
everything in this folder to it.

### 2. Enable GitHub Pages

Repo → **Settings → Pages → Source: Deploy from a branch → Branch:
`gh-pages` / `/(root)`**.

The `gh-pages` branch doesn't exist yet -- that's fine, the workflow
creates it automatically the first time it runs. Come back and set this
after the first successful run if the branch isn't selectable yet.

### 3. Run the poller once to publish initial data

Repo → **Actions → F1 Live Poller → Run workflow**. Give it a minute,
then check `https://<you>.github.io/<repo>/` -- it should show a small
status page confirming `state.json` is being published (there's a human-
readable status page at `docs-source/index.html`, copied into the
published branch automatically).

### 4. Point your laptop's Django app at it

Edit `f1panel/settings.py`:

```python
GITHUB_STATE_URL = "https://<you>.github.io/<repo>/state.json"
```

### 5. Install and run Django, same as always

```bash
cd f1panel
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Find your laptop's LAN IP (Mac: System Settings → Wi-Fi → Details;
Windows: `ipconfig`; Linux: `ip addr`), then on your phone (same wifi):

```
http://<your-laptop-LAN-IP>:8000/
```

## On race day

1. A few minutes before the session, open the GitHub app (or website) on
   your phone → your repo → Actions → **F1 Live Poller** → Run workflow.
2. Start (or leave running) `python manage.py runserver 0.0.0.0:8000` on
   your laptop.
3. Open the dashboard on your phone. Watch the `data Ns old` indicator in
   the header -- if it climbs past ~30-60s and stays there, check the
   Action's logs (it may have hit the session-length cap, or F1's feed
   may be blocking the runner's IP).

## Replay mode (no live session)

Every tick, the poller first checks F1's own feed for a live session. If
there isn't one, it fetches the most recently completed session from
[OpenF1](https://openf1.org)'s free API and replays it: a virtual clock
steps forward through that session a bit each tick, and `state.json` is
published as if that much of the session had just happened. That gives
you a fully populated dashboard to test the panel against, or just to
have something on screen, between real sessions -- instead of a blank
"no live session found" screen.

- The dashboard shows a small **REPLAY** badge next to the header dot
  whenever this is happening, along with how far through the replayed
  session it's gotten and how many times it's looped back to the start.
- Replay mode publishes faster than live mode -- every 2-3 seconds
  instead of every 8 -- since it's just replaying already-fetched data
  rather than scraping a live feed gently.
- It keeps re-checking F1's own feed every tick, so if a real session
  goes live while you're looking at a replay, the very next tick
  switches over to live data by itself -- you don't need to restart
  anything.
- How fast the replayed session plays back (in virtual seconds per
  tick) is controlled by the `REPLAY_STEP_SECONDS` env var in
  `replay.py` (default `25`) -- raise it to blow through a session
  faster, lower it to linger.
- The track map in replay mode is best-effort, same as live: OpenF1's
  car-position feed is fetched incrementally as the replay clock moves,
  so it fills in gradually rather than all at once.
- Want to replay something other than "whatever OpenF1 last has" --
  a specific past race, a custom/simulated session, a recording of your
  own? Any script that writes a `state.json` matching the schema in
  [`docs/STATE_SCHEMA.md`](docs/STATE_SCHEMA.md) is a valid drop-in
  source; `replay.py` is just one example of a producer for that
  schema.

## What each part of the panel shows

- **Timing table** — position, gap to leader, interval to the car ahead,
  last lap time (purple highlight if it's their personal best), current
  tyre compound + age in laps. Tap a driver's row to run the pit
  projection below.
- **Pit projection** — tapping a driver estimates where they'd rejoin if
  they pitted *this lap*, using this circuit's typical pit lane loss
  (edit `CIRCUIT_PIT_LOSS_SECONDS` in `settings.py`). This is a heuristic
  for commentary, not a strategy tool: it ignores traffic, fuel-corrected
  pace changes, and whether other cars pit too.
- **Weather** — track/air temp, humidity, wind, rain flag.
- **Rain radar** — a live map centered on the circuit with a RainViewer
  precipitation overlay. This loads directly in your phone's browser
  using your phone's own internet connection, not through your laptop or
  the poller, so it works even if the poller stalls. Needs the circuit
  to be in `CIRCUIT_COORDINATES` in `poller.py` — most current-calendar
  circuits are included; add missing ones there (lat/lon of the venue is
  plenty precise for this).
- **Track map** — dots showing where each car currently is, drawn on top
  of an outline that traces itself in from car GPS data as the session
  runs (it takes a lap or two of data to look like a track). This is the
  least certain part of the whole project: F1 compresses this feed and
  the exact wrapping format was reverse-engineered from community
  write-ups rather than an official spec. If cars never appear:
  - Check the Action's logs for errors around the position-feed code.
  - Run `python poller.py --debug-position` locally (needs a live or
    very recently completed session) to print the raw shape of a line
    from that feed, and adjust `_extract_position_frames()` /
    `_decompress_z_value()` in `poller.py` to match what you see.
  - Orientation is schematic (not rotated/mirrored to match the TV
    broadcast) and one car's positions get sampled at a time to build
    the outline, so the shape firms up gradually rather than instantly.
- **Race control** — flags, safety car, investigations, incident
  messages, most recent first.
- **Pit stops** — which lap each driver pitted on and what tyre they
  fitted, inferred from tyre-stint changes. No stop/lane duration is
  shown: F1's dedicated pit-timing feed has an inconsistent enough
  schema across tools that it wasn't worth guessing at field names here
  (see the comment at the top of `poller.py` if you want to add it --
  there's a spot to plug in a raw dump of that feed for a specific
  session to reverse-engineer the real fields).

## Repo layout

```
poller.py                        Standalone script the Action runs each "tick":
                                  checks F1's raw feed and either parses it (live)
                                  or falls back to replay.py, then writes state.json
replay.py                        Fallback data source: replays the last completed
                                  session from OpenF1 when nothing is live
circuits.py                      Shared circuit lat/lon table (used by both
                                  poller.py and replay.py, for the rain radar map)
docs/STATE_SCHEMA.md             The state.json contract -- read this to plug in
                                  another data source (custom races, simulations, ...)
docs-source/index.html           Tiny human status page (copied into gh-pages)
.github/workflows/poll.yml       The Action: loops poller.py, commits/pushes to gh-pages
f1panel/                         Django project (settings, URLs, WSGI)
timing/                          The app: services.py (fetches state.json),
                                  views.py, and the dashboard template
requirements.txt                 Django + requests, for the local app only
                                  (poller.py/replay.py only need requests, installed
                                  separately inside the workflow)
```

## Adjusting behavior

- `GITHUB_STATE_URL` / `GITHUB_STATE_CACHE_SECONDS` in
  `f1panel/settings.py` — where to fetch published data from, and how
  often.
- `DEFAULT_PIT_LOSS_SECONDS` / `CIRCUIT_PIT_LOSS_SECONDS` in the same
  file — used by the pit projection tool.
- Poll/commit interval on the Action side — the `SLEEP=8` (live) /
  `SLEEP=$(( (RANDOM % 2) + 2 ))` (replay) lines in
  `.github/workflows/poll.yml`. Lower = fresher data, more commits, more
  Action minutes used.
- How fast replay mode plays back a session — `REPLAY_STEP_SECONDS` in
  `replay.py` (see "Replay mode" above).
- Polling frequency on the phone side — `TIMING_POLL_MS` / `SLOW_POLL_MS`
  in `timing/templates/timing/dashboard.html`.
