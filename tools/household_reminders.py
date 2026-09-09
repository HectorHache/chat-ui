"""
household_reminders — Local, simple reminders.

Per-user store at <DATA_DIR>/reminders/<user_id>.json. Setting a reminder
stores it and confirms the due time; the watchdog checks every 5 minutes
and flags due reminders (admin machine gets a macOS notification).
Reminders surface again in chat via list_reminders.
"""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path.home() / "Documents/Workspaces/ui/data"))
_REM_DIR = _DATA_DIR / "reminders"


def _store_path(user_id: str) -> Path:
    _REM_DIR.mkdir(parents=True, exist_ok=True)
    return _REM_DIR / f"{user_id}.json"


def _load(user_id: str) -> list:
    p = _store_path(user_id)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return []


def _save(user_id: str, items: list) -> None:
    p = _store_path(user_id)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")


def _fmt(ts: float, user_tz: str = "Europe/Amsterdam") -> str:
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(ts, ZoneInfo(user_tz)).strftime("%A %d %B, %H:%M")


class Tools:
    async def set_reminder(self, text: str, minutes_from_now: int = 30, timezone: str = "Europe/Amsterdam", __user__: dict | None = None) -> str:
        """
        Set a local reminder for the user.

        Use when the user says "remind me in X minutes", "remind me to ...",
        "set a reminder". The reminder is stored locally per user and
        checked by the watchdog every 5 minutes.

        :param text: What to remind about (e.g. "take the pizza out of the oven").
        :param minutes_from_now: In how many minutes the reminder should fire (1-1440).
        :param timezone: IANA timezone for the due time display (default Europe/Amsterdam).
        :return: Confirmation with the due date/time.
        """
        text = (text or "").strip()
        if not text:
            return "Please tell me what to remind you about."
        minutes = max(1, min(1440, int(minutes_from_now or 30)))
        user_id = (__user__ or {}).get("id") or "anonymous"
        due = time.time() + minutes * 60
        item = {
            "id": str(uuid.uuid4())[:8],
            "text": text,
            "set_at": time.time(),
            "due_at": due,
            "status": "pending",
        }
        items = _load(user_id)
        items.append(item)
        _save(user_id, items)
        return (
            f"✅ Reminder set for **{_fmt(due, timezone)}** (in {minutes} min): \"{text}\" "
            f"(id {item['id']}). The watchdog will flag it when due; ask me 'any reminders?' anytime."
        )

    async def list_reminders(self, __user__: dict | None = None) -> str:
        """
        List the user's pending reminders.

        Use when the user asks "what reminders do I have", "any reminders?",
        "show my reminders".

        :return: List of pending reminders with due times, or none.
        """
        user_id = (__user__ or {}).get("id") or "anonymous"
        items = [i for i in _load(user_id) if i.get("status") == "pending"]
        if not items:
            return "You have no pending reminders."
        lines = []
        for i in sorted(items, key=lambda x: x.get("due_at", 0)):
            lines.append(f"- **{i.get('text')}** — due {_fmt(i.get('due_at', 0))} (id {i.get('id')})")
        return "Your pending reminders:\n" + "\n".join(lines)

    async def clear_reminder(self, reminder_id: str, __user__: dict | None = None) -> str:
        """
        Remove a reminder.

        Use when the user asks to cancel/delete/clear a specific reminder.

        :param reminder_id: The reminder id shown when it was set or listed.
        :return: Confirmation or error.
        """
        user_id = (__user__ or {}).get("id") or "anonymous"
        rid = (reminder_id or "").strip()
        items = _load(user_id)
        kept = [i for i in items if i.get("id") != rid]
        if len(kept) == len(items):
            return f"No reminder with id '{rid}' was found. Use list_reminders to see active reminders."
        _save(user_id, kept)
        return f"✅ Reminder {rid} cleared."
