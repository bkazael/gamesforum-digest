"""Whole-episode proofreading: the last text stage before the voices.

Why a separate stage (2026-11-11): the per-story review is a FACT editor. It
reads one story against its article and is told to report only what misleads,
so a misspelt word, a letter from another alphabet, an unfinished sentence, an
English headline read out raw or a line about a different story all reached the
listener. Proofreading needs the opposite stance: read the finished episode as
the listener will hear it, and fix the language.

One model call for the whole episode (not one per story), then deterministic
cleanup that costs nothing. Every edit is guarded so a proofreader can only
correct wording, never change what is said:
  * the numbers in the turn stay exactly the same;
  * no new Latin-script names appear (an English word may be translated away,
    a company name may not be invented);
  * the corrected turn stays close to the original (a correction, not a rewrite);
  * a turn may be deleted only when it is off-topic or a repeat, never the first
    turn of the episode, and at most a few per episode.
A failed proofreading call never fails the run: the episode ships as written,
with a warning, exactly as before this stage existed.
"""
from __future__ import annotations

import difflib
import re

import segments

PROOF_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "edits": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "turn": {"type": "INTEGER"},
                    "category": {"type": "STRING", "enum": [
                        "SPELLING", "GRAMMAR", "WRONG_WORD", "FOREIGN_LETTERS", "UNFINISHED",
                        "ENGLISH_LEFT", "READ_ALOUD", "OFF_TOPIC", "REPEAT"]},
                    "action": {"type": "STRING", "enum": ["REPLACE", "DELETE"]},
                    "corrected": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["turn", "category", "action", "corrected", "reason"],
            },
        },
        # Names a text-to-speech voice is likely to misread inside Hebrew speech.
        # Only logged, so the owner can add the wrong ones to [speech.replacements].
        "pronunciation_risks": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["edits"],
}

TIMEOUT = 240
TRIES = 3
MIN_SIMILARITY = 0.6          # a correction, not a rewrite
# Putting an English headline into Hebrew changes many characters by nature; the
# number and name guards still hold, so only the similarity floor is lower.
MIN_SIMILARITY_ENGLISH = 0.3
MAX_DELETES = 3
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'&.+-]*")
_HEBREW = re.compile(r"[א-ת]")


def proofread_prompt(script: list[dict], lang: str) -> str:
    turns = "\n".join(f"{i}. {t['speaker']}: {t['text']}" for i, t in enumerate(script, 1))
    language = "Hebrew" if lang == "he" else "English"
    return f"""You are the copy editor of a {language} podcast about the mobile games industry.
Below is the full script of this week's episode, exactly as two text-to-speech voices will
read it aloud. Proofread it as a listener will hear it. Do NOT check facts (that was done),
do NOT restyle, shorten or improve good sentences. Fix only real errors:

SPELLING      a misspelt word ("ענייה" for "עלייה").
GRAMMAR       wrong gender or number agreement, a broken construction.
WRONG_WORD    a real word that is the wrong one in context.
FOREIGN_LETTERS a letter from another alphabet inside a word (Arabic, Cyrillic...).
UNFINISHED    a sentence that stops in the middle.
ENGLISH_LEFT  an English phrase or a whole English headline read out where natural spoken
              {language} exists. Company, game and product names and accepted industry
              terms (UA, LiveOps, soft launch, loot boxes, hybrid-casual) stay in English.
READ_ALOUD    something a voice will misread: doubled apostrophes in an acronym (סמנכ''ל ->
              סמנכ"ל), symbols, a slash, a raw URL.
OFF_TOPIC     a turn that brings in a game, company or story unrelated to the story it sits in.
REPEAT        a turn that repeats, almost word for word, what was just said.

For REPLACE give the WHOLE corrected turn in "corrected": the same sentence with only the
error fixed, every number and name kept exactly. For DELETE (only OFF_TOPIC or REPEAT) leave
"corrected" empty. Turn numbers are the numbers below. Return an empty list if the script is
clean; most turns should not be touched.

Separately, in "pronunciation_risks" list up to 15 company, game or product names (exactly as
written in the script) that a text-to-speech voice reading Hebrew is likely to mispronounce.
This is only a list for the owner; do not change those names in the script.

SCRIPT:
{turns}
"""


def _latin(text: str) -> set[str]:
    return {w.lower().strip(".'") for w in _LATIN_WORD.findall(text)}


def _hebrew_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if _HEBREW.match(c)) / len(letters) if letters else 0.0


def accept_replacement(old: str, new: str, lang: str, category: str = "") -> str | None:
    """None when the replacement is safe to apply, else why it was refused."""
    if not new or new == old:
        return "empty or unchanged"
    if segments.numerals(new) != segments.numerals(old):
        return "changes a number"
    if not _latin(new) <= _latin(old):
        return "adds a name or English word"
    floor = MIN_SIMILARITY_ENGLISH if category == "ENGLISH_LEFT" else MIN_SIMILARITY
    if difflib.SequenceMatcher(None, old, new).ratio() < floor:
        return "rewrites the turn"
    if not 0.6 <= len(new) / max(len(old), 1) <= 1.4:
        return "changes the length too much"
    if lang == "he" and _hebrew_share(new) + 0.02 < _hebrew_share(old):
        return "less Hebrew than before"
    return None


def apply_edits(script: list[dict], edits: list[dict], lang: str,
                log=lambda m: None) -> tuple[list[dict], list[dict]]:
    """Apply the guarded edits. Returns the new script and a record of every
    edit: applied or refused, and why."""
    out = [dict(t) for t in script]
    record: list[dict] = []
    deletes: set[int] = set()
    for e in edits:
        n = e.get("turn")
        if not isinstance(n, int) or not 1 <= n <= len(out):
            continue
        entry = {"turn": n, "category": e.get("category"), "action": e.get("action"),
                 "reason": str(e.get("reason") or "")[:200]}
        if e.get("action") == "DELETE":
            ok = (e.get("category") in ("OFF_TOPIC", "REPEAT") and n > 1
                  and len(deletes) < MAX_DELETES)
            entry["applied"] = ok
            if ok:
                deletes.add(n)
                entry["text"] = out[n - 1]["text"]
            else:
                entry["refused"] = "delete not allowed here"
        else:
            old, new = out[n - 1]["text"], str(e.get("corrected") or "").strip()
            why = accept_replacement(old, new, lang, str(e.get("category") or ""))
            entry.update(applied=why is None, before=old, after=new)
            if why is None:
                out[n - 1]["text"] = new
            else:
                entry["refused"] = why
        record.append(entry)
    out = [t for i, t in enumerate(out, 1) if i not in deletes]
    for r in record:
        state = "applied" if r["applied"] else f"refused ({r.get('refused')})"
        log(f"  proofread turn {r['turn']} {r['category']} {r['action']}: {state}")
    return out, record


def deterministic_cleanup(script: list[dict], lang: str) -> tuple[list[dict], int]:
    """Free fixes that need no model: stray letters of other alphabets, a doubled
    apostrophe used as the Hebrew acronym mark, doubled spaces."""
    changed = 0
    out = []
    for t in script:
        text = segments.strip_stray_script(t["text"])
        if lang == "he":
            text = re.sub(r"([א-ת])''([א-ת])", r'\1"\2', text)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        changed += text != t["text"]
        out.append({**t, "text": text})
    return out, changed


def pronunciation_risks(raw: dict, known: set[str]) -> list[str]:
    """The names the proofreader expects the voice to misread, minus those already in
    the glossary ([speech.replacements]), deduplicated, at most 15."""
    out: list[str] = []
    for name in raw.get("pronunciation_risks") or []:
        name = " ".join(str(name).split())[:40]
        if name and name.lower() not in known and name.lower() not in {o.lower() for o in out}:
            out.append(name)
    return out[:15]


def proofread_episode(script: list[dict], *, call, model: str, lang: str,
                      log=lambda m: None, glossary: dict | None = None) -> tuple[list[dict], dict]:
    """Proofread the whole episode. Never raises: on a failed call the script is
    returned after the free cleanup only, and the report says so."""
    script, cleaned = deterministic_cleanup(script, lang)
    report: dict = {"deterministic_fixes": cleaned, "edits": []}
    try:
        raw = call(proofread_prompt(script, lang), PROOF_SCHEMA, model=model,
                   tries=TRIES, timeout=TIMEOUT)
        edits = [e for e in raw.get("edits") or [] if isinstance(e, dict)]
    except Exception as e:                                      # noqa: BLE001
        log(f"::warning::proofreading unavailable ({type(e).__name__}: {str(e)[:120]}); "
            "the episode goes out as written")
        report["status"] = "unavailable"
        return script, report
    script, record = apply_edits(script, edits, lang, log)
    script, _ = deterministic_cleanup(script, lang)
    known = {str(k).lower() for k in (glossary or {})}
    report["pronunciation_risks"] = pronunciation_risks(raw, known)
    if report["pronunciation_risks"]:
        log("  names the voice may misread (add the wrong ones to [speech.replacements]): "
            + ", ".join(report["pronunciation_risks"]))
    report.update(status="done", edits=record,
                  applied=sum(1 for r in record if r["applied"]),
                  refused=sum(1 for r in record if not r["applied"]))
    log(f"proofreading: {report['applied']} fixes applied, {report['refused']} refused, "
        f"{cleaned} free character fixes")
    return script, report
