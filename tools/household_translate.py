"""
household_translate — Translation (EN/ES/NL + more).

Uses the free MyMemory API (no key). The assistant itself is multilingual,
so this tool is for quick, natural translations of words, phrases or short
texts — especially for the household's EN/ES/NL mix.
"""

import asyncio
import json
import urllib.parse
import urllib.request

_LANGS = {
    "english": "en", "en": "en",
    "spanish": "es", "es": "es",
    "dutch": "nl", "nl": "nl",
    "french": "fr", "fr": "fr",
    "german": "de", "de": "de",
    "italian": "it", "it": "it",
    "portuguese": "pt", "pt": "pt",
    "catalan": "ca", "ca": "ca",
    "galician": "gl", "gl": "gl",
}


def _code(lang: str) -> str:
    lang = (lang or "").strip().lower()
    return _LANGS.get(lang, lang.split("-")[0].split("_")[0])


class Tools:
    async def translate_text(self, text: str, target_lang: str, source_lang: str = "") -> str:
        """
        Translate a word, phrase or short text between languages.

        Use when the user asks "how do you say X in Spanish", wants a menu
        or note translated, or asks for a translation. For full documents
        the model can translate directly.

        :param text: The text to translate.
        :param target_lang: Target language (name or code, e.g. Spanish, es,
            Dutch, nl, English, en).
        :param source_lang: Optional source language (name or code). Leave
            empty for automatic detection.
        :return: The translated text plus the detected/used language pair.
        """
        text = (text or "").strip()
        if not text:
            return "Please provide text to translate."
        if len(text) > 4500:
            return "Text too long for the free translation service (max ~4500 chars). Translate it yourself in the user's language."
        tgt = _code(target_lang)
        src = _code(source_lang) if source_lang and source_lang.strip() else "Autodetect"
        if tgt == src:
            return f"The text is already in {target_lang}."
        try:
            params = {"q": text, "langpair": f"{src}|{tgt}"}
            url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params)
            data = await asyncio.to_thread(self._get_json, url)
            if data.get("responseStatus") == 200:
                translated = (data.get("responseData") or {}).get("translatedText", "")
                if translated:
                    detected = (data.get("responseData") or {}).get("detectedLanguage") or src
                    return f"{translated}\n\n(_translated from {detected} to {tgt}_)"
            return (
                "Translation service unavailable or quota exceeded. "
                "Translate the text yourself in the user's language instead."
            )
        except Exception:
            return "Translation service unreachable. Translate the text yourself in the user's language instead."

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "Chat-hache-household/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
