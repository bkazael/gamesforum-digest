#!/usr/bin/env python3
"""
Gamesforum -> Digest + Podcast Pipeline v6.3 (Gemini 3-Chunk TTS Edition)
"""

from __future__ import annotations

import base64
import datetime as dt
import html
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

# ---------------------------------------------------------------- config

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
TTS_MODEL = os.environ.get("TTS_MODEL", "gemini-2.5-flash-preview-tts")

BASE_URL = os.environ.get("PODCAST_BASE_URL", "").rstrip("/")
LANG = os.environ.get("DIGEST_LANG", "he")

SITE = "https://www.globalgamesforum.com"
ROOT = pathlib.Path(__file__).resolve().parent
EPISODES = ROOT / "episodes"
DIGESTS = ROOT / "digests"
STATE_FILE = ROOT / "state.json"
ASSETS_DIR = ROOT / "assets"

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
# (RUN_DEADLINE_SEC = 25 min) < the step (30 min) < the job (35 min).
HTTP_TIMEOUT = int(os.environ.get("API_TIMEOUT_SEC", "300"))

# TTS gets its own, shorter per-attempt ceiling. Real successful chunks in
# production have taken 1-4 minutes; the calls that hang never come back at
# all and just burn the full HTTP_TIMEOUT. 270s keeps the slowest observed
# *successful* call comfortably inside the window while capping a hung one.
TTS_TIMEOUT = int(os.environ.get("TTS_TIMEOUT_SEC", "270"))

RUN_DEADLINE_SEC = int(os.environ.get("RUN_DEADLINE_SEC", "1500"))

# One source of truth for the TTS chunk split, so test_episode.py's chunking
# test can import this instead of hand-copying the number -- a copy is
# exactly how that test drifted out of sync with production before (it
# tested against 1500 while this was already 3800).
TTS_CHUNK_CHAR_LIMIT = 3800
_run_started = time.monotonic()

def _load_voice() -> dict:
    cfg = {
        "speaker_a": "Dana", "voice_a": "Kore",
        "speaker_b": "Yoni", "voice_b": "Charon",
        "direction": "Two industry colleagues talking. Measured, unhurried, genuinely interested. Strategic and deep.",
    }
    path = ROOT / "profile.toml"
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

USAGE_LOG = {"input_tokens": 0, "output_tokens": 0, "tts_chunks": 0}

def log(*a):
    print("[pipeline]", *a, flush=True)

def _check_deadline():
    elapsed = time.monotonic() - _run_started
    if elapsed > RUN_DEADLINE_SEC:
        # Deliberately loud and specific: this is the *graceful* exit, and
        # it needs to be distinguishable at a glance from the ungraceful
        # one (the Actions step timeout killing us mid-call). If you are
        # reading this in a log, the run stopped itself on purpose, and any
        # checkpoint written so far is intact for the next attempt.
        raise RuntimeError(
            f"Run exceeded its {RUN_DEADLINE_SEC}s deadline after "
            f"{elapsed:.0f}s; aborting before the CI step gets killed. "
            "Progress so far is checkpointed -- re-running resumes from it."
        )

def http_get(url: str, tries: int = 3) -> str:
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)
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

    m = re.search(r'meta property="og:title" content="([^"]+)"', page)
    title = html.unescape(m.group(1)) if m else url.rsplit("/", 1)[-1]
    body = page
    h1 = re.search(r"(?is)<h1[^>]*>.*?</h1>", page)
    if h1:
        body = page[h1.end():]
    cut = re.search(r"(?i)you might also like|SIGN UP TO OUR NEWSLETTER", body)
    if cut:
        body = body[: cut.start()]

    text = strip_tags(body)
    if len(text) < 400:
        return None
    return {"url": url, "title": title, "text": text[:14000]}

# ---------------------------------------------------------------- Gemini Text API

def gemini_json(prompt: str, schema: dict | None = None, tries: int = 6,
                timeout: int | None = None) -> dict:
    """One schema-constrained Gemini call, retried on failure.

    `tries` and `timeout` exist for the OPTIONAL stages (editorial prep, the
    intro): at the default 6 x 300s a hung call can spend half an hour, which
    is fine to say about a stage that must succeed but is exactly the trap
    that burned the 2026-09-21 run on TTS. A stage whose failure just means
    "fall back to the old behaviour" gets 2 short attempts, not a whole
    run's budget.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is required.")
    attempt_timeout = timeout or HTTP_TIMEOUT

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={GEMINI_API_KEY}"

    gen_config = {"responseMimeType": "application/json"}
    if schema:
        gen_config["responseSchema"] = schema

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_config
    }

    body = json.dumps(payload).encode()
    for attempt in range(tries):
        _check_deadline()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=attempt_timeout) as r:
                data = json.loads(r.read())
            cand = (data.get("candidates") or [{}])[0]
            text = (cand.get("content") or {}).get("parts", [{}])[0].get("text", "")
            log(f"    [Gemini responded in {time.monotonic() - started:.1f}s]")
            return json.loads(text)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            log(f"    Gemini API attempt {attempt + 1}/{tries} failed: HTTP {e.code} - {err_body}")
            if attempt == tries - 1:
                raise
            time.sleep(10 * (attempt + 1))
        except Exception as e:
            log(f"    Gemini API attempt {attempt + 1}/{tries} failed: {e}")
            if attempt == tries - 1:
                raise
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("unreachable")

# ---------------------------------------------------------------- JSON Schema & Prompt

PODCAST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "episode_title": {
            "type": "STRING",
            "description": "Catchy podcast title based on top stories"
        },
        "digest_summary": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "key_takeaway": {"type": "STRING"},
                    "metrics_mentioned": {"type": "ARRAY", "items": {"type": "STRING"}}
                },
                "required": ["title", "key_takeaway", "metrics_mentioned"]
            }
        },
        "script": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "speaker": {"type": "STRING", "enum": [SPEAKER_A, SPEAKER_B]},
                    "text": {"type": "STRING"}
                },
                "required": ["speaker", "text"]
            }
        }
    },
    "required": ["episode_title", "digest_summary", "script"]
}

# ---------------------------------------------------------------- editorial prep, and the intro written last
#
# The episode used to be one call: read everything, write intro + stories +
# outro in a single pass. Two things went wrong with that. The intro was
# written before the model had decided what the episode contained, so it
# could only be generic (every week opened "a fascinating week in the
# industry"). And the "debate" was fake: the analyst raised open questions
# that the anchor never answered, because nothing had worked out the answers
# before the dialogue started. So the work is now ordered the way a producer
# would do it:
#
#   1. prepare_episode(): read ALL the articles, pull the concrete points from
#      each, judge how far each source can be trusted, then write the
#      questions worth debating and answer them from the material (this
#      week's articles, or earlier episodes) -- or say plainly that nothing
#      answers them.
#   2. generate_podcast_content(prep=...): the stories and outro, working from
#      those notes. No greeting -- there is no intro yet.
#   3. write_intro(): only now, with the finished episode in hand, the
#      welcome, a moment of host banter, and a preview of what is actually in
#      it.
#
# Both new stages degrade to the old behaviour instead of failing the run:
# no prep -> the script is written from the articles alone; no intro -> a
# plain welcome plus a one-line list of the stories.

STANCES = ["NEUTRAL_REPORTING", "COMPANY_CLAIMS", "VENDOR_MARKETING"]

PREP_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "articles": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER",
                           "description": "The article's number, as in 'ARTICLE 3'."},
                    "stance": {"type": "STRING", "enum": STANCES},
                    "key_points": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "caution": {"type": "STRING"},
                    "takeaway": {"type": "STRING"},
                },
                "required": ["id", "stance", "key_points", "caution", "takeaway"],
            },
        },
        "questions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "about": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "answer_basis": {"type": "STRING",
                                     "enum": ["ARTICLE", "PREVIOUS_EPISODE", "OPEN"]},
                    "answer": {"type": "STRING"},
                },
                "required": ["question", "about", "answer_basis", "answer"],
            },
        },
        "through_line": {"type": "STRING"},
    },
    "required": ["articles", "questions", "through_line"],
}

INTRO_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "turns": {
            "type": "ARRAY",
            "items": PODCAST_SCHEMA["properties"]["script"]["items"],
        },
    },
    "required": ["turns"],
}

MAX_QUESTIONS = 6
MAX_OPEN_QUESTIONS = 2

# Stories + outro only; the intro adds ~200 words on top, which keeps the
# finished episode in the show's long-standing 1,500-1,900 range.
SCRIPT_TARGET_WORDS = (1400, 1800)
SCRIPT_FLOOR_WORDS = 1250


def _clean_prep(prep: dict, n_articles: int) -> dict:
    """Make a model response safe to build prompts from.

    Everything here is defensive: the schema guarantees the shape, not that
    the model numbered articles sensibly or kept its promise to leave an
    unanswerable question OPEN. A prep that is wrong in a small way should
    be repaired or trimmed, not trusted and not thrown away whole.
    """
    articles, seen = [], set()
    for a in prep.get("articles", []) or []:
        try:
            aid = int(a.get("id"))
        except (TypeError, ValueError):
            continue
        if not 1 <= aid <= n_articles or aid in seen:
            continue
        seen.add(aid)
        points = [str(p).strip() for p in (a.get("key_points") or []) if str(p).strip()]
        articles.append({
            "id": aid,
            "stance": a.get("stance") if a.get("stance") in STANCES else "NEUTRAL_REPORTING",
            "key_points": points[:6],
            "caution": str(a.get("caution") or "").strip(),
            "takeaway": str(a.get("takeaway") or "").strip(),
        })
    articles.sort(key=lambda a: a["id"])

    questions, opens = [], 0
    for q in prep.get("questions", []) or []:
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        basis = q.get("answer_basis")
        answer = str(q.get("answer") or "").strip()
        # An "answer" with no text is no answer; and a question can only be
        # called answered if it says where the answer came from.
        if basis not in ("ARTICLE", "PREVIOUS_EPISODE") or not answer:
            basis, answer = "OPEN", ""
        if basis == "OPEN":
            if opens >= MAX_OPEN_QUESTIONS:
                continue
            opens += 1
        about = [i for i in (q.get("about") or []) if isinstance(i, int) and 1 <= i <= n_articles]
        questions.append({"question": text, "about": about,
                          "answer_basis": basis, "answer": answer})
        if len(questions) >= MAX_QUESTIONS:
            break
    return {"articles": articles, "questions": questions,
            "through_line": str(prep.get("through_line") or "").strip()}


def prepare_episode(articles: list[dict], memory_context: str = "") -> dict | None:
    """Editorial prep: points per article, source stance, and answered
    questions. Returns None on any failure -- the caller then writes the
    script from the articles alone, exactly as before this stage existed.
    """
    corpus = "\n\n".join(
        f"ARTICLE {i + 1}: {a['title']}\nSOURCE: {a.get('source', '')}\n\n{a['text']}"
        for i, a in enumerate(articles)
    )
    history = ""
    if memory_context:
        history = (
            "\nPREVIOUS EPISODES (an answer may come from here, with basis "
            "PREVIOUS_EPISODE; it must not name a company or vendor that none "
            "of today's articles is about):\n" + memory_context + "\n"
        )
    prompt = f"""You are the producer preparing one episode of a two-host
podcast for a mobile-games company owner whose portfolio is casual/puzzle,
hybrid-casual, and real-money skill games. The hosts have not written
anything yet. Read ALL the articles below, then produce the notes they will
work from. Write the notes in English.

FOR EACH ARTICLE
- stance: NEUTRAL_REPORTING (a publication reporting facts or data);
  COMPANY_CLAIMS (a company's own announcement, or an executive's byline:
  claims without independent evidence); VENDOR_MARKETING (a vendor promoting
  its own product, even when it cites real numbers).
- key_points: 2-5 specific, checkable points. Figures WITH what they compare
  to (period, base, whose data); named parties; and status/timeline (a
  proposal or a ruling? in force from when?). Use only what the article text
  says. No generalities.
- caution: what the article does NOT show, or what must not be overstated
  (a vendor's own data, a small sample, a proposal not a law, totals that
  differ from another source). Empty only if there is truly nothing.
- takeaway: the one concrete thing an operator in these genres should check,
  test or watch because of this article. Specific -- never "stay informed".

THEN {MAX_QUESTIONS} QUESTIONS AT MOST (3-6 is right): what a sharp operator
would ask that connects stories or probes a weak point. Answer each ONLY from
the articles below or from PREVIOUS EPISODES, and set answer_basis to say
which. If the material does not answer it, answer_basis is OPEN and answer is
"" -- never invent an answer. At most {MAX_OPEN_QUESTIONS} OPEN questions;
prefer ones the material can answer. If two articles give different figures
for the same thing, that is itself a question.

through_line: one sentence naming a theme that genuinely ties several of the
stories together, or "" if there is none. Do not force one.
{history}
ARTICLES:
{corpus}
"""
    log("editorial prep: points, source stance, and answered questions...")
    try:
        raw = gemini_json(prompt, PREP_SCHEMA, tries=2, timeout=240)
        prep = _clean_prep(raw, len(articles))
    except Exception as e:                                  # noqa: BLE001
        log(f"  editorial prep failed ({type(e).__name__}: {e}); "
            "the script will be written from the articles alone")
        return None
    if not prep["articles"]:
        log("  editorial prep came back with no usable articles; ignoring it")
        return None
    answered = sum(1 for q in prep["questions"] if q["answer_basis"] != "OPEN")
    log(f"  prep: {len(prep['articles'])} articles, {len(prep['questions'])} questions "
        f"({answered} answered from the material, "
        f"{len(prep['questions']) - answered} open)")
    return prep


def _format_prep(prep: dict, articles: list[dict]) -> str:
    """The prep as a briefing block inside the script prompt."""
    lines = []
    by_id = {a["id"]: a for a in prep["articles"]}
    for i, art in enumerate(articles, 1):
        a = by_id.get(i)
        if not a:
            continue
        lines.append(f"ARTICLE {i} -- {art['title'][:80]}  [stance: {a['stance']}]")
        for p in a["key_points"]:
            lines.append(f"  - {p}")
        if a["caution"]:
            lines.append(f"  careful: {a['caution']}")
        if a["takeaway"]:
            lines.append(f"  takeaway: {a['takeaway']}")
    if prep["questions"]:
        lines.append("\nQUESTIONS TO DEBATE")
        for n, q in enumerate(prep["questions"], 1):
            arts = ",".join(str(i) for i in q["about"]) or "-"
            lines.append(f"Q{n} (articles {arts}): {q['question']}")
            if q["answer_basis"] == "OPEN":
                lines.append("   no answer in the material -- OPEN")
            else:
                lines.append(f"   answer [{q['answer_basis']}]: {q['answer']}")
    if prep["through_line"]:
        lines.append(f"\nTHROUGH-LINE: {prep['through_line']}")
    return "\n".join(lines)


def welcome_turn() -> dict:
    """The one fixed line of the show. Written in code, not asked of the
    model, because it is the brand and must be identical every week."""
    if LANG == "he":
        return {"speaker": SPEAKER_A,
                "text": "ברוכים הבאים ל-Ben's Weekly Digest. אני דנה, ואיתי יוני."}
    return {"speaker": SPEAKER_A,
            "text": "Welcome to Ben's Weekly Digest. I'm Dana, and with me is Yoni."}


INTRO_MIN_TURNS, INTRO_MAX_TURNS = 3, 12
INTRO_MIN_WORDS, INTRO_MAX_WORDS = 60, 320
STYLE = "style: "


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
        lang_inst = ("Spoken Hebrew, as Israeli industry people talk. English terms "
                     "(UA, CPI, LTV, IAP, ROAS, web shop, webstore) and all company and product names stay in English.")
    else:
        lang_inst = "Natural spoken English."

    prompt = f"""You write the INTRO of a weekly mobile-games podcast, AFTER the
rest of the episode already exists, so that it can preview what is really in it.

HOSTS: {SPEAKER_A} (the anchor) and {SPEAKER_B} (the analyst). {SPEAKER_A} has
just said the welcome line: "{welcome['text']}"

EPISODE TITLE: {data.get('episode_title', '')}

WHAT THE EPISODE CONTAINS, in running order:
{stories}

THE MOST STRIKING FACTS (from the editorial notes):
{facts or '(none)'}

QUESTIONS THE EPISODE TAKES ON:
{questions or '(none)'}

Write the turns that come right after the welcome line:
1. {SPEAKER_B} replies in one short, natural line that reacts to ONE specific
   thing from the facts above -- a figure, a ruling, a reversal. It is NOT
   about the week as a whole: no "what a week", no "a week in which...", no
   "שבוע ..." -- a sentence that could open any episode is the wrong sentence.
2. A quick bit of easy banter, 2-3 short turns in all, between people who
   work together and like it: a tease, a small reaction, a half-joke about
   the work itself. Avoid stock lines like "only you could make sense of this
   mess". Do NOT invent personal anecdotes presented as fact, and no weather
   or holiday small talk.
3. {SPEAKER_A} then previews the episode as a TEASER, not a table of
   contents: lead with the single most interesting thing, name 3-4 stories in
   the order they will be told, include at least one concrete number from the
   facts above, and pose ONE of the questions -- one the episode actually
   answers, never one marked "left open" -- without giving its answer away.
   Split the teaser across two turns with a short interjection from
   {SPEAKER_B} if it runs long.
4. The last turn hands over to the first story with one short line that
   names it.

Tone: calm, like colleagues, not radio hosts. No hype words (avoid עצום,
מטורף, מהפכה, דרמטי, וואו, מדהים, "game changer") and no exclamations. No turn
opens with an agreement word (בדיוק, לגמרי, בהחלט, נכון, אכן). Nobody speaks
more than two turns in a row.

Total length: {INTRO_MIN_WORDS + 70}-{INTRO_MAX_WORDS - 90} words across all
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
            raw = gemini_json(attempt_prompt, INTRO_SCHEMA, tries=2, timeout=150)
        except Exception as e:                              # noqa: BLE001
            log(f"  intro attempt {attempt} failed ({type(e).__name__}: {e})")
            break
        turns, repairs = script_quality.repair_script(raw.get("turns", []))
        for note in repairs:
            log(f"  intro repair: {note}")
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


def generate_podcast_content(articles: list[dict], today_date: str, memory_context: str = "",
                              target_words: int | None = None, prep: dict | None = None) -> dict:
    lang_inst = f"Write in natural Hebrew as spoken by Israeli mobile gaming executives (use {SPEAKER_A} [Female Anchor] and {SPEAKER_B} [Male Analyst]). Keep English terms like UA, CPI, ROAS, LTV, SKAN, DTC, IAP, web shop, webstore in English (never translate them literally)." if LANG == "he" else "Write in natural spoken English."

    # target_words exists only for live_smoke.py (Tier 2): production
    # (main(), below) never passes it, so this branch never runs for a real
    # episode and the show's target length is untouched.
    if target_words:
        length_inst = f"Target Script Length: about {target_words} words. This is a short smoke-test run -- keep it brief."
        budget_words = target_words
    else:
        # Stories + outro only: the intro is written separately afterwards
        # and adds roughly 200 words, so the finished episode still lands in
        # the old 1,500-1,900 range. SCRIPT_FLOOR_WORDS is where a short
        # script earns a retry (2026-09-28 came out at 1,301 words total).
        length_inst = (f"Target Script Length: {SCRIPT_TARGET_WORDS[0]:,} to "
                       f"{SCRIPT_TARGET_WORDS[1]:,} words for the stories and outro "
                       "(the intro is recorded separately). Keep it detailed, "
                       "engaging, and professional.")
        budget_words = sum(SCRIPT_TARGET_WORDS) // 2

    # Airtime was computed by discovery.assign_airtime() and printed in the
    # ledger ("40% זמן אוויר") but never reached this prompt -- which said
    # "3-5 turns PER ARTICLE" for all of them alike, so the lead story and a
    # one-line item got the same weight. Each article now carries its share.
    def _header(i: int, a: dict) -> str:
        share = a.get("_airtime")
        note = ""
        if share:
            note = f"  [airtime: ~{int(share * 100)}% of the episode, about {int(share * budget_words)} words]"
        return f"ARTICLE {i + 1}: {a['title']}{note}"

    corpus = "\n\n".join(f"{_header(i, a)}\nURL: {a['url']}\n\n{a['text']}"
                         for i, a in enumerate(articles))

    # Continuity is opt-in in the prompt itself: the section only exists when
    # there is real history to draw on, and the instruction is explicit about
    # not forcing a callback. A memory feature that makes every episode open
    # with "as we discussed last week" would work against the calm, unforced
    # tone the show is already tuned for -- so this is additive, not a
    # rewrite of how the hosts talk.
    memory_block = ""
    if memory_context:
        # "Mention only when genuinely relevant" alone was not enough: the
        # 2026-09-15 episode had Yoni cite "Xsolla and ZBD" from a previous
        # week's summary even though that week's SOURCE ARTICLES below
        # contained no Xsolla piece at all -- the model treated a company
        # name appearing in its own memory as license to bring it back up
        # unprompted. This block is for topical continuity (the D2C shift
        # we've been tracking, the Apple/DMA saga), not a standing invite to
        # re-cite a vendor's name or pitch that today's actual reporting
        # doesn't independently raise.
        memory_block = f"""
PREVIOUS EPISODES (for natural continuity on ongoing STORYLINES -- mention
only when genuinely relevant to today's stories; never force a callback):
{memory_context}

Do not name a specific company, product, or vendor from the block above
unless a SOURCE ARTICLE below is actually about that company this week. It
is fine to say "the D2C trend we've been tracking keeps accelerating"; it
is not fine to name a vendor again this week just because it came up before.
"""

    prep_block = ""
    prep_rules = ""
    if prep:
        prep_block = (
            "\nEDITORIAL PREP -- your briefing notes, worked out BEFORE the "
            "dialogue is written. Every number and fact in it comes from the "
            "articles below:\n" + _format_prep(prep, articles) + "\n"
        )
        prep_rules = """   - Work from the EDITORIAL PREP above: use its points, in your own words.
   - A source marked COMPANY_CLAIMS or VENDOR_MARKETING is ATTRIBUTED, never
     endorsed ("according to the company", "by their own numbers"). Do not
     present its claim as established fact, and never repeat a company's
     slogan as if it were an insight. Its airtime follows the evidence, not
     the enthusiasm.
   - Raise every QUESTION TO DEBATE where its stories come up. An answered
     question is answered on air from the supplied answer, in your words. An
     OPEN question is said plainly to be open ("we don't know yet", "one to
     watch") -- never answered with something invented.
   - Respect each "careful" note: do not overstate what it warns about.
"""

    prompt = f"""You are the lead executive producer of a top-tier mobile gaming industry podcast.

Your goal is an in-depth, highly structured episode covering key developments.
{memory_block}{prep_block}
STRUCTURE OF THE SHOW:
1. NO INTRO. The welcome, a moment of host banter and a preview of the
   episode are recorded separately, AFTER this script is finished, so that
   they can preview what is actually said. Do not greet, do not introduce the
   show or the hosts, do not outline the topics. {SPEAKER_A}'s first turn
   opens the first story directly, in one natural sentence a listener who has
   just heard the preview can follow.
2. DEEP DIVE SEGMENTS, in the order of the articles, each given the airtime
   marked on it (the lead story gets the longest treatment; the last may get
   only a few sentences):
   - Say what happened and give the numbers WITH what they compare to.
   - Then a real exchange. One host raises a doubt or a question and the OTHER
     ANSWERS it with specifics in the next turn -- a question is never left
     hanging. When the hosts agree, the second must add something the first
     did not say. Where the evidence supports it, they disagree or point out
     a caveat.
   - Close each story with one concrete takeaway an operator in casual,
     puzzle, hybrid-casual or real-money skill games could check, test or
     watch.
{prep_rules}3. SHOW OUTRO: Summarize the actionable takeaway and sign off.
4. EPISODE METADATA: Generate a highly engaging, catchy episode title based on the stories covered.

CHARACTER DYNAMICS:
- {SPEAKER_A} (Dana - Female Anchor): Leads strategy, numbers, and overarching market trends, and answers {SPEAKER_B}'s questions with specifics.
- {SPEAKER_B} (Yoni - Male Analyst): Analytical, questions assumptions, probes UA/LTV realities.

SPEECH NATURALISM:
- {lang_inst}
- {length_inst}
- Calm and precise, like two colleagues -- not radio hosts. No stacked
  intensifiers or hype: avoid words like עצום, מטורף, מהפכה, מהפכני, דרמטי,
  וואו, מדהים, "game changer" and their English equivalents; at most one
  strong adjective per story, and only when the number justifies it. No
  exclamations of surprise.
- Do not begin more than two turns in the whole episode with an agreement
  word (בדיוק, לגמרי, בהחלט, נכון, אכן). Respond to what was just said by
  adding to it or questioning it.
- Never read a URL aloud; attribute by outlet name.
- Never use the ASCII double-quote character (") anywhere in the spoken text.
  Write Hebrew acronyms with the Hebrew gershayim ״ (ארה״ב, מנכ״ל, צה״ל,
  בע״מ) or spell them out, and quote a phrase with single quotes ' or leave
  it unquoted. Every turn must end with complete punctuation (. ? ! …).

SOURCE ARTICLES:
{corpus}
"""
    log("generating podcast content via Gemini text call...")
    # Up to two attempts. script_quality.py explains the bug this guards
    # against (a Hebrew acronym's ASCII quote ending the JSON string and
    # taking the rest of the sentence with it). Deterministic repair runs
    # first and usually fully fixes it for free; a retry is only spent when
    # something is still malformed afterwards, and only once, because every
    # attempt is a full-script Gemini request against a small daily quota.
    # A script under the word floor counts as a problem too (2026-09-28 came
    # out at 1,301 words against a 1,500-1,900 target), but shares the same
    # single retry rather than adding a second one.
    attempt_prompt = prompt
    for attempt in (1, 2):
        data = gemini_json(attempt_prompt, PODCAST_SCHEMA, tries=4)
        data["script"], repairs = script_quality.repair_script(data.get("script", []))
        for note in repairs:
            log(f"  script repair: {note}")
        problems = script_quality.script_problems(data["script"])
        words = script_quality.count_words(data["script"])
        short = (not target_words) and words < SCRIPT_FLOOR_WORDS
        if short:
            problems.append(
                f"the script is only {words} words; the target is "
                f"{SCRIPT_TARGET_WORDS[0]:,}-{SCRIPT_TARGET_WORDS[1]:,}"
            )
        if not problems:
            break
        for p in problems:
            log(f"  script problem (attempt {attempt}): {p}")
        if attempt == 1:
            attempt_prompt = prompt + (
                "\n\nA PREVIOUS ATTEMPT AT THIS SCRIPT WAS REJECTED because: "
                + "; ".join(problems[:3])
                + ". Regenerate the whole script with every turn ending in "
                "complete punctuation and no ASCII double quotes anywhere."
                + (" Go deeper on the highest-airtime stories -- more specifics, "
                   "and every question answered in full; do not pad." if short else "")
            )
    else:
        data["script"], closed = script_quality.close_remaining(data["script"])
        log(f"  script still had problems after a retry; closed {closed} "
            "turn(s) with a full stop rather than ship a cut-off fragment")

    total_words = script_quality.count_words(data.get("script", []))
    log(f"generated script: {len(data.get('script', []))} turns, {total_words} words")
    for line in script_quality.style_report(data.get("script", [])):
        log(f"  style: {line}")

    if total_words < 600:
        log(f"Warning: Script is short ({total_words} words), but proceeding.")

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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{TTS_MODEL}:generateContent?key={GEMINI_API_KEY}"
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

    for attempt in range(tries):
        _check_deadline()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=TTS_TIMEOUT) as r:
                data = json.loads(r.read())

            cand = (data.get("candidates") or [{}])[0]
            finish_reason = cand.get("finishReason")
            audio_bytes = None
            for part in (cand.get("content") or {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    audio_bytes = base64.b64decode(inline["data"])
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
            log(f"    TTS HTTP {e.code} (attempt {attempt + 1}/{tries})")
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
        time.sleep(min(25 * (attempt + 1), 60))

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
    lines = [f"{turn['speaker']}: {turn['text']}" for turn in script_turns]

    # Chunk size set to restrict total chunks to ~2-3
    chunks, current_chunk, current_len = [], [], 0
    for line in lines:
        if current_len + len(line) > TTS_CHUNK_CHAR_LIMIT and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk, current_len = [], 0
        current_chunk.append(line)
        current_len += len(line)
    if current_chunk:
        chunks.append("\n".join(current_chunk))

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
                raise
            mid = len(turns) // 2
            log(f"    {label} kept truncating; splitting into two and retrying each half...")
            first = synthesize_with_split("\n".join(turns[:mid]), f"{label}a")
            second = synthesize_with_split("\n".join(turns[mid:]), f"{label}b")
            return first + second

    pcm = bytearray()
    for idx, chunk_text in enumerate(chunks, 1):
        cached = checkpoint.load_chunk(date, idx) if date else None
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
            checkpoint.save_chunk(date, idx, audio_bytes)
        pcm += audio_bytes
        time.sleep(25)

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(bytes(pcm))

    jingle_m4a = ASSETS_DIR / "jingle.m4a"
    jingle_mp3 = ASSETS_DIR / "jingle.mp3"
    jingle_file = jingle_m4a if jingle_m4a.exists() else (jingle_mp3 if jingle_mp3.exists() else None)

    if jingle_file:
        log(f"found jingle asset ({jingle_file.name}), applying radio ducking & outro fade...")
        filter_complex = (
            "[0:a]afade=t=out:st=2.0:d=2.5[jingle_fade];"
            "[1:a]adelay=2200|2200[speech_delayed];"
            "[jingle_fade][speech_delayed]amix=inputs=2:weights=1 1:dropout_transition=2[intro_mixed];"
            "[intro_mixed][2:a]concat=n=2:v=0:a=1[outa]"
        )
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(jingle_file),
            "-i", str(wav_path),
            "-i", str(jingle_file),
            "-filter_complex", filter_complex,
            "-map", "[outa]",
            "-codec:a", "libmp3lame", "-b:a", "96k",
            str(mp3_path)
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-b:a", "96k",
            str(mp3_path)
        ]

    subprocess.run(cmd, check=True)
    wav_path.unlink(missing_ok=True)
    log(f"audio generated successfully: {mp3_path.name} ({mp3_path.stat().st_size / 1e6:.2f} MB)")

def get_duration(mp3_path: pathlib.Path) -> int:
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
            capture_output=True, text=True, check=True
        )
        return int(float(res.stdout.strip()))
    except Exception:
        return 300

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
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", mp3.stem)
        if not m:
            log(f"  skipping {mp3.name}: filename doesn't start with a date")
            continue
        date_str = m.group(1)
        meta_path = mp3.with_suffix(".json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        try:
            pub = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(hour=6, tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        notes = meta.get("notes") or meta.get("summary", "")
        plain = xml_escape(re.sub(r"<[^>]+>", " ", notes).strip()[:900])

        title = xml_escape(meta.get('title', f"Ben's Weekly Digest {date_str}"))

        items.append(f"""    <item>
      <title>{title}</title>
      <description><![CDATA[{notes}]]></description>
      <content:encoded><![CDATA[{notes}]]></content:encoded>
      <itunes:summary>{plain}</itunes:summary>
      <pubDate>{format_datetime(pub)}</pubDate>
      <guid isPermaLink="false">bens-digest-{mp3.stem}</guid>
      <enclosure url="{BASE_URL}/episodes/{mp3.name}" length="{mp3.stat().st_size}" type="audio/mpeg"/>
      <itunes:duration>{meta.get('duration', 0)}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Ben's Weekly Digest</title>
    <link>{BASE_URL}</link>
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
    (ROOT / "feed.xml").write_text(feed, encoding="utf-8")
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


def main() -> int:
    EPISODES.mkdir(exist_ok=True)
    DIGESTS.mkdir(exist_ok=True)
    ASSETS_DIR.mkdir(exist_ok=True)

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    done: set[str] = set(state.get("processed", []))

    # NOT "+ '-v2'": that suffix was a one-off manual fix (see CHANGELOG,
    # 2026-09-02 guid incident) for republishing an ALREADY-LIVE date under
    # a fresh RSS guid. Baking it into every run here would permanently
    # name every future episode "-v2" -- build_feed()'s regex-based date
    # parsing (also from that incident) supports any suffix or none, so a
    # plain date is exactly what a normal, first-time weekly run needs.
    today = dt.date.today().isoformat()

    # Idempotence guard, which is what makes an automatic retry safe: the
    # workflow fires a few times on Monday so a bad Gemini day heals
    # itself without anyone noticing it, and every firing after a
    # successful one has to be a cheap no-op rather than a second episode
    # for the same date. Set FORCE_REGENERATE=1 to rebuild a date on
    # purpose (as on 2026-08-31, when an episode had to be regenerated
    # after a selection bug).
    if (EPISODES / f"{today}.mp3").exists() and not os.environ.get("FORCE_REGENERATE"):
        log(f"episode for {today} already exists; nothing to do. "
            "(set FORCE_REGENERATE=1 to rebuild it deliberately)")
        return 0

    # Resume first, before spending anything. If an earlier attempt today
    # already got as far as a finished script, every Gemini *text* call for
    # this episode (scoring batches, dedupe checks, script generation) is
    # already paid for -- redoing them costs real free-tier quota for an
    # identical result, which is precisely what exhausted the daily
    # allowance on 2026-09-14/15. See checkpoint.py.
    resumed = checkpoint.load_content(today)
    if resumed:
        articles, data = resumed
        log(f"resuming from checkpoint: {len(articles)} articles, "
            f"{len(data.get('script', []))} script turns already generated")
    else:
        from discovery import select
        articles = select()
        if not articles:
            log("No articles cleared relevance threshold. Exiting clean.")
            state["last_run"] = today
            STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
            return 0

    # 1. Gemini Single-Pass Text & Script Generation -- skipped entirely
    # when resuming, since `data` came back from the checkpoint above.
    if not resumed:
        # Recent-episode context for the script prompt. Reads memory.json
        # directly (no LLM call), so this costs nothing and cannot itself
        # fail the run -- see memory.py for why.
        memory_context = memory.load_recent_context()
        # Editorial prep first (None on any failure: the script is then
        # written from the articles alone, as it was before this stage).
        prep = prepare_episode(articles, memory_context)
        data = generate_podcast_content(articles, today, memory_context, prep=prep)
        data["prep"] = prep
        # Explicitly False, so a resumed run knows the intro is still owed
        # (see below) and a legacy checkpoint without the key is never given
        # a second greeting.
        data["intro_done"] = False
        # Checkpoint the moment the script is paid for -- the intro below is
        # a separate call, and a hard kill between the two must not cost the
        # script.
        checkpoint.save_content(today, articles, data)

    # The intro is written LAST, once the episode it previews exists. Never
    # raises (it falls back to a plain welcome), so this cannot fail a run.
    if data.get("intro_done") is False:
        data = add_intro(data)
        checkpoint.save_content(today, articles, data)

    episode_title = data.get("episode_title", "Weekly Gaming Digest")
    full_title = show_title(episode_title)

    # 2. Save Digest & Script
    (DIGESTS / f"{today}.md").write_text(render_digest_md(data, articles, today, full_title), encoding="utf-8")
    script_text = "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in data["script"])
    (DIGESTS / f"{today}-script.md").write_text(script_text, encoding="utf-8")

    # 3. Audio Synthesis via Gemini TTS + Ducking Jingle Assembly
    wav_path = EPISODES / f"{today}.wav"
    mp3_path = EPISODES / f"{today}.mp3"
    synthesize_audio(data["script"], wav_path, mp3_path, date=today)
    duration = get_duration(mp3_path)

    # 4. Save Metadata & Update RSS
    first_para = data["digest_summary"][0]["key_takeaway"] if data.get("digest_summary") else "Weekly Gaming Digest"
    notes_links = "".join(f'<li><a href="{xml_escape(a["url"])}">{xml_escape(a["title"])}</a> <em>({xml_escape(a.get("source", ""))})</em></li>' for a in articles)
    notes = f"<p>{xml_escape(first_para)}</p><p><strong>Sources ({len(articles)}):</strong></p><ol>{notes_links}</ol>"

    mp3_path.with_suffix(".json").write_text(
        json.dumps({
            "title": full_title,
            "summary": first_para,
            "notes": notes,
            "duration": duration,
            "sources": [{"title": a["title"], "url": a["url"], "source": a.get("source", "")} for a in articles]
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    build_feed()

    state["processed"] = sorted(done | {a["url"] for a in articles})
    state["last_run"] = today
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    # Written only after everything above succeeded, so a failed run never
    # leaves a phantom episode in the show's memory.
    memory.append_entry(memory.build_entry(full_title, data.get("digest_summary", [])))

    # Last thing, deliberately: while any of the above can still fail, the
    # checkpoint is the thing that makes the retry cheap. Only a fully
    # published episode has earned the right to delete it.
    checkpoint.clear(today)

    log("Finished run cleanly using Gemini API.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
