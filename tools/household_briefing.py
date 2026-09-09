"""
household_briefing — Daily briefing preset.

"Good morning" — weather today + a fact of the day (Wikimedia on-this-day).
Calendar items join when the Calendar tool (batch 2) is live.
"""

import asyncio
import json
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

_UA = "Chat-hache-household/1.0 (private home assistant)"

WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Slight rain", 63: "Rain",
    65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Slight snow",
    73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers",
    81: "Rain showers", 82: "Violent showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}

from pydantic import BaseModel


class Valves(BaseModel):
    latitude: float = 52.0907
    longitude: float = 5.1214
    city_name: str = "Utrecht"
    timezone: str = "Europe/Amsterdam"


class Tools:
    valves = Valves()

    async def daily_briefing(self) -> str:
        """
        Compose the daily morning briefing: date, weather today, one fact.

        Use when the user says good morning, asks for the daily briefing,
        "what's on today" or wants a morning summary.

        :return: A short markdown briefing (weather + fact). Calendar items
            will be added once the calendar tool is available.
        """
        tz = self.valves.timezone
        now = datetime.now(ZoneInfo(tz))
        parts = [f"**{now.strftime('%A %d %B %Y')}** — {self.valves.city_name}"]

        # weather today
        try:
            params = {
                "latitude": self.valves.latitude,
                "longitude": self.valves.longitude,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": tz,
                "forecast_days": 1,
            }
            url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
            data = await asyncio.to_thread(self._get_json, url)
            d = data.get("daily", {})
            if d:
                code = (d.get("weather_code") or [None])[0]
                tmax = (d.get("temperature_2m_max") or ["?"])[0]
                tmin = (d.get("temperature_2m_min") or ["?"])[0]
                rain = (d.get("precipitation_probability_max") or [None])[0]
                w = WMO.get(code, "Unknown")
                rain_txt = f", rain chance {rain}%" if rain is not None else ""
                parts.append(f"🌤 Weather: {w}, {tmin}° / {tmax}°{rain_txt}")
        except Exception:
            parts.append("🌤 Weather: unavailable right now")

        # fact of the day
        try:
            mm, dd = now.month, now.day
            url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/{mm:02d}/{dd:02d}"
            data = await asyncio.to_thread(self._get_json, url)
            sel = data.get("selected") or []
            if sel:
                text = sel[0].get("text", "")
                year = ""
                if "year" in sel[0]:
                    year = str(sel[0]["year"]) + ": "
                if text:
                    parts.append(f"📅 On this day: {year}{text}")
        except Exception:
            pass

        parts.append("_Calendar items join the briefing when the calendar tool is active._")
        return "\n".join(parts)

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
