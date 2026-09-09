"""
household_utils — Utilities pack.

Coin flip, dice, random picker, countdowns. Pure stdlib.
"""

import random
from datetime import datetime
from zoneinfo import ZoneInfo


class Tools:
    async def coin_flip(self) -> str:
        """
        Flip a coin.

        Use when the user asks to flip a coin, heads or tails, or wants a
        random yes/no decision.

        :return: Heads or Tails.
        """
        return f"🪙 **{random.choice(['Heads', 'Tails'])}**"

    async def roll_dice(self, sides: int = 6, count: int = 1) -> str:
        """
        Roll one or more dice.

        Use when the user asks to roll a die/dice or needs random numbers.

        :param sides: Number of sides per die (default 6).
        :param count: Number of dice to roll (1-10).
        :return: The individual results and the total.
        """
        sides = max(2, min(100, int(sides or 6)))
        count = max(1, min(10, int(count or 1)))
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls)
        if count == 1:
            return f"🎲 **{rolls[0]}** (d{sides})"
        return f"🎲 Rolls: **{', '.join(map(str, rolls))}** — total **{total}** (d{sides} × {count})"

    async def pick_random(self, items: str) -> str:
        """
        Pick a random item from a list.

        Use when the user asks to choose randomly between options: "who
        does the dishes", "what do we eat", "which movie".

        :param items: Comma-separated options (e.g. "pizza, sushi, tacos").
        :return: One randomly chosen option.
        """
        opts = [x.strip() for x in (items or "").split(",") if x.strip()]
        if not opts:
            return "Please provide options separated by commas."
        if len(opts) == 1:
            return f"Only one option given: **{opts[0]}**."
        return f"🎯 I pick: **{random.choice(opts)}**"

    async def countdown(self, target_datetime: str, timezone: str = "Europe/Amsterdam") -> str:
        """
        Calculate the time remaining until a date/time.

        Use when the user asks "how long until X", countdowns to an event,
        holidays, trips, deadlines.

        :param target_datetime: Target as ISO datetime (e.g. 2026-09-15T09:00).
        :param timezone: IANA timezone of the target (default Europe/Amsterdam).
        :return: Remaining days/hours/minutes (or how long ago if past).
        """
        try:
            tz = ZoneInfo(timezone)
            target = datetime.fromisoformat((target_datetime or "").strip())
            if target.tzinfo is None:
                target = target.replace(tzinfo=tz)
            now = datetime.now(tz)
        except Exception:
            return f"Could not parse '{target_datetime}' or timezone '{timezone}'. Use ISO format, e.g. 2026-09-15T09:00."
        delta = target - now
        if delta.total_seconds() < 0:
            ago = -delta
            return f"⏳ That was **{ago.days} days, {ago.seconds // 3600} hours, {(ago.seconds % 3600) // 60} minutes ago** ({target.strftime('%A %d %B %Y, %H:%M')})."
        days = delta.days
        hours = delta.seconds // 3600
        mins = (delta.seconds % 3600) // 60
        return f"⏳ **{days} days, {hours} hours, {mins} minutes** until {target.strftime('%A %d %B %Y, %H:%M')} ({timezone})."
