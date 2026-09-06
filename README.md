# F1 Commentator's Panel

A phone-friendly live F1 dashboard: running order, gaps, tyres, race
control, weather, and a "what if he pits this lap" position projector.

## How it works

```
F1's raw live timing feed (falls back to OpenF1 replay when nothing's live)
        │
        ▼
Cloudflare Worker (cf-worker/) polls the feed every 4-5 seconds via a
   Durable Object alarm and serves one combined state.json -- this
   replaces the old GitHub Action + gh-pages publishing step
        │
        ▼
Django, running on your laptop (unchanged -- same views, same template,
   same pit-projection logic), fetches that URL every second or so
   instead of talking to F1 or OpenF1 directly
        │
        ▼
Your phone, on the same wifi as your laptop
```

This replaces OpenF1 (which paywalls real-time access) with your own
free pipeline: the worker does the actual scraping/parsing of F1's feed,
your laptop just serves the polished dashboard from that.

**No live session running?** The worker automatically falls back to
replaying the most recently completed session from OpenF1's free
(historical) API instead of publishing an empty dashboard -- see
"Replay mode" below.

**Latency, honestly**: this is not the same as a direct live connection,
but it's a lot tighter than the old git-commit-based pipeline. Each
update is just poll → store in the Durable Object → Django fetches it
straight over HTTP, so you should see low single-digit seconds of lag,
not the ~30-60s the GitHub Pages version could drift to. The dashboard
header still shows exactly how stale the data is (`data Ns old`) so
you're never guessing.

**Why Cloudflare and not GitHub Actions**: GitHub Actions/Cron can't
usefully poll faster than about once a minute for a scheduled job, and
a manually-started Action job is capped at 6 hours and needs restarting
per session. A Cloudflare Worker with a Durable Object alarm has no such
floor -- it ticks itself every 4-5 seconds indefinitely once started (see
`cf-worker/README.md` for exactly how), so there's nothing to remember to
kick off before a session.

**IP risk**: same caveat as before, just with a different set of IPs --
Cloudflare's edge network fetches F1's feed now instead of a GitHub-hosted
runner. If the worker's fetches start failing (check with `npx wrangler
tail`), that's the likeliest reason.

**Bandwidth/CPU note**: the track map's car-position feed is F1's
highest-frequency data stream. The worker only decompresses lines it
hasn't seen yet (not the whole growing file every tick), so CPU cost
stays roughly flat through a session, but it still downloads that
growing file in full on every tick, so bandwidth use climbs as a session
goes on. Not a real problem on Cloudflare's free tier for a single
session, just worth knowing.

## One-time setup

### 1. Deploy the Cloudflare Worker

See [`cf-worker/README.md`](cf-worker/README.md) for the full walkthrough
(`npm install`, `wrangler login`, `wrangler deploy`, optionally pointing
your own domain at it). At the end you'll have a URL like
`https://f1.yourdomain.com/state.json` (or a `*.workers.dev` one) that's
already updating itself every few seconds -- nothing to start manually
per race weekend.

### 2. Point your laptop's Django app at it

Edit `f1panel/settings.py`:

```python
WORKER_STATE_URL = "https://f1.yourdomain.com/state.json"
```

If you set a `STATE_TOKEN` secret on the worker (optional, see
`cf-worker/README.md`), also set:

```python
WORKER_STATE_TOKEN = "same value as the worker's STATE_TOKEN secret"
```

### 3. Install and run Django, same as always

```bash
cd f1panel   # this directory, not the inner f1panel/ package folder
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

1. Nothing to start on the worker side -- it's been polling continuously
   since you deployed it (falling back to replay mode between sessions).
2. Start (or leave running) `python manage.py runserver 0.0.0.0:8000` on
   your laptop.
3. Open the dashboard on your phone. Watch the `data Ns old` indicator in
   the header -- if it climbs and stays high, check `npx wrangler tail`
   from `cf-worker/` for errors (F1's feed may be blocking Cloudflare's
   IPs, or the feed shape may have changed).

## Replay mode (no live session)

Every tick, the worker first checks F1's own feed for a live session. If
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
  instead of every 4-5 -- since it's just replaying already-fetched data
  rather than scraping a live feed gently.
- It keeps re-checking F1's own feed every tick, so if a real session
  goes live while you're looking at a replay, the very next tick
  switches over to live data by itself -- you don't need to restart
  anything.
- How fast the replayed session plays back (in virtual seconds per tick)
  is `REPLAY_STEP_SECONDS` (default `25`) near the top of
  `cf-worker/src/replay.js` -- raise it to blow through a session faster,
  lower it to linger. Redeploy (`wrangler deploy`) after changing it.
- The track map in replay mode is best-effort, same as live: OpenF1's
  car-position feed is fetched incrementally as the replay clock moves,
  so it fills in gradually rather than all at once.
- Want to replay something other than "whatever OpenF1 last has" -- a
  specific past race, a custom/simulated session, a recording of your
  own? Any producer that writes a `state.json` matching the schema in
  [`docs/STATE_SCHEMA.md`](docs/STATE_SCHEMA.md) is a valid drop-in
  source; `cf-worker/src/replay.js` is just one example.

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
  the worker, so it works even if the worker stalls. Needs the circuit to
  be in `CIRCUIT_COORDINATES` in `cf-worker/src/circuits.js` — most
  current-calendar circuits are included; add missing ones there (lat/lon
  of the venue is plenty precise for this).
- **Track map** — dots showing where each car currently is, drawn on top
  of an outline that traces itself in from car GPS data as the session
  runs (it takes a lap or two of data to look like a track). This is the
  least certain part of the whole project: F1 compresses this feed and
  the exact wrapping format was reverse-engineered from community
  write-ups rather than an official spec. If cars never appear:
  - Run `npx wrangler tail` from `cf-worker/` and hit `/tick` on the
    worker to see errors around the position-feed code live.
  - Hit `GET /tick` on the worker directly (needs a live or very recently
    completed session) and inspect the JSON it returns; adjust
    `extractPositionFrames()` / `decompressZValue()` in
    `cf-worker/src/live.js` to match what F1's feed is actually sending.
  - Orientation is schematic (not rotated/mirrored to match the TV
    broadcast) and one car's positions get sampled at a time to build
    the outline, so the shape firms up gradually rather than instantly.
- **Race control** — flags, safety car, investigations, incident
  messages, most recent first.
- **Pit stops** — which lap each driver pitted on and what tyre they
  fitted, inferred from tyre-stint changes. No stop/lane duration is
  shown: F1's dedicated pit-timing feed has an inconsistent enough
  schema across tools that it wasn't worth guessing at field names here
  (see the comment at the top of `cf-worker/src/live.js` if you want to
  add it).

## Repo layout

```
cf-worker/                       Cloudflare Worker that replaces the old GitHub
                                  Action -- polls F1's feed (or OpenF1 replay) via
                                  a Durable Object alarm and serves state.json.
                                  See cf-worker/README.md for deployment.
docs/STATE_SCHEMA.md             The state.json contract -- read this to plug in
                                  another data source (custom races, simulations, ...)
f1panel/                         Django project (settings, URLs, WSGI)
timing/                          The app: services.py (fetches state.json),
                                  views.py, and the dashboard template
requirements.txt                 Django + requests, for the local app
```

## Adjusting behavior

- `WORKER_STATE_URL` / `WORKER_STATE_TOKEN` / `WORKER_STATE_CACHE_SECONDS`
  in `f1panel/settings.py` — where to fetch published data from, the
  optional shared-secret token, and how long Django caches a response
  before re-fetching.
- `DEFAULT_PIT_LOSS_SECONDS` / `CIRCUIT_PIT_LOSS_SECONDS` in the same
  file — used by the pit projection tool.
- Poll interval on the worker side — `LIVE_TICK_MS` /
  `REPLAY_TICK_MS_MIN` / `REPLAY_TICK_MS_MAX` at the top of
  `cf-worker/src/durable-object.js`. Lower = fresher data, more Worker
  invocations used. Redeploy after changing it.
- How fast replay mode plays back a session — `REPLAY_STEP_SECONDS` in
  `cf-worker/src/replay.js` (see "Replay mode" above).
- Polling frequency on the phone side — `TIMING_POLL_MS` / `SLOW_POLL_MS`
  in `timing/templates/timing/dashboard.html`.
