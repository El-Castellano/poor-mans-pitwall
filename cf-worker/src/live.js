/**
 * Parses F1's own raw live timing feed (livetiming.formula1.com/static),
 * the same unofficial/undocumented endpoint OpenF1 itself reads from. No
 * login/key needed for this HTTP-polling style access.
 *
 * This is a direct port of the root project's poller.py -- see that file's
 * (now removed, see git history / the top-level README) comments for the
 * original reasoning. The one real behavior difference porting to a Worker
 * bought us: Python's `zlib` module isn't available here, so Position.z
 * decompression uses the Workers runtime's native DecompressionStream with
 * "deflate-raw" / "deflate" / "gzip", which line up with Python's
 * `-MAX_WBITS` / `MAX_WBITS` / `MAX_WBITS|16` attempts respectively.
 *
 * Returns `null` (not an error) when F1's feed reports no session is
 * currently running -- durable-object.js treats that as "fall back to
 * replay.js", exactly like poller.py's build_state() dispatch.
 */

import { CIRCUIT_COORDINATES } from "./circuits.js";

const BASE_URL = "https://livetiming.formula1.com/static";
const HEADERS = { "User-Agent": "Mozilla/5.0 (compatible; f1panel-worker/1.0)" };

// Same constants/reasoning as poller.py.
const MAX_OUTLINE_POINTS = 3000;
const OUTLINE_SAMPLE_EVERY = 4;

// --- Raw F1 feed client ----------------------------------------------------

async function httpGetText(path) {
  try {
    const resp = await fetch(`${BASE_URL}/${path}`, { headers: HEADERS });
    if (!resp.ok) return null;
    const buf = await resp.arrayBuffer();
    let text = new TextDecoder("utf-8").decode(buf);
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1); // strip BOM
    return text;
  } catch (err) {
    return null;
  }
}

async function fetchJson(path) {
  const text = await httpGetText(path);
  if (text == null) return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    return null;
  }
}

function stripEdges(line) {
  return line.replace(/^[\ufeff \t]+|[\ufeff \t]+$/g, "");
}

function parseStreamLines(text) {
  const out = [];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = stripEdges(rawLine);
    if (line.length < 13) continue;
    try {
      const obj = JSON.parse(line.slice(12));
      if (obj && typeof obj === "object" && !Array.isArray(obj)) out.push(obj);
    } catch (err) {
      /* skip malformed line */
    }
  }
  return out;
}

function deepMerge(base, patch) {
  if (
    base && typeof base === "object" && !Array.isArray(base) &&
    patch && typeof patch === "object" && !Array.isArray(patch)
  ) {
    const merged = { ...base };
    for (const key of Object.keys(patch)) merged[key] = deepMerge(merged[key], patch[key]);
    return merged;
  }
  return patch;
}

async function getSessionInfo() {
  return fetchJson("SessionInfo.json");
}

async function getFeedIndex(sessionPath) {
  return (await fetchJson(`${sessionPath}Index.json`)) || {};
}

async function getFeedState(sessionPath, feedIndex, feedName) {
  const paths = (feedIndex.Feeds || {})[feedName];
  if (!paths) return {};
  const keyframe = (await fetchJson(`${sessionPath}${paths.KeyFramePath}`)) || {};
  const streamText = await httpGetText(`${sessionPath}${paths.StreamPath}`);
  let state = keyframe;
  if (streamText) {
    for (const patch of parseStreamLines(streamText)) state = deepMerge(state, patch);
  }
  return state;
}

// --- Car position feed ("Position.z") --------------------------------------
//
// CONFIDENCE CAVEAT (carried over from poller.py): this is the least
// certain part of the whole project. F1 compresses this feed (raw deflate
// or zlib/gzip, base64-encoded, wrapped in a JSON object keyed by the feed
// name) and the exact wrapping has only been cross-checked against
// community write-ups, not an official spec. If car dots never appear on
// the track map, hit GET /tick on the worker directly and inspect the
// response, or temporarily log the raw payload in extractPositionFrames().

function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function decompressWithFormat(bytes, format) {
  try {
    const ds = new DecompressionStream(format);
    const writer = ds.writable.getWriter();
    writer.write(bytes);
    writer.close();
    const buf = await new Response(ds.readable).arrayBuffer();
    return JSON.parse(new TextDecoder("utf-8").decode(buf));
  } catch (err) {
    return null;
  }
}

async function decompressZValue(raw) {
  let bytes;
  try {
    bytes = base64ToBytes(raw);
  } catch (err) {
    return null;
  }
  // Mirrors poller.py's three wbits attempts, in order:
  //   -MAX_WBITS (raw deflate) -> "deflate-raw"
  //    MAX_WBITS (zlib-wrapped) -> "deflate"
  //    MAX_WBITS|16 (gzip)      -> "gzip"
  for (const format of ["deflate-raw", "deflate", "gzip"]) {
    const result = await decompressWithFormat(bytes, format);
    if (result != null) return result;
  }
  return null;
}

async function extractPositionFrames(obj, feedName) {
  if (!obj || typeof obj !== "object") return [];
  for (const key of [feedName, "Position"]) {
    const val = obj[key];
    if (typeof val === "string") {
      const decompressed = await decompressZValue(val);
      if (decompressed && typeof decompressed === "object" && Array.isArray(decompressed.Position)) {
        return decompressed.Position;
      }
    } else if (Array.isArray(val)) {
      return val;
    }
  }
  return [];
}

async function getNewPositionFrames(sessionPath, feedIndex, linesAlreadySeen) {
  const feedName = "Position.z";
  const feeds = feedIndex.Feeds || {};
  const paths = feeds[feedName] || feeds["Position"];
  if (!paths) return { frames: [], totalLines: linesAlreadySeen };

  const frames = [];

  if (linesAlreadySeen === 0) {
    const keyframe = await fetchJson(`${sessionPath}${paths.KeyFramePath}`);
    if (keyframe && typeof keyframe === "object") {
      frames.push(...(await extractPositionFrames(keyframe, feedName)));
    }
  }

  const streamText = (await httpGetText(`${sessionPath}${paths.StreamPath}`)) || "";
  const rawLines = streamText.split(/\r?\n/).filter((ln) => stripEdges(ln).length >= 13);
  const totalLines = rawLines.length;

  for (const rawLine of rawLines.slice(linesAlreadySeen)) {
    const line = stripEdges(rawLine);
    let obj;
    try {
      obj = JSON.parse(line.slice(12));
    } catch (err) {
      continue;
    }
    frames.push(...(await extractPositionFrames(obj, feedName)));
  }

  return { frames, totalLines };
}

// --- Small parsing helpers ---------------------------------------------

function toFloat(value) {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function parseGap(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return value;
  const cleaned = String(value).trim().replace(/^\+/, "");
  const n = Number(cleaned);
  return Number.isFinite(n) && cleaned !== "" ? n : String(value).trim();
}

function parseLapTime(value) {
  if (!value) return null;
  const parts = String(value).split(":");
  if (parts.length === 2) {
    const mins = parseInt(parts[0], 10);
    const secs = parseFloat(parts[1]);
    return Number.isFinite(mins) && Number.isFinite(secs) ? mins * 60 + secs : null;
  }
  const n = parseFloat(parts[0]);
  return Number.isFinite(n) ? n : null;
}

// --- Main tick ---------------------------------------------------------

/**
 * One tick against F1's live feed. Returns null if no session is currently
 * reported (caller should fall back to replay.js), or {state, cache} on
 * success. `cache` carries everything that has to survive between ticks
 * (best lap per driver, the accumulated track outline, etc.) -- the
 * Durable Object persists it in its own storage, the same role poller.py's
 * local .poller_cache.json file used to play.
 */
export async function buildLiveState(cache) {
  const info = await getSessionInfo();
  if (!info || !info.Path) return null;

  const bestLaps = { ...(cache.best_laps || {}) };
  let trackOutline = [...(cache.track_outline || [])];
  const lastCarPositions = { ...(cache.last_car_positions || {}) };
  let positionLinesSeen = cache.position_lines_seen || 0;
  let outlineSampleCounter = cache.outline_sample_counter || 0;

  const sessionPath = info.Path;
  const meeting = info.Meeting || {};
  const circuit = meeting.Circuit || {};

  const feedIndex = await getFeedIndex(sessionPath);

  const [driverState, timingState, appState, rcState, weatherState] = await Promise.all([
    getFeedState(sessionPath, feedIndex, "DriverList"),
    getFeedState(sessionPath, feedIndex, "TimingData"),
    getFeedState(sessionPath, feedIndex, "TimingAppData"),
    getFeedState(sessionPath, feedIndex, "RaceControlMessages"),
    getFeedState(sessionPath, feedIndex, "WeatherData"),
  ]);

  // Car positions -- wrapped so an unexpected feed shape degrades to "no
  // live map" instead of breaking the rest of the dashboard.
  try {
    const { frames, totalLines } = await getNewPositionFrames(sessionPath, feedIndex, positionLinesSeen);
    positionLinesSeen = totalLines;
    for (const frame of frames) {
      const entries = frame && frame.Entries;
      if (!entries || typeof entries !== "object") continue;
      outlineSampleCounter += 1;
      const takeSample = outlineSampleCounter % OUTLINE_SAMPLE_EVERY === 0;
      for (const [numStr, car] of Object.entries(entries)) {
        if (!car || typeof car !== "object" || !/^\d+$/.test(numStr)) continue;
        const { X: x, Y: y } = car;
        if (x === undefined || x === null || y === undefined || y === null) continue;
        lastCarPositions[numStr] = { x, y, status: car.Status ?? null };
        if (takeSample) trackOutline.push([x, y]);
      }
    }
    if (trackOutline.length > MAX_OUTLINE_POINTS) {
      trackOutline = trackOutline.slice(trackOutline.length - MAX_OUTLINE_POINTS);
    }
  } catch (err) {
    /* track map is a bonus feature, never fatal */
  }

  // Drivers
  const drivers = {};
  for (const [numStr, d] of Object.entries(driverState || {})) {
    if (!d || typeof d !== "object" || !/^\d+$/.test(numStr)) continue;
    drivers[numStr] = {
      name_acronym: d.Tla ?? null,
      full_name: d.FullName ?? d.BroadcastName ?? null,
      team_name: d.TeamName ?? null,
      team_colour: d.TeamColour ?? null,
      headshot_url: d.HeadshotUrl ?? d.HeadShotUrl ?? null,
    };
  }

  // Timing lines -> positions / intervals / laps
  const timingLines = {};
  for (const [k, v] of Object.entries(timingState.Lines || {})) {
    if (v && typeof v === "object" && /^\d+$/.test(k)) timingLines[k] = v;
  }

  const positions = {};
  const intervals = {};
  const laps = {};
  for (const [numStr, line] of Object.entries(timingLines)) {
    if (line.Position !== undefined && line.Position !== null) {
      const p = parseInt(line.Position, 10);
      if (!Number.isNaN(p)) positions[numStr] = p;
    }
    const intervalBlock = line.IntervalToPositionAhead || {};
    intervals[numStr] = {
      gap_to_leader: parseGap(line.GapToLeader),
      interval: parseGap(intervalBlock.Value),
    };
    const lastLap = line.LastLapTime || {};
    const lapDuration = parseLapTime(lastLap.Value);
    laps[numStr] = { lap_duration: lapDuration, lap_number: line.NumberOfLaps ?? null };
    if (lapDuration !== null && (!(numStr in bestLaps) || lapDuration < bestLaps[numStr])) {
      bestLaps[numStr] = lapDuration;
    }
  }

  // Stints (current tyre) + full pit-stop log, from TimingAppData
  const stints = {};
  let pits = [];
  const appLines = {};
  for (const [k, v] of Object.entries(appState.Lines || {})) {
    if (v && typeof v === "object" && /^\d+$/.test(k)) appLines[k] = v;
  }
  for (const [numStr, line] of Object.entries(appLines)) {
    const stintMap = {};
    for (const [k, v] of Object.entries(line.Stints || {})) {
      if (v && typeof v === "object" && /^\d+$/.test(k)) stintMap[k] = v;
    }
    const stintItems = Object.entries(stintMap)
      .map(([k, v]) => [parseInt(k, 10), v])
      .sort((a, b) => a[0] - b[0]);
    if (stintItems.length === 0) continue;

    const [latestIdx, latest] = stintItems[stintItems.length - 1];
    stints[numStr] = {
      compound: latest.Compound ?? null,
      tyre_age: latest.TotalLaps ?? null,
      stint_number: latestIdx + 1,
    };

    let cumulativeLaps = 0;
    for (const [idx, stint] of stintItems) {
      if (idx > 0) {
        pits.push({
          driver_number: parseInt(numStr, 10),
          lap_number: cumulativeLaps || null,
          compound: stint.Compound ?? null,
          lane_duration: null,
          stop_duration: null,
        });
      }
      if (typeof stint.TotalLaps === "number") cumulativeLaps = Math.trunc(stint.TotalLaps);
    }
  }
  pits.sort((a, b) => (b.lap_number || 0) - (a.lap_number || 0));

  // Race control
  let rawMessages = rcState.Messages || [];
  if (rawMessages && typeof rawMessages === "object" && !Array.isArray(rawMessages)) {
    rawMessages = Object.values(rawMessages).filter((v) => v && typeof v === "object");
  } else if (!Array.isArray(rawMessages)) {
    rawMessages = [];
  }
  let raceControl = rawMessages.map((m) => ({
    date: m.Utc ?? null,
    message: m.Message ?? null,
    flag: m.Flag ?? null,
    category: m.Category ?? null,
    lap_number: m.Lap ?? null,
  }));
  raceControl.sort((a, b) => (b.date || "").localeCompare(a.date || ""));
  raceControl = raceControl.slice(0, 20);

  // Weather
  let weather = null;
  if (weatherState && Object.keys(weatherState).length > 0) {
    const rainRaw = weatherState.Rainfall;
    weather = {
      air_temperature: toFloat(weatherState.AirTemp),
      track_temperature: toFloat(weatherState.TrackTemp),
      humidity: toFloat(weatherState.Humidity),
      wind_speed: toFloat(weatherState.WindSpeed),
      wind_direction: toFloat(weatherState.WindDirection),
      rainfall: !["0", "None", "False", "false"].includes(String(rainRaw)),
    };
  }

  const newCache = {
    ...cache,
    best_laps: bestLaps,
    track_outline: trackOutline,
    last_car_positions: lastCarPositions,
    position_lines_seen: positionLinesSeen,
    outline_sample_counter: outlineSampleCounter,
  };

  const latLon = CIRCUIT_COORDINATES[circuit.ShortName];

  const state = {
    ok: true,
    mode: "live",
    session: {
      session_key: sessionPath,
      meeting_key: sessionPath,
      session_name: info.Name ?? null,
      session_type: info.Type ?? null,
    },
    meeting: {
      meeting_name: meeting.Name ?? meeting.OfficialName ?? null,
      circuit_short_name: circuit.ShortName ?? null,
      latitude: latLon ? latLon[0] : null,
      longitude: latLon ? latLon[1] : null,
    },
    drivers,
    positions,
    intervals,
    laps,
    best_laps: bestLaps,
    stints,
    pits: pits.slice(0, 20),
    race_control: raceControl,
    weather,
    track: { outline: trackOutline, cars: lastCarPositions },
  };

  return { state, cache: newCache };
}
