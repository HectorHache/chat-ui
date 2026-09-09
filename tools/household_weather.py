"""
household_weather — Local weather forecast + radar image.

Provides the current conditions and a 7-day forecast for the household
location (admin-configurable via Valves) and always ends with the local
radar image so the assistant can show it in the chat.
"""

import asyncio
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from pydantic import BaseModel

_DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path.home() / "Documents/Workspaces/ui/data"))

WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class Valves(BaseModel):
    latitude: float = 52.0907
    longitude: float = 5.1214
    city_name: str = "Utrecht"
    temperature_unit: str = "celsius"
    wind_speed_unit: str = "kmh"
    radar_url: str = "https://api.buienradar.nl/image/1.0/RadarMapNL?w=800&h=800"
    radar_alt_text: str = "Weather radar (Netherlands)"


class Tools:
    valves = Valves()

    async def weather_forecast(self, city: str = "") -> str:
        """
        Get the current weather and 7-day forecast for the household location.

        Use this whenever the user asks about the weather, "what's the weather
        like", "do I need a coat/umbrella", outdoor plans, or the radar.

        :param city: Optional city name. Only used as a hint if the user names
            a specific place; if empty or unknown the household location
            (admin-configured) is used.
        :return: Markdown text with current conditions, a 7-day daily summary
            and the radar image. Always ends with a markdown image line.
        """
        lat = self.valves.latitude
        lon = self.valves.longitude
        city_name = self.valves.city_name
        if city and city.strip():
            city_name = city.strip()

        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
            "timezone": "auto",
            "forecast_days": 7,
        }
        if self.valves.temperature_unit == "fahrenheit":
            params["temperature_unit"] = "fahrenheit"
        if self.valves.wind_speed_unit == "ms":
            params["wind_speed_unit"] = "ms"

        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
        try:
            data = await asyncio.to_thread(self._get_json, url)
        except Exception as e:
            return f"Could not reach the weather service: {e}. Please tell the user the weather service is temporarily unavailable."

        current = data.get("current", {})
        daily = data.get("daily", {})
        if not current or not daily:
            return "Weather service returned no data. Please tell the user the weather is currently unknown."

        code = current.get("weather_code")
        now = (
            f"**{city_name} — current conditions**\n"
            f"- {WMO_CODES.get(code, 'Unknown')} ({current.get('temperature_2m')}°)\n"
            f"- Feels like {current.get('apparent_temperature')}°, humidity {current.get('relative_humidity_2m')}%\n"
            f"- Wind {current.get('wind_speed_10m')} {data.get('current_units', {}).get('wind_speed_10m', 'km/h')}, "
            f"precipitation {current.get('precipitation')} mm"
        )

        days = []
        for i, d in enumerate(daily.get("time", [])):
            wc = daily.get("weather_code", [])[i] if i < len(daily.get("weather_code", [])) else None
            tmax = daily.get("temperature_2m_max", [])[i] if i < len(daily.get("temperature_2m_max", [])) else "?"
            tmin = daily.get("temperature_2m_min", [])[i] if i < len(daily.get("temperature_2m_min", [])) else "?"
            rain = daily.get("precipitation_probability_max", [])[i] if i < len(daily.get("precipitation_probability_max", [])) else None
            day_name = "Today" if i == 0 else d[-5:]
            rain_part = f", rain {rain}%" if rain is not None else ""
            days.append(f"- {day_name}: {WMO_CODES.get(wc, 'Unknown')}, {tmin}° / {tmax}°{rain_part}")

        radar = self.valves.radar_url
        alt = self.valves.radar_alt_text

        return (
            f"{now}\n\n**7-day outlook**\n" + "\n".join(days)
            + f"\n\n![{alt}]({radar})\n"
            + f"Show the radar image above in your answer."
        )

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Chat-hache-household/1.0 (private home assistant)"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
