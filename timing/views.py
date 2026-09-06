from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render

from . import services


def dashboard(request):
    session = services.get_current_session()
    meeting = services.get_meeting(session["meeting_key"]) if session else None
    context = {
        "session": session,
        "meeting": meeting,
    }
    return render(request, "timing/dashboard.html", context)


def api_session_info(request):
    session = services.get_current_session()
    meeting = services.get_meeting(session["meeting_key"]) if session else None
    return JsonResponse({"session": session, "meeting": meeting})


def api_status(request):
    return JsonResponse(services.get_data_freshness())


def api_timing_table(request):
    session = services.get_current_session()
    if not session:
        return JsonResponse({"rows": [], "error": "No session found"})
    rows = services.build_timing_table(session["session_key"])
    return JsonResponse({"rows": rows})


def api_pit_stops(request):
    session = services.get_current_session()
    if not session:
        return JsonResponse({"pits": []})
    drivers = services.get_drivers(session["session_key"])
    pits = services.get_recent_pits(session["session_key"])
    for p in pits:
        d = drivers.get(p.get("driver_number"), {})
        p["name_acronym"] = d.get("name_acronym")
    return JsonResponse({"pits": pits})


def api_race_control(request):
    session = services.get_current_session()
    if not session:
        return JsonResponse({"messages": []})
    messages = services.get_race_control(session["session_key"])
    return JsonResponse({"messages": messages})


def api_weather(request):
    session = services.get_current_session()
    if not session:
        return JsonResponse({"weather": None})
    weather = services.get_latest_weather(session["session_key"])
    return JsonResponse({"weather": weather})


def api_track(request):
    session = services.get_current_session()
    if not session:
        return JsonResponse({"track": None})
    track = services.get_track_state(session["session_key"])
    drivers = services.get_drivers(session["session_key"])
    if track and track.get("cars"):
        cars = {}
        for num_str, car in track["cars"].items():
            d = drivers.get(int(num_str), {}) if num_str.isdigit() else {}
            cars[num_str] = {**car, "name_acronym": d.get("name_acronym"), "team_colour": d.get("team_colour")}
        track = {**track, "cars": cars}
    return JsonResponse({"track": track})


def api_pit_projection(request):
    driver_number = request.GET.get("driver_number")
    if not driver_number or not driver_number.isdigit():
        return HttpResponseBadRequest("driver_number is required")

    session = services.get_current_session()
    if not session:
        return JsonResponse({"ok": False, "reason": "No session found"})

    meeting = services.get_meeting(session["meeting_key"])
    circuit = meeting.get("circuit_short_name") if meeting else None

    result = services.project_pit_stop(session["session_key"], int(driver_number), circuit)
    return JsonResponse(result)
