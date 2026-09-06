"""
Django settings for the F1 Commentator's Panel.

This app is meant to run on your laptop and be viewed from your phone
over your home wifi. It is NOT hardened for exposure to the public
internet -- keep it on your local network.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Security -----------------------------------------------------------
# Fine for a local-network tool. Do not deploy this externally as-is.
SECRET_KEY = "django-insecure-commentator-panel-local-use-only"
DEBUG = True

# '*' lets your phone reach the server at whatever LAN IP your laptop has
# (e.g. 192.168.1.42). Tighten this if you want, but it must include the
# IP you'll actually browse to from your phone.
ALLOWED_HOSTS = ["*"]

CSRF_TRUSTED_ORIGINS = []  # not needed: the app has no login/forms that POST

# --- Apps -----------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "timing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "f1panel.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "f1panel.wsgi.application"

# --- Database ---------------------------------------------------------
# Not really used (no models persist race data), but Django wants one
# for sessions. SQLite is fine.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "timing" / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- In-memory cache ------------------------------------------------------
# Used to avoid re-fetching state.json from GitHub Pages on every single
# phone request if you have the dashboard open on more than one device.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "f1panel-cache",
    }
}

# --- Live timing data source ------------------------------------------
# A GitHub Action in this same repo (.github/workflows/poll.yml +
# poller.py) polls F1's own live timing feed and publishes one combined
# JSON snapshot to this repo's gh-pages branch. This app just fetches
# that file -- it never talks to F1 or OpenF1 directly.
#
# Fill in your own repo's published URL, e.g.:
#   "https://yourusername.github.io/f1panel/state.json"
# (enable it under repo Settings -> Pages -> Source: gh-pages branch)
GITHUB_STATE_URL = "https://el-castellano.github.io/F1-Commentators-Panel/state.json"  # <-- set this before running

# How long (seconds) to cache the fetched state.json before re-fetching.
# The Action itself only publishes a new version every ~8s at best (plus
# GitHub Pages' own propagation delay), so there's no point polling much
# faster than that.
GITHUB_STATE_CACHE_SECONDS = 0.5

# Typical total pit lane loss (seconds) if no circuit-specific value is
# set below. This is "time lost relative to staying out at racing speed",
# used by the pit-stop projection tool.
DEFAULT_PIT_LOSS_SECONDS = 22.0

# Circuit-specific pit lane loss, in seconds, keyed by circuit_short_name.
# These are rough published figures -- adjust freely as you learn actual
# values during a weekend (e.g. after seeing a couple of real pit stops,
# update the number here).
CIRCUIT_PIT_LOSS_SECONDS = {
    "Monaco": 19.0,
    "Singapore": 26.0,
    "Spa-Francorchamps": 20.0,
    "Monza": 19.5,
    "Silverstone": 21.0,
    "Suzuka": 20.5,
    "Baku": 18.0,
    "Jeddah": 20.0,
    "Las Vegas": 19.0,
    "Yas Marina": 20.5,
    "Bahrain": 21.5,
    "Zandvoort": 21.0,
    "Hungaroring": 21.0,
    "Interlagos": 20.0,
    "Miami": 18.5,
    "Austin": 19.5,
    "Mexico City": 21.5,
    "Melbourne": 20.0,
    "Shanghai": 22.0,
    "Imola": 27.0,
    "Barcelona": 22.5,
    "Montreal": 15.5,
}
