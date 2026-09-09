"""
household_games — Trivia & word games.

Trivia from the free Open Trivia DB API; word games use a small embedded
word list (no network needed).
"""

import asyncio
import json
import random
import urllib.parse
import urllib.request

_WORDS = [
    ("serendipity", "the occurrence of happy or beneficial events by chance"),
    ("ephemeral", "lasting for a very short time"),
    ("resilient", "able to recover quickly from difficulties"),
    ("meticulous", "showing great attention to detail"),
    ("eloquent", "fluent and persuasive in speaking or writing"),
    ("candid", "truthful and straightforward; frank"),
    ("pragmatic", "dealing with things sensibly and realistically"),
    ("ambiguous", "open to more than one interpretation"),
    ("whimsical", "playfully quaint or fanciful"),
    ("luminous", "full of light; bright or shining"),
    ("tenacious", "holding firmly; persistent"),
    ("voracious", "wanting or devouring great quantities"),
    ("mellow", "soft, rich, and pleasantly smooth"),
    ("brisk", "active, fast, and energetic"),
    ("quaint", "attractively unusual or old-fashioned"),
    ("diligent", "showing care and effort in work"),
    ("fervent", "having or displaying passionate intensity"),
    ("glimpse", "a brief or partial view"),
    ("harbor", "a place of shelter; to keep a thought or feeling secretly"),
    ("jubilant", "feeling or expressing great happiness"),
    ("keen", "eager or enthusiastic; sharp"),
    ("lucid", "expressed clearly; easy to understand"),
    ("nimble", "quick and light in movement or action"),
    ("opulent", "ostentatiously rich and luxurious"),
    ("placid", "calm and peaceful"),
    ("quiver", "to shake with a slight rapid motion"),
    ("radiant", "emitting light; glowing"),
    ("sturdy", "strongly built; robust"),
    ("tranquil", "free from disturbance; calm"),
    ("vivid", "producing powerful feelings or strong clear images"),
    ("wander", "to walk or move in a leisurely or aimless way"),
    ("yearn", "to have an intense feeling of longing"),
    ("zealous", "having or showing great energy or enthusiasm"),
    ("ample", "enough or more than enough; plentiful"),
    ("benevolent", "well meaning and kindly"),
    ("crisp", "firm, dry, and brittle; pleasantly fresh"),
    ("dazzling", "extremely bright; impressively skilful"),
    ("eager", "wanting to do or have something very much"),
    ("fragrant", "having a pleasant, sweet smell"),
    ("gloomy", "dark or poorly lit; sad or pessimistic"),
]

_DIFF = {"easy": "easy", "medium": "medium", "hard": "hard"}


class Tools:
    async def trivia_question(self, difficulty: str = "medium", category: str = "") -> str:
        """
        Get a trivia question (multiple choice) for the user to answer.

        Use when the user wants a quiz question, trivia, "ask me something",
        or a quiet-evening game.

        :param difficulty: easy, medium or hard.
        :param category: Optional category hint (e.g. general, science,
            history, music, film). Passed to the trivia service as-is.
        :return: The question with options. Ask the user to pick one; do not
            reveal the answer until they answer.
        """
        diff = _DIFF.get((difficulty or "medium").strip().lower(), "medium")
        params = {"amount": 1, "type": "multiple", "difficulty": diff}
        if category and category.strip():
            params["category"] = category.strip()
        try:
            url = "https://opentdb.com/api.php?" + urllib.parse.urlencode(params)
            data = await asyncio.to_thread(self._get_json, url)
            if data.get("response_code") == 0 and data.get("results"):
                r = data["results"][0]
                question = r.get("question", "?")
                correct = r.get("correct_answer", "")
                wrong = r.get("incorrect_answers", []) or []
                options = wrong + [correct]
                random.shuffle(options)
                cat = r.get("category", "")
                lines = [f"**{cat}** ({diff})", "", question, ""]
                for i, opt in enumerate(options, 1):
                    lines.append(f"{i}. {opt}")
                lines.append("")
                lines.append("_Ask the user to answer; reveal the correct answer only after they answer (or give up)._")
                return "\n".join(lines)
            return "The trivia service returned no question right now — try again in a moment."
        except Exception:
            return "Trivia service unreachable. Ask a trivia question from your own knowledge instead."

    async def word_game(self) -> str:
        """
        Start a word game: guess the word from its definition (reverse hangman).

        Use when the user wants to play a word game, "let's play a game",
        or a vocabulary game.

        :return: A definition the user must guess the word for, with letter count.
        """
        word, definition = random.choice(_WORDS)
        hidden = " ".join("_" if ch != " " else "  " for ch in word)
        return (
            f"**Word game!** Guess the word.\n\n"
            f"Definition: _{definition}_\n\n"
            f"Letters: {len(word)} ({hidden})\n\n"
            "_Play: the user guesses letters or the whole word. Only reveal the word when they guess correctly or give up._"
        )

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "Chat-hache-household/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
