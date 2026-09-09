"""
household_meals — Meal planner + grocery list.

Plans dinners for N days from TheMealDB recipes (matching available
ingredients when given) and automatically adds the missing ingredients to
the shared household shopping list.
"""

import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

_DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path.home() / "Documents/Workspaces/ui/data"))
_UA = "Chat-hache-household/1.0 (private home assistant)"


def _plan_path(user_id: str) -> Path:
    d = _DATA_DIR / "meals"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{user_id}.json"


def _load_plan(user_id: str) -> dict:
    p = _plan_path(user_id)
    try:
        return json.loads(p.read_text("utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _save_plan(user_id: str, plan: dict) -> None:
    _plan_path(user_id).write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")


def _shopping_add(items: list) -> list:
    """Add items to the shared shopping list; returns newly added names."""
    shop_file = _DATA_DIR / "shopping" / "household.json"
    try:
        lst = json.loads(shop_file.read_text("utf-8")) if shop_file.exists() else []
    except Exception:
        lst = []
    shop_file.parent.mkdir(parents=True, exist_ok=True)
    existing = {x["text"].lower() for x in lst if not x.get("done")}
    import uuid

    added = []
    for it in items:
        if not it or it.lower() in existing:
            continue
        lst.append({"id": str(uuid.uuid4())[:8], "text": it, "added_at": time.time(), "done": False, "done_at": None})
        added.append(it)
    shop_file.write_text(json.dumps(lst, ensure_ascii=False, indent=2), "utf-8")
    return added


class Tools:
    async def plan_meals(self, days: int = 5, ingredients: str = "", __user__: dict | None = None) -> str:
        """
        Plan dinners for the next days and add the needed groceries to the
        shopping list.

        Use when the user says "plan 5 dinners", "what should we eat this
        week", "meal plan" — optionally with ingredients they already have.

        :param days: Number of days to plan (1-7).
        :param ingredients: Optional comma-separated ingredients already in
            the kitchen (recipes matching these are preferred).
        :return: The plan with recipes per day, plus what was added to the
            shopping list.
        """
        days = max(1, min(7, int(days or 5)))
        user_id = (__user__ or {}).get("id") or "anonymous"
        have = [i.strip().lower() for i in (ingredients or "").split(",") if i.strip()]

        meals = []
        seen = set()
        # 1) match available ingredients
        for ing in have[:3]:
            try:
                url = "https://www.themealdb.com/api/json/v1/1/filter.php?" + urllib.parse.urlencode({"i": ing})
                data = await asyncio.to_thread(self._get_json, url)
                for m in (data.get("meals") or []):
                    if m["idMeal"] not in seen:
                        seen.add(m["idMeal"])
                        meals.append((ing, m))
            except Exception:
                continue

        # 2) fill remaining days with popular category meals
        cats = ["Chicken", "Beef", "Seafood", "Vegetarian", "Pasta", "Dessert"]
        ci = 0
        try:
            while len(meals) < days:
                cat = cats[ci % len(cats)]
                ci += 1
                url = "https://www.themealdb.com/api/json/v1/1/filter.php?" + urllib.parse.urlencode({"c": cat})
                data = await asyncio.to_thread(self._get_json, url)
                for m in (data.get("meals") or []):
                    if m["idMeal"] not in seen:
                        seen.add(m["idMeal"])
                        meals.append((None, m))
                        break
        except Exception:
            pass

        if not meals:
            return "Could not load any recipes right now — try again in a moment."

        # 3) fetch details for the planned meals
        plan = []
        for i, (ing, m) in enumerate(meals[:days]):
            try:
                url = "https://www.themealdb.com/api/json/v1/1/lookup.php?" + urllib.parse.urlencode({"i": m["idMeal"]})
                data = await asyncio.to_thread(self._get_json, url)
                meal = (data.get("meals") or [None])[0]
            except Exception:
                meal = None
            if meal is None:
                continue
            ings = []
            for k in range(1, 21):
                name = (meal.get(f"strIngredient{k}") or "").strip()
                meas = (meal.get(f"strMeasure{k}") or "").strip()
                if name:
                    ings.append({"name": name, "measure": meas})
            plan.append({"day": i + 1, "name": meal.get("strMeal"), "area": meal.get("strArea", ""), "ingredients": ings})

        if not plan:
            return "Recipe details could not be loaded — try again in a moment."

        _save_plan(user_id, {"planned_at": time.time(), "days": plan})

        # 4) add missing ingredients to the shared shopping list
        missing = []
        for day in plan:
            for ing in day["ingredients"]:
                nm = ing["name"].lower()
                if have and nm in have:
                    continue
                if nm in ("salt", "pepper", "water", "sugar", "oil", "olive oil", "butter"):
                    continue  # staples
                missing.append(ing["name"])
        added = _shopping_add(sorted(set(missing)))

        lines = ["🍽 **Dinner plan:**"]
        for day in plan:
            lines.append(f"- Day {day['day']}: **{day['name']}** ({day['area']})")
        if added:
            lines.append("")
            lines.append(f"🛒 Added to shopping list: {', '.join(added[:15])}" + (" …" if len(added) > 15 else ""))
        lines.append("")
        lines.append("_Ask the user which day to cook in detail._")
        return "\n".join(lines)

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
