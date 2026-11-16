"""Vowel marks (nikud) on the words a voice is likely to misread, for the voice only.

Hebrew is written without vowels, and a text-to-speech voice has to guess: ספר is "sefer",
"sapar" or "safar"; חברה is a company or a friend. Hebrew writers solve this by pointing
only the ambiguous word. One model call lists such words in the finished script with their
pointed form; the pointed text goes to the speech model, while the script that is saved and
published stays unpointed.

Guards: a pointed form is used only if, with the marks removed, it is exactly the word it
replaces, and that word is in the turn. The call can fail without failing the run. Whether
the speech model reads pointed words better was not verified by ear when this was written
(2026-11-16): `[speech] vocalize = false` in profile.toml switches it off.
"""
from __future__ import annotations

import re

NIKUD_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "words": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"turn": {"type": "INTEGER"}, "word": {"type": "STRING"},
                           "vocalized": {"type": "STRING"}},
            "required": ["turn", "word", "vocalized"],
        }},
    },
    "required": ["words"],
}
_MARKS = re.compile(r"[֑-ׇֽֿׁׂׅׄ]")
_HEB_LETTER = r"א-ת"
MAX_WORDS = 80


def strip_marks(text: str) -> str:
    return _MARKS.sub("", text)


def nikud_prompt(script: list[dict]) -> str:
    turns = "\n".join(f"{i}. {t['speaker']}: {t['text']}" for i, t in enumerate(script, 1))
    return f"""Below is the script of a Hebrew podcast that a text-to-speech voice will read aloud.
The voice reads unpointed Hebrew and guesses the vowels. List the words it is LIKELY TO MISREAD,
each with its fully pointed (menukad) form as it must be pronounced IN THIS SENTENCE:

- words with more than one reading (ספר, חברה, עלה, מספר, הרכב, מנוי, שער, דבר, משחק/מְשַׂחֵק,
  verbs whose tense or binyan is ambiguous, construct forms, passive vs active participles);
- Hebrew transliterations of foreign words and names written in Hebrew letters;
- uncommon or technical Hebrew words.

Do not list words any reader would pronounce correctly, and do not list words in Latin
letters. "word" is the word exactly as it appears in that turn, including attached prefixes
(ב, ה, ו, ל, מ, ש, כ). "vocalized" is the same letters with vowel marks, nothing added or
removed. At most {MAX_WORDS} words in all; choose the ones that matter most.

SCRIPT:
{turns}
"""


def accept(script: list[dict], items: list[dict]) -> list[dict]:
    """The usable entries: same letters once the marks are removed, word present in its turn,
    one entry per (turn, word)."""
    out, seen = [], set()
    for it in items:
        try:
            n, word, voc = int(it.get("turn")), str(it.get("word") or "").strip(), str(it.get("vocalized") or "").strip()
        except (AttributeError, TypeError, ValueError):
            continue
        if not 1 <= n <= len(script) or not word or (n, word) in seen:
            continue
        if voc == word or strip_marks(voc) != word or not re.fullmatch(f"[{_HEB_LETTER}\"'״׳-]+", word):
            continue
        if not re.search(f"(?<![{_HEB_LETTER}]){re.escape(word)}(?![{_HEB_LETTER}])", script[n - 1]["text"]):
            continue
        seen.add((n, word))
        out.append({"turn": n, "word": word, "vocalized": voc})
    return out[:MAX_WORDS]


def apply(script: list[dict], words: list[dict] | None) -> list[dict]:
    """A copy of the script with the accepted words pointed (every occurrence in its turn)."""
    out = [dict(t) for t in script]
    for it in words or []:
        n = it["turn"]
        if 1 <= n <= len(out):
            out[n - 1]["text"] = re.sub(
                f"(?<![{_HEB_LETTER}]){re.escape(it['word'])}(?![{_HEB_LETTER}])",
                lambda _m, v=it["vocalized"]: v, out[n - 1]["text"])
    return out


def vocalize(script: list[dict], *, call, model: str, log=lambda m: None) -> list[dict]:
    """The accepted list of pointed words for `script`. Never raises: [] on any failure."""
    try:
        raw = call(nikud_prompt(script), NIKUD_SCHEMA, model=model, tries=3, timeout=240)
    except Exception as e:                                      # noqa: BLE001
        log(f"::warning::vowel marks unavailable ({type(e).__name__}); the voice reads the plain script")
        return []
    items = [x for x in raw.get("words") or [] if isinstance(x, dict)]
    words = accept(script, items)
    log(f"vowel marks: {len(words)} words pointed for the voice ({len(items) - len(words)} refused)")
    return words
