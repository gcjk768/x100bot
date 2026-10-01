"""Open-Meteo hourly forecast (no key) and a light label per hour. One call per plan run, through the openmeteo bucket."""
from __future__ import annotations

import logging
from collections import Counter

import httpx

from .ratelimit import Limiter, LimitError

log = logging.getLogger(__name__)
URL = "https://api.open-meteo.com/v1/forecast"
PERIODS = {"morning": range(7, 12), "afternoon": range(12, 17), "evening": range(17, 21)}
WORDS = {"rain": "rain likely", "overcast": "overcast", "sunny": "sunny", "partly cloudy": "partly cloudy"}


def fetch(s, limiter: Limiter, transport: httpx.BaseTransport | None = None) -> dict | None:
    """The raw Open-Meteo response, or None when the call fails or the budget is used up."""
    params = {"latitude": s.location.latitude, "longitude": s.location.longitude,
              "hourly": "cloud_cover,precipitation_probability,weather_code",
              "timezone": s.schedule.timezone, "forecast_days": 2}
    try:
        t = limiter.acquire("openmeteo", target="/v1/forecast")
    except LimitError as ex:
        log.warning("weather skipped: %s", ex)
        return None
    try:
        with httpx.Client(transport=transport, timeout=30,
                          headers={"User-Agent": s.limits.web.user_agent}) as c:
            r = c.get(URL, params=params)
        r.raise_for_status()
        limiter.report(t, "ok", status=r.status_code)
        return r.json()
    except (httpx.HTTPError, ValueError) as ex:
        status = getattr(getattr(ex, "response", None), "status_code", None)
        limiter.report(t, "soft_fail", reason=type(ex).__name__, status=status)
        log.warning("weather call failed: %s", ex)
        return None


def label(cloud: float | None, rain_prob: float | None, cfg) -> str:
    if rain_prob is not None and rain_prob >= cfg.rain_probability:
        return "rain"
    if cloud is not None and cloud >= cfg.overcast_cloud_cover:
        return "overcast"
    if cloud is not None and cloud <= cfg.sunny_cloud_cover:
        return "sunny"
    return "partly cloudy"


def hours_for(data: dict | None, day: str, cfg) -> dict[int, str]:
    """{hour: label} for one local date. Empty when there is no forecast."""
    if not data:
        return {}
    h = data.get("hourly", {})
    out = {}
    for i, t in enumerate(h.get("time", [])):
        if t.startswith(day):
            out[int(t[11:13])] = label(h["cloud_cover"][i], h["precipitation_probability"][i], cfg)
    return out


def periods(hours: dict[int, str]) -> dict[str, str]:
    """The label per period: rain if any hour is likely rain, else the most common label."""
    out = {}
    for name, rng in PERIODS.items():
        labels = [hours[h] for h in rng if h in hours]
        if not labels:
            out[name] = "forecast unavailable"
        elif "rain" in labels:
            out[name] = "rain"
        else:
            out[name] = Counter(labels).most_common(1)[0][0]
    return out


def main_condition(hours: dict[int, str]) -> str:
    """The day's main condition for the recipe of the day, over daylight hours."""
    day = [hours[h] for h in range(8, 18) if h in hours]
    if not day:
        return "any"
    c = Counter("overcast" if lb == "partly cloudy" else lb for lb in day).most_common(1)[0][0]
    return c
