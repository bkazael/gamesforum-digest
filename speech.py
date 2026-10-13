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
