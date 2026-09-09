"""
household_waste — Waste collection calendar (local municipality).

Parses the municipality's public iCal (ICS) feed configured by the admin
(Valve: feed_urls, comma-separated). Returns the next collection dates and
waste types.
"""

import asyncio
import os
import re
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

_UA = "Chat-hache-household/1.0 (private home assistant)"


# English-only built content (B22): translate Dutch municipality type names
_NL_EN = {
    "restafval": "Residual waste",
    "gft": "Organic waste (GFT)",
    "papier & karton": "Paper & cardboard",
    "papier en karton": "Paper & cardboard",
    "papier": "Paper & cardboard",
    "kerstboom": "Christmas trees",
    "kerstbomen": "Christmas trees",
    "pmd": "Plastic, metal & drink cartons",
}


def _en_type(raw: str) -> str:
    return _NL_EN.get((raw or "").strip().lower(), raw or "Collection")


class Valves(BaseModel):
    feed_urls: str = "/Users/mick/Documents/Workspaces/ui/data/waste/afval2026.ics"
    timezone_offset_hours: int = 2


def _parse_ics(text: str) -> list:
    """Extract (summary, start_date) from VEVENT blocks."""
    events = []
    blocks = re.split(r"BEGIN:VEVENT", text)[1:]
    for b in blocks:
        end = b.find("END:VEVENT")
        block = b[:end] if end != -1 else b
        summary = ""
        dtstart = None
        for m in re.finditer(r"^([A-Z0-9;:=\-]+):(.*)$", block, re.M):
            line = m.group(0)
            if line.startswith("SUMMARY"):
                summary = re.sub(r"^SUMMARY(?:;[^:]*)?:", "", line).strip()
                summary = summary.replace("\\,", ",").replace("\\;", ";")
            elif line.startswith("DTSTART"):
                raw = line.split(":", 1)[-1].strip()
                try:
                    if "VALUE=DATE" in line.split(":")[0]:
                        dtstart = datetime.strptime(raw[:8], "%Y%m%d").date()
                    else:
                        dtstart = datetime.strptime(raw[:8], "%Y%m%d").date()
                except Exception:
                    dtstart = None
        if summary and dtstart:
            events.append({"type": _en_type(summary), "date": dtstart})
    return events


class Tools:
    valves = Valves()

    async def next_waste_pickups(self, count: int = 6) -> str:
        """
        Get the next waste collection dates for the household address.

        Use when the user asks "when is the trash picked up", "waste
        calendar", "which bin goes out when".

        :param count: How many upcoming collections to return (1-10).
        :return: Upcoming pickups (type + date + days from now). If the
            calendar feed is not configured, explain how to set it up.
        """
        count = max(1, min(10, int(count or 6)))
        urls = [u.strip() for u in (self.valves.feed_urls or "").split(",") if u.strip()]
        if not urls:
            return (
                "The waste calendar is not configured yet. Ask the administrator to set the "
                "municipality's iCal feed URL in the tool settings (Settings → Tools → "
                "Household Waste → feed_urls)."
            )

        events = []
        for u in urls:
            try:
                if u.startswith("file://"):
                    u = u[len("file://"):]
                if u.startswith("/") or u.startswith("."):
                    # local file (municipality PDF -> ICS conversion output)
                    text = Path(u).read_text("utf-8", errors="replace")
                else:
                    req = urllib.request.Request(u, headers={"User-Agent": _UA})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        text = resp.read().decode("utf-8", errors="replace")
                events.extend(_parse_ics(text))
            except Exception as e:
                events.append({"type": f"feed error: {u}", "date": None})
                continue

        if not events:
            return "No waste events found in the configured feed(s). The feed may have a different format — check the URLs."

        today = datetime.now().date()
        upcoming = sorted(
            [e for e in events if e["date"] and e["date"] >= today - timedelta(days=1)],
            key=lambda e: e["date"],
        )[:count]

        if not upcoming:
            return "No upcoming collections found in the configured feed(s)."

        lines = ["🗑 **Upcoming waste collections:**"]
        for e in upcoming:
            days = (e["date"] - today).days
            when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
            lines.append(f"- **{e['type']}** — {e['date'].strftime('%A %d %B')} ({when})")
        return "\n".join(lines)
