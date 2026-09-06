import { buildLiveState } from "./live.js";
import { buildReplayState } from "./replay.js";

// Roughly matches what you were getting from the GitHub Action, but tighter
// since a Durable Object alarm isn't limited to cron's 1-minute floor.
const LIVE_TICK_MS = 4500; // one tick every ~4-5s while a real session is live
const REPLAY_TICK_MS_MIN = 2000;
const REPLAY_TICK_MS_MAX = 3000; // replay has no upstream feed to be gentle with

/**
 * One Durable Object instance (see index.js -- always addressed by the same
 * name, so there's only ever one) does the whole "poller" job that used to
 * live in the GitHub Action:
 *
 *   - alarm() fires every few seconds, forever, and re-schedules itself
 *     each time. This is what makes sub-minute polling possible on
 *     Cloudflare at all -- Cron Triggers can't go below 1 minute, but a
 *     Durable Object alarm can be set for any point in time, including a
 *     few seconds out, and the platform will wake the object up for it
 *     even if it's been idle/evicted in between.
 *   - Each tick calls live.js first (F1's real feed); if no session is
 *     live, it falls back to replay.js (OpenF1), exactly like poller.py's
 *     build_state() used to.
 *   - The result is stored in this object's own durable storage as
 *     "state" (what GET /state.json serves) and "cache" (cross-tick state
 *     -- best laps, track outline, etc. -- the same role the local
 *     .poller_cache.json file used to play).
 *
 * Your laptop's Django app fetches GET https://<your-worker>/state.json
 * on the same short cache/poll cycle it used to fetch the GitHub Pages
 * URL with -- see f1panel/settings.py's WORKER_STATE_URL.
 */
export class TimingPoller {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === "/state.json") {
      if (!this._authorized(request, url)) return new Response("Unauthorized", { status: 401 });
      await this.ensureRunning();
      const state = (await this.state.storage.get("state")) || {
        ok: false,
        reason: "No tick has completed yet -- try again in a few seconds.",
      };
      return new Response(JSON.stringify(state), {
        headers: {
          "content-type": "application/json",
          // Django is the one fetching this, but keep it harmless to load
          // directly in a browser too (e.g. to sanity-check the URL).
          "access-control-allow-origin": "*",
          "cache-control": "no-store",
        },
      });
    }

    if (url.pathname === "/tick") {
      // Manual/debug: run one tick right now and return exactly what it
      // produced, regardless of the alarm schedule.
      if (!this._authorized(request, url)) return new Response("Unauthorized", { status: 401 });
      const state = await this.tick();
      return new Response(JSON.stringify(state, null, 2), { headers: { "content-type": "application/json" } });
    }

    if (url.pathname === "/ping") {
      // Cheap keep-alive hit from the Worker's cron trigger (see
      // wrangler.toml) -- self-healing in case the alarm chain ever breaks.
      await this.ensureRunning();
      return new Response("ok");
    }

    return new Response(
      "F1 Commentator's Panel timing worker.\nGET /state.json for the published dashboard state.\n",
      { status: 200, headers: { "content-type": "text/plain" } }
    );
  }

  _authorized(request, url) {
    const token = this.env.STATE_TOKEN;
    if (!token) return true; // no token configured -- open, same as the old GitHub Pages URL was
    const header = request.headers.get("Authorization") || "";
    const provided = header.replace(/^Bearer\s+/i, "") || url.searchParams.get("token") || "";
    return provided === token;
  }

  async ensureRunning() {
    const alarm = await this.state.storage.getAlarm();
    if (alarm === null) {
      // Fresh object (first-ever request) or the alarm chain fell over --
      // start it now instead of waiting for the next cron ping.
      await this.tick();
    }
  }

  async alarm() {
    await this.tick();
  }

  async tick() {
    const cache = (await this.state.storage.get("cache")) || {};
    let result;
    try {
      result = (await buildLiveState(cache)) || (await buildReplayState(cache));
    } catch (exc) {
      result = { state: { ok: false, reason: `Poller error: ${exc}` }, cache };
    }

    let { state, cache: newCache } = result;
    state.generated_at = new Date().toISOString();

    if (!state.ok) {
      // Don't overwrite good data with nothing -- merge onto whatever was
      // last published so the dashboard shows slightly-stale data instead
      // of blanking out, same behavior poller.py's main() had.
      const previous = await this.state.storage.get("state");
      if (previous) {
        state = { ...previous, ok: false, reason: state.reason, generated_at: state.generated_at };
      }
    }

    await this.state.storage.put("state", state);
    await this.state.storage.put("cache", newCache);

    const delay =
      state.mode === "replay"
        ? REPLAY_TICK_MS_MIN + Math.random() * (REPLAY_TICK_MS_MAX - REPLAY_TICK_MS_MIN)
        : LIVE_TICK_MS;
    await this.state.storage.setAlarm(Date.now() + delay);

    return state;
  }
}
