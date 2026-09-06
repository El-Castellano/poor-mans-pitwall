/**
 * Fallback data source used when F1's own live feed (live.js) reports no
 * session currently running. Pulls the most recently completed session
 * from OpenF1's free public API (https://openf1.org) and "replays" it: each
 * tick advances a virtual clock through that session's timeline. Direct
 * port of the root project's replay.py -- see docs/STATE_SCHEMA.md for the
 * JSON contract both this and live.js produce.
 *
 * CONFIDENCE CAVEAT, same spirit as live.js's Position.z caveat: written
 * against publicly documented OpenF1 fields but not exercised against a
 * live response. Sprint weekends / red-flagged sessions are the likeliest
 * to differ -- fix it here without touching live.js or the schema.
 */

import { CIRCUIT_COORDINATES } from "./circuits.js";

const OPENF1_BASE = "https://api.openf1.org/v1";

// How many virtual (in-session) seconds each tick advances the replay by.
// The worker ticks every 2-3 real seconds in replay mode (see
// durable-object.js), so 25 virtual seconds/tick plays a ~90 minute
// session back in a few minutes of wall-clock time.
const REPLAY_STEP_SECONDS = 25;

const MAX_OUTLINE_POINTS = 3000;
const OUTLINE_SAMPLE_EVERY = 4;
const MAX_LOCATION_FETCH_WINDOW_SECONDS = 120;

// --- OpenF1 REST client -----------------------------------------------------

async function get(endpoint, params = {}, rawFilters = []) {
  const query = Object.entries(params)
    .map(([k, v]) => `${k}=${v}`)
    .join("&");
  const parts = [query, ...rawFilters].filter(Boolean);
  const url = `${OPENF1_BASE}/${endpoint}${parts.length ? "?" + parts.join("&") : ""}`;
  try {
    const resp = await fetch(url);
    if (!resp.ok) return [];
    const data = await resp.json();
    return Array.isArray(data) ? data : [];
  } catch (err) {
    return [];
  }
}

async function findLastSession() {
  const rows = await get("sessions", { session_key: "latest" });
  return rows[0] || null;
}

async function findMeeting(meetingKey) {
  const rows = await get("meetings", { meeting_key: meetingKey });
  return rows[0] || null;
}

async function fetchSessionDataset(sessionKey) {
  const [driversRaw, lapsRaw, classRaw, intervalsRaw, stintsRaw, pitsRaw, rcRaw, weatherRaw] = await Promise.all([
    get("drivers", { session_key: sessionKey }),
    get("laps", { session_key: sessionKey }),
    get("position", { session_key: sessionKey }),
    get("intervals", { session_key: sessionKey }),
    get("stints", { session_key: sessionKey }),
    get("pit", { session_key: sessionKey }),
    get("race_control", { session_key: sessionKey }),
    get("weather", { session_key: sessionKey }),
  ]);

  const drivers = {};
  for (const d of driversRaw) {
    if (d.driver_number === undefined || d.driver_number === null) continue;
    drivers[String(parseInt(d.driver_number, 10))] = {
      name_acronym: d.name_acronym ?? null,
      full_name: d.full_name ?? d.broadcast_name ?? null,
      team_name: d.team_name ?? null,
      team_colour: d.team_colour ?? null,
      headshot_url: d.headshot_url ?? null,
    };
  }

  const byDate = (key) => (a, b) => (a[key] || "").localeCompare(b[key] || "");

  return {
    drivers,
    laps: lapsRaw.filter((l) => l.date_start && l.driver_number != null).sort(byDate("date_start")),
    classifications: classRaw.filter((p) => p.date && p.driver_number != null).sort(byDate("date")),
    intervals: intervalsRaw.filter((i) => i.date && i.driver_number != null).sort(byDate("date")),
    stints: stintsRaw
      .filter((s) => s.driver_number != null)
      .sort((a, b) => a.driver_number - b.driver_number || (a.stint_number || 0) - (b.stint_number || 0)),
    pits: pitsRaw.filter((p) => p.date).sort(byDate("date")),
    race_control: rcRaw.filter((m) => m.date).sort(byDate("date")),
    weather: weatherRaw.filter((w) => w.date).sort(byDate("date")),
  };
}

async function fetchLocationWindow(sessionKey, dateFrom, dateTo) {
  return get("location", { session_key: sessionKey }, [`date>${dateFrom}`, `date<=${dateTo}`]);
}

// --- Small parsing helpers ---------------------------------------------

function parseIso(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

function latestAtOrBefore(records, dateKey, t, perKey = null) {
  if (perKey === null) {
    let best = null;
    for (const r of records) {
      const rd = parseIso(r[dateKey]);
      if (rd && rd <= t) best = r;
      else if (rd && rd > t) break;
    }
    return best;
  }
  const out = {};
  for (const r of records) {
    const rd = parseIso(r[dateKey]);
    if (rd && rd <= t) out[r[perKey]] = r;
    else if (rd && rd > t) break;
  }
  return out;
}

// --- Main entry point --------------------------------------------------------

/**
 * One replay tick. Returns {state, cache} -- never null, since replay is
 * itself the fallback (unlike live.js, which returns null to signal "try
 * the fallback instead").
 */
export async function buildReplayState(cache) {
  let r = cache.replay || {};
  let { dataset, session_meta: sessionMeta } = r;

  if (!dataset || !sessionMeta) {
    // First tick of a replay: pick a session and pull its whole dataset
    // once. Everything after this just replays what's already cached.
    const session = await findLastSession();
    if (!session || !session.session_key) {
      return { state: { ok: false, mode: "replay", reason: "OpenF1 has no session to replay right now." }, cache };
    }

    const sessionKey = session.session_key;
    const meeting = (await findMeeting(session.meeting_key)) || {};
    const latLon = CIRCUIT_COORDINATES[session.circuit_short_name];

    sessionMeta = {
      session_key: sessionKey,
      meeting_key: session.meeting_key,
      session_name: session.session_name ?? null,
      session_type: session.session_type ?? null,
      meeting_name: meeting.meeting_official_name ?? meeting.meeting_name ?? null,
      circuit_short_name: session.circuit_short_name ?? null,
      latitude: latLon ? latLon[0] : null,
      longitude: latLon ? latLon[1] : null,
      date_start: session.date_start,
      date_end: session.date_end,
    };
    dataset = await fetchSessionDataset(sessionKey);
    r = {
      session_meta: sessionMeta,
      dataset,
      virtual_time: sessionMeta.date_start,
      loop_count: 0,
      track_outline: [],
      last_car_positions: {},
      outline_sample_counter: 0,
    };
  }

  const sessionKey = sessionMeta.session_key;
  const tStart = parseIso(sessionMeta.date_start);
  const tEnd = parseIso(sessionMeta.date_end);
  const tPrev = parseIso(r.virtual_time) || tStart;

  if (!tStart || !tEnd || tStart >= tEnd) {
    return { state: { ok: false, mode: "replay", reason: "Replay session had no usable date_start/date_end from OpenF1." }, cache };
  }

  let tNext = new Date(tPrev.getTime() + REPLAY_STEP_SECONDS * 1000);
  const looped = tNext > tEnd;
  if (looped) {
    tNext = tStart;
    r.loop_count = (r.loop_count || 0) + 1;
    r.track_outline = [];
    r.last_car_positions = {};
    r.outline_sample_counter = 0;
  }

  const state = stateAt(sessionMeta, dataset, tNext);
  await advanceTrackMap(sessionKey, r, looped ? tStart : tPrev, tNext);

  r.virtual_time = tNext.toISOString();
  const newCache = {
    ...cache,
    replay: {
      session_meta: sessionMeta,
      dataset,
      virtual_time: r.virtual_time,
      loop_count: r.loop_count,
      track_outline: r.track_outline,
      last_car_positions: r.last_car_positions,
      outline_sample_counter: r.outline_sample_counter,
    },
  };

  state.track = { outline: r.track_outline, cars: r.last_car_positions };
  const progress = (tNext - tStart) / Math.max(tEnd - tStart, 1);
  state.replay = {
    source: "openf1",
    session_key: sessionKey,
    virtual_time: r.virtual_time,
    session_progress: Math.round(Math.min(Math.max(progress, 0), 1) * 10000) / 10000,
    loop_count: r.loop_count,
    note: "Historical data from OpenF1, replayed for testing/demo purposes -- not a live session.",
  };

  return { state, cache: newCache };
}

function stateAt(sessionMeta, dataset, t) {
  const classifications = latestAtOrBefore(dataset.classifications, "date", t, "driver_number");
  const positions = {};
  for (const [num, rec] of Object.entries(classifications)) {
    if (rec.position == null) continue;
    positions[String(parseInt(num, 10))] = parseInt(rec.position, 10);
  }

  const intervalRecs = latestAtOrBefore(dataset.intervals, "date", t, "driver_number");
  const intervals = {};
  for (const [num, rec] of Object.entries(intervalRecs)) {
    intervals[String(parseInt(num, 10))] = { gap_to_leader: rec.gap_to_leader ?? null, interval: rec.interval ?? null };
  }

  const lapRecs = latestAtOrBefore(dataset.laps, "date_start", t, "driver_number");
  const laps = {};
  const currentLapNumber = {};
  for (const [num, rec] of Object.entries(lapRecs)) {
    const key = String(parseInt(num, 10));
    laps[key] = { lap_duration: rec.lap_duration ?? null, lap_number: rec.lap_number ?? null };
    if (typeof rec.lap_number === "number") currentLapNumber[parseInt(num, 10)] = rec.lap_number;
  }
  // Best lap so far needs every lap up to t, not just each driver's latest one.
  const bestLaps = {};
  for (const rec of dataset.laps) {
    const recDate = parseIso(rec.date_start);
    if (!recDate || recDate > t) continue;
    if (rec.driver_number == null || typeof rec.lap_duration !== "number") continue;
    const key = String(parseInt(rec.driver_number, 10));
    if (!(key in bestLaps) || rec.lap_duration < bestLaps[key]) bestLaps[key] = rec.lap_duration;
  }

  const stints = {};
  const stintDriverNums = new Set(dataset.stints.filter((s) => s.driver_number != null).map((s) => s.driver_number));
  for (const numInt of stintDriverNums) {
    const driverStints = dataset.stints.filter((s) => s.driver_number === numInt);
    const lapNow = currentLapNumber[numInt] || 0;
    let current = null;
    for (const s of driverStints) {
      const lapStart = s.lap_start || 0;
      if (lapStart <= lapNow || current === null) current = s;
    }
    if (current) {
      let tyreAge = current.tyre_age_at_start;
      if (typeof tyreAge === "number" && current.lap_start) {
        tyreAge = tyreAge + Math.max(lapNow - current.lap_start, 0);
      }
      stints[String(numInt)] = { compound: current.compound ?? null, tyre_age: tyreAge ?? null, stint_number: current.stint_number ?? null };
    }
  }

  let pits = [];
  for (const rec of dataset.pits) {
    const recDate = parseIso(rec.date);
    if (!recDate || recDate > t || rec.driver_number == null) continue;
    pits.push({
      driver_number: parseInt(rec.driver_number, 10),
      lap_number: rec.lap_number ?? null,
      compound: null, // OpenF1's `pit` endpoint doesn't include the tyre fitted; see docs/STATE_SCHEMA.md
      lane_duration: rec.pit_duration ?? null,
      stop_duration: null, // not broken out separately by OpenF1
    });
  }
  pits.sort((a, b) => (b.lap_number || 0) - (a.lap_number || 0));

  let raceControl = [];
  for (const rec of dataset.race_control) {
    const recDate = parseIso(rec.date);
    if (!recDate || recDate > t) continue;
    raceControl.push({ date: rec.date, message: rec.message ?? null, flag: rec.flag ?? null, category: rec.category ?? null, lap_number: rec.lap_number ?? null });
  }
  raceControl.sort((a, b) => (b.date || "").localeCompare(a.date || ""));
  raceControl = raceControl.slice(0, 20);

  const weatherRec = latestAtOrBefore(dataset.weather, "date", t);
  let weather = null;
  if (weatherRec) {
    weather = {
      air_temperature: weatherRec.air_temperature ?? null,
      track_temperature: weatherRec.track_temperature ?? null,
      humidity: weatherRec.humidity ?? null,
      wind_speed: weatherRec.wind_speed ?? null,
      wind_direction: weatherRec.wind_direction ?? null,
      rainfall: !["0", "None", "False", "false", "null", "undefined"].includes(String(weatherRec.rainfall)),
    };
  }

  return {
    ok: true,
    mode: "replay",
    session: {
      session_key: `openf1:${sessionMeta.session_key}`,
      meeting_key: `openf1:${sessionMeta.meeting_key}`,
      session_name: sessionMeta.session_name,
      session_type: sessionMeta.session_type,
    },
    meeting: {
      meeting_name: sessionMeta.meeting_name,
      circuit_short_name: sessionMeta.circuit_short_name,
      latitude: sessionMeta.latitude,
      longitude: sessionMeta.longitude,
    },
    drivers: dataset.drivers,
    positions,
    intervals,
    laps,
    best_laps: bestLaps,
    stints,
    pits: pits.slice(0, 20),
    race_control: raceControl,
    weather,
  };
}

async function advanceTrackMap(sessionKey, r, tFrom, tTo) {
  try {
    const windowSeconds = (tTo - tFrom) / 1000;
    if (windowSeconds <= 0 || windowSeconds > MAX_LOCATION_FETCH_WINDOW_SECONDS) return;
    const frames = await fetchLocationWindow(sessionKey, tFrom.toISOString(), tTo.toISOString());
    const outline = r.track_outline;
    const cars = r.last_car_positions;
    let counter = r.outline_sample_counter || 0;
    for (const rec of frames) {
      if (rec.driver_number == null || rec.x == null || rec.y == null) continue;
      counter += 1;
      cars[String(parseInt(rec.driver_number, 10))] = { x: rec.x, y: rec.y, status: null };
      if (counter % OUTLINE_SAMPLE_EVERY === 0) outline.push([rec.x, rec.y]);
    }
    if (outline.length > MAX_OUTLINE_POINTS) outline.splice(0, outline.length - MAX_OUTLINE_POINTS);
    r.outline_sample_counter = counter;
  } catch (err) {
    /* track map is a bonus feature, never fatal */
  }
}
