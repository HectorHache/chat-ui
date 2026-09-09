"""
household_ledger — Chat-native household finance.

Three isolated ledgers: each user's personal (data/ledger/<user_id>.db)
and one shared household ledger (data/ledger/household.db). SQLite,
stdlib only. Chat entry is the UX: the model parses "spent €23.45 at
Albert Heijn on groceries" and records it here.
"""

import csv
import io
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path.home() / "Documents/Workspaces/ui/data"))
_LEDGER_DIR = _DATA_DIR / "ledger"


def _db(scope: str, user_id: str) -> Path:
    _LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    if scope == "personal":
        return _LEDGER_DIR / f"{user_id}.db"
    return _LEDGER_DIR / "household.db"


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, amount_cents INTEGER, category TEXT, note TEXT, added_by TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS budgets (category TEXT PRIMARY KEY, amount_cents INTEGER)"
    )
    conn.commit()
    return conn


_CATEGORIES = {
    "groceries": "Groceries", "supermarket": "Groceries", "ah": "Groceries", "aldi": "Groceries",
    "jumbo": "Groceries", "lidl": "Groceries",
    "dining": "Dining out", "restaurant": "Dining out", "cafe": "Dining out", "lunch": "Dining out",
    "transport": "Transport", "train": "Transport", "bus": "Transport", "fuel": "Transport", "parking": "Transport",
    "household": "Household", "home": "Household", "furniture": "Household", "garden": "Household",
    "utilities": "Utilities", "energy": "Utilities", "water": "Utilities", "internet": "Utilities", "phone": "Utilities",
    "health": "Health", "pharmacy": "Health", "doctor": "Health", "gym": "Health",
    "clothing": "Clothing", "clothes": "Clothing",
    "entertainment": "Entertainment", "movies": "Entertainment", "streaming": "Entertainment", "games": "Entertainment",
    "kids": "Kids", "childcare": "Kids",
    "pets": "Pets",
    "gifts": "Gifts",
    "travel": "Travel", "holiday": "Travel", "vacation": "Travel",
    "insurance": "Insurance",
    "taxes": "Taxes",
    "education": "Education",
    "other": "Other",
}


def _category(raw: str) -> str:
    key = (raw or "").strip().lower()
    if not key:
        return "Other"
    if key in _CATEGORIES:
        return _CATEGORIES[key]
    # substring match: "Albert Heijn groceries" -> Groceries
    for k, v in sorted(_CATEGORIES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if k in key:
            return v
    return key.title()


class Tools:
    async def record_expense(self, amount: float, category: str = "", note: str = "", scope: str = "household", __user__: dict | None = None) -> str:
        """
        Record an expense in the household or personal ledger.

        Use when the user says "spent €23.45 at Albert Heijn on groceries",
        "paid 12 euros for parking", or "record X in my personal ledger".
        Default scope is the shared household ledger.

        :param amount: The amount in euros (e.g. 23.45).
        :param category: Optional category (groceries, dining, transport,
            household, utilities, health, clothing, entertainment, kids,
            pets, gifts, travel, insurance, other). Auto-mapped if given
            as a store or keyword.
        :param note: Short note (e.g. store or what it was for).
        :param scope: "household" (shared, default) or "personal".
        :return: Confirmation with the running monthly total for that scope.
        """
        try:
            cents = int(round(float(amount) * 100))
        except Exception:
            return "Please provide the amount as a number (e.g. 23.45)."
        if cents <= 0:
            return "The amount must be positive."
        user_id = (__user__ or {}).get("id") or "anonymous"
        if scope != "personal":
            scope = "household"

        cat = _category(category)
        conn = _conn(_db(scope, user_id))
        conn.execute(
            "INSERT INTO entries (ts, amount_cents, category, note, added_by) VALUES (?,?,?,?,?)",
            (time.time(), cents, cat, (note or "").strip(), user_id),
        )
        # running monthly total
        now = datetime.now(ZoneInfo("Europe/Amsterdam"))
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM entries WHERE ts >= ?", (month_start,)
        ).fetchone()[0]
        conn.commit()
        conn.close()
        scope_txt = "household ledger" if scope == "household" else "your personal ledger"
        return (
            f"💰 Recorded **€{cents / 100:.2f}** ({cat}) in the {scope_txt}."
            f" Month-to-date total: **€{total / 100:.2f}**."
        )

    async def ledger_summary(self, scope: str = "household", month: str = "", __user__: dict | None = None) -> str:
        """
        Summarize spending for the household or personal ledger.

        Use when the user asks "how much did we spend this month", "ledger
        summary", "where did the money go".

        :param scope: "household" (shared, default) or "personal".
        :param month: Optional month filter (YYYY-MM, default current).
        :return: Total + per-category breakdown, budgets if set.
        """
        user_id = (__user__ or {}).get("id") or "anonymous"
        if scope != "personal":
            scope = "household"
        conn = _conn(_db(scope, user_id))

        if month and len(month) == 7:
            start = datetime.strptime(month + "-01", "%Y-%m").replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
            end = start.replace(month=start.month % 12 + 1, day=1) if start.month < 12 else start.replace(year=start.year + 1, month=1, day=1)
            label = start.strftime("%B %Y")
        else:
            now = datetime.now(ZoneInfo("Europe/Amsterdam"))
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now
            label = "this month"
        start_ts = start.timestamp()
        end_ts = end.timestamp()

        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM entries WHERE ts >= ? AND ts < ?", (start_ts, end_ts)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT category, COALESCE(SUM(amount_cents),0) AS s FROM entries WHERE ts >= ? AND ts < ? GROUP BY category ORDER BY s DESC",
            (start_ts, end_ts),
        ).fetchall()
        budgets = {r[0]: r[1] for r in conn.execute("SELECT category, amount_cents FROM budgets")}
        conn.close()

        if total == 0 and not rows:
            scope_txt = "household" if scope == "household" else "personal"
            return f"No expenses recorded in the {scope_txt} ledger for {label}."

        lines = [f"📊 **{scope.title()} ledger — {label}:**", "", f"Total: **€{total / 100:.2f}**", ""]
        for cat, s in rows:
            line = f"- {cat}: €{s / 100:.2f}"
            if cat in budgets:
                b = budgets[cat]
                line += f" (budget €{b / 100:.2f}, {'✅' if s <= b else '⚠️ over by €' + f'{(s - b) / 100:.2f}'})"
            lines.append(line)
        if not rows:
            lines.append("- no categories yet")
        return "\n".join(lines)

    async def ledger_export(self, scope: str = "household", __user__: dict | None = None) -> str:
        """
        Export a ledger as CSV text.

        Use when the user asks for a CSV export or wants the ledger in
        spreadsheet form.

        :param scope: "household" (shared, default) or "personal".
        :return: CSV content the user can save.
        """
        user_id = (__user__ or {}).get("id") or "anonymous"
        if scope != "personal":
            scope = "household"
        conn = _conn(_db(scope, user_id))
        rows = conn.execute(
            "SELECT ts, amount_cents, category, note, added_by FROM entries ORDER BY ts DESC"
        ).fetchall()
        conn.close()

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "amount_eur", "category", "note", "added_by"])
        for ts, cents, cat, note, by in rows:
            dt = datetime.fromtimestamp(ts, ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d %H:%M")
            w.writerow([dt, f"{cents / 100:.2f}", cat, note or "", by or ""])
        return f"CSV export ({scope} ledger, {len(rows)} entries):\n\n```csv\n{buf.getvalue()}```"

    async def set_budget(self, category: str, amount: float, scope: str = "household", __user__: dict | None = None) -> str:
        """
        Set a monthly budget for a category.

        Use when the user says "set a budget of 300 euros for groceries".

        :param category: Category name (e.g. Groceries).
        :param amount: Monthly budget in euros.
        :param scope: "household" (shared, default) or "personal".
        :return: Confirmation.
        """
        try:
            cents = int(round(float(amount) * 100))
        except Exception:
            return "Please provide the budget as a number (e.g. 300)."
        if cents <= 0:
            return "The budget must be positive."
        user_id = (__user__ or {}).get("id") or "anonymous"
        if scope != "personal":
            scope = "household"
        cat = _category(category)
        conn = _conn(_db(scope, user_id))
        conn.execute(
            "INSERT INTO budgets (category, amount_cents) VALUES (?,?) ON CONFLICT(category) DO UPDATE SET amount_cents=excluded.amount_cents",
            (cat, cents),
        )
        conn.commit()
        conn.close()
        return f"🎯 Budget set: **€{cents / 100:.2f}/month** for {cat} in the {scope} ledger."
