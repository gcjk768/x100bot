"""Sunrise, sunset, golden hour and blue hour for the configured location, from astral."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import SunDirection, blue_hour, golden_hour, sun


@dataclass
class Light:
    date: str
    sunrise: datetime
    sunset: datetime
    golden_morning_start: datetime
    golden_morning_end: datetime
    golden_evening_start: datetime
    golden_evening_end: datetime
    blue_morning_start: datetime
    blue_evening_start: datetime
    blue_end: datetime          # end of the evening blue hour

    def hhmm(self, name: str) -> str:
        return getattr(self, name).strftime("%H:%M")

    def as_text(self) -> dict:
        return {k: (v.strftime("%H:%M") if isinstance(v, datetime) else v) for k, v in asdict(self).items()}


def _on_day(fn, obs, day: date, direction, z):
    """astral computes some events on the UTC date, so east of Greenwich the morning event can land on the next
    local day. Try the neighbouring dates and keep the event that starts on the local day."""
    for d in (day, day - timedelta(days=1), day + timedelta(days=1)):
        ev = fn(obs, d, direction, tzinfo=z)
        if ev[0].date() == day:
            return ev
    return fn(obs, day, direction, tzinfo=z)


def compute(loc, day: date, tz: str) -> Light:
    z = ZoneInfo(tz)
    obs = LocationInfo(loc.name, "", tz, loc.latitude, loc.longitude).observer
    s = sun(obs, date=day, tzinfo=z)
    gm = _on_day(golden_hour, obs, day, SunDirection.RISING, z)
    ge = _on_day(golden_hour, obs, day, SunDirection.SETTING, z)
    bm = _on_day(blue_hour, obs, day, SunDirection.RISING, z)
    be = _on_day(blue_hour, obs, day, SunDirection.SETTING, z)
    return Light(day.isoformat(), s["sunrise"], s["sunset"], gm[0], gm[1], ge[0], ge[1], bm[0], be[0], be[1])


def hour_light(light: Light, hour: int) -> str | None:
    """golden_hour when the hour overlaps an evening or morning golden hour, night after sunset or before sunrise."""
    def overlaps(a, b):
        return a.hour <= hour <= b.hour
    if overlaps(light.golden_evening_start, light.golden_evening_end) or \
            overlaps(light.golden_morning_start, light.golden_morning_end):
        return "golden_hour"
    if hour > light.sunset.hour or hour < light.sunrise.hour:
        return "night"
    return None
