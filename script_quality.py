#!/usr/bin/env python3
"""
Deterministic checks and repairs for a generated podcast script.

Why this exists: 13 turns across 6 of the 11 episodes recorded so far end
mid-word -- "...ירידה קלה של 1% חודש בחודש. ארה" -- and the audio then says a
clipped word before the next host starts. Every one of them is a Hebrew
acronym written with an ASCII double quote (ארה"ב, מנכ"ל). The script comes
back as schema-constrained JSON, and the model sometimes emits that quote
unescaped: the JSON string closes right there, and whatever the sentence
would have said next is lost (usually; in one case it resurfaced as a new
turn from the same host, missing its first letter). This is a different bug
from the TTS-side truncation that cut the 2026-09-07 audio: that one lives
in the audio response, this one is already baked into the text before TTS
ever runs.

Three layers, cheapest first, none of which costs a Gemini call:

  1. normalize_quotes(): any ASCII quote that sits between two Hebrew
     letters is an acronym, so it becomes the real Hebrew gershayim (U+05F4)
     -- also what TTS should be fed. This covers the case where the model
     DID escape it correctly.
  2. repair_script(): for a turn the model cut at a known acronym stem, the
     acronym is restored (ארה -> ארה״ב) and the sentence is closed, or --
     when the same host's next turn is visibly the continuation -- the two
     are stitched back together. Only known stems are touched; anything
     else is left alone rather than guessed at.
  3. script_problems(): whatever is still malformed after that is reported,
     so the caller can spend one retry on it instead of shipping it.

The prompt also tells the model not to use ASCII quotes at all, which is the
real prevention; the three layers above are for when it ignores that.
"""

from __future__ import annotations

import re

GERSHAYIM = "״"
_HEB = "א-ת"
_HEB_RE = re.compile(f"[{_HEB}]")
_QUOTE_IN_WORD = re.compile(f'(?<=[{_HEB}])"(?=[{_HEB}])')

# Known acronym stems -> the full acronym they were cut from. Deliberately
# short and conservative: repair only ever runs on a turn that already ends
# in a bare Hebrew letter, and a stem not in this table is never "fixed",
# only reported. Better one flagged oddity than a confidently wrong word.
ACRONYM_STEMS = {
    "ארה": "ארה" + GERSHAYIM + "ב",
    "מנכ": "מנכ" + GERSHAYIM + "ל",
    "סמנכ": "סמנכ" + GERSHAYIM + "ל",
    "צה": "צה" + GERSHAYIM + "ל",
    "בע": "בע" + GERSHAYIM + "מ",
    "מע": "מע" + GERSHAYIM + "מ",
    "חו": "חו" + GERSHAYIM + "ל",
    "עו": "עו" + GERSHAYIM + "ד",
    "רוה": "רוה" + GERSHAYIM + "מ",
}
# One-letter Hebrew prefixes (in/the/to/from/and/that/as) that can sit in
# front of an acronym: בארה"ב, המנכ"ל, ולמנכ"ל.
_PREFIXES = "בהלמושכ"

_TERMINATORS = ".?!…:;)\"'”’" + GERSHAYIM + "׃"


def normalize_quotes(text: str) -> str:
    """ASCII quote between two Hebrew letters -> gershayim."""
    return _QUOTE_IN_WORD.sub(GERSHAYIM, text)


def _is_unterminated(text: str) -> bool:
    """A turn that stops on a bare Hebrew letter -- the signature of a cut.

    A turn ending in a digit, %, a Latin letter or any punctuation is left
    alone: the bug is specific to Hebrew acronyms, and flagging every
    "...up 44%" with no full stop would just burn retries on healthy output.
    """
    t = text.rstrip()
    return bool(t) and bool(_HEB_RE.match(t[-1]))


def _split_acronym_stem(token: str):
    """(prefix, stem) if token is [prefix letters]+known stem, else None."""
    for cut in range(0, 3):
        prefix, rest = token[:cut], token[cut:]
        if prefix and any(ch not in _PREFIXES for ch in prefix):
            continue
        if rest in ACRONYM_STEMS:
            return prefix, rest
    return None


def script_problems(script: list[dict]) -> list[str]:
    """Human-readable problems that still need a regenerate (or a log line)."""
    problems = []
    for i, turn in enumerate(script):
        text = (turn.get("text") or "").strip()
        if not text:
            problems.append(f"turn {i + 1} ({turn.get('speaker')}) is empty")
        elif _is_unterminated(text):
            problems.append(
                f"turn {i + 1} ({turn.get('speaker')}) stops mid-sentence at "
                f"...{text[-20:]!r}"
            )
    return problems


def repair_script(script: list[dict]) -> tuple[list[dict], list[str]]:
    """Normalize quotes, then restore/stitch turns cut at a known acronym.

    Returns (new_script, notes). Never raises; an unrecognised cut is left
    exactly as it was, for script_problems() to report.
    """
    notes: list[str] = []
    turns = [dict(t, text=normalize_quotes((t.get("text") or "").strip()))
             for t in script]
    out: list[dict] = []
    i = 0
    while i < len(turns):
        turn = turns[i]
        text = turn["text"]
        if _is_unterminated(text):
            token = text.split()[-1]
            hit = _split_acronym_stem(token)
            if hit:
                prefix, stem = hit
                full = ACRONYM_STEMS[stem]
                head = text[: len(text) - len(token)] + prefix + full
                nxt = turns[i + 1] if i + 1 < len(turns) else None
                last_letter = full[-1]
                if (nxt and nxt.get("speaker") == turn.get("speaker")
                        and nxt["text"].startswith(last_letter)):
                    # The model re-opened the same host's turn with the rest
                    # of the sentence, missing only the acronym's last letter
                    # (which it had already spent before the quote).
                    merged = head + " " + nxt["text"][1:].lstrip()
                    notes.append(f"stitched {token!r} + next turn -> {full!r}")
                    turns[i + 1] = dict(nxt, text="")
                    turn = dict(turn, text=merged)
                else:
                    notes.append(f"restored {token!r} -> {prefix + full!r} and closed the sentence")
                    turn = dict(turn, text=head + ".")
        if turn["text"]:
            out.append(turn)
        i += 1
    return out, notes


def count_words(script: list[dict]) -> int:
    return sum(len((t.get("text") or "").split()) for t in script)


# Hype vocabulary the script prompt now bans, and the agreement words it
# limits to two openers. Measured, not enforced: a retry costs a full
# Gemini request against a small daily quota, so these are numbers in the
# log to watch (measured on past episodes: 09-22 had 0.25 hype words per
# 100, 09-28 had 0.71, and 09-15 had 13 of 35 turns opening with an agreement
# word).
# Both spellings of each stem that ends in a final-form letter: Hebrew
# switches ף/ם to פ/מ the moment a suffix is added, so "מטורף" never matches
# "מטורפים" and "עצום" never matches "עצומה". The first live intro
# (2026-10-04) said "סכומים מטורפים" and slipped straight past a stem list
# that only had the final form.
HYPE_STEMS = ("עצום", "עצומ", "מטורף", "מטורפ", "מהפכ", "דרמט", "דרמה", "דרמת", "וואו",
              "מדהים", "מדהימ", "מטאור", "game changer")
AGREEMENT_OPENERS = ("בדיוק", "לגמרי", "בהחלט", "נכון", "אכן")


_OPENER_RE = re.compile(
    r"^(?:" + "|".join(AGREEMENT_OPENERS) + r")\s*[,.!:\u2014-]\s+(?=\S)"
)


def trim_agreement_openers(script: list[dict], keep: int = 2) -> tuple[list[dict], int]:
    """Drop the leading agreement word from every turn after the first `keep`.

    The script prompt limits turns that open with "בדיוק / לגמרי / בהחלט /
    נכון / אכן" to two. The first real episode (2026-10-04) had six of 36,
    despite the rule, and 09-15 had thirteen of 35 -- a prompt rule alone
    does not hold, and a retry would cost a full Gemini request against a
    small daily quota. This enforces the limit for free.

    Deliberately narrow, so it can't damage a sentence: it only strips a
    standalone opener followed by punctuation ("בדיוק. ועם כל..." ->
    "ועם כל..."), and only when at least six words remain. A bare "בהחלט."
    answering a question, or "בדיוק כמו ש..." where the word is part of the
    sentence, is left exactly as it was. Returns (script, turns_changed).
    """
    seen, changed, out = 0, 0, []
    for t in script:
        text = (t.get("text") or "").strip()
        m = _OPENER_RE.match(text)
        if m:
            seen += 1
            rest = text[m.end():].lstrip()
            if seen > keep and len(rest.split()) >= 6:
                t = dict(t, text=rest)
                changed += 1
        out.append(t)
    return out, changed


def style_report(script: list[dict]) -> list[str]:
    """A few lines of numbers describing how the script sounds."""
    words = max(count_words(script), 1)
    text = " ".join((t.get("text") or "") for t in script).lower()
    hype = sum(text.count(s) for s in HYPE_STEMS)
    openers = sum(1 for t in script
                  if (t.get("text") or "").strip().startswith(AGREEMENT_OPENERS))
    questions = sum(1 for t in script if (t.get("text") or "").rstrip().endswith("?"))
    return [
        f"{hype} hype words ({100 * hype / words:.2f} per 100 words; past episodes ran 0.25-0.71)",
        f"{openers} of {len(script)} turns open with an agreement word (limit: 2)",
        f"{questions} question turns",
    ]


def close_remaining(script: list[dict]) -> tuple[list[dict], int]:
    """Last resort after retries: end any still-unterminated turn with a
    full stop so TTS closes the sentence instead of hanging on a fragment.
    Returns (script, number_of_turns_closed)."""
    fixed = 0
    out = []
    for t in script:
        text = (t.get("text") or "").strip()
        if _is_unterminated(text):
            t = dict(t, text=text + ".")
            fixed += 1
        out.append(t)
    return out, fixed
