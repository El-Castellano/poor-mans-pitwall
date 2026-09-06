"""
Data source for the dashboard: a single state.json file served by the
Cloudflare Worker in ../cf-worker/. That worker does the actual talking to
F1's live timing feed (falling back to an OpenF1 replay); this module just
fetches its output on a short cache, the same role OpenF1 used to play
directly, and the GitHub Pages URL played before that.

Set settings.WORKER_STATE_URL to your own deployed worker's URL before
running this (see README and cf-worker/README.md).
"""

from __future__ import annotations

from typing import Optional

import requests
from django.conf import settings
from django.core.cache import cache

TIMEOUT = 6


def _fetch_state() -> dict:
    cached = cache.get("worker_state")
    if cached is not None:
        return cached

    url = settings.WORKER_STATE_URL
    if not url:
        return {"ok": False, "reason": "WORKER_STATE_URL is not set in settings.py"}

    headers = {}
    token = getattr(settings, "WORKER_STATE_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            data = {"ok": False, "reason": "state.json was not a JSON object"}
    except (requests.RequestException, ValueError) as exc:
        # Fail soft: keep the dashboard showing whatever we fetched last
        # time rather than blanking out on a hiccup or the worker being down.
        data = {"ok": False, "reason": f"Could not fetch state.json: {exc}"}

    cache.set("worker_state", data, settings.WORKER_STATE_CACHE_SECONDS)
    return data


def _int_keyed(d: Optional[dict]) -> dict[int, dict]:
    if not d:
        return {}
    out = {}
    for k, v in d.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


# --- Session / meeting -----------------------------------------------------

def get_current_session() -> Optional[dict]:
    state = _fetch_state()
    session = state.get("session")
    return session or None


def get_meeting(meeting_key) -> Optional[dict]:
    state = _fetch_state()
    return state.get("meeting") or None


def get_pit_loss_seconds(circuit_short_name: Optional[str]) -> float:
    table = settings.CIRCUIT_PIT_LOSS_SECONDS
    if circuit_short_name and circuit_short_name in table:
        return table[circuit_short_name]
    return settings.DEFAULT_PIT_LOSS_SECONDS


# --- Drivers / live state, all read straight out of the published blob ----

def get_drivers(session_key) -> dict[int, dict]:
    return _int_keyed(_fetch_state().get("drivers"))


def get_latest_positions(session_key) -> dict[int, int]:
    raw = _int_keyed(_fetch_state().get("positions"))
    return {k: int(v) for k, v in raw.items() if v is not None}


def get_latest_intervals(session_key) -> dict[int, dict]:
    return _int_keyed(_fetch_state().get("intervals"))


def get_latest_laps(session_key) -> dict[int, dict]:
    return _int_keyed(_fetch_state().get("laps"))


def get_best_laps(session_key) -> dict[int, float]:
    raw = _int_keyed(_fetch_state().get("best_laps"))
    return {k: float(v) for k, v in raw.items() if v is not None}


def get_current_stints(session_key) -> dict[int, dict]:
    return _int_keyed(_fetch_state().get("stints"))


def get_recent_pits(session_key, limit: int = 20) -> list[dict]:
    return (_fetch_state().get("pits") or [])[:limit]


def get_race_control(session_key, limit: int = 20) -> list[dict]:
    return (_fetch_state().get("race_control") or [])[:limit]


def get_latest_weather(session_key) -> Optional[dict]:
    return _fetch_state().get("weather")


def get_track_state(session_key) -> Optional[dict]:
    """{"outline": [[x,y],...], "cars": {driver_num: {x,y,status}}} or None."""
    return _fetch_state().get("track")


def get_data_freshness() -> dict:
    """
    Exposed for the dashboard header: is the published data healthy, how
    old is it, and is it a live session or an OpenF1 replay (see
    replay.py / docs/STATE_SCHEMA.md)? `mode` is "live" or "replay";
    `replay` is only present (and non-null) in replay mode.
    """
    state = _fetch_state()
    return {
        "ok": state.get("ok", False),
        "reason": state.get("reason"),
        "generated_at": state.get("generated_at"),
        "mode": state.get("mode", "live"),
        "replay": state.get("replay"),
    }


# --- Assembled dashboard payload -----------------------------------------

def build_timing_table(session_key) -> list[dict]:
    """One row per driver: position, driver info, gaps, tyre, last/best lap."""
    drivers = get_drivers(session_key)
    positions = get_latest_positions(session_key)
    intervals = get_latest_intervals(session_key)
    laps = get_latest_laps(session_key)
    best_laps = get_best_laps(session_key)
    stints = get_current_stints(session_key)

    rows = []
    for drv_num, driver in drivers.items():
        stint = stints.get(drv_num, {})
        lap = laps.get(drv_num, {})
        interval = intervals.get(drv_num, {})

        rows.append({
            "driver_number": drv_num,
            "name_acronym": driver.get("name_acronym"),
            "full_name": driver.get("full_name"),
            "team_name": driver.get("team_name"),
            "team_colour": driver.get("team_colour"),
            "headshot_url": driver.get("headshot_url"),
            "position": positions.get(drv_num),
            "gap_to_leader": interval.get("gap_to_leader"),
            "interval": interval.get("interval"),
            "last_lap": lap.get("lap_duration"),
            "best_lap": best_laps.get(drv_num),
            "last_lap_number": lap.get("lap_number"),
            "compound": stint.get("compound"),
            "tyre_age": stint.get("tyre_age"),
            "stint_number": stint.get("stint_number"),
        })

    rows.sort(key=lambda r: (r["position"] is None, r["position"] if r["position"] is not None else 99))
    return rows


def project_pit_stop(session_key, driver_number: int, circuit_short_name: Optional[str]) -> dict:
    """
    Heuristic "if he pits this lap, where does he come out" projection.

    Method: take every driver's current gap_to_leader (seconds behind the
    race leader). Add this circuit's typical total pit lane loss to the
    target driver's own gap. Then count how many other cars now have a
    smaller gap-to-leader than that projected value -- that count + 1 is
    the projected position.

    This deliberately ignores: fuel-corrected lap time changes, whether
    other cars pit too, traffic in the pit lane, and out-lap tyre
    warm-up. It is a "roughly this position, give or take one" tool for
    commentary, not a strategy engine.
    """
    intervals = get_latest_intervals(session_key)
    positions = get_latest_positions(session_key)
    drivers = get_drivers(session_key)

    pit_loss = get_pit_loss_seconds(circuit_short_name)

    target = intervals.get(driver_number)
    if not target or not isinstance(target.get("gap_to_leader"), (int, float)):
        return {
            "ok": False,
            "reason": "No live gap data available for this driver right now "
                      "(either the session isn't a race, the poller isn't "
                      "running, or they're the race leader / just left the pits).",
        }

    current_gap = float(target["gap_to_leader"])
    projected_gap = current_gap + pit_loss
    current_position = positions.get(driver_number)

    others = []
    for drv, rec in intervals.items():
        if drv == driver_number:
            continue
        gap = rec.get("gap_to_leader")
        if isinstance(gap, (int, float)):
            others.append((drv, float(gap)))

    others.sort(key=lambda x: x[1])

    projected_position = 1 + sum(1 for _, gap in others if gap < projected_gap)

    ahead = [d for d, g in others if g < projected_gap]
    behind = [d for d, g in others if g >= projected_gap]
    car_ahead = ahead[-1] if ahead else None
    car_behind = behind[0] if behind else None

    def label(drv_num):
        d = drivers.get(drv_num, {})
        return d.get("name_acronym") or d.get("full_name") or f"#{drv_num}"

    return {
        "ok": True,
        "driver": label(driver_number),
        "current_position": current_position,
        "current_gap_to_leader": round(current_gap, 1),
        "pit_loss_used": pit_loss,
        "projected_gap_to_leader": round(projected_gap, 1),
        "projected_position": projected_position,
        "emerges_ahead_of": label(car_behind) if car_behind else None,
        "emerges_behind": label(car_ahead) if car_ahead else None,
    }
