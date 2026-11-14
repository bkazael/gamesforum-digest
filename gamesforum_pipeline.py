#!/usr/bin/env python3
"""
Gamesforum -> Digest + Podcast Pipeline v6.3 (Gemini 3-Chunk TTS Edition)
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from email.utils import format_datetime
from xml.sax.saxutils import escape as xml_escape

import checkpoint
import memory
import script_quality
import runtime
import article_parser
import audio_tools
import speech as speech_tools
import source_health
import proofread

# ---------------------------------------------------------------- config

from llm import (GEMINI_API_KEY, GEMINI_SCORING_MODEL, GEMINI_CREATIVE_MODEL, GEMINI_REVIEW_MODEL,
                 TTS_MODEL, TTS_FALLBACK_MODEL, QUOTA_FALLBACKS, HTTP_TIMEOUT, TTS_TIMEOUT,
                 RUN_DEADLINE_SEC, USAGE_LOG, log, live_model, swap_retired_model,
                 explain_billing_error, retry_delay, gemini_json, GeminiQuotaExhausted,
                 _QUOTA_SPENT, _UNHEALTHY, _MODEL_FAILS, UNHEALTHY_AFTER_FAILED_CALLS,
                 FIRST_MODEL_TRIES, _is_transient)
from llm import call_model as gemini_json_quota
from llm import check_deadline as _check_deadline, remaining_timeout as _remaining_timeout
from llm import retry_sleep as _retry_sleep
import llm

BASE_URL = os.environ.get("PODCAST_BASE_URL", "").rstrip("/")
LANG = os.environ.get("DIGEST_LANG", "he")

SITE = "https://www.globalgamesforum.com"
ROOT = runtime.OUTPUT_ROOT
EPISODES = ROOT / "episodes"
DIGESTS = ROOT / "digests"
STATE_FILE = ROOT / "state.json"
ASSETS_DIR = runtime.CODE_ROOT / "assets"

# A browser UA, not a self-identifying bot string: the 2026-09-14 run hit
# an HTTP 403 fetching Gamigion's Substack feed specifically (every other
# source, on the same UA, worked fine) -- Substack's front door blocks
# obvious non-browser clients like the old "compatible; bens-digest/6.3"
# string. This won't get past real bot-detection (TLS fingerprinting, JS
# challenges), but it's the standard first fix for a plain UA-based block,
# and it can only make other sources more compatible, not less.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
# These three have to nest, innermost first, or the whole retry design is
# theatre. The 2026-09-21 run proved it: RUN_DEADLINE_SEC was 2400s (40
# min) while weekly-digest.yml's "Run pipeline" step times out at 30, so
# _check_deadline() could never fire first -- the step was always killed
# mid-flight instead of exiting cleanly, losing everything it had already
# built. The budget now reads: one attempt (TTS_TIMEOUT) < the whole run
# (RUN_DEADLINE_SEC = 45 min) < the step (50 min) < the job (55 min). Raised
# from 25/30/35 on 2026-11-09, when the show's owner chose to wait longer for
# the strong model rather than publish a short episode from a weaker one.

# TTS gets its own, shorter per-attempt ceiling. Real successful chunks in
# production have taken 1-4 minutes; the calls that hang never come back at
# all and just burn the full HTTP_TIMEOUT. 270s keeps the slowest observed
# *successful* call comfortably inside the window while capping a hung one.


# One source of truth for the TTS chunk split, so test_episode.py's chunking
# test can import this instead of hand-copying the number -- a copy is
# exactly how that test drifted out of sync with production before (it
# tested against 1500 while this was already 3800).
TTS_CHUNK_CHAR_LIMIT = 3800

def _load_voice() -> dict:
    cfg = {
        "speaker_a": "Dana", "voice_a": "Kore",
        "speaker_b": "Yoni", "voice_b": "Charon",
        "direction": "Two industry colleagues talking. Measured, unhurried, genuinely interested. Strategic and deep.",
    }
    path = runtime.PROFILE_PATH
    if path.exists():
        try:
            import tomllib
            with path.open("rb") as f:
                loaded = tomllib.load(f).get("voice", {})
                if loaded:
                    cfg.update(loaded)
        except Exception:
            pass
    return cfg

_VOICE = _load_voice()
SPEAKER_A, VOICE_A = _VOICE["speaker_a"], _VOICE["voice_a"]
SPEAKER_B, VOICE_B = _VOICE["speaker_b"], _VOICE["voice_b"]
DIRECTION = _VOICE["direction"].strip()

def http_get(url: str, tries: int = 3) -> str:
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=_remaining_timeout(25)) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError) and e.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == tries - 1:
                raise
            _retry_sleep(2 ** attempt)
    raise RuntimeError("unreachable")

def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style|nav|footer|svg)\b.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</(p|div|h[1-6]|li)>", "\n", fragment)
    text = re.sub(r"(?s)<[^>]+>", " ", fragment)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()

def fetch_article(url: str) -> dict | None:
    try:
        page = http_get(url)
    except Exception as e:
        log(f"fetch failed {url}: {e}")
        return None

    return article_parser.extract(page, url, strip_tags)

# ---------------------------------------------------------------- the intro, written last
#
# The stories are written first (segments.py), one call per story. The intro is
# written only afterwards, with the finished stories in hand, so that its
# preview describes what is really said: the welcome, a moment of host banter,
# and a teaser. It degrades instead of failing the run: no intro -> a plain
# welcome plus a one-line list of the stories.

INTRO_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "turns": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "speaker": {"type": "STRING", "enum": [SPEAKER_A, SPEAKER_B]},
                    "text": {"type": "STRING"},
                },
                "required": ["speaker", "text"],
            },
        },
    },
    "required": ["turns"],
}


def welcome_turn() -> dict:
    """The one fixed line of the show. Written in code, not asked of the
    model, because it is the brand and must be identical every week."""
    if LANG == "he":
        return {"speaker": SPEAKER_A,
                "text": "ברוכים הבאים ל-Ben's Weekly Digest. אני דנה, ואיתי יוני."}
    return {"speaker": SPEAKER_A,
            "text": "Welcome to Ben's Weekly Digest. I'm Dana, and with me is Yoni."}


INTRO_MIN_TURNS, INTRO_MAX_TURNS = 3, 12
INTRO_MIN_WORDS, INTRO_MAX_WORDS = 45, 150
STYLE = "style: "
# How a person answers "welcome to the show, I'm Dana, and with me is Yoni".
INTRO_DAY_RE = re.compile(
    r"יום\s+(?:ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)|סוף[\s-]?שבוע|סופ.ש|\b(?:monday|tuesday|wednesday|thursday|friday|weekend)\b")
INTRO_GREETINGS = ("היי", "הי ", "הי,", "שלום", "בוקר טוב", "ערב טוב", "צהריים טובים",
                   "אהלן", "מה קורה", "hi", "hello", "good morning", "good evening")


def intro_problems(turns: list[dict]) -> list[str]:
    problems = []
    if not INTRO_MIN_TURNS <= len(turns) <= INTRO_MAX_TURNS:
        problems.append(f"{len(turns)} turns (want {INTRO_MIN_TURNS}-{INTRO_MAX_TURNS})")
    if turns and turns[0].get("speaker") != SPEAKER_B:
        problems.append(f"first turn must be {SPEAKER_B}'s reply to the welcome, "
                        f"not {turns[0].get('speaker')}'s")
    words = sum(len((t.get("text") or "").split()) for t in turns)
    if not INTRO_MIN_WORDS <= words <= INTRO_MAX_WORDS:
        problems.append(f"{words} words (want {INTRO_MIN_WORDS}-{INTRO_MAX_WORDS})")
    if any("ברוכים הבאים" in (t.get("text") or "") or "welcome to" in (t.get("text") or "").lower()
           for t in turns):
        problems.append("it repeats the welcome line")
    problems += script_quality.script_problems(turns)

    # Style rules. Prefixed STYLE so write_intro() can tell them apart from
    # structural faults: a structural fault means the intro is unusable, a
    # style fault earns one retry but is accepted if it persists -- a
    # slightly hyped intro still beats the plain fallback. The first live
    # run (2026-10-04) produced exactly these, despite the prompt asking
    # otherwise: a generic "a week where..." opener with the word מטורפים,
    # two turns opening with an agreement word, and three Dana turns in a row.
    text = " ".join((t.get("text") or "") for t in turns).lower()
    hype = [s for s in script_quality.HYPE_STEMS if s in text]
    if hype:
        problems.append(f"{STYLE}hype wording ({', '.join(hype)})")
    agree = sum(1 for t in turns
                if (t.get("text") or "").strip().startswith(script_quality.AGREEMENT_OPENERS))
    if agree:
        problems.append(f"{STYLE}{agree} turn(s) open with an agreement word")
    if turns and turns[0].get("speaker") == SPEAKER_B:
        first = (turns[0].get("text") or "")
        if "שבוע" in first or "week" in first.lower():
            problems.append(f"{STYLE}the first reply is about the week itself, "
                            "not about one specific thing from the episode")
    # The 2026-10-05 production intro: no greeting, a cold open on an
    # unexplained figure, banter built on it, and a teaser ending in a
    # compound interview question. A greeting is checkable; "explained in the
    # same breath" is not, so that rule lives in the prompt (and in the
    # example) while these catch the mechanical symptoms.
    if turns and turns[0].get("speaker") == SPEAKER_B:
        head = (turns[0].get("text") or "").strip().lower()
        if not head.startswith(INTRO_GREETINGS):
            problems.append(f"{STYLE}the first reply does not greet back "
                            "(it starts in the middle of a thought)")
    # The model has no idea what day it is and invented "איזה כיף שהגענו ליום
    # חמישי" for an episode recorded on a Monday (2026-10-05 comparison run).
    if INTRO_DAY_RE.search(text):
        problems.append(f"{STYLE}it names a day of the week (the episode does not know "
                        "what day the listener hears it)")
    questions = sum((t.get("text") or "").count("?") for t in turns)
    if questions > 2:
        problems.append(f"{STYLE}{questions} question marks in the intro (at most 2, short ones)")
    for t in turns:
        n = len((t.get("text") or "").split())
        if n > 110:
            problems.append(f"{STYLE}a {t.get('speaker')} turn runs {n} words "
                            "(a monologue; split it or cut it)")
            break
    run = 1
    for prev, cur in zip(turns, turns[1:]):
        run = run + 1 if cur.get("speaker") == prev.get("speaker") else 1
        if run > 2:
            problems.append(f"{STYLE}{cur.get('speaker')} has more than two turns in a row")
            break
    return problems


def write_intro(data: dict, prep: dict | None) -> tuple[list[dict], str]:
    """The intro: the welcome, a beat of host banter, and a preview of the
    episode that was actually written. Returns (turns, source) where source
    is "model" or "fallback". Never raises -- an intro is not worth a run.
    """
    welcome = welcome_turn()
    stories = "\n".join(
        f"{n}. {s.get('title', '')} -- {s.get('key_takeaway', '')}"
        for n, s in enumerate(data.get("digest_summary", []), 1)
    )
    facts = ""
    questions = ""
    if prep:
        pts = [p for a in prep["articles"][:3] for p in a["key_points"][:2]]
        facts = "\n".join(f"- {p}" for p in pts)
        questions = "\n".join(
            f"- {q['question']}" + ("" if q["answer_basis"] != "OPEN" else "  (left open)")
            for q in prep["questions"]
        )
        if prep["through_line"]:
            facts += f"\n- Through-line: {prep['through_line']}"

    if LANG == "he":
        # Same term policy as the script prompt: the editorial gate rejects a
        # spoken D2C/IAP/MENA, and the intro is written after the script, so a
        # mismatch here put them straight back in (2026-10-16 preview).
        lang_inst = ("Spoken Hebrew, as Israeli industry people talk. Say 'מכירה ישירה לצרכן' "
                     "rather than D2C, 'רכישות בתוך האפליקציה' rather than IAP, and "
                     "'המזרח התיכון וצפון אפריקה' rather than MENA. Familiar terms (UA, CPI, "
                     "LTV, ROAS, web shop, webstore) and all company and product names stay in English.")
    else:
        lang_inst = "Natural spoken English."

    # The story list above comes from the model's own summary and is not
    # guaranteed to be in the order the script tells them (the comparison run
    # announced "let's start with NetEase" on an episode that opens with
    # AppLovin v. Unity). What the script actually opens with is the ground
    # truth for the teaser's lead and the hand-over.
    opening = " ".join((t.get("text") or "") for t in (data.get("script") or [])[:2])

    prompt = f"""You write the INTRO of a weekly mobile-games podcast, AFTER the
rest of the episode already exists, so that it can preview what is really in it.

HOSTS: {SPEAKER_A} (the anchor) and {SPEAKER_B} (the analyst). {SPEAKER_A} has
just said the welcome line: "{welcome['text']}"

EPISODE TITLE: {data.get('episode_title', '')}

WHAT THE EPISODE CONTAINS, in running order:
{stories}

THE SCRIPT ACTUALLY OPENS WITH (the first story told -- the list above may
not be in running order, so this decides which story the teaser starts with
and which one the last line hands over to; the other stories follow after it):
"{opening[:600]}"

FACTS YOU MAY USE (each is only usable with its subject spelled out):
{facts or '(none)'}

QUESTIONS THE EPISODE TAKES ON:
{questions or '(none)'}

THE RULE THAT MATTERS MOST: the listener has heard nothing yet. Every
company, product, system or figure you mention must be explained in the same
breath, in a few plain words, the first time it appears -- "Pix, מערכת
התשלומים המיידית של ברזיל", not "Pix". Never refer to "the number", "that
story", "what we saw" or anything else the listener has not been told. If you
cannot explain it in one short clause, leave it out. (The previous intro opened
with the analyst saying a number about Pix in Brazil had surprised him: no
hello, no idea what Pix is, and the banter that followed was built on it. It
sounded like someone who forgot to say hello and started in the middle.)

Write the turns that come right after the welcome line, in this order:
1. {SPEAKER_B} greets back, simply and warmly ("היי דנה", "בוקר טוב, דנה"),
   with at most one light line of his own that needs no context -- about the
   work or the mood in the studio, never about this episode's content and
   never a mood line about the week (no "what a week", no "שבוע ...").
2. {SPEAKER_A} answers in one short, friendly line, then turns to the episode:
   a TEASER in plain spoken sentences -- 2 or 3 stories, the first being the one
   the script opens with, each in ONE sentence saying what happened and why it
   matters to someone who runs mobile games. Give the lead story one concrete
   number WITH its subject. No question marks in the teaser,
   except at most one short question (under 12 words) that the episode answers
   -- never one marked "left open", and never a compound question.
3. {SPEAKER_B} reacts in one short, human line to something she just said --
   now that the listener knows what it is. A raised eyebrow, a half-joke, a
   quick opinion. Not a summary, and not a cliche ("just the tip of the
   iceberg", "only you could...").
4. {SPEAKER_A} hands over to the first story with one short line that names it.
Do NOT invent personal anecdotes presented as fact, and no weather or holiday
small talk. Never name a day of the week or say "the weekend": you do not know
when the listener hears this. Every turn is short and spoken: nothing over about 60 words except
the teaser (about 90 at most).

EXAMPLE OF THE SHAPE ONLY -- different stories; never reuse its wording or
facts:
Yoni: היי דנה, בוקר טוב.
Dana: בוקר טוב, יוני. יש לנו היום פרק עם הרבה מספרים. נתחיל בחברת פרסום גדולה שעוברת למודל תמחור חדש, שבו מפתחים משלמים רק על התקנות שעדיין פעילות אחרי שבוע, ויש סטודיו אחד שמדווח שהעלות שלו ירדה בשליש. אחרי זה, חנות אפליקציות שפתחה בשוק גדול אפשרות לתשלום מחוץ לחנות, ומשחק פאזל ותיק שחזר לראש הטבלה אחרי שינוי בקצב העדכונים.
Yoni: שליש זה לא מעט, אם זה מחזיק.
Dana: נבדוק אם זה מחזיק. נתחיל בחברת הפרסום.

Company and product names stay in English even here -- write AppLovin and
Unity, never אפלובין or יוניטי.

Tone: calm, like colleagues, not radio hosts. No hype words (avoid עצום,
מטורף, מהפכה, דרמה, דרמטי, וואו, מדהים, "game changer") and no exclamations. No
turn opens with an agreement word (בדיוק, לגמרי, בהחלט, נכון, אכן). Nobody
speaks more than two turns in a row.

Total length: 60-100 words across all
turns. {lang_inst}
Never use the ASCII double-quote character (") in the text: write Hebrew
acronyms with the gershayim ״ (ארה״ב, מנכ״ל) or spell them out. Every turn
ends with full punctuation. Do not repeat the welcome and do not introduce the
hosts again.
"""
    attempt_prompt = prompt
    turns: list[dict] = []
    for attempt in (1, 2):
        try:
            raw = gemini_json_quota(attempt_prompt, INTRO_SCHEMA, tries=4, timeout=150,
                                    model=GEMINI_CREATIVE_MODEL)
        except Exception as e:                              # noqa: BLE001
            log(f"  intro attempt {attempt} failed ({type(e).__name__}: {e})")
            break
        turns, repairs = script_quality.repair_script(raw.get("turns", []))
        for note in repairs:
            log(f"  intro repair: {note}")
        turns, _ = script_quality.trim_agreement_openers(turns, keep=0)
        problems = intro_problems(turns)
        if not problems:
            log(f"  intro: {len(turns)} turns written after the episode")
            return [welcome] + turns, "model"
        for p in problems:
            log(f"  intro problem (attempt {attempt}): {p}")
        # On the last attempt, style faults alone are not worth throwing the
        # whole intro away for: a slightly hyped, real intro is better for the
        # listener than the plain fallback.
        if attempt == 2 and turns and all(p.startswith(STYLE) for p in problems):
            log("  intro: accepting it despite style faults (the fallback would be worse)")
            return [welcome] + turns, "model"
        attempt_prompt = prompt + (
            "\n\nA PREVIOUS ATTEMPT WAS REJECTED because: " + "; ".join(problems[:3])
            + ". Write it again so it satisfies every rule above."
        )

    # Fallback: the old shape, minimally -- a welcome and a one-line preview.
    titles = [s.get("title", "") for s in data.get("digest_summary", [])[:3] if s.get("title")]
    if titles:
        if LANG == "he":
            preview = "בפרק הזה: " + ", ".join(titles) + ". נתחיל."
        else:
            preview = "In this episode: " + ", ".join(titles) + ". Let's start."
        log("  intro: using the plain fallback (welcome + story list)")
        return [welcome, {"speaker": SPEAKER_A, "text": preview}], "fallback"
    log("  intro: using the welcome line alone")
    return [welcome], "fallback"


def add_intro(data: dict) -> dict:
    """Prepend the intro to the finished script. Marks the data so a resumed
    run never adds a second one."""
    intro, source = write_intro(data, data.get("prep"))
    data["script"] = intro + data["script"]
    data["intro_done"] = True
    data["intro_source"] = source
    return data



# ---------------------------------------------------------------- Gemini TTS

# finishReason values other than STOP mean the model stopped generating
# audio before it reached the end of the transcript it was given (hit its
# own output-length ceiling, a safety filter, etc). The API still returns
# whatever partial inlineData it produced, so this can't be told apart from
# a normal successful response just by looking at "is there audio bytes" --
# without this check gemini_tts_chunk() happily returns a clipped-mid-word
# recording as if it were the full chunk. This is what produced the
# 2026-09-07 episode's audio cutting off mid-sentence: one chunk was long
# enough to hit the ceiling, and nothing downstream noticed.
class TTSTruncatedError(RuntimeError):
    pass

# 8 was never a survivable number: at HTTP_TIMEOUT (300s) per hung attempt
# plus sleeps escalating to 175s, one chunk could legitimately spend ~52
# minutes before giving up -- longer than the entire job budget, for one
# quarter of one episode's audio. 2026-09-21 spent 20 minutes on chunk 1
# alone that way and died on chunk 3. _check_deadline() at the top of each
# attempt is the real bound now; this just stops the loop from being
# absurd on its own terms.
def gemini_tts_chunk(script_chunk_text: str, tries: int = 4) -> bytes:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is required for TTS.")

    prompt = f"""Synthesize the following conversation as speech.
Do not read any instructions aloud.

# AUDIO PROFILE
{SPEAKER_A} (Female Anchor): clear, professional, authoritative female voice.
{SPEAKER_B} (Male Analyst): deep, slightly skeptical, analytical male voice.

# DIRECTOR'S NOTES
{DIRECTION}

TRANSCRIPT:
{script_chunk_text}"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": [
                        {"speaker": SPEAKER_A, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE_A}}},
                        {"speaker": SPEAKER_B, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE_B}}},
                    ]
                }
            },
        },
    }

    body = json.dumps(payload).encode()

    attempt = -1
    while attempt < tries - 1:
        attempt += 1
        _check_deadline()
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{live_model(TTS_MODEL)}:generateContent")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
        )
        try:
            with urllib.request.urlopen(req, timeout=_remaining_timeout(TTS_TIMEOUT)) as r:
                data = json.loads(r.read())

            cand = (data.get("candidates") or [{}])[0]
            finish_reason = cand.get("finishReason")
            audio_bytes = None
            for part in (cand.get("content") or {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    audio_bytes = audio_tools.decode_pcm(
                        base64.b64decode(inline["data"], validate=True),
                        inline.get("mimeType", "audio/L16;rate=24000"))
                    break

            if audio_bytes is not None and finish_reason in (None, "STOP"):
                return audio_bytes

            if audio_bytes is not None:
                log(f"    TTS chunk truncated (finishReason={finish_reason}, "
                    f"attempt {attempt + 1}/{tries}); discarding partial audio and retrying...")
                if attempt == tries - 1:
                    raise TTSTruncatedError(
                        f"Gemini TTS kept truncating this chunk (finishReason={finish_reason}) "
                        f"after {tries} attempts."
                    )
            else:
                log(f"    TTS chunk returned no audio data (attempt {attempt + 1}/{tries}); retrying...")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:700]
            except Exception:                              # noqa: BLE001
                detail = ""
            log(f"    TTS HTTP {e.code} (attempt {attempt + 1}/{tries}) {detail[:300]}")
            if swap_retired_model(TTS_MODEL, e.code, detail, "TTS_MODEL"):
                tries += 1
                continue
            explain_billing_error(e.code)
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except TTSTruncatedError:
            raise
        except Exception as e:
            log(f"    TTS error (attempt {attempt + 1}/{tries}): {e}")
            if attempt == tries - 1:
                raise

        # Capped, not unbounded: the old 25*(attempt+1) reached 175s by the
        # last try, spending more of the run's budget waiting than working.
        _retry_sleep(min(25 * (attempt + 1), 60))

    raise RuntimeError("Gemini TTS returned no audio payload after retries.")

def synthesize_audio(script_turns: list[dict], wav_path: pathlib.Path,
                     mp3_path: pathlib.Path, date: str | None = None):
    """Synthesize the script and mux it with the jingle.

    `date` enables per-chunk checkpointing: each chunk is saved the moment
    it comes back, and a later run for the same date reuses what's already
    on disk instead of paying for it again. TTS is by far the slowest and
    most failure-prone stage, and it is the one where losing work hurts
    most -- see checkpoint.py for the incident this came from. Passing None
    keeps the old stateless behaviour, which is what the tests want.
    """
    # Preserve the editorial transcript; pronunciation substitutions apply to
    # synthesized speech only and are configurable in profile.toml.
    glossary = runtime.load_profile().get("speech", {}).get("replacements", {})
    # Listeners found the hosts rushed (2026-10-27): the model's own pace,
    # even when told "unhurried", leaves no room between points. atempo
    # slows the speech without changing pitch; 1.0 turns it off.
    tempo = float(runtime.load_profile().get("speech", {}).get("tempo", 1.0))
    if not 0.8 <= tempo <= 1.2:
        raise ValueError(f"[speech] tempo must be between 0.8 and 1.2, got {tempo}")
    slow = f"atempo={tempo:g}," if tempo != 1.0 else ""
    spoken_turns = speech_tools.prepare_turns(script_turns, glossary)
    chunks = audio_tools.chunks_for_turns(spoken_turns, TTS_CHUNK_CHAR_LIMIT)

    log(f"synthesizing audio via Gemini TTS in {len(chunks)} batched chunks...")
    USAGE_LOG["tts_chunks"] = len(chunks)

    def synthesize_with_split(chunk_text: str, label: str) -> bytes:
        # A chunk that keeps hitting the model's output ceiling (see
        # TTSTruncatedError) won't magically fit on a plain retry -- the
        # text is the same length every time. Halving it along turn
        # boundaries and synthesizing each half separately is what actually
        # gets under the ceiling; recursion handles a half that's still too
        # long. A single turn can't be split without cutting a sentence
        # mid-word, so that's the base case where we give up loudly instead
        # of shipping broken audio.
        try:
            return gemini_tts_chunk(chunk_text)
        except TTSTruncatedError:
            turns = chunk_text.split("\n")
            if len(turns) <= 1:
                speaker, text = chunk_text.split(": ", 1)
                if len(text.split()) < 12:
                    raise
                turns = audio_tools.chunks_for_turns(
                    [{"speaker": speaker, "text": text}], max(100, len(chunk_text) // 2 + 20))
            mid = len(turns) // 2
            log(f"    {label} kept truncating; splitting into two and retrying each half...")
            first = synthesize_with_split("\n".join(turns[:mid]), f"{label}a")
            second = synthesize_with_split("\n".join(turns[mid:]), f"{label}b")
            return first + second

    pcm = bytearray()
    for idx, chunk_text in enumerate(chunks, 1):
        identity = audio_identity(chunk_text)
        cached = checkpoint.load_chunk(date, idx, identity) if date else None
        if cached is not None:
            log(f"  TTS chunk {idx}/{len(chunks)}: reusing checkpoint "
                f"({len(cached)} bytes) -- no API call")
            pcm += cached
            continue

        log(f"  processing TTS chunk {idx}/{len(chunks)} ({len(chunk_text)} chars)...")
        audio_bytes = synthesize_with_split(chunk_text, f"chunk {idx}")
        if date:
            # Save before the inter-chunk sleep, not after the loop: the
            # whole point is that a chunk survives whatever kills the run
            # next. 2026-09-21 finished two chunks and kept neither.
            checkpoint.save_chunk(date, idx, audio_bytes, identity)
        pcm += audio_bytes
        if idx < len(chunks):
            _retry_sleep(float(os.environ.get("TTS_CHUNK_DELAY_SEC", "0")))

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(bytes(pcm))

    jingle_m4a = ASSETS_DIR / "jingle.m4a"
    jingle_mp3 = ASSETS_DIR / "jingle.mp3"
    jingle_file = jingle_m4a if jingle_m4a.exists() else (jingle_mp3 if jingle_mp3.exists() else None)

    staged = mp3_path.with_name(mp3_path.stem + ".part.mp3")
    loud = "loudnorm=I=-19:TP=-1.5:LRA=9"

    def jingle_cmd(speech_filter: str) -> list[str]:
        mix = (
            "[0:a]atrim=duration=4.5,afade=t=out:st=2.0:d=2.5,volume=0.30[jingle_fade];"
            f"[1:a]{speech_filter}adelay=2200|2200[speech_delayed];"
            "[jingle_fade][speech_delayed]amix=inputs=2:normalize=0:duration=longest[intro_mixed];"
            "[2:a]atrim=duration=5,afade=t=out:st=2:d=3,volume=0.30[outro];"
            f"[intro_mixed][outro]concat=n=2:v=0:a=1,{loud}[outa]"
        )
        return ["ffmpeg", "-y", "-loglevel", "error", "-i", str(jingle_file), "-i", str(wav_path),
                "-i", str(jingle_file), "-filter_complex", mix, "-map", "[outa]",
                "-ar", "24000", "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "96k", str(staged)]

    def plain_cmd(speech_filter: str) -> list[str]:
        return ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-af", f"{speech_filter}{loud}",
                "-ar", "24000", "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "96k", str(staged)]

    # 2026-11-11: the full graph died with "Invalid data found when processing
    # input" after the speech had been paid for and voiced (14 minutes of TTS).
    # A finished voice track must not be lost to a cosmetic filter, so each step
    # drops one nicety: first the slow-down, then the jingle.
    variants: list[tuple[str, list[str]]] = []
    if jingle_file:
        log(f"found jingle asset ({jingle_file.name}), applying radio ducking & outro fade...")
        variants.append(("jingle + slow-down" if slow else "jingle", jingle_cmd(slow)))
        if slow:
            variants.append(("jingle, without the slow-down", jingle_cmd("")))
        variants.append(("speech only, without the jingle", plain_cmd("")))
    else:
        variants.append(("speech only", plain_cmd(slow)))
        if slow:
            variants.append(("speech only, without the slow-down", plain_cmd("")))
    last_error = ""
    for label, cmd in variants:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_remaining_timeout(600))
        if done.returncode == 0 and staged.exists() and staged.stat().st_size > 0:
            if label != variants[0][0]:
                print(f"::warning::the audio was assembled as: {label} (the full mix failed: "
                      f"{last_error[-300:].strip()})", flush=True)
            break
        last_error = (done.stderr or "").strip() or f"exit {done.returncode}"
        log(f"  ffmpeg ({label}) failed: {last_error[-400:]}")
        staged.unlink(missing_ok=True)
    else:
        raise RuntimeError(f"ffmpeg could not assemble the episode: {last_error[-400:]}")
    if staged.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced an empty episode")
    staged.replace(mp3_path)
    wav_path.unlink(missing_ok=True)
    log(f"audio generated successfully: {mp3_path.name} ({mp3_path.stat().st_size / 1e6:.2f} MB)")

def get_duration(mp3_path: pathlib.Path) -> int:
    res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
            capture_output=True, text=True, check=True, timeout=30
        )
    duration = int(float(res.stdout.strip()))
    if duration <= 0:
        raise ValueError("Episode has no playable duration")
    return duration


def audio_identity(text: str) -> str:
    return runtime.fingerprint([text, TTS_MODEL, SPEAKER_A, SPEAKER_B, VOICE_A, VOICE_B,
                                DIRECTION, "pcm-mono-24000-v2"])

# ---------------------------------------------------------------- Feed & Output

def render_digest_md(data: dict, articles: list[dict], today: str, episode_title: str) -> str:
    md = [f"# {episode_title}\n"]
    for item in data.get("digest_summary", []):
        md.append(f"## {item.get('title', 'Topic')}")
        md.append(f"**Key Takeaway:** {item.get('key_takeaway', '')}\n")
        if item.get("metrics_mentioned"):
            md.append("**Metrics:** " + ", ".join(item["metrics_mentioned"]))
        md.append("")
    # The editorial prep's questions, so the show's reasoning is visible in
    # the repo next to the episode it produced: what was asked, whether the
    # material answered it, and which questions were honestly left open.
    prep = data.get("prep")
    if prep and prep.get("questions"):
        md.append("## Discussion questions (editorial prep)")
        for q in prep["questions"]:
            md.append(f"- **{q['question']}**")
            if q["answer_basis"] == "OPEN":
                md.append("  - _Open: nothing in the material answers this._")
            else:
                md.append(f"  - [{q['answer_basis']}] {q['answer']}")
        md.append("")
    md.append("## Sources")
    for a in articles:
        md.append(f"- [{a['title']}]({a['url']}) ({a.get('source', '')})")
    return "\n".join(md)

def build_feed():
    items = []
    for mp3 in sorted(EPISODES.glob("*.mp3"), reverse=True):
        # Filenames are "<date>[-vN]" (a version suffix is added whenever an
        # already-published episode gets regenerated -- see CHANGELOG,
        # 2026-08-31 guid incident). Pull the leading date out directly
        # instead of stripping a specific hardcoded suffix: the old
        # `.replace("-v2", "")` silently dropped every non-"-v2" episode
        # from the feed, because a failed strptime() just skips it.
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:-v\d+)?", mp3.stem)
        if not m:
            log(f"  skipping {mp3.name}: filename doesn't start with a date")
            continue
        date_str = m.group(1)
        meta_path = mp3.with_suffix(".json")
        if not meta_path.exists() or mp3.stat().st_size == 0:
            log(f"  skipping incomplete episode {mp3.name}")
            continue
        meta = json.loads(meta_path.read_text())
        try:
            pub = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(hour=6, tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        notes = meta.get("notes") or meta.get("summary", "")
        plain = xml_escape(re.sub(r"<[^>]+>", " ", notes).strip()[:900])

        title = xml_escape(meta.get('title', f"Ben's Weekly Digest {date_str}"))

        items.append(f"""    <item>
      <title>{title}</title>
      <description><![CDATA[{notes.replace(']]>', ']]]]><![CDATA[>')}]]></description>
      <content:encoded><![CDATA[{notes.replace(']]>', ']]]]><![CDATA[>')}]]></content:encoded>
      <itunes:summary>{plain}</itunes:summary>
      <pubDate>{format_datetime(pub)}</pubDate>
      <guid isPermaLink="false">bens-digest-{mp3.stem}</guid>
      <enclosure url="{xml_escape(BASE_URL, {chr(34): '&quot;'})}/episodes/{mp3.name}" length="{mp3.stat().st_size}" type="audio/mpeg"/>
      <itunes:duration>{meta.get('duration', 0)}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Ben's Weekly Digest</title>
    <link>{xml_escape(BASE_URL)}</link>
    <description>Weekly mobile-gaming briefing.</description>
    <language>{'he' if LANG == 'he' else 'en'}</language>
    <itunes:author>Automated</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Technology"/>
    <itunes:image href="{BASE_URL}/assets/cover.jpg"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    runtime.atomic_write(ROOT / "feed.xml", feed)
    log(f"feed.xml rebuilt with {len(items)} episodes")

# ---------------------------------------------------------------- main

SHOW_NAME = "Ben's Weekly Digest"


def show_title(episode_title: str) -> str:
    """Episode title with the show name appended exactly once.

    The 2026-09-15 episode shipped to the live feed as "... | Ben's Weekly
    Digest | Ben's Weekly Digest". The suffix used to be appended
    unconditionally, and the script prompt has the hosts say the show name
    out loud, so Gemini sometimes folds it into episode_title as well.
    Stripping before appending makes this idempotent no matter what the
    model returns. It matters more than a cosmetic bug normally would: the
    title is what a subscriber actually reads in their player, and a
    published item's guid is permanent, so the title is the only part of it
    still worth getting right afterwards.
    """
    title = (episode_title or "").strip()
    while title.endswith(SHOW_NAME):
        title = title[: -len(SHOW_NAME)].rstrip().rstrip("|").rstrip()
    return f"{title} | {SHOW_NAME}" if title else SHOW_NAME






def preflight(audio: bool = True) -> None:
    """Catch local setup faults before spending model quota."""
    import shutil
    runtime.load_profile()
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is required")
    if LANG not in {"he", "en"}:
        raise ValueError("DIGEST_LANG must be he or en")
    if not 0 < max(HTTP_TIMEOUT, TTS_TIMEOUT) < RUN_DEADLINE_SEC:
        raise ValueError("API and TTS timeouts must be positive and below RUN_DEADLINE_SEC")
    if audio:
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                raise RuntimeError(f"Install {tool} before generating an episode")


def publication_complete(today: str) -> bool:
    """A playable file alone is not a committed publication transaction."""
    required = [EPISODES / f"{today}.mp3", EPISODES / f"{today}.json",
                DIGESTS / f"{today}.md", DIGESTS / f"{today}-script.md", ROOT / "feed.xml"]
    if not all(p.exists() and p.stat().st_size > 0 for p in required):
        return False
    try:
        manifest = json.loads((ROOT / "reports" / f"{today}-run.json").read_text())
        fingerprints = manifest.get("artifacts", {})
        intact = all(fingerprints.get(str(p.relative_to(ROOT)))
                     == hashlib.sha256(p.read_bytes()).hexdigest() for p in required)
        return (manifest.get("status") == "complete" and intact
                and f"bens-digest-{today}</guid>" in (ROOT / "feed.xml").read_text()
                and json.loads(STATE_FILE.read_text()).get("last_run") == today)
    except (OSError, ValueError):
        return False


CATCH_UP_WINDOW_DAYS = 4


def recent_episode(today: str, days: int = CATCH_UP_WINDOW_DAYS) -> str | None:
    """The date of an episode published in the `days` before `today`, if any.

    The workflow also fires on Tuesday and Wednesday so a Monday lost to a
    Google outage (2026-11-10: 35 minutes of 503s on scoring, then the stories)
    still gets its episode that week. Those catch-up firings must do nothing
    when the week already has one. Four days: a Wednesday catch-up is five
    days before the next Monday, which must still run.
    """
    day = dt.date.fromisoformat(today)
    for mp3 in sorted(EPISODES.glob("????-??-??.mp3"), reverse=True):
        try:
            published = dt.date.fromisoformat(mp3.stem)
        except ValueError:
            continue
        if 0 < (day - published).days <= days:
            return mp3.stem
    return None


def produce_script(articles: list[dict], today: str, memory_context: str,
                   resume: dict | None, min_segments: int | None = None) -> dict:
    """The stories, title, outro and digest summary (no intro yet). One Gemini
    call per story, three at a time; each finished story is checkpointed."""
    import segments

    def call(prompt, schema, *, model, tries, timeout):
        return gemini_json_quota(prompt, schema, model=model, tries=tries, timeout=timeout)

    def save(done: dict) -> None:
        checkpoint.save_content(today, articles, {"segments": done})

    log(f"writing {len(articles)} stories, {segments.WORKERS} at a time...")
    return segments.build_episode(
        articles, call=call, save=save, resume=resume, log=log,
        models={"creative": GEMINI_CREATIVE_MODEL, "review": GEMINI_REVIEW_MODEL},
        speakers=(SPEAKER_A, SPEAKER_B), lang=LANG, memory_context=memory_context,
        **({"min_segments": min_segments} if min_segments else {}))


def finalize_episode(data: dict, today: str) -> dict:
    """Last checks before audio, no model calls: the script is whole, and the
    per-story review is written to reports/ so what was cut and why is visible."""
    script = data.get("script")
    if not isinstance(script, list) or not script:
        raise RuntimeError("The episode has no script")
    if {t.get("speaker") for t in script} != {SPEAKER_A, SPEAKER_B}:
        raise RuntimeError("Both hosts must speak in the episode")
    stories = data.get("segments") or {}
    dropped = data.get("dropped") or []
    notes = [i for s in stories.values() for i in s.get("review", []) or []]
    data["editorial_review"] = {
        "passed": not dropped,
        "published_with_warnings": bool(dropped),
        "stories": len(data.get("kept") or []),
        "dropped": dropped,
        "model": live_model(GEMINI_REVIEW_MODEL),
        "notes": notes,
        "proofread": data.get("proofread"),
    }
    runtime.atomic_write(ROOT / "reports" / f"{today}-editorial.json", json.dumps({
        **data["editorial_review"],
        "segments": {k: {kk: v.get(kk) for kk in
                         ("status", "reason", "stance", "headline", "key_points", "caution", "review")}
                     for k, v in sorted(stories.items(), key=lambda kv: int(kv[0]))},
    }, ensure_ascii=False, indent=2))
    return data


def main() -> int:
    llm.start_run()
    EPISODES.mkdir(parents=True, exist_ok=True)
    DIGESTS.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(exist_ok=True)

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    done: set[str] = set(state.get("processed", []))

    # NOT "+ '-v2'": that suffix was a one-off manual fix (see CHANGELOG,
    # 2026-09-02 guid incident) for republishing an ALREADY-LIVE date under
    # a fresh RSS guid. Baking it into every run here would permanently
    # name every future episode "-v2" -- build_feed()'s regex-based date
    # parsing (also from that incident) supports any suffix or none, so a
    # plain date is exactly what a normal, first-time weekly run needs.
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()

    # Idempotence guard, which is what makes an automatic retry safe: the
    # workflow fires a few times on Monday so a bad Gemini day heals
    # itself without anyone noticing it, and every firing after a
    # successful one has to be a cheap no-op rather than a second episode
    # for the same date. Set FORCE_REGENERATE=1 to rebuild a date on
    # purpose (as on 2026-08-31, when an episode had to be regenerated
    # after a selection bug).
    force = os.environ.get("FORCE_REGENERATE", "").lower() in {"1", "true", "yes"}
    if publication_complete(today) and not force:
        log(f"episode for {today} already exists; nothing to do. "
            "(set FORCE_REGENERATE=1 to rebuild it deliberately)")
        return 0
    if force:
        checkpoint.clear(today)
    earlier = recent_episode(today)
    if earlier and dt.date.fromisoformat(today).weekday() != 0 and not force:
        log(f"this week already has an episode ({earlier}); a catch-up run has nothing to do.")
        return 0

    # Resume first, before spending anything. An earlier attempt today may have
    # finished some or all of the stories (each is checkpointed the moment it
    # is done), so a retry pays only for what is missing. See checkpoint.py.
    adopted = checkpoint.adopt_recent(today)
    if adopted:
        log(f"continuing the unfinished episode of {adopted} (a catch-up run)")
    resumed = checkpoint.load_content(today)
    if resumed:
        articles, data = resumed
        source_health.record(data.get("source_alerts", []))
        log(f"resuming from checkpoint: {len(articles)} articles, "
            f"{len(data.get('segments') or {})} stories written, "
            f"{len(data.get('script') or [])} script turns assembled")
    else:
        from discovery import select
        articles = select()
        data = {}
        if not articles:
            log("No articles cleared relevance threshold. Exiting clean.")
            state["last_run"] = today
            runtime.atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))
            return 0
        # Save the selection itself: scoring is ~5 paid calls, and a run that
        # fails before its first story finishes must not buy it again.
        data = {"segments": {}}
        checkpoint.save_content(today, articles, data)

    # 1. The stories, one call each (segments.py). Skipped when the checkpoint
    # already holds the assembled script. The checkpoint always keeps the full
    # selection: which stories survive review is recorded in data["kept"] as
    # indexes into it.
    selected = articles
    if not data.get("script"):
        built = produce_script(selected, today, memory.load_recent_context(), data.get("segments"))
        data = {**built, "source_alerts": source_health.pending_alerts(),
                # Explicitly False: a resumed run knows the intro is still owed
                # and a legacy checkpoint without the key never gets a second greeting.
                "intro_done": False,
                # Same idea for the proofreading pass (proofread.py): a legacy
                # checkpoint without the key, whose speech may already be cached,
                # is never re-proofread, so its cached chunks stay valid.
                "proofread_done": False}
        checkpoint.save_content(today, selected, data)

    # The intro is written LAST, once the stories it previews exist. Never
    # raises (it falls back to a plain welcome), so this cannot fail a run.
    if data.get("intro_done") is False:
        data = add_intro(data)
        checkpoint.save_content(today, selected, data)

    # Proofreading: one call over the whole episode, after the intro so it reads
    # what the listener hears. Never raises; see proofread.py.
    if data.get("proofread_done") is False:
        data["script"], data["proofread"] = proofread.proofread_episode(
            data["script"], call=lambda *a, **k: gemini_json_quota(*a, **k),
            model=GEMINI_CREATIVE_MODEL, lang=LANG, log=log)
        data["proofread_done"] = True
        checkpoint.save_content(today, selected, data)

    data = finalize_episode(data, today)

    # Stories that did not survive review are not part of the published episode.
    kept = data.get("kept")
    articles = [a for i, a in enumerate(selected) if kept is None or i in kept]

    episode_title = data.get("episode_title", "Weekly Gaming Digest")
    full_title = show_title(episode_title)

    # 2. Save Digest & Script
    runtime.atomic_write(DIGESTS / f"{today}.md", render_digest_md(data, articles, today, full_title))
    script_text = "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in data["script"])
    runtime.atomic_write(DIGESTS / f"{today}-script.md", script_text)

    # 3. Audio Synthesis via Gemini TTS + Ducking Jingle Assembly
    wav_path = EPISODES / f"{today}.wav"
    mp3_path = EPISODES / f"{today}.mp3"
    synthesize_audio(data["script"], wav_path, mp3_path, date=today)
    duration = get_duration(mp3_path)
    words = script_quality.count_words(data["script"])
    wpm = round(words / max(duration - 7, 1) * 60, 1)
    if not 65 <= wpm <= 230:
        raise RuntimeError(f"Audio duration is implausible for script ({wpm} words/min); inspect before publication")

    # 4. Save Metadata & Update RSS
    first_para = data["digest_summary"][0]["key_takeaway"] if data.get("digest_summary") else "Weekly Gaming Digest"
    credited = []
    for a in articles:
        credited.extend([a] + a.get("_also", []))
    notes_links = "".join(f'<li><a href="{xml_escape(a["url"], {chr(34): "&quot;"})}">{xml_escape(a["title"])}</a> <em>({xml_escape(a.get("source", ""))})</em></li>' for a in credited)
    notes = f"<p>{xml_escape(first_para)}</p><p><strong>Sources ({len(articles)}):</strong></p><ol>{notes_links}</ol>"

    runtime.atomic_write(mp3_path.with_suffix(".json"),
        json.dumps({
            "title": full_title,
            "summary": first_para,
            "notes": notes,
            "duration": duration,
            "sources": [{"title": a["title"], "url": a["url"], "source": a.get("source", "")} for a in articles]
        }, ensure_ascii=False, indent=2),
    )

    build_feed()

    state["processed"] = sorted(done | {a["url"] for a in articles})
    state["last_run"] = today
    runtime.atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))

    # Written only after everything above succeeded, so a failed run never
    # leaves a phantom episode in the show's memory.
    entry = memory.build_entry(full_title, data.get("digest_summary", []))
    entry["date"] = today
    memory.append_entry(entry)

    published_files = [mp3_path, mp3_path.with_suffix(".json"), DIGESTS / f"{today}.md",
                       DIGESTS / f"{today}-script.md", ROOT / "feed.xml"]
    runtime.atomic_write(ROOT / "reports" / f"{today}-run.json", json.dumps({
        "status": "complete", "date": today, "words": words, "duration_seconds": duration,
        "words_per_minute": wpm, "models": {"text": live_model(GEMINI_TEXT_MODEL),
        "scoring": live_model(GEMINI_SCORING_MODEL), "creative": live_model(GEMINI_CREATIVE_MODEL),
        "review": live_model(GEMINI_REVIEW_MODEL),
        "tts": live_model(TTS_MODEL)}, "usage": USAGE_LOG,
        "elapsed_seconds": round(time.monotonic() - llm._run_started, 1),
        "profile_fingerprint": runtime.fingerprint(runtime.load_profile()),
        "editorial": data.get("editorial_review"),
        "artifacts": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in published_files},
        "sources": [{"title": a["title"], "url": a["url"], "published": a.get("published"),
                     "score": a.get("_score"), "airtime": a.get("_airtime")} for a in articles],
    }, ensure_ascii=False, indent=2, default=str))

    # Last thing, deliberately: while any of the above can still fail, the
    # checkpoint is the thing that makes the retry cheap. Only a fully
    # published episode has earned the right to delete it.
    checkpoint.clear(today)

    log("Finished run cleanly using Gemini API.")
    return 0

if __name__ == "__main__":
    from urllib.parse import urlsplit
    if urlsplit(BASE_URL).scheme not in {"https", "http"} or not urlsplit(BASE_URL).netloc:
        sys.exit("PODCAST_BASE_URL must be an absolute http(s) URL")
    runtime.load_profile()
    preflight()
    sys.exit(main())
