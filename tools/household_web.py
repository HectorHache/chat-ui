"""
household_web — Web search with citations + page summarizer.

No API key needed: primary search path renders DuckDuckGo results through
r.jina.ai, with the DuckDuckGo Instant Answer API and Wikipedia as
fallbacks. The summarizer fetches any public URL and extracts readable text.
"""

import asyncio
import html as html_lib
import json
import re
import time
import urllib.parse
import urllib.request

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Per-process fetch cache: repeated summarization of the SAME URL within a short
# window returns the cached text instead of re-downloading the page. This stops
# the "stuck on Executing" / repeated-fetch churn when a chat saves several
# recipes from one long article.
_URL_CACHE = {}
_URL_CACHE_TTL = 900  # seconds
_PAGE_MAX_CHARS = 24000  # readable text cap returned to the model
_FETCH_TOTAL_MAX_S = 12.0  # hard wall-clock cap for one fetch (anti-hang)
_FETCH_READ_TIMEOUT = 10.0


def _purge_cache(now):
    stale = [k for k, (t, _) in _URL_CACHE.items() if now - t > _URL_CACHE_TTL]
    for k in stale:
        _URL_CACHE.pop(k, None)


def _bounded_fetch(url):
    """Fetch up to a max byte size with a hard wall-clock cap (runs in a thread)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=_FETCH_READ_TIMEOUT) as resp:
        final_url = resp.geturl()
        chunks = []
        size = 0
        while True:
            if time.monotonic() - start > _FETCH_TOTAL_MAX_S:
                chunks.append(b"\n\n\u2026[fetch timed out mid-read]")
                break
            chunk = resp.read(min(64_000, max(1, 2_000_000 - size)))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size >= 2_000_000:
                chunks.append(b"\n\n\u2026[page too large; truncated]")
                break
    return final_url, b"".join(chunks).decode("utf-8", errors="replace")


class Tools:
    async def web_search(self, query: str, max_results: int = 5) -> str:
        """
        Search the web for up-to-date information with citations.

        Use whenever the user asks about current events, facts, prices,
        opening hours, news, or anything the model may not know.

        :param query: The search query, as the user would type it.
        :param max_results: Maximum number of results to return (1-8).
        :return: Numbered list of results with title, URL and a short
            snippet. Tell the user to click the links for details.
        """
        max_results = max(1, min(8, int(max_results or 5)))

        # 1) DuckDuckGo Instant Answer API — structured abstract when available
        try:
            ia_url = "https://api.duckduckgo.com/?format=json&" + urllib.parse.urlencode({"q": query})
            ia = await asyncio.to_thread(self._get_json, ia_url)
            abstract = (ia.get("AbstractText") or "").strip()
            if abstract and ia.get("AbstractURL"):
                return (
                    f"**Instant answer: {ia.get('Heading', query)}**\n\n"
                    f"{abstract}\n\nSource: {ia.get('AbstractURL')}"
                )
        except Exception:
            pass

        # 2) DuckDuckGo HTML rendered via r.jina.ai
        try:
            ddg_url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
            jina_url = "https://r.jina.ai/" + ddg_url
            text = await asyncio.to_thread(self._get_text, jina_url)
            results = self._parse_jina_results(text, max_results)
            if results:
                lines = []
                for i, (title, url, snippet) in enumerate(results, 1):
                    lines.append(f"{i}. **[{title}]({url})**")
                    if snippet:
                        lines.append(f"   {snippet}")
                return "Search results:\n\n" + "\n".join(lines) + "\n\nGive a concise answer with the sources above."
        except Exception:
            pass

        # 3) Wikipedia opensearch fallback
        try:
            wiki_url = "https://en.wikipedia.org/w/api.php?action=opensearch&format=json&limit=5&" + urllib.parse.urlencode(
                {"search": query}
            )
            ws = await asyncio.to_thread(self._get_json, wiki_url)
            titles = ws[1]
            links = ws[3]
            if titles:
                lines = [
                    f"{i}. [{t}]({links[i]})" for i, t in enumerate(titles) if i < len(links)
                ]
                return "Search results (Wikipedia):\n\n" + "\n".join(lines)
        except Exception:
            pass

        return "Search is temporarily unavailable. Tell the user you could not reach the search service and offer a general answer."

    async def summarize_url(self, url: str) -> str:
        """
        Fetch a webpage and return its readable text for summarization.

        Use when the user asks to summarize an article or page, says
        "summarize this URL/link", or pastes a link asking what it is about.
        Returns up to ~24k chars. Repeated fetches of the same URL within a
        short window return the cached text (no re-download).

        :param url: The full URL (http/https) to summarize.
        :return: Page title + extracted text (truncated). Summarize it in
            the user's language and cite the URL.
        """
        url = ((url or "").strip()).rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        now = time.monotonic()
        _purge_cache(now)
        hit = _URL_CACHE.get(url)
        if hit and now - hit[0] < _URL_CACHE_TTL:
            return hit[1] + "\n\n_[cached from earlier fetch; not re-downloaded]_"
        try:
            final_url, raw = await asyncio.to_thread(_bounded_fetch, url)
        except Exception as e:
            return f"Could not fetch the page ({e}). Tell the user the page could not be retrieved."

        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        if m:
            title = html_lib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()

        text = self._html_to_text(raw)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > _PAGE_MAX_CHARS:
            text = text[:_PAGE_MAX_CHARS] + "\n…[truncated]"

        if not text:
            return f"The page {url} appears to have no readable text content."

        out = f"**Page title:** {title}\n**URL:** {final_url}\n\n{text}"
        _URL_CACHE[url] = (now, out)
        return out

    # ------------------------------------------------------------------ helpers

    def _parse_jina_results(self, text: str, max_results: int) -> list:
        results = []
        # markdown links: [title](url)
        pattern = re.compile(r"\[([^\]]{2,200})\]\((https?://[^)]+)\)")
        for m in pattern.finditer(text):
            title = html_lib.unescape(m.group(1)).strip()
            url = m.group(2).strip()
            if "duckduckgo.com" in url or "duck.co" in url:
                continue
            results.append({"title": title, "url": url, "snippet": ""})
            if len(results) >= max_results:
                break
        return results

    def _html_to_text(self, raw: str) -> str:
        # drop script/style blocks
        raw = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        # convert some block tags to newlines
        raw = re.sub(r"<(p|div|br|li|h[1-6]|tr|section|article)[^>]*>", "\n", raw, flags=re.I)
        raw = re.sub(r"<[^>]+>", " ", raw)
        return html_lib.unescape(raw)

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.read().decode("utf-8", errors="replace")
