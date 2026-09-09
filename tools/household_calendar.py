"""
household_calendar — Google Calendar via secret ICS feed (read-only, no OAuth).

B40 (2026-08-31): Google's hosted Calendar MCP requires a PUBLISHED OAuth
consent screen (manual human verification was blocked for this project) and
testing-mode refresh tokens expire after 7 days. Instead we use Google
Calendar's "Secret address in iCal format" — a private, unguessable,
read-only ICS URL that needs no OAuth at all, never expires, and needs no
per-user consent. The URL lives in ui/.env as CALENDAR_ICS_URL (also on
disk as ui/.icalsec, mode 600). Never log or echo the URL.

B22: output is English only.
"""

import os
import re
import time
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

_UI_DIR = Path(os.environ.get("UI_DIR") or Path.home() / "Documents/Workspaces/ui")
_DATA_DIR = Path(os.environ.get("DATA_DIR") or _UI_DIR / "data")
_UA = "Chat-hache-household/1.0 (private home assistant)"
_TZ = ZoneInfo("Europe/Amsterdam")

_ICS_CACHE: dict = {"t": 0.0, "body": ""}
_ICS_TTL = 300  # seconds — don't hammer Google's feed


class Valves(BaseModel):
    ics_url: str = ""
    days_ahead: int = 14


def _ics_url() -> str:
    """Valve value wins; fall back to env, then ui/.env (secret kept off logs)."""
    v = os.environ.get("CALENDAR_ICS_URL", "")
    if v:
        return v.strip()
    try:
        for line in (_UI_DIR / ".env").read_text("utf-8").splitlines():
            if line.startswith("CALENDAR_ICS_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _fetch_ics(url: str) -> str:
    if time.time() - _ICS_CACHE["t"] < _ICS_TTL and _ICS_CACHE["body"]:
        return _ICS_CACHE["body"]
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    _ICS_CACHE.update(t=time.time(), body=body)
    return body


# ---------- ICS parsing (stdlib only) ----------

def _unfold(block: str) -> list[tuple[str, str]]:
    lines = []
    for raw in block.splitlines():
        if raw[:1] in (" ", "\t"):
            if lines:
                lines[-1] = lines[-1] + raw[1:]
            continue
        lines.append(raw)
    out = []
    for ln in lines:
        if ":" in ln:
            name, _, value = ln.partition(":")
            out.append((name.upper(), value.strip()))
    return out


def _parse_dt(key: str, value: str):
    """Return (datetime, is_all_day) from an ICS property key+value.
    Handles VALUE=DATE, TZID=..., Z suffix, and plain date/datetime."""
    value = value.strip()
    params = {}
    if ";" in key:
        for p in key.split(";")[1:]:
            if "=" in p:
                k, _, v = p.partition("=")
                params[k.upper()] = v
    if params.get("VALUE", "").upper() == "DATE":
        return datetime.combine(date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}"), dtime.min, tzinfo=_TZ), True
    if params.get("TZID"):
        try:
            tz = ZoneInfo(params["TZID"])
        except Exception:
            tz = _TZ
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz), False
    if value.endswith("Z"):
        return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo("UTC")), False
    if len(value) == 8 and value.isdigit():
        return datetime.combine(date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}"), dtime.min, tzinfo=_TZ), True
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=_TZ), False


def _get(fields: dict, name: str):
    """Value for a property, tolerating parameters in the key (e.g. DTSTART;VALUE=DATE)."""
    if name in fields:
        return name, fields[name]
    for k, v in fields.items():
        if k.startswith(name + ";"):
            return k, v
    return None, None


def _parse_duration(value: str) -> timedelta:
    m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value or "")
    if not m:
        return timedelta(hours=1)
    w, d, h, mi, s = (int(x or 0) for x in m.groups())
    return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def _parse_rrule(value: str) -> dict:
    r = {}
    for part in value.split(";"):
        k, _, v = part.partition("=")
        r[k.upper()] = v
    return r


def _jump_ahead(start: datetime, freq: str, interval: int, floor: datetime, byday: set) -> datetime:
    """Move `start` forward to the first occurrence at/after `floor` (approximate)."""
    if floor <= start:
        return start
    if freq == "DAILY":
        days = (floor.date() - start.date()).days
        skip = max(0, -(-days // interval))
        return start + timedelta(days=skip * interval)
    if freq == "WEEKLY":
        if byday:
            weeks = max(0, (floor.date() - start.date()).days // 7)
            cur = start + timedelta(weeks=(weeks // interval) * interval)
            for _ in range(300):
                for off in range(7):
                    c = cur + timedelta(days=off)
                    if c.weekday() in byday and c >= floor:
                        return c.replace(hour=start.hour, minute=start.minute, second=start.second,
                                         tzinfo=start.tzinfo)
                cur += timedelta(weeks=interval)
            return start
        weeks = max(0, (floor.date() - start.date()).days // 7)
        skip = max(0, -(-weeks // interval))
        return start + timedelta(weeks=skip * interval)
    if freq == "MONTHLY":
        months = (floor.year - start.year) * 12 + (floor.month - start.month)
        skip = max(0, -(-months // interval))
        cur = start
        for _ in range(skip):
            try:
                cur = cur.replace(month=cur.month + interval)
            except ValueError:
                cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=min(start.day, 28))
        if cur < floor:  # clamp edge (e.g. 31st -> shorter month)
            try:
                cur = cur.replace(month=cur.month + interval)
            except ValueError:
                cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=min(start.day, 28))
        return cur
    if freq == "YEARLY":
        years = floor.year - start.year
        skip = max(0, -(-years // interval))
        cur = start
        for _ in range(skip):
            try:
                cur = cur.replace(year=cur.year + interval)
            except ValueError:  # Feb 29
                cur = cur.replace(month=2, day=28)
        if cur < floor:
            try:
                cur = cur.replace(year=cur.year + interval)
            except ValueError:
                cur = cur.replace(month=2, day=28)
        return cur
    return start


def _expand(start: datetime, duration: timedelta, rrule: str,
            floor: datetime, horizon: datetime, cap: int = 80) -> list[datetime]:
    """Expand a possibly recurring event into start datetimes within [floor, horizon)."""
    if not rrule:
        return [start] if start >= floor else []

    r = _parse_rrule(rrule)
    freq = r.get("FREQ", "").upper()
    interval = int(r.get("INTERVAL", 1) or 1)
    until = None
    if r.get("UNTIL"):
        try:
            u = r["UNTIL"].replace("Z", "")
            until = datetime.strptime(u, "%Y%m%dT%H%M%S").replace(tzinfo=_TZ)
        except Exception:
            until = None
    count = int(r.get("COUNT", 0) or 0)
    byday = {d.strip().upper().lstrip("+-0123456789") for d in r.get("BYDAY", "").split(",") if d.strip()}
    weekday_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    daynums = {weekday_map[d] for d in byday if d in weekday_map}

    cur = _jump_ahead(start, freq, interval, floor, daynums) if freq else start
    out = []
    emitted = 0
    while len(out) < cap:
        if until and cur > until:
            break
        if count and emitted >= count:
            break
        if cur >= horizon:
            break
        if freq == "WEEKLY" and daynums:
            week_start = cur - timedelta(days=cur.weekday())
            for off in range(7):
                cand = week_start + timedelta(days=off)
                if cand.weekday() not in daynums:
                    continue
                cand = cand.replace(hour=start.hour, minute=start.minute, second=start.second, tzinfo=start.tzinfo)
                if until and cand > until:
                    continue
                if cand < floor:
                    continue
                if cand >= horizon:
                    break
                out.append(cand)
                emitted += 1
                if count and emitted >= count:
                    break
            if count and emitted >= count:
                break
        else:
            if cur >= floor:
                out.append(cur)
                emitted += 1
        if freq == "DAILY":
            cur += timedelta(days=interval)
        elif freq == "WEEKLY":
            cur += timedelta(weeks=interval)
        elif freq == "MONTHLY":
            try:
                cur = cur.replace(month=cur.month + interval)
            except ValueError:
                cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=min(start.day, 28))
        elif freq == "YEARLY":
            try:
                cur = cur.replace(year=cur.year + interval)
            except ValueError:
                cur = cur.replace(month=2, day=28)
        else:
            break
    return out


class Tools:
    valves = Valves()

    async def calendar_events(self, days: int = 1, __user__: dict | None = None) -> str:
        """
        Get upcoming household calendar events (read-only).

        Use when the user asks "what's on today", "what's on the calendar",
        "when is the appointment", "do we have anything this week".

        :param days: How many days ahead to look (1-30).
        :return: Sorted upcoming events (day, time, title) or a clear
            message when there is nothing.
        """
        days = max(1, min(30, int(days or 1)))
        url = (self.valves.ics_url or _ics_url()).strip()
        if not url:
            return (
                "The household calendar is not configured yet. Ask the administrator "
                "to add the calendar ICS URL to the environment, then try again."
            )

        try:
            body = _fetch_ics(url)
        except Exception as e:
            return f"Could not reach the household calendar feed right now ({e}). Try again later."

        now = datetime.now(_TZ)
        floor = now - timedelta(hours=1)
        horizon = now + timedelta(days=days)
        overrides: dict[str, set] = {}  # uid -> {override start datetimes}
        masters: list[dict] = []

        for m in re.finditer(r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT", body, re.S):
            block = m.group(1)
            fields = dict(_unfold(block))
            if fields.get("STATUS", "").upper() == "CANCELLED":
                continue
            summary = (fields.get("SUMMARY") or "(no title)").strip()
            uid = fields.get("UID", "")
            dkey, dval = _get(fields, "DTSTART")
            if dval is None:
                continue  # no start time — nothing sensible to show
            try:
                start, all_day = _parse_dt(dkey, dval)
            except Exception:
                continue  # unparseable start — skip rather than guess
            ekey, eval_ = _get(fields, "DTEND")
            if eval_ is not None:
                try:
                    end, _ = _parse_dt(ekey, eval_)
                    duration = end - start if end > start else timedelta(hours=1)
                except Exception:
                    duration = _parse_duration(fields.get("DURATION"))
            else:
                duration = _parse_duration(fields.get("DURATION"))
            if all_day:
                duration = max(duration, timedelta(days=1))
            rkey, _ = _get(fields, "RECURRENCE-ID")
            if rkey is not None:
                overrides.setdefault(uid, set()).add(start.replace(tzinfo=None))
                continue
            masters.append({
                "summary": summary,
                "start": start,
                "all_day": all_day,
                "duration": duration,
                "location": (fields.get("LOCATION") or "").strip(),
                "rrule": fields.get("RRULE", ""),
                "uid": uid,
            })

        # expand + dedupe against explicit overrides
        events = []
        for ev in masters:
            starts = _expand(ev["start"], ev["duration"], ev["rrule"], floor, horizon)
            for s in starts:
                if s.replace(tzinfo=None) in overrides.get(ev["uid"], set()):
                    continue
                events.append((s, ev))
        events.sort(key=lambda t: t[0])
        events = events[:50]

        if not events:
            return f"No upcoming events in the next {days} day(s)."

        def _fmt(s: datetime, all_day: bool) -> str:
            if all_day:
                return s.strftime("%a %d %b")
            return s.strftime("%a %d %b %H:%M")

        lines = [f"📅 Upcoming events (next {days} day(s)):"]
        for s, ev in events:
            when = _fmt(s, ev["all_day"])
            line = f"• {when} — {ev['summary']}"
            if ev["location"]:
                line += f" ({ev['location']})"
            lines.append(line)
        return "\n".join(lines)
