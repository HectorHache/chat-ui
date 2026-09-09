"""
household_shopping — Shared household shopping list.

One persistent household list (data/shopping/household.json) shared by all
users: add/check/clear/list.
"""

import json
import os
import time
import uuid
from pathlib import Path

_DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path.home() / "Documents/Workspaces/ui/data"))
_LIST_FILE = _DATA_DIR / "shopping" / "household.json"


def _load() -> list:
    try:
        if _LIST_FILE.exists():
            return json.loads(_LIST_FILE.read_text("utf-8"))
    except Exception:
        pass
    return []


def _save(items: list) -> None:
    _LIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LIST_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")


class Tools:
    async def shopping_add(self, items: str) -> str:
        """
        Add one or more items to the shared household shopping list.

        Use when the user says "add milk to the shopping list", "we need
        eggs and bread", or "add X to the list".

        :param items: Comma-separated items (e.g. "milk, eggs, bread").
        :return: Confirmation with the current open count.
        """
        parsed = [x.strip() for x in (items or "").split(",") if x.strip()]
        if not parsed:
            return "Please tell me what to add to the shopping list."
        lst = _load()
        existing = {x["text"].lower() for x in lst if not x.get("done")}
        added = []
        for it in parsed:
            if it.lower() in existing:
                continue
            lst.append({"id": str(uuid.uuid4())[:8], "text": it, "added_at": time.time(), "done": False, "done_at": None})
            added.append(it)
        _save(lst)
        open_count = len([x for x in lst if not x.get("done")])
        if added:
            return f"🛒 Added to the shopping list: **{', '.join(added)}**. ({open_count} open items)"
        return f"Those items are already on the list. ({open_count} open items)"

    async def shopping_list(self) -> str:
        """
        Show the shared household shopping list.

        Use when the user asks "what's on the shopping list", "show the
        shopping list", "what do we need to buy".

        :return: Open items (numbered), then recently checked items.
        """
        lst = _load()
        open_items = [x for x in lst if not x.get("done")]
        done_items = [x for x in lst if x.get("done")]
        if not open_items:
            return "The shopping list is empty 🎉." if not done_items else "All shopping list items are checked off 🎉."
        lines = ["🛒 **Shopping list:**"]
        for i, x in enumerate(sorted(open_items, key=lambda k: k.get("added_at", 0)), 1):
            lines.append(f"{i}. {x['text']} (id {x['id']})")
        if done_items:
            recent = sorted(done_items, key=lambda k: k.get("done_at", 0), reverse=True)[:5]
            lines.append("")
            lines.append("Recently checked: " + ", ".join(f"~~{x['text']}~~" for x in recent))
        return "\n".join(lines)

    async def shopping_check(self, item: str) -> str:
        """
        Mark one or more shopping list items as done.

        Use when the user says "bought the milk", "checked off eggs",
        "remove X from the list".

        :param item: Item text or its id (shown by shopping_list).
        :return: Confirmation.
        """
        needle = (item or "").strip().lower()
        if not needle:
            return "Which item did you buy?"
        lst = _load()
        matched = 0
        for x in lst:
            if not x.get("done") and (x["text"].lower() == needle or x["id"].lower() == needle):
                x["done"] = True
                x["done_at"] = time.time()
                matched += 1
        if matched:
            _save(lst)
            return f"✅ Checked off **{item}**. ({len([x for x in lst if not x.get('done')])} open items)"
        return f"No open item matches '{item}'. Use shopping_list to see what's on the list."

    async def shopping_clear(self, only_done: bool = True) -> str:
        """
        Clear the shopping list (by default only the checked items).

        Use when the user says "clear the shopping list" or "empty the list".

        :param only_done: True (default) removes only checked items; False
            removes everything.
        :return: Confirmation.
        """
        lst = _load()
        if not only_done:
            _save([])
            return "🗑️ Shopping list cleared (all items)."
        kept = [x for x in lst if not x.get("done")]
        removed = len(lst) - len(kept)
        _save(kept)
        return f"🗑️ Removed {removed} checked item(s). {len(kept)} open item(s) remain."
