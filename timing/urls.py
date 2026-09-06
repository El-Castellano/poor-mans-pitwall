from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/session/", views.api_session_info, name="api_session_info"),
    path("api/status/", views.api_status, name="api_status"),
    path("api/timing/", views.api_timing_table, name="api_timing_table"),
    path("api/pits/", views.api_pit_stops, name="api_pit_stops"),
    path("api/race-control/", views.api_race_control, name="api_race_control"),
    path("api/weather/", views.api_weather, name="api_weather"),
    path("api/track/", views.api_track, name="api_track"),
    path("api/pit-projection/", views.api_pit_projection, name="api_pit_projection"),
]
