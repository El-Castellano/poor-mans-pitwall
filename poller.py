#!/usr/bin/env python3
"""
Runs inside a GitHub Action (see .github/workflows/poll.yml), NOT on your
laptop. Each invocation is one "tick": fetch F1's raw live timing feed,
parse/merge it, and write a single combined JSON snapshot that your
laptop's Django app polls instead of talking to F1 or OpenF1 directly.

This is intentionally standalone (only needs `requests`) so it has no
Django dependency and can run in a bare Action runner.

Data source: https://livetiming.formula1.com/static/ -- F1's own live
timing service (unofficial/undocumented; this is the same upstream that
OpenF1 itself reads from). No login/key needed for this HTTP-polling
style access. See the workflow file for why this runs as a loop of
single ticks rather than one long-lived process.

Persistence across ticks: each tick is a fresh process, but they all run
on the same Action runner disk for the whole job, so anything that needs
to survive between ticks (currently just each driver's best lap of the
session, since the live feed only ever gives you the *last* lap) is kept
in a small local cache file (not published) alongside the script.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import zlib
from pathlib import Path
from typing import Any, Optional

import requests

from circuits import CIRCUIT_COORDINATES
import replay

BASE_URL = "https://livetiming.formula1.com/static"
TIMEOUT = 8
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; commentator-panel-poller/1.0)"}

# How many outline points to keep (older ones get dropped once the track
# shape is established -- this is plenty to trace any F1 circuit clearly).
MAX_OUTLINE_POINTS = 3000
# Only bank one outline sample every N newly-seen position frames, so we
# don't end up with an unreadable solid blob of thousands of points from
# one lap alone.
OUTLINE_SAMPLE_EVERY = 4


# --- Raw F1 feed client (see module docstring for the endpoint shapes) ----

def _http_get_text(path: str) -> Optional[str]:
    try:
        resp = requests.get(f"{BASE_URL}/{path}", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.content.decode("utf-8-sig", errors="replace")
    except requests.RequestException:
        return None


def fetch_json(path: str) -> Optional[Any]:
    text = _http_get_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _parse_stream_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip("\ufeff \t")
        if len(line) < 13:
            continue
        payload = line[12:]
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def deep_merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        merged = dict(base)
        for key, val in patch.items():
            merged[key] = deep_merge(merged.get(key), val)
        return merged
    return patch


def get_session_info() -> Optional[dict]:
    return fetch_json("SessionInfo.json")


def get_feed_index(session_path: str) -> dict:
    return fetch_json(f"{session_path}Index.json") or {}


def get_feed_state(session_path: str, feed_index: dict, feed_name: str) -> dict:
    paths = feed_index.get("Feeds", {}).get(feed_name)
    if not paths:
        return {}
    keyframe = fetch_json(f"{session_path}{paths['KeyFramePath']}") or {}
    stream_text = _http_get_text(f"{session_path}{paths['StreamPath']}")
    state = keyframe
    if stream_text:
        for patch in _parse_stream_lines(stream_text):
            state = deep_merge(state, patch)
    return state


# --- Car position feed ("Position.z") --------------------------------------
#
# CONFIDENCE CAVEAT: this is the least certain part of this whole project.
# F1 compresses this particular feed (raw deflate + base64, wrapped in a
# JSON object keyed by the feed name), and the exact wrapping has only
# been cross-checked against community write-ups, not an official spec.
# If car dots never appear on the track map, this is the first place to
# look -- see debug_position_feed() below, which you can run manually
# (`python poller.py --debug-position`) during a live session to print
# exactly what a raw line looks like so the parsing here can be adjusted.

def _decompress_z_value(raw: str) -> Optional[Any]:
    """Try the couple of plausible ways this feed's payload might be packed."""
    try:
        compressed = base64.b64decode(raw)
    except (ValueError, TypeError):
        return None
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
        try:
            decompressed = zlib.decompress(compressed, wbits)
            return json.loads(decompressed.decode("utf-8"))
        except (zlib.error, ValueError, UnicodeDecodeError):
            continue
    return None


def _extract_position_frames(obj: dict, feed_name: str) -> list[dict]:
    """
    A parsed line/keyframe for Position.z is expected to look like either
    {"Position.z": "<base64+deflate>"} (needs decompression) or, for some
    keyframes, already-plain {"Position": [{"Timestamp":.., "Entries":{..}}]}.
    Returns whatever list of frames we can find, or [] if the shape didn't
    match either expectation.
    """
    if not isinstance(obj, dict):
        return []
    for key in (feed_name, "Position"):
        val = obj.get(key)
        if isinstance(val, str):
            decompressed = _decompress_z_value(val)
            if isinstance(decompressed, dict):
                frames = decompressed.get("Position")
                if isinstance(frames, list):
                    return frames
        elif isinstance(val, list):
            return val
    return []


def get_new_position_frames(session_path: str, feed_index: dict, lines_already_seen: int) -> tuple[list[dict], int]:
    """
    Returns (new_frames, total_lines_now) for the Position.z stream, only
    decompressing lines beyond what a previous tick already processed
    (tracked via the local cache), to keep CPU cost roughly constant
    through a session instead of re-parsing the whole growing file every
    ~8 seconds.
    """
    feed_name = "Position.z"
    paths = feed_index.get("Feeds", {}).get(feed_name) or feed_index.get("Feeds", {}).get("Position")
    if not paths:
        return [], lines_already_seen

    frames: list[dict] = []

    if lines_already_seen == 0:
        keyframe = fetch_json(f"{session_path}{paths['KeyFramePath']}")
        if isinstance(keyframe, dict):
            frames.extend(_extract_position_frames(keyframe, feed_name))

    stream_text = _http_get_text(f"{session_path}{paths['StreamPath']}") or ""
    raw_lines = [ln for ln in stream_text.splitlines() if len(ln.strip("\ufeff \t")) >= 13]
    total_lines_now = len(raw_lines)

    for line in raw_lines[lines_already_seen:]:
        line = line.strip("\ufeff \t")
        payload = line[12:]
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        frames.extend(_extract_position_frames(obj, feed_name))

    return frames, total_lines_now


def debug_position_feed(session_path: str, feed_index: dict) -> None:
    """Run with --debug-position to eyeball the raw shape of this feed."""
    feed_name = "Position.z"
    paths = feed_index.get("Feeds", {}).get(feed_name) or feed_index.get("Feeds", {}).get("Position")
    print(f"Feed paths: {paths}")
    if not paths:
        print("No Position.z / Position feed found in this session's Index.json Feeds.")
        return
    stream_text = _http_get_text(f"{session_path}{paths['StreamPath']}") or ""
    lines = [ln for ln in stream_text.splitlines() if len(ln.strip()) >= 13]
    print(f"{len(lines)} stream lines found. First line raw payload (truncated):")
    if lines:
        print(lines[0][12:][:300])
        frames = _extract_position_frames(json.loads(lines[0][12:]), feed_name)
        print(f"Decoded into {len(frames)} frame(s). First frame (truncated):")
        print(json.dumps(frames[0], indent=2)[:600] if frames else "(none -- decompression didn't match expected shape)")


# --- Parsing helpers -----------------------------------------------------

def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_gap(value) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip().lstrip("+")
    try:
        return float(cleaned)
    except ValueError:
        return str(value).strip()


def _parse_lap_time(value) -> Optional[float]:
    if not value:
        return None
    parts = str(value).split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return None


# --- Local (unpublished) cache for cross-tick state ------------------------

def load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data))
    except OSError:
        pass


# --- Main tick -------------------------------------------------------------

def build_state(cache_path: Path) -> dict:
    """
    One tick. Tries F1's own live feed first; if it reports no session in
    progress, falls back to replaying the most recent completed session
    from OpenF1 instead (see replay.py) so the dashboard has something to
    show -- and something to test against -- outside of a live session.

    Every call re-checks the live feed from scratch, so if a real session
    goes live partway through a replay, the very next tick switches back
    to live data automatically; nothing needs to be restarted by hand.
    """
    info = get_session_info()
    if info and info.get("Path"):
        return _build_live_state(info, cache_path)
    return replay.build_replay_state(cache_path)


def _build_live_state(info: dict, cache_path: Path) -> dict:
    cache = load_cache(cache_path)
    best_laps: dict[str, float] = cache.get("best_laps", {})
    track_outline: list[list[float]] = cache.get("track_outline", [])
    last_car_positions: dict[str, dict] = cache.get("last_car_positions", {})
    position_lines_seen: int = cache.get("position_lines_seen", 0)
    outline_sample_counter: int = cache.get("outline_sample_counter", 0)

    session_path = info["Path"]
    meeting = info.get("Meeting") or {}
    circuit = meeting.get("Circuit") or {}

    feed_index = get_feed_index(session_path)

    driver_state = get_feed_state(session_path, feed_index, "DriverList")
    timing_state = get_feed_state(session_path, feed_index, "TimingData")
    app_state = get_feed_state(session_path, feed_index, "TimingAppData")
    rc_state = get_feed_state(session_path, feed_index, "RaceControlMessages")
    weather_state = get_feed_state(session_path, feed_index, "WeatherData")

    # Car positions -- see the confidence caveat above get_new_position_frames.
    # Wrapped in try/except so an unexpected feed shape degrades to "no
    # live map" rather than breaking the rest of the dashboard.
    try:
        new_frames, position_lines_seen = get_new_position_frames(session_path, feed_index, position_lines_seen)
        for frame in new_frames:
            entries = frame.get("Entries") if isinstance(frame, dict) else None
            if not isinstance(entries, dict):
                continue
            outline_sample_counter += 1
            take_sample = (outline_sample_counter % OUTLINE_SAMPLE_EVERY) == 0
            for num_str, car in entries.items():
                if not isinstance(car, dict) or not str(num_str).isdigit():
                    continue
                x, y = car.get("X"), car.get("Y")
                if x is None or y is None:
                    continue
                last_car_positions[str(num_str)] = {
                    "x": x, "y": y, "status": car.get("Status"),
                }
                if take_sample:
                    track_outline.append([x, y])
        if len(track_outline) > MAX_OUTLINE_POINTS:
            track_outline = track_outline[-MAX_OUTLINE_POINTS:]
    except Exception:  # noqa: BLE001 -- position map is a bonus feature, never fatal
        pass

    # Drivers
    drivers = {}
    for num_str, d in driver_state.items():
        if not isinstance(d, dict) or not num_str.isdigit():
            continue
        drivers[num_str] = {
            "name_acronym": d.get("Tla"),
            "full_name": d.get("FullName") or d.get("BroadcastName"),
            "team_name": d.get("TeamName"),
            "team_colour": d.get("TeamColour"),
            "headshot_url": d.get("HeadshotUrl") or d.get("HeadShotUrl"),
        }

    # Timing lines -> positions / intervals / laps
    timing_lines = {
        k: v for k, v in (timing_state.get("Lines") or {}).items()
        if isinstance(v, dict) and k.isdigit()
    }

    positions = {}
    intervals = {}
    laps = {}
    for num_str, line in timing_lines.items():
        pos = line.get("Position")
        if pos is not None:
            try:
                positions[num_str] = int(pos)
            except (TypeError, ValueError):
                pass

        interval_block = line.get("IntervalToPositionAhead") or {}
        intervals[num_str] = {
            "gap_to_leader": _parse_gap(line.get("GapToLeader")),
            "interval": _parse_gap(interval_block.get("Value")),
        }

        last_lap = line.get("LastLapTime") or {}
        lap_duration = _parse_lap_time(last_lap.get("Value"))
        laps[num_str] = {
            "lap_duration": lap_duration,
            "lap_number": line.get("NumberOfLaps"),
        }
        if lap_duration is not None:
            if num_str not in best_laps or lap_duration < best_laps[num_str]:
                best_laps[num_str] = lap_duration

    # Stints (current tyre) + full pit-stop log, from TimingAppData
    stints = {}
    pits = []
    app_lines = {
        k: v for k, v in (app_state.get("Lines") or {}).items()
        if isinstance(v, dict) and k.isdigit()
    }
    for num_str, line in app_lines.items():
        stint_map = {
            k: v for k, v in (line.get("Stints") or {}).items()
            if isinstance(v, dict) and k.isdigit()
        }
        if not stint_map:
            continue
        stint_items = sorted(((int(k), v) for k, v in stint_map.items()), key=lambda kv: kv[0])

        latest_idx, latest = stint_items[-1]
        stints[num_str] = {
            "compound": latest.get("Compound"),
            "tyre_age": latest.get("TotalLaps"),
            "stint_number": latest_idx + 1,
        }

        cumulative_laps = 0
        for idx, stint in stint_items:
            if idx > 0:
                pits.append({
                    "driver_number": int(num_str),
                    "lap_number": cumulative_laps or None,
                    "compound": stint.get("Compound"),
                    "lane_duration": None,
                    "stop_duration": None,
                })
            total = stint.get("TotalLaps")
            if isinstance(total, (int, float)):
                cumulative_laps = int(total)
    pits.sort(key=lambda e: e["lap_number"] or 0, reverse=True)

    # Race control
    raw_messages = rc_state.get("Messages", [])
    if isinstance(raw_messages, dict):
        raw_messages = [v for v in raw_messages.values() if isinstance(v, dict)]
    elif not isinstance(raw_messages, list):
        raw_messages = []
    race_control = []
    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        race_control.append({
            "date": m.get("Utc"),
            "message": m.get("Message"),
            "flag": m.get("Flag"),
            "category": m.get("Category"),
            "lap_number": m.get("Lap"),
        })
    race_control.sort(key=lambda m: m.get("date") or "", reverse=True)
    race_control = race_control[:20]

    # Weather
    weather = None
    if weather_state:
        weather = {
            "air_temperature": _to_float(weather_state.get("AirTemp")),
            "track_temperature": _to_float(weather_state.get("TrackTemp")),
            "humidity": _to_float(weather_state.get("Humidity")),
            "wind_speed": _to_float(weather_state.get("WindSpeed")),
            "wind_direction": _to_float(weather_state.get("WindDirection")),
            "rainfall": str(weather_state.get("Rainfall")) not in ("0", "None", "False", "false"),
        }

    # Merge into the full cache rather than overwriting it wholesale -- a
    # "replay" key may also live in this same file (see replay.py), and a
    # session can flip from replay back to live mid-job, so both branches
    # need to leave the other's saved state alone.
    full_cache = load_cache(cache_path)
    full_cache.update({
        "best_laps": best_laps,
        "track_outline": track_outline,
        "last_car_positions": last_car_positions,
        "position_lines_seen": position_lines_seen,
        "outline_sample_counter": outline_sample_counter,
    })
    save_cache(cache_path, full_cache)

    lat_lon = CIRCUIT_COORDINATES.get(circuit.get("ShortName"))

    return {
        "ok": True,
        "mode": "live",
        "session": {
            "session_key": session_path,
            "meeting_key": session_path,
            "session_name": info.get("Name"),
            "session_type": info.get("Type"),
        },
        "meeting": {
            "meeting_name": meeting.get("Name") or meeting.get("OfficialName"),
            "circuit_short_name": circuit.get("ShortName"),
            "latitude": lat_lon[0] if lat_lon else None,
            "longitude": lat_lon[1] if lat_lon else None,
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
        "track": {
            "outline": track_outline,
            "cars": last_car_positions,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="site", help="Directory to write state.json into")
    parser.add_argument("--cache", default=".poller_cache.json", help="Local cross-tick cache file (not published)")
    parser.add_argument("--debug-position", action="store_true",
                         help="Print the raw shape of the Position.z feed for the current session and exit")
    args = parser.parse_args()

    if args.debug_position:
        info = get_session_info()
        if not info or not info.get("Path"):
            print("No session currently reported by F1's feed.")
            return
        feed_index = get_feed_index(info["Path"])
        debug_position_feed(info["Path"], feed_index)
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "state.json"

    import datetime as dt

    try:
        state = build_state(Path(args.cache))
    except Exception as exc:  # noqa: BLE001 -- a bad tick should never crash the loop
        state = {"ok": False, "reason": f"Poller error: {exc}"}

    state["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    # If this tick failed, don't overwrite good data with nothing --
    # merge onto whatever was last published so the dashboard just shows
    # slightly stale data instead of blanking out.
    if not state.get("ok") and state_path.exists():
        try:
            previous = json.loads(state_path.read_text())
            previous["ok"] = False
            previous["reason"] = state.get("reason")
            previous["generated_at"] = state["generated_at"]
            state = previous
        except (OSError, ValueError):
            pass

    state_path.write_text(json.dumps(state))
    print(f"wrote {state_path} ok={state.get('ok')} mode={state.get('mode')} at {state['generated_at']}")


if __name__ == "__main__":
    main()
