"""
Fallback data source used by poller.py when F1's own live feed
(SessionInfo.json) reports that no session is currently running.

Instead of publishing an empty/"no session" state.json, we pull the most
recently completed session from OpenF1's free public API
(https://openf1.org) and "replay" it: each tick advances a virtual clock
through that session's timeline and republishes state.json as if that
much of the session had happened live. This gives you a dashboard that's
actually populated (timing table, tyres, pit stops, race control, weather,
a slowly-traced track map) for testing/demoing the panel between real
sessions, instead of a blank "no session found" screen.

This is intentionally a second, independent implementation of "build a
state.json", not a wrapper around the live one -- the live path parses
F1's raw undocumented feed, this path parses OpenF1's REST responses,
and the two have basically nothing in common except the shape of the
JSON they both produce (see docs/STATE_SCHEMA.md for that contract).

CONFIDENCE CAVEAT, same spirit as poller.py's Position.z caveat: this
was written against publicly documented OpenF1 fields but couldn't be
exercised against a live OpenF1 response while writing it. If a field
below doesn't match what OpenF1 actually returns for a given session
(sprint weekends and red-flagged sessions are the likeliest to differ),
that just degrades this one fallback path -- fix it here without
touching the live path or the JSON contract other sources rely on.

Nothing in this module is Django-specific; like poller.py, it only needs
`requests` and runs fine in a bare Action runner.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Optional

import requests

from circuits import CIRCUIT_COORDINATES

OPENF1_BASE = "https://api.openf1.org/v1"
TIMEOUT = 15

# How many virtual (in-session) seconds each tick advances the replay by.
# The Action ticks every 2-3 real seconds in replay mode (see poll.yml), so
# the default here (25 virtual seconds/tick) plays a ~90 minute session
# back in a few minutes of wall-clock time -- fast enough to actually watch
# the panel change, slow enough that the timing table isn't just flickering.
# Override with the REPLAY_STEP_SECONDS env var if you want it faster/slower.
REPLAY_STEP_SECONDS = float(os.environ.get("REPLAY_STEP_SECONDS", "25"))

# Track map sampling, same idea as poller.py's live MAX_OUTLINE_POINTS /
# OUTLINE_SAMPLE_EVERY -- kept as separate small constants here (rather than
# imported from poller.py) to avoid poller.py <-> replay.py importing each
# other in a circle.
MAX_OUTLINE_POINTS = 3000
OUTLINE_SAMPLE_EVERY = 4

# If, after a loop restart or a long gap, the window of location data we'd
# need to fetch for the track map is bigger than this many seconds, skip
# fetching it for that tick rather than pulling a huge range in one go.
# The map just stays sparse/blank a little longer in that case -- never fatal.
MAX_LOCATION_FETCH_WINDOW_SECONDS = 120


# --- OpenF1 REST client -----------------------------------------------------

def _get(endpoint: str, raw_filters: Optional[list[str]] = None, **params: Any) -> list:
    """
    GET one OpenF1 endpoint. Always returns a list (possibly empty) --
    OpenF1 errors, timeouts, and unexpected shapes all degrade to []
    rather than raising, matching poller.py's "a bad feed should never
    crash the tick" philosophy.

    `params` are plain equality filters (session_key=1234), sent the
    normal `?key=value` way. `raw_filters` are comparator filters
    (date ranges, etc.) -- OpenF1's own examples show these as a bare
    `field<value` / `field>value` token with no `=`, e.g.
    `?session_key=9165&interval<0.005`, which isn't something a
    standard key=value params dict can produce, so those are appended
    to the URL as-is.
    """
    query = "&".join(f"{k}={v}" for k, v in params.items())
    if raw_filters:
        query = "&".join([query] + list(raw_filters)) if query else "&".join(raw_filters)
    url = f"{OPENF1_BASE}/{endpoint}"
    if query:
        url = f"{url}?{query}"
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except (requests.RequestException, ValueError):
        return []


def find_last_session() -> Optional[dict]:
    """The session OpenF1 currently considers "latest". Since this module
    is only ever called after F1's own feed says nothing is live, this is
    normally the most recently completed session -- but if the two feeds
    briefly disagree (e.g. right at a session's start/end), you may get a
    session that's technically still in progress by OpenF1's clock. That's
    fine here: worst case, one tick's replay is a few minutes behind an
    F1 feed that's about to take over anyway."""
    rows = _get("sessions", session_key="latest")
    return rows[0] if rows else None


def find_meeting(meeting_key) -> Optional[dict]:
    rows = _get("meetings", meeting_key=meeting_key)
    return rows[0] if rows else None


def fetch_session_dataset(session_key) -> dict:
    """
    One-time (per session) bulk fetch of everything except the high-rate
    location feed, which is instead pulled incrementally per tick (see
    _advance_track_map). Each call is independently best-effort: a failed
    endpoint just means that part of the dashboard is empty for this
    replay, not that the whole replay fails.
    """
    drivers_raw = _get("drivers", session_key=session_key)
    drivers = {}
    for d in drivers_raw:
        num = d.get("driver_number")
        if num is None:
            continue
        drivers[str(int(num))] = {
            "name_acronym": d.get("name_acronym"),
            "full_name": d.get("full_name") or d.get("broadcast_name"),
            "team_name": d.get("team_name"),
            # Raw hex, no leading "#" -- matches the live feed's TeamColour
            # shape; the dashboard template adds the "#" itself.
            "team_colour": d.get("team_colour"),
            "headshot_url": d.get("headshot_url"),
        }

    laps = sorted(
        (l for l in _get("laps", session_key=session_key) if l.get("date_start") and l.get("driver_number") is not None),
        key=lambda l: l["date_start"],
    )
    classifications = sorted(
        (p for p in _get("position", session_key=session_key) if p.get("date") and p.get("driver_number") is not None),
        key=lambda p: p["date"],
    )
    intervals = sorted(
        (i for i in _get("intervals", session_key=session_key) if i.get("date") and i.get("driver_number") is not None),
        key=lambda i: i["date"],
    )
    stints = sorted(
        (s for s in _get("stints", session_key=session_key) if s.get("driver_number") is not None),
        key=lambda s: (s["driver_number"], s.get("stint_number", 0)),
    )
    pits = sorted(
        (p for p in _get("pit", session_key=session_key) if p.get("date")),
        key=lambda p: p["date"],
    )
    race_control = sorted(
        (m for m in _get("race_control", session_key=session_key) if m.get("date")),
        key=lambda m: m["date"],
    )
    weather = sorted(
        (w for w in _get("weather", session_key=session_key) if w.get("date")),
        key=lambda w: w["date"],
    )

    return {
        "drivers": drivers,
        "laps": laps,
        "classifications": classifications,
        "intervals": intervals,
        "stints": stints,
        "pits": pits,
        "race_control": race_control,
        "weather": weather,
    }


def fetch_location_window(session_key, date_from: str, date_to: str) -> list:
    """Car x/y/z telemetry (OpenF1's `location` endpoint) for one narrow
    time window, fetched incrementally tick-by-tick as the replay clock
    advances -- pulling a whole session's worth in one call would be a lot
    of data (~3.7Hz per car) for what's just a "nice to have" track map."""
    return _get(
        "location",
        session_key=session_key,
        raw_filters=[f"date>{date_from}", f"date<={date_to}"],
    )


# --- Small parsing helpers ---------------------------------------------------

def _parse_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_at_or_before(records: list, date_key: str, t: dt.datetime, per_key=None):
    """
    From a date-sorted list of records, return the latest one at or before
    virtual time `t`, optionally grouped by `per_key` (e.g. one result per
    driver_number instead of one overall). Records are visited oldest to
    newest so later ones win ties/overwrite earlier ones, same as the live
    feed's own "last value wins" semantics.
    """
    if per_key is None:
        best = None
        for r in records:
            r_date = _parse_iso(r.get(date_key))
            if r_date and r_date <= t:
                best = r
            elif r_date and r_date > t:
                break
        return best

    out: dict = {}
    for r in records:
        r_date = _parse_iso(r.get(date_key))
        if r_date and r_date <= t:
            out[r.get(per_key)] = r
        elif r_date and r_date > t:
            break
    return out


# --- Local (unpublished) cache for cross-tick replay state ------------------

def _load(cache_path: Path) -> dict:
    try:
        return json.loads(cache_path.read_text())
    except (OSError, ValueError):
        return {}


def _save(cache_path: Path, full_cache: dict) -> None:
    try:
        cache_path.write_text(json.dumps(full_cache))
    except OSError:
        pass


# --- Main entry point --------------------------------------------------------

def build_replay_state(cache_path: Path) -> dict:
    full_cache = _load(cache_path)
    r = full_cache.get("replay") or {}

    dataset = r.get("dataset")
    session_meta = r.get("session_meta")

    if not dataset or not session_meta:
        # First tick of a replay (or the previous one never got this far):
        # pick a session and pull its whole dataset once. Everything after
        # this just replays what's already on disk -- no repeated fetching.
        session = find_last_session()
        if not session or not session.get("session_key"):
            return {"ok": False, "mode": "replay", "reason": "OpenF1 has no session to replay right now."}

        session_key = session["session_key"]
        meeting = find_meeting(session.get("meeting_key")) or {}
        lat_lon = CIRCUIT_COORDINATES.get(session.get("circuit_short_name"))

        session_meta = {
            "session_key": session_key,
            "meeting_key": session.get("meeting_key"),
            "session_name": session.get("session_name"),
            "session_type": session.get("session_type"),
            "meeting_name": meeting.get("meeting_official_name") or meeting.get("meeting_name"),
            "circuit_short_name": session.get("circuit_short_name"),
            "latitude": lat_lon[0] if lat_lon else None,
            "longitude": lat_lon[1] if lat_lon else None,
            "date_start": session.get("date_start"),
            "date_end": session.get("date_end"),
        }
        dataset = fetch_session_dataset(session_key)
        r = {
            "session_meta": session_meta,
            "dataset": dataset,
            "virtual_time": session_meta["date_start"],
            "loop_count": 0,
            "track_outline": [],
            "last_car_positions": {},
            "outline_sample_counter": 0,
        }

    session_key = session_meta["session_key"]
    t_start = _parse_iso(session_meta.get("date_start"))
    t_end = _parse_iso(session_meta.get("date_end"))
    t_prev = _parse_iso(r.get("virtual_time")) or t_start

    if not t_start or not t_end or t_start >= t_end:
        return {"ok": False, "mode": "replay", "reason": "Replay session had no usable date_start/date_end from OpenF1."}

    t_next = t_prev + dt.timedelta(seconds=REPLAY_STEP_SECONDS)
    looped = t_next > t_end
    if looped:
        # Reached the end of the session's data -- start over from the
        # beginning so the Action keeps showing *something* for as long as
        # it's left running, rather than freezing on the final lap forever.
        t_next = t_start
        r["loop_count"] = r.get("loop_count", 0) + 1
        r["track_outline"] = []
        r["last_car_positions"] = {}
        r["outline_sample_counter"] = 0

    state = _state_at(session_meta, dataset, t_next)
    _advance_track_map(session_key, r, t_start if looped else t_prev, t_next)

    r["virtual_time"] = t_next.isoformat()
    full_cache["replay"] = {
        "session_meta": session_meta,
        "dataset": dataset,
        "virtual_time": r["virtual_time"],
        "loop_count": r["loop_count"],
        "track_outline": r["track_outline"],
        "last_car_positions": r["last_car_positions"],
        "outline_sample_counter": r["outline_sample_counter"],
    }
    _save(cache_path, full_cache)

    state["track"] = {"outline": r["track_outline"], "cars": r["last_car_positions"]}
    progress = (t_next - t_start).total_seconds() / max((t_end - t_start).total_seconds(), 1)
    state["replay"] = {
        "source": "openf1",
        "session_key": session_key,
        "virtual_time": r["virtual_time"],
        "session_progress": round(min(max(progress, 0.0), 1.0), 4),
        "loop_count": r["loop_count"],
        "note": "Historical data from OpenF1, replayed for testing/demo purposes -- not a live session.",
    }
    return state


def _state_at(session_meta: dict, dataset: dict, t: dt.datetime) -> dict:
    drivers = dataset["drivers"]

    classifications = _latest_at_or_before(dataset["classifications"], "date", t, per_key="driver_number")
    positions = {}
    for num, rec in classifications.items():
        if num is None or rec.get("position") is None:
            continue
        positions[str(int(num))] = int(rec["position"])

    interval_recs = _latest_at_or_before(dataset["intervals"], "date", t, per_key="driver_number")
    intervals = {}
    for num, rec in interval_recs.items():
        if num is None:
            continue
        intervals[str(int(num))] = {
            "gap_to_leader": rec.get("gap_to_leader"),
            "interval": rec.get("interval"),
        }

    lap_recs = _latest_at_or_before(dataset["laps"], "date_start", t, per_key="driver_number")
    laps = {}
    best_laps: dict[str, float] = {}
    current_lap_number: dict[int, int] = {}
    for num, rec in lap_recs.items():
        if num is None:
            continue
        key = str(int(num))
        laps[key] = {
            "lap_duration": rec.get("lap_duration"),
            "lap_number": rec.get("lap_number"),
        }
        if isinstance(rec.get("lap_number"), int):
            current_lap_number[int(num)] = rec["lap_number"]
    # Best lap so far needs every lap up to t, not just each driver's latest one.
    for rec in dataset["laps"]:
        rec_date = _parse_iso(rec.get("date_start"))
        if not rec_date or rec_date > t:
            continue
        num, dur = rec.get("driver_number"), rec.get("lap_duration")
        if num is None or not isinstance(dur, (int, float)):
            continue
        key = str(int(num))
        if key not in best_laps or dur < best_laps[key]:
            best_laps[key] = float(dur)

    stints = {}
    for num_int in {s["driver_number"] for s in dataset["stints"] if s.get("driver_number") is not None}:
        driver_stints = [s for s in dataset["stints"] if s.get("driver_number") == num_int]
        lap_now = current_lap_number.get(num_int, 0)
        current = None
        for s in driver_stints:
            lap_start = s.get("lap_start") or 0
            if lap_start <= lap_now or current is None:
                current = s
        if current:
            tyre_age = current.get("tyre_age_at_start")
            if isinstance(tyre_age, (int, float)) and current.get("lap_start"):
                tyre_age = tyre_age + max(lap_now - current["lap_start"], 0)
            stints[str(num_int)] = {
                "compound": current.get("compound"),
                "tyre_age": tyre_age,
                "stint_number": current.get("stint_number"),
            }

    pits = []
    for rec in dataset["pits"]:
        rec_date = _parse_iso(rec.get("date"))
        if not rec_date or rec_date > t or rec.get("driver_number") is None:
            continue
        pits.append({
            "driver_number": int(rec["driver_number"]),
            "lap_number": rec.get("lap_number"),
            "compound": None,  # OpenF1's `pit` endpoint doesn't include the tyre fitted; see docs/STATE_SCHEMA.md
            "lane_duration": rec.get("pit_duration"),
            "stop_duration": None,  # not broken out separately by OpenF1
        })
    pits.sort(key=lambda e: e["lap_number"] or 0, reverse=True)

    race_control = []
    for rec in dataset["race_control"]:
        rec_date = _parse_iso(rec.get("date"))
        if not rec_date or rec_date > t:
            continue
        race_control.append({
            "date": rec.get("date"),
            "message": rec.get("message"),
            "flag": rec.get("flag"),
            "category": rec.get("category"),
            "lap_number": rec.get("lap_number"),
        })
    race_control.sort(key=lambda m: m.get("date") or "", reverse=True)
    race_control = race_control[:20]

    weather_rec = _latest_at_or_before(dataset["weather"], "date", t)
    weather = None
    if weather_rec:
        weather = {
            "air_temperature": weather_rec.get("air_temperature"),
            "track_temperature": weather_rec.get("track_temperature"),
            "humidity": weather_rec.get("humidity"),
            "wind_speed": weather_rec.get("wind_speed"),
            "wind_direction": weather_rec.get("wind_direction"),
            "rainfall": str(weather_rec.get("rainfall")) not in ("0", "None", "False", "false"),
        }

    return {
        "ok": True,
        "mode": "replay",
        "session": {
            "session_key": f"openf1:{session_meta['session_key']}",
            "meeting_key": f"openf1:{session_meta['meeting_key']}",
            "session_name": session_meta.get("session_name"),
            "session_type": session_meta.get("session_type"),
        },
        "meeting": {
            "meeting_name": session_meta.get("meeting_name"),
            "circuit_short_name": session_meta.get("circuit_short_name"),
            "latitude": session_meta.get("latitude"),
            "longitude": session_meta.get("longitude"),
        },
        "drivers": drivers,
        "positions": positions,
        "intervals": intervals,
        "laps": laps,
        "best_laps": best_laps,
        "stints": stints,
        "pits": pits[:20],
        "race_control": race_control,
        "weather": weather,
    }


def _advance_track_map(session_key, r: dict, t_from: dt.datetime, t_to: dt.datetime) -> None:
    """Best-effort track map update: pull location records for just the
    slice of session time this tick covers and fold them into the same
    outline/car-position shape the live poller produces. Never raises --
    a track map is a bonus feature here, same as in the live path."""
    try:
        window_seconds = (t_to - t_from).total_seconds()
        if window_seconds <= 0 or window_seconds > MAX_LOCATION_FETCH_WINDOW_SECONDS:
            return
        frames = fetch_location_window(session_key, t_from.isoformat(), t_to.isoformat())
        outline = r["track_outline"]
        cars = r["last_car_positions"]
        counter = r.get("outline_sample_counter", 0)
        for rec in frames:
            num = rec.get("driver_number")
            x, y = rec.get("x"), rec.get("y")
            if num is None or x is None or y is None:
                continue
            counter += 1
            cars[str(int(num))] = {"x": x, "y": y, "status": None}
            if counter % OUTLINE_SAMPLE_EVERY == 0:
                outline.append([x, y])
        if len(outline) > MAX_OUTLINE_POINTS:
            del outline[: len(outline) - MAX_OUTLINE_POINTS]
        r["outline_sample_counter"] = counter
    except Exception:  # noqa: BLE001 -- track map is a bonus feature, never fatal
        pass
