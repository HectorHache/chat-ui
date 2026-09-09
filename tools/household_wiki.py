"""
household_wiki — Wikipedia/Wikidata lookup with sources.

Factual answers with sources via the Wikipedia REST summary API (no key).
"""

import asyncio
import json
import urllib.parse
import urllib.request

_UA = "Chat-hache-household/1.0 (private home assistant)"


class Tools:
    async def wiki_lookup(self, topic: str) -> str:
        """
        Look up a topic on Wikipedia and return a factual summary with a link.

        Use when the user asks about a person, place, event, concept or thing
        and wants a reliable factual answer, or asks "who is", "what is".

        :param topic: The topic to look up (e.g. "Utrecht", "Eiffel Tower").
        :return: Summary (first ~2 paragraphs), page URL, thumbnail if any.
        """
        topic = (topic or "").strip()
        if not topic:
            return "Please provide a topic to look up."
        try:
            # try exact title first, then opensearch for a match
            title = urllib.parse.quote(topic.replace(" ", "_"))
            url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
            data = await asyncio.to_thread(self._get_json, url)
            if not data or data.get("type") == "disambiguation":
                # fall back to search
                s_url = "https://en.wikipedia.org/w/api.php?action=opensearch&format=json&limit=1&" + urllib.parse.urlencode(
                    {"search": topic}
                )
                ws = await asyncio.to_thread(self._get_json, s_url)
                titles = ws[1] or []
                links = ws[3] or []
                if not titles:
                    return f"No Wikipedia article found for '{topic}'."
                title = urllib.parse.quote(titles[0].replace(" ", "_"))
                url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
                data = await asyncio.to_thread(self._get_json, url)
            if not data or not data.get("extract"):
                return f"No Wikipedia article found for '{topic}'."

            extract = data["extract"]
            if len(extract) > 900:
                extract = extract[:900] + "…"
            page_url = data.get("content_urls", {}).get("desktop", {}).get("page", data.get("url", ""))
            thumb = data.get("thumbnail", {}).get("source", "")
            lines = [f"**{data.get('title', topic)}**", "", extract, "", f"Source: {page_url}"]
            if thumb:
                lines.append("")
                lines.append(f"![{data.get('title', topic)}]({thumb})")
            return "\n".join(lines)
        except Exception as e:
            return f"Wikipedia lookup failed ({e}). Answer from your own knowledge and note the uncertainty."

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
