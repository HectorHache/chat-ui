"""
household_prices — Price watch.

Track a product's price over time by URL; the watchdog checks each watch
periodically and notifies when the price is at or below the target.
Store: data/prices/<user_id>.json.
"""

import json
import os
import re
import time
import uuid
from pathlib import Path

_DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path.home() / "Documents/Workspaces/ui/data"))
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _path(user_id: str) -> Path:
    d = _DATA_DIR / "prices"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{user_id}.json"


def _load(user_id: str) -> list:
    p = _path(user_id)
    try:
        return json.loads(p.read_text("utf-8")) if p.exists() else []
    except Exception:
        return []


def _save(user_id: str, items: list) -> None:
    _path(user_id).write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")


class Tools:
    async def watch_price(self, name: str, url: str, target_price: float, __user__: dict | None = None) -> str:
        """
        Track a product's price and alert when it drops to or below a target.

        Use when the user says "track this price", "notify me when X is
        under €Y", or pastes a product link with a target price.

        :param name: Short name for the product (e.g. "Sony headphones").
        :param url: The product page URL to watch.
        :param target_price: Alert when the price is at or below this
            amount in EUR (e.g. 89.99).
        :return: Confirmation with the current detected price if available.
        """
        name = (name or "").strip()
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "Please provide a full product URL (https://…)."
        if not name:
            return "Please give the product a short name."
        try:
            target_price = float(target_price)
        except Exception:
            return "Please provide the target price as a number (e.g. 89.99)."

        user_id = (__user__ or {}).get("id") or "anonymous"
        items = _load(user_id)
        for it in items:
            if it.get("url") == url:
                it["name"] = name
                it["target_price"] = target_price
                it["active"] = True
                _save(user_id, items)
                return f"📈 Updated the price watch for **{name}** (target ≤ €{target_price:.2f})."
        item = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "url": url,
            "target_price": target_price,
            "added_at": time.time(),
            "active": True,
            "last_price": None,
            "last_check": None,
            "notified": False,
        }
        items.append(item)
        _save(user_id, items)
        return (
            f"📈 Price watch set for **{name}** — I'll check it regularly and "
            f"alert you when it's ≤ €{target_price:.2f} (id {item['id']})."
        )

    async def list_price_watches(self, __user__: dict | None = None) -> str:
        """
        List the active price watches.

        Use when the user asks "what am I tracking", "show my price watches".

        :return: Each watch with target and last known price.
        """
        user_id = (__user__ or {}).get("id") or "anonymous"
        items = [x for x in _load(user_id) if x.get("active")]
        if not items:
            return "No active price watches."
        lines = ["📊 **Price watches:**"]
        for it in items:
            last = f"last €{it['last_price']:.2f}" if it.get("last_price") is not None else "not checked yet"
            lines.append(f"- **{it['name']}** — target ≤ €{it['target_price']:.2f} ({last}, id {it['id']})")
        return "\n".join(lines)

    async def remove_price_watch(self, watch_id: str, __user__: dict | None = None) -> str:
        """
        Remove a price watch.

        Use when the user wants to stop tracking a product.

        :param watch_id: The watch id shown by list_price_watches.
        :return: Confirmation.
        """
        user_id = (__user__ or {}).get("id") or "anonymous"
        rid = (watch_id or "").strip()
        items = _load(user_id)
        kept = [x for x in items if x.get("id") != rid]
        if len(kept) == len(items):
            return f"No price watch with id '{rid}' found. Use list_price_watches to see active watches."
        _save(user_id, kept)
        return f"🗑️ Price watch {rid} removed."
