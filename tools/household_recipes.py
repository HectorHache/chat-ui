"""
household_recipes — Recipe helper.

Two halves:
1. "What can I make with chicken, rice and an onion?" via the free
   TheMealDB API (no key).
2. Save/list recipes in the shared "Household > Recipes" knowledge collection
   so every member can browse them in Workspace → Knowledge and every model
   can answer questions about them (RAG).

Design (so overlapping recipe names stay useful):
- Each recipe file is a markdown doc with a YAML front-matter block carrying
  {title, summary, saved_on, tags, source}. `summary` is a one-line "what
  makes THIS variant different" — it drives describe_recipes() and the index.
- save_recipe is VARIANT-AWARE: same title + same content is idempotent; same
  title + DIFFERENT content never silently overwrites — it asks the user
  whether to overwrite or save as a new (distinctly named) variant.
- describe_recipes() returns a numbered shortlist of variants with a
  differentiator each, so nobody has to open files to tell them apart.
- recipe_content() returns the FULL content of one chosen file so the model
  can act on exactly that variant (not a blended RAG read).
- Every successful save/update regenerates `000-recipe-index.md` in the
  Recipes folder (sorts to the top of the list) = the manual glance map.
"""

import asyncio
import datetime as _dt
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

_UA = "Chat-hache-household/1.0 (private home assistant)"

# Shared Household knowledge collection (owned by the admin) + its Recipes folder.
_KB_ID = "8d2674fc-39f8-4876-aec7-a283932dd2ca"          # Household
_RECIPES_DIR_ID = "5d497a1b-2499-4706-9c61-4403eb59ac96"  # Household > Recipes
_ADMIN_ID = "4800bc91-2e6b-4a41-82db-65777edcbad7"
_API = "http://127.0.0.1:8390"
_INDEX_FILENAME = "000-recipe-index.md"


# --------------------------------------------------------------------------
# Internal helpers (leading "_" -> never exposed as tool calls by Open WebUI)
# --------------------------------------------------------------------------

def _admin_token() -> str:
    """Stateless admin JWT, minted exactly like scripts/register-tools.py.

    Tools run inside the OWUI server process, whose environment carries
    WEBUI_SECRET_KEY_FILE (set in the launchd plist). No stored secret needed.
    """
    import open_webui.utils.auth as _auth  # lazy: heavy, already imported server-side

    key_file = os.environ.get("WEBUI_SECRET_KEY_FILE")
    if key_file and os.path.exists(key_file):
        os.environ.setdefault("WEBUI_SECRET_KEY", open(key_file).read().strip())
    return _auth.create_token(data={"id": _ADMIN_ID})


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "recipe"


def _api(method: str, path: str, payload: dict | None = None, multipart: tuple | None = None) -> dict | list:
    """Call the local OWUI API. `multipart` = (filename, content_bytes, content_type)."""
    token = _admin_token()
    url = _API + path
    headers = {"Authorization": f"Bearer {token}"}
    data = None

    if multipart is not None:
        filename, content, content_type = multipart
        boundary = "----hachekb" + uuid.uuid4().hex
        body = bytearray()
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        body += content
        body += f"\r\n--{boundary}--\r\n".encode()
        data = bytes(body)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {body[:400]}")


def _kb_files(directory_id: str) -> list[dict]:
    """List files in the Household collection (optionally one folder)."""
    q = urllib.parse.urlencode({"directory_id": directory_id} if directory_id else {"directory_id": ""})
    resp = _api("GET", f"/api/v1/knowledge/{_KB_ID}/files?{q}")
    return resp.get("items", []) if isinstance(resp, dict) else []


def _file_content(file_id: str) -> str:
    resp = _api("GET", f"/api/v1/files/{file_id}/data/content")
    return resp.get("content", "") if isinstance(resp, dict) else ""


def _is_index_file(filename: str) -> bool:
    return (filename or "").lower() == _INDEX_FILENAME


def _parse_fm(content: str) -> dict:
    """Best-effort YAML front-matter parse for our own files; never fails."""
    meta = {"title": "", "summary": "", "tags": "", "source": "", "saved_on": ""}
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", content or "", re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip()
    return meta


def _recipe_files() -> list[dict]:
    """All recipe files in the Recipes folder (content read), excluding the index."""
    out = []
    for f in _kb_files(_RECIPES_DIR_ID):
        name = f.get("filename") or ""
        if _is_index_file(name):
            continue
        fid = f.get("id")
        try:
            content = _file_content(fid)
        except Exception:
            content = ""
        meta = _parse_fm(content)
        fallback = name.rsplit(".", 1)[0]
        h1 = re.search(r"^#\s+(.+)$", content or "", re.M)
        title = meta.get("title") or (h1.group(1).strip() if h1 else fallback)
        out.append(
            {
                "id": fid,
                "filename": name,
                "content": content,
                "title": title,
                "summary": meta.get("summary") or "",
                "tags": meta.get("tags") or "",
                "source": meta.get("source") or "",
                "saved_on": meta.get("saved_on") or "",
            }
        )
    return out


def _find_existing(slug: str) -> dict | None:
    """Find an existing Household recipe file this title already maps to.

    Matches on the slug in the filename (robust to names like
    'natillas.md' / '260905-recipe-natillas.md'), across the Recipes folder
    and the collection root.
    """
    for directory_id in (_RECIPES_DIR_ID, ""):
        for f in _kb_files(directory_id):
            if _is_index_file(f.get("filename") or ""):
                continue
            name = (f.get("filename") or "").rsplit(".", 1)[0].lower()
            if name == slug or name.endswith("-" + slug) or name.endswith(slug):
                return f
    return None


def _content_eq(content_a: str, content_b: str) -> bool:
    """Content equality ignoring the auto-written saved_on line (a re-save on a
    later day of the SAME recipe must count as unchanged)."""
    def norm(s: str) -> str:
        lines = []
        for ln in (s or "").splitlines():
            if ln.strip().lower().startswith("saved_on:"):
                continue
            lines.append(ln.rstrip())
        return "\n".join(lines).strip()

    return norm(content_a) == norm(content_b)


def _split_lines(value: str) -> list[str]:
    out = []
    for ln in re.split(r"[\r\n]+", (value or "").strip()):
        for part in re.split(r"\s*;\s*|\s*,\s*", ln.strip()):
            if part.strip():
                out.append(part.strip())
    return out


def _match_recipe(name: str, recipes: list[dict]) -> tuple[dict | None, str | None]:
    """Resolve a user-supplied name/number to ONE recipe dict.

    Returns (recipe, None) on a single match, else (None, friendly_message).
    """
    name = (name or "").strip().lower()
    if not name:
        return None, "Please tell me which saved recipe you mean."
    if not recipes:
        return None, "No recipes saved yet. Ask me to save one first."

    mnum = re.match(r"#?\s*(\d+)$", name)
    if mnum:
        ordered = sorted(recipes, key=lambda x: (x["title"] or "").lower())
        idx = int(mnum.group(1)) - 1
        if 0 <= idx < len(ordered):
            return ordered[idx], None
        return None, f"Only {len(ordered)} recipe(s) saved. Use describe_recipes to list them."

    matches = []
    for r in recipes:
        title = (r["title"] or "").lower()
        base = (r["filename"] or "").rsplit(".", 1)[0].lower()
        if title == name or base == name:
            return r, None
        if name in title or name in base:
            matches.append(r)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        lines = ["Several saved recipes match; which one do you mean?"]
        for i, r in enumerate(sorted(matches, key=lambda x: (x["title"] or "").lower()), 1):
            what = r["summary"] or r["tags"] or "—"
            lines.append(f"{i}. {r['title']} · {what}")
        return None, "\n".join(lines)
    return None, f"No saved recipe named '{name}'. Use describe_recipes to list what we have."


def _render_card(recipe: dict) -> str:
    """Deterministic 'pretty card' for a recipe (no LLM calls).

    Uses the YAML metadata + Ingredients/Instructions sections when present
    (our template). For hand-written docs that don't follow the template,
    gracefully shows the cleaned content (front-matter/H1 stripped) so the
    chat renderer still displays it nicely.
    """
    content = recipe.get("content") or ""
    meta = _parse_fm(content)
    title = (recipe.get("title") or meta.get("title") or "").strip()
    summary = meta.get("summary") or ""
    tags = meta.get("tags") or ""
    source = meta.get("source") or ""
    saved = meta.get("saved_on") or recipe.get("saved_on") or ""

    out = [f"### {title}"]
    if summary:
        out += ["", f"_{summary}_"]

    has_template_sections = bool(
        re.search(r"^##\s+(ingredients|ingredientes|instructions|method|elaboraci|modo|preparaci)", content or "", re.M | re.I)
    )
    if has_template_sections:
        ings, steps = _extract_sections(content)
        if ings:
            out += ["", "**Ingredients**", ""]
            out += [f"- {i}" for i in ings]
        if steps:
            out += ["", "**Method**", ""]
            out += [f"{n}. {s}" for n, s in enumerate(steps, 1)]
        if not ings and not steps:
            body = re.sub(r"\A---.*?---\s*", "", content, flags=re.DOTALL).strip()
            out += ["", body or "*No readable body.*"]
    else:
        body = re.sub(r"\A---.*?---\s*", "", content, flags=re.DOTALL)
        body = re.sub(r"^#\s+.+\n?", "", body, count=1).strip()
        out += ["", body] if body else []

    bits = [b for b in (tags, source) if b]
    if bits or saved:
        out += ["", "—", f"*{' · '.join(bits)}{' · saved ' + saved if saved else ''}*"]
    return "\n".join(out).strip()


def _extract_sections(content: str) -> tuple[list[str], list[str]]:
    """Pull (ingredients, steps) from template-style ## sections."""
    ings: list[str] = []
    steps: list[str] = []
    section = ""
    for ln in (content or "").splitlines():
        m = re.match(r"^##\s+(.+)$", ln.strip())
        if m:
            section = (m.group(1) or "").strip().lower()
            continue
        s = ln.strip()
        if not s or s.startswith("|") or s.startswith("---"):
            continue
        if section in ("ingredients", "ingredientes"):
            if s.startswith("-"):
                ings.append(s.lstrip("- ").strip())
        elif section in (
            "instructions", "method", "elaboración", "elaboracion",
            "modo de preparación", "modo de preparacion",
            "preparación", "preparacion", "pasos", "steps",
        ):
            mm = re.match(r"^\d+[\.\)]\s*(.*)$", s)
            if mm:
                steps.append(mm.group(1).strip())
            elif not s.startswith(("#", "**")):
                steps.append(s)
    return ings, steps


def _to_markdown(
    title: str, ingredients: str, instructions: str, tags: str, source: str, summary: str
) -> tuple[str, str]:
    slug = _slugify(title)
    today = _dt.date.today().isoformat()

    ings = _split_lines(ingredients)
    steps = [ln.strip() for ln in re.split(r"[\r\n]+", (instructions or "").strip()) if ln.strip()]
    steps = [re.sub(r"^\s*(?:\d+[\.\)]|[-*])\s*", "", s) for s in steps]

    md = ["---", f"title: {title.strip()}"]
    if summary:
        md.append(f"summary: {summary.strip()}")
    md.append(f"saved_on: {today}")
    if tags:
        md.append("tags: " + ", ".join(_split_lines(tags)))
    if source:
        md.append(f"source: {source.strip()}")
    md += ["---", "", f"# {title.strip()}", ""]
    if summary:
        md += [f"> {summary.strip()}", ""]
    md += ["## Ingredients", ""]
    md += [f"- {i}" for i in ings] or ["- (none provided)"]
    md += ["", "## Instructions", ""]
    md += [f"{n}. {s}" for n, s in enumerate(steps, 1)] or ["1. (none provided)"]
    return "\n".join(md), slug


def _create_or_update_file(content_md: str, filename: str, existing_file_id: str | None) -> None:
    """Create a new recipe file or update an existing one in Household > Recipes."""
    if existing_file_id:
        _api("POST", f"/api/v1/files/{existing_file_id}/data/content/update", {"content": content_md})
        return
    # New file: upload + process synchronously, then register into the Recipes folder.
    # process_in_background=false is required: file/add re-ingests the content into
    # the KB collection, and that content must already be extracted synchronously or
    # OWUI reports "content provided is empty".
    up = _api("POST", "/api/v1/files/?process=true&process_in_background=false",
              multipart=(filename, content_md.encode("utf-8"), "text/markdown"))
    file_id = up.get("id") if isinstance(up, dict) else None
    if not file_id:
        raise RuntimeError("File upload returned no id.")
    _api("POST", f"/api/v1/knowledge/{_KB_ID}/file/add", {"file_id": file_id, "directory_id": _RECIPES_DIR_ID})


def _rebuild_index(recipe_list: list[dict] | None = None) -> None:
    """Regenerate 000-recipe-index.md in the Recipes folder (auto, best-effort)."""
    try:
        recipes = recipe_list if recipe_list is not None else _recipe_files()
        rows = sorted(recipes, key=lambda r: (r["title"] or "").lower())
        lines = [
            "# Household Recipe Index",
            "",
            "_Automatically refreshed whenever a recipe is saved or updated._",
            "",
            "| Recipe | What it is | Saved |",
            "|---|---|---|",
        ]
        for i, r in enumerate(rows, 1):
            what = r["summary"] or (r["tags"] or "—")
            saved = r["saved_on"] or "—"
            title = (r["title"] or "").replace("|", "/").strip()
            what_safe = str(what).replace("|", "/").strip()
            lines.append(f"| {i}. {title} | {what_safe} | {saved} |")
        if not rows:
            lines.append("| — | No recipes saved yet. | — |")
        md = "\n".join(lines) + "\n"

        existing = None
        for f in _kb_files(_RECIPES_DIR_ID):
            if (f.get("filename") or "").lower() == _INDEX_FILENAME:
                existing = f
                break
        _create_or_update_file(md, _INDEX_FILENAME, existing["id"] if existing else None)
    except Exception:
        pass  # never let an index hiccup fail a recipe save


class Tools:
    async def find_recipes(self, ingredients: str, limit: int = 3) -> str:
        """
        Find recipes you can make with the ingredients you have.

        Use when the user asks what to cook, "what can I make with X",
        dinner ideas from available ingredients.

        :param ingredients: Comma-separated list of ingredients the user
            has (e.g. "chicken, rice, onion").
        :param limit: Maximum number of recipe ideas (1-5).
        :return: Recipes with name, origin, needed ingredients and a short
            preparation summary.
        """
        ingredients = (ingredients or "").strip()
        if not ingredients:
            return "Please list some ingredients you have."
        limit = max(1, min(5, int(limit or 3)))
        items = [i.strip() for i in ingredients.split(",") if i.strip()]

        meal_ids = []
        for ing in items[:2]:  # the API filters by a single ingredient; try up to 2
            try:
                url = "https://www.themealdb.com/api/json/v1/1/filter.php?" + urllib.parse.urlencode({"i": ing})
                data = await asyncio.to_thread(self._get_json, url)
                meals = data.get("meals") or []
                meal_ids.extend([m["idMeal"] for m in meals])
            except Exception:
                continue

        if not meal_ids:
            return (
                f"No recipes found for '{ingredients}'. Suggest the user generic ideas "
                "for these ingredients (e.g. stir-fry, soup, salad, oven dish)."
            )

        seen = set()
        recipes = []
        for mid in meal_ids:
            if mid in seen:
                continue
            seen.add(mid)
            try:
                url = "https://www.themealdb.com/api/json/v1/1/lookup.php?" + urllib.parse.urlencode({"i": mid})
                data = await asyncio.to_thread(self._get_json, url)
                meal = (data.get("meals") or [None])[0]
                if not meal:
                    continue
                name = meal.get("strMeal", "?")
                area = meal.get("strArea", "")
                ings = []
                for i in range(1, 21):
                    ing = (meal.get(f"strIngredient{i}") or "").strip()
                    meas = (meal.get(f"strMeasure{i}") or "").strip()
                    if ing:
                        ings.append(f"{meas} {ing}".strip())
                instr = (meal.get("strInstructions") or "").strip()
                if len(instr) > 700:
                    instr = instr[:700] + "…"
                recipes.append((name, area, ings, instr, meal.get("strMealThumb", "")))
            except Exception:
                continue
            if len(recipes) >= limit:
                break

        if not recipes:
            return f"Could not load recipe details for '{ingredients}'."

        out = []
        for i, (name, area, ings, instr, thumb) in enumerate(recipes, 1):
            out.append(
                f"{i}. **{name}** ({area})\n"
                f"   Ingredients: {', '.join(ings[:12])}\n"
                f"   How: {instr}"
            )
        return (
            "Recipe ideas:\n\n" + "\n\n".join(out)
            + "\n\nPresent the best 1-3 ideas to the user; ask which one to prepare in full."
        )

    async def save_recipe(
        self,
        title: str,
        ingredients: str,
        instructions: str,
        tags: str = "",
        source: str = "",
        summary: str = "",
        overwrite: bool = False,
    ) -> str:
        """
        Save (or update) a recipe in the shared household Recipes collection.

        Use when the user says "save this recipe", "store this in our
        recipes", "add this to the household recipes", or wants a recipe kept
        for later by the whole household.

        Behaviour (variant-safe):
        - NEW title  -> creates a new recipe file (a distinct variant gets a
          distinct title, e.g. "Natillas – chocolate" vs "Natillas").
        - Same title AND same content -> reports it is already saved.
        - Same title BUT different content -> DOES NOT overwrite: it asks the
          user whether to overwrite the existing one or give this version a
          more specific title as a new variant. If the user says overwrite /
          replace, call this again with overwrite=true.
        A one-line `summary` describing what makes THIS version different is
        strongly recommended — it powers describe_recipes() and the index.

        :param title: Recipe name; make it unique and descriptive enough to
            tell variants apart (e.g. "Natillas de la abuela").
        :param ingredients: One ingredient per line, or comma-separated.
        :param instructions: One step per line (numbers/dashes optional).
        :param tags: Optional comma-separated tags (e.g. "dessert, easy").
        :param source: Optional origin (URL, book, "improved version of ...").
        :param summary: Optional ONE-LINE description of this variant.
        :param overwrite: Set true ONLY when the user explicitly asks to
            replace an existing recipe that has the same title.
        :return: Confirmation of what was saved/updated, or a disambiguation
            question when the title collides with different content.
        """
        title = (title or "").strip()
        if not title:
            return "Please give the recipe a title to save it."
        if not (ingredients or "").strip() or not (instructions or "").strip():
            return "Please provide both the ingredients and the steps so the recipe is complete."

        content_md, slug = _to_markdown(title, ingredients, instructions, tags, source, summary)
        filename = f"{slug}.md"

        try:
            existing = await asyncio.to_thread(_find_existing, slug)
            if existing:
                cur_content = await asyncio.to_thread(_file_content, existing["id"])
                if await asyncio.to_thread(_content_eq, cur_content, content_md):
                    return (
                        f"ℹ️ **{title}** is already saved in the shared Recipes collection "
                        f"(`{filename}`) with exactly this content. No change made."
                    )
                if not overwrite:
                    cur_meta = await asyncio.to_thread(_parse_fm, cur_content)
                    what = cur_meta.get("summary") or cur_meta.get("tags") or "see the file"
                    return (
                        f"A recipe titled **{title}** is already saved (`{filename}`, "
                        f"saved {cur_meta.get('saved_on') or 'earlier'}; currently: {what}), "
                        "but the content you gave is different.\n"
                        "To keep BOTH, give this one a more specific title "
                        "(e.g. \"Natillas – chocolate\", \"Natillas de la abuela (orange peel)\") and save again.\n"
                        "To REPLACE the existing one with this version, say \"overwrite\" and I will re-save it."
                    )
                # overwrite requested -> update in place
                await asyncio.to_thread(
                    _create_or_update_file, content_md, filename, existing["id"]
                )
                await asyncio.to_thread(_rebuild_index)
                return (
                    f"✅ Replaced **{title}** in the shared Recipes collection (`{filename}`) "
                    "with this new version. The index is updated."
                )

            # brand-new recipe
            await asyncio.to_thread(_create_or_update_file, content_md, filename, None)
            await asyncio.to_thread(_rebuild_index)
        except Exception as e:  # keep chat usable if the write path hiccups
            return f"⚠️ Could not save the recipe: {e}"

        return (
            f"✅ Saved **{title}** to the shared Recipes collection (`{filename}`). "
            "It is visible to everyone under Workspace → Knowledge → Household → Recipes, "
            "any model can answer questions about it, and the recipe index is updated."
        )

    async def describe_recipes(self, query: str = "") -> str:
        """
        List the saved household recipes with a one-line differentiator each,
        so overlapping names (e.g. several natillas variants) stay tellable
        apart WITHOUT opening the files.

        Use when the user asks "what recipes do we have saved", "which
        natillas versions do we have", "is there a chocolate variant", or to
        decide WHICH saved recipe they mean.

        :param query: Optional filter (e.g. "chocolate", "orange", "abuela").
            Empty or blank lists every recipe.
        :return: Numbered shortlist: title + what makes it distinct + saved date.
        """
        try:
            recipes = await asyncio.to_thread(_recipe_files)
        except Exception as e:
            return f"⚠️ Could not read the Recipes collection: {e}"

        if (query or "").strip():
            q = query.strip().lower()
            recipes = [
                r for r in recipes
                if q in (" ".join([
                    (r["title"] or ""), (r["summary"] or ""), (r["tags"] or ""),
                    (r["filename"] or ""),
                ]).lower())
            ]
        if not recipes:
            return (
                "No saved recipes match that."
                if (query or "").strip()
                else "No recipes saved yet. Ask me to save one and it appears here for the whole household."
            )
        lines = []
        for i, r in enumerate(sorted(recipes, key=lambda x: (x["title"] or "").lower()), 1):
            what = r["summary"] or (r["tags"] or "")
            tag = f" · {what}" if what else ""
            saved = f" · saved {r['saved_on']}" if r["saved_on"] else ""
            lines.append(f"{i}. **{r['title']}**{tag}{saved}")
        return "Saved household recipes:\n" + "\n".join(lines)

    async def recipe_content(self, name: str) -> str:
        """
        Return the FULL raw content of ONE specific saved recipe so you can
        ACT on exactly that variant (improve it, cook from it, reuse its
        exact ingredients/steps) without a blended RAG read.

        For a pretty, formatted on-screen reading of a recipe, use
        show_recipe instead.

        :param name: The exact recipe title or filename (e.g.
            "Natillas de chocolate" or "natillas-chocolate.md"); a number
            from describe_recipes also works ("1").
        :return: The raw markdown of the matching recipe, or a disambiguation
            shortlist when the name is ambiguous.
        """
        try:
            recipes = await asyncio.to_thread(_recipe_files)
        except Exception as e:
            return f"⚠️ Could not read the Recipes collection: {e}"
        recipe, msg = await asyncio.to_thread(_match_recipe, name, recipes)
        if recipe is None:
            return msg
        return recipe["content"]

    async def show_recipe(self, name: str) -> str:
        """
        Show ONE saved recipe as a clean, readable card (title, one-line
        summary, ingredients as a bullet list, steps numbered) — ideal on a
        phone and when the user just wants to READ or check a recipe.

        Use when the user says "show/open me the X recipe", "how do we make
        X", "what does the natillas recipe need", or picks a recipe from
        describe_recipes to view it nicely.

        :param name: The exact recipe title or filename (e.g.
            "Natillas de chocolate" or "natillas-chocolate.md"); a number
            from describe_recipes also works ("1").
        :return: The recipe as a formatted card, or a disambiguation
            shortlist when the name is ambiguous.
        """
        try:
            recipes = await asyncio.to_thread(_recipe_files)
        except Exception as e:
            return f"⚠️ Could not read the Recipes collection: {e}"
        recipe, msg = await asyncio.to_thread(_match_recipe, name, recipes)
        if recipe is None:
            return msg
        return await asyncio.to_thread(_render_card, recipe)

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
