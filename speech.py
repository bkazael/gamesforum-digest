"""Configurable, testable speech normalization for Hebrew TTS.

The published script remains readable and source-faithful. These replacements
are applied only to the text sent to speech synthesis, so pronunciation can be
tuned without corrupting links, credits, or the editorial record.
"""
from __future__ import annotations

import re


def prepare_turns(turns: list[dict], replacements: dict[str, str]) -> list[dict]:
    """Return TTS-only copies with case-insensitive whole-term replacements."""
    compiled = []
    for source, spoken in replacements.items():
        if not source or not isinstance(spoken, str) or not spoken.strip():
            raise ValueError("Speech replacements need a term and spoken form")
        compiled.append((re.compile(rf"(?<![\w]){re.escape(source)}(?![\w])", re.I), spoken))
    prepared = []
    for turn in turns:
        copy = dict(turn)
        text = turn.get("text", "")
        for pattern, spoken in compiled:
            text = pattern.sub(lambda _: spoken, text)
        copy["text"] = text
        prepared.append(copy)
    return prepared


def untranslated_hebrew_terms(turns: list[dict], lang: str) -> list[str]:
    """Catch common English acronyms that should not be read as Hebrew words."""
    if lang.lower().split("-")[0] != "he":
        return []
    text = " ".join(t.get("text", "") for t in turns)
    checks = {
        "MENA": r"(?<![\w])MENA(?![\w])",
        "D2C": r"(?<![\w])D2C(?![\w])",
        "IAP": r"(?<![\w])IAP(?![\w])",
    }
    return [term for term, pattern in checks.items() if re.search(pattern, text, re.I)]


# Spoken Hebrew forms for the acronyms untranslated_hebrew_terms() flags. A
# Hebrew one-letter prefix written before the term ("ה-D2C", "בIAP") is kept
# and joined to the Hebrew phrase.
HEBREW_TERMS = {
    "D2C": "מכירה ישירה לצרכן",
    "IAP": "רכישות בתוך האפליקציה",
    "MENA": "המזרח התיכון וצפון אפריקה",
}


def translate_hebrew_terms(turns: list[dict], lang: str) -> tuple[list[dict], int]:
    """Replace D2C/IAP/MENA with their Hebrew forms in Hebrew speech.

    Returns (turns, replacements_made). The editorial gate rejects these
    acronyms; fixing them here is free and deterministic, where a model
    revision costs a request and may not fix them either.
    """
    if lang.lower().split("-")[0] != "he":
        return turns, 0
    count = 0
    out = []
    for turn in turns:
        text = turn.get("text", "")
        for term, hebrew in HEBREW_TERMS.items():
            pattern = re.compile(rf"(?<![\w])([ובלהמשכ]{{0,2}})-?{term}(?![\w])", re.I)
            # MENA's form is already definite (ה...): a ה prefix is dropped,
            # and after ב/ל/כ the ה is absorbed ("במזרח", not "בהמזרח").
            def sub(m, hebrew=hebrew):
                prefix = m.group(1)
                if hebrew.startswith("ה"):
                    if prefix.endswith("ה"):
                        prefix = prefix[:-1]
                    if prefix.endswith(("ב", "ל", "כ")):
                        hebrew = hebrew[1:]
                return prefix + hebrew
            text, n = pattern.subn(sub, text)
            count += n
        out.append(dict(turn, text=text))
    return out, count
