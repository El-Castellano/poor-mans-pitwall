# F1 timing worker (Cloudflare)

Replaces the old GitHub Action + `gh-pages` publishing step. This Worker
polls F1's live timing feed itself (falling back to an OpenF1 replay when
nothing's live, exactly like `poller.py`/`replay.py` used to) and serves
one JSON file your laptop's Django app fetches:

```
OpenF1 / F1 live timing  --polled by-->  Cloudflare Worker  --fetched by-->  Django (laptop)  -->  phone
```

## Why a Durable Object, not a Cron Trigger

Cloudflare Cron Triggers can't fire more often than once a minute, and you
want updates every 4-5 seconds. A [Durable Object
Alarm](https://developers.cloudflare.com/durable-objects/api/alarms/) has
no such floor -- `src/durable-object.js` schedules its own next alarm a
few seconds out every time one fires, so it just keeps ticking forever
once started, waking back up even after being idle. The `crons` entry in
`wrangler.toml` is only a once-a-minute safety net that restarts the loop
if it were ever interrupted -- it isn't what drives normal updates.

## One-time setup

### 1. Install and log in

```bash
cd cf-worker
npm install
npx wrangler login
```

### 2. Deploy

```bash
npx wrangler deploy
```

This prints a `*.workers.dev` URL -- something like
`https://f1panel-timing.<your-subdomain>.workers.dev`. That already works
end-to-end; the custom domain step below is optional polish.

### 3. (Optional) put it on your own domain

If you've already got a domain on Cloudflare, so in
`wrangler.toml` uncomment and edit:

```toml
[[routes]]
pattern = "f1.yourdomain.com/*"
custom_domain = true
```

then `npx wrangler deploy` again.

### 4. (Optional) lock it down with a shared token

The old GitHub Pages URL was public by nature; this doesn't have to be.
To require a token on every request:

```bash
npx wrangler secret put STATE_TOKEN
```

Pick any random string. Once set, requests to `/state.json` need either
`?token=<value>` or an `Authorization: Bearer <value>` header, or they get
a 401. Put the same value in Django's `WORKER_STATE_TOKEN` setting (see
the root README / `f1panel/settings.py`).

### 5. Start it

Just hit the URL once:

```bash
curl https://f1.yourdomain.com/state.json
```

The first request to a fresh Durable Object has no alarm scheduled yet,
so it runs one tick synchronously and returns it, then the alarm loop
takes over and it keeps updating itself indefinitely -- no need to "start"
anything before a race weekend the way the old Action required. If no F1
session is live, you'll immediately see `"mode": "replay"` data instead
of an empty response.

## Endpoints

- `GET /state.json` -- what Django fetches. The full `state.json` payload
  described in `../docs/STATE_SCHEMA.md`.
- `GET /tick` -- runs one tick immediately and returns it (bypasses the
  alarm schedule). Handy for debugging without waiting.
- `GET /ping` -- cheap keep-alive used by the cron trigger; also usable
  manually to make sure the alarm loop is running.

## Debugging

```bash
npx wrangler tail
```

streams live logs while you hit `/tick` or `/state.json`, which is the
easiest way to see exceptions from a bad feed shape (e.g. if F1 changes
the `Position.z` wrapping -- see the confidence caveat at the top of
`src/live.js`).

## Files

```
src/index.js           Worker entry point -- routes every request to the
                        one TimingPoller Durable Object instance
src/durable-object.js   The poll loop: alarm() ticks every few seconds,
                        stores the latest state + cross-tick cache
src/live.js             Parses F1's raw live timing feed (port of the old
                        poller.py)
src/replay.js           OpenF1 replay fallback (port of the old replay.py)
src/circuits.js         Circuit lat/lon table (port of circuits.py)
wrangler.toml           Worker + Durable Object + cron config
```
