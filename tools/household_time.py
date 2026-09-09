"""
household_time — Time & timezone helper.

Schedule-aware answers: "what time is it there?", "what time is the call in
Madrid?", current weekday/date in any IANA timezone.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_COMMON = {
    "nl": "Europe/Amsterdam",
    "netherlands": "Europe/Amsterdam",
    "amsterdam": "Europe/Amsterdam",
    "es": "Europe/Madrid",
    "spain": "Europe/Madrid",
    "madrid": "Europe/Madrid",
    "uk": "Europe/London",
    "england": "Europe/London",
    "london": "Europe/London",
    "us": "America/New_York",
    "usa": "America/New_York",
    "ny": "America/New_York",
    "new york": "America/New_York",
}


def _resolve_tz(name: str) -> str:
    name = (name or "").strip().lower()
    if not name:
        return "Europe/Amsterdam"
    return _COMMON.get(name, name)


class Tools:
    async def current_time(self, timezone: str = "Europe/Amsterdam") -> str:
        """
        Get the current date, time and weekday in any timezone.

        Use for "what time is it", "what day is it", schedule questions,
        or to know whether it is day/night somewhere.

        :param timezone: IANA timezone name (e.g. Europe/Amsterdam,
            America/New_York) or a short alias (nl, es, uk, us).
        :return: Current date, time and weekday for the requested zone.
        """
        try:
            tz = ZoneInfo(_resolve_tz(timezone))
        except Exception:
            return f"Unknown timezone '{timezone}'. Ask the user for a city or IANA zone name."
        now = datetime.now(tz)
        return (
            f"In **{now.tzinfo.key}** it is **{now.strftime('%A %d %B %Y, %H:%M')}** "
            f"({now.utcoffset().total_seconds() / 3600:+.0f} h UTC)."
        )

    async def timezone_convert(self, date_time: str, from_timezone: str, to_timezone: str) -> str:
        """
        Convert a date/time from one timezone to another.

        Use for "what time is 14:00 Amsterdam in Madrid?", meeting times,
        call times across timezones.

        :param date_time: Date/time as ISO 8601 (e.g. 2026-08-31T14:00) or
            "14:00" for today. 24h format preferred.
        :param from_timezone: IANA timezone or alias of the source zone.
        :param to_timezone: IANA timezone or alias of the target zone.
        :return: The converted date/time in the target zone.
        """
        dt_str = (date_time or "").strip()
        if not dt_str:
            return "Please provide a date/time to convert."
        try:
            if len(dt_str) <= 5 and ":" in dt_str:
                dt_str = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "T" + dt_str
            if "T" not in dt_str and " " in dt_str:
                dt_str = dt_str.replace(" ", "T", 1)
            dt = datetime.fromisoformat(dt_str)
        except Exception:
            return f"Could not parse '{date_time}'. Use e.g. 2026-08-31T14:00 or 14:00."
        try:
            src_tz = ZoneInfo(_resolve_tz(from_timezone))
            dst_tz = ZoneInfo(_resolve_tz(to_timezone))
        except Exception:
            return f"Unknown timezone in '{from_timezone}' or '{to_timezone}'."
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=src_tz)
        converted = dt.astimezone(dst_tz)
        return (
            f"**{dt.strftime('%A %d %B %Y, %H:%M')}** in {src_tz.key} is "
            f"**{converted.strftime('%A %d %B %Y, %H:%M')}** in {dst_tz.key}."
        )
