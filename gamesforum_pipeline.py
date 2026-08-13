#!/usr/bin/env python3
"""
Gamesforum -> digest + podcast pipeline.

Runs headless on GitHub Actions cron. Zero infra.

Flow:
  1. scrape globalgamesforum.com /features + /news listing pages
  2. skip anything already processed (state.json)
  3. fetch full article text for the new ones
  4. Gemini writes: (a) markdown digest  (b) 2-speaker podcast script
  5. Gemini TTS multi-speaker -> chunked PCM -> single mp3
  6. write episodes/, digest/, and a private podcast feed.xml

Env vars required:
  GEMINI_API_KEY   - from aistudio.google.com/apikey
  PODCAST_BASE_URL - e.g. https://<user>.github.io/<repo>   (no trailing slash)

Optional:
  DIGEST_LANG      - "he" (default) or "en"
  TTS_MODEL        - default gemini-2.5-flash-preview-tts
                     (gemini-3.1-flash-tts-preview = better, 2x cost, flakier)
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

# ---------------------------------------------------------------- config

API_KEY = os.environ["GEMINI_API_KEY"]
BASE_URL = os.environ.get("PODCAST_BASE_URL", "").rstrip("/")
LANG = os.environ.get("DIGEST_LANG", "he")
# Off by default: the digest reports what the articles say, nothing more.
# Set to "1" to append a clearly fenced machine-commentary section.
INCLUDE_OPINION = os.environ.get("INCLUDE_OPINION", "0") == "1"
TTS_MODEL = os.environ.get("TTS_MODEL", "gemini-2.5-flash-preview-tts")
# gemini-3.5-flash sat on a much stingier free-tier daily quota than its
# generation number suggests - the 429 kept recurring even with pacing
# applied, which only makes sense as a daily cap, not a burst problem.
#
# Split by task instead of dropping the newer model everywhere: relevance
# scoring is mechanical (schema-enforced JSON, a rubric, a number) and the
# bulk of the call volume, so it runs on gemini-2.5-flash, which has real
# free-tier daily headroom. The digest and the podcast script are where
# nuance actually shows up in the output - phrasing, natural dialogue,
# metaphor quality - so those stay on the newer model, at a fraction of the
# call count (roughly 2-11 calls a run vs. the 5+ scoring batches).
TEXT_MODEL = os.environ.get("TEXT_MODEL", "gemini-3.5-flash")
SCORE_MODEL = os.environ.get("SCORE_MODEL", "gemini-2.5-flash")

SITE = "https://www.globalgamesforum.com"
LISTINGS = [f"{SITE}/features", f"{SITE}/news"]

ROOT = pathlib.Path(__file__).resolve().parent
EPISODES = ROOT / "episodes"
DIGESTS = ROOT / "digests"
STATE_FILE = ROOT / "state.json"

# Voices and delivery live in profile.toml so the tone can be tuned without
# editing code. Gemini TTS caps at 2 speakers.
def _load_voice() -> dict:
    cfg = {
        "speaker_a": "Dana", "voice_a": "Charon",
        "speaker_b": "Yoni", "voice_b": "Umbriel",
        "direction": "Two industry colleagues talking. Measured, unhurried, "
                     "genuinely interested. Not broadcast energy.",
    }
    path = pathlib.Path(__file__).resolve().parent / "profile.toml"
    if path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:
            try:
                import tomli as tomllib      # type: ignore[no-redef]
            except ModuleNotFoundError:
                return cfg
        try:
            with path.open("rb") as f:
                cfg.update(tomllib.load(f).get("voice", {}))
        except Exception:                    # noqa: BLE001
            pass
    return cfg


_VOICE = _load_voice()
SPEAKER_A, VOICE_A = _VOICE["speaker_a"], _VOICE["voice_a"]
SPEAKER_B, VOICE_B = _VOICE["speaker_b"], _VOICE["voice_b"]
DIRECTION = _VOICE["direction"].strip()

# Docs warn quality drifts past a few minutes -> synthesize in chunks.
# ~1200 chars lands around 60-90s of speech.
CHUNK_CHARS = 1200

# Selection limits now live in profile.toml, so they can be tuned without
# touching code. See discovery.py.
UA = "Mozilla/5.0 (compatible; gamesforum-digest/1.0)"

# ---------------------------------------------------------------- helpers


def log(*a):
    print("[pipeline]", *a, flush=True)


def http_get(url: str, tries: int = 3) -> str:
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            log(f"GET {url} failed ({e}); retrying")
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def strip_tags(fragment: str) -> str:
    """Crude but dependency-free HTML -> text."""
    fragment = re.sub(r"(?is)<(script|style|nav|footer|svg)\b.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</(p|div|h[1-6]|li)>", "\n", fragment)
    text = re.sub(r"(?s)<[^>]+>", " ", fragment)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- scraping


def discover_articles() -> list[dict]:
    """Pull article links off the listing pages, newest first, de-duped."""
    seen: set[str] = set()
    out: list[dict] = []
    link_re = re.compile(
        r'href="(/(?:features|news)/[a-z0-9][a-z0-9\-.]{8,})"', re.I
    )
    for listing in LISTINGS:
        try:
            page_html = http_get(listing)
        except Exception as e:  # noqa: BLE001
            log(f"skipping listing {listing}: {e}")
            continue
        for path in link_re.findall(page_html):
            url = SITE + path
            if url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "slug": path.rsplit("/", 1)[-1]})
    log(f"discovered {len(out)} article links")
    return out


def fetch_article(url: str) -> dict | None:
    try:
        page = http_get(url)
    except Exception as e:  # noqa: BLE001
        log(f"fetch failed {url}: {e}")
        return None

    m = re.search(r'meta property="og:title" content="([^"]+)"', page)
    title = html.unescape(m.group(1)) if m else url.rsplit("/", 1)[-1]

    # Body sits between the <h1> and the "You might also like" block.
    body = page
    h1 = re.search(r"(?is)<h1[^>]*>.*?</h1>", page)
    if h1:
        body = page[h1.end():]
    cut = re.search(r"(?i)you might also like|SIGN UP TO OUR NEWSLETTER", body)
    if cut:
        body = body[: cut.start()]

    text = strip_tags(body)
    if len(text) < 400:          # listing stub or paywalled - not worth tokens
        return None
    return {"url": url, "title": title, "text": text[:14000]}


# ---------------------------------------------------------------- gemini


class Truncated(RuntimeError):
    """The model ran out of output budget mid-answer."""


def gemini_json(prompt: str, schema: dict, model: str | None = None) -> list | dict:
    """Ask for JSON and have the API enforce the shape.

    Free-text prompting for JSON fails in the field: the model will happily
    answer in prose, or echo whatever bracket notation appears in the prompt.
    responseSchema makes malformed output an API-level impossibility rather
    than something to regex around afterwards.

    model defaults to TEXT_MODEL but callers doing high-volume mechanical
    work (relevance scoring) pass SCORE_MODEL instead, to keep that call
    volume off the model whose free-tier quota actually matters for quality.
    """
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model or TEXT_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            # A truncated JSON array is a parse error rather than a silent
            # short answer, but it still costs the whole batch, so give it
            # room and cap thinking the same way gemini_text does.
            "maxOutputTokens": 16384,
            "thinkingConfig": {"thinkingBudget": 1024},
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    data = _post_json(url, payload)
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts).strip()
    if cand.get("finishReason") == "MAX_TOKENS":
        raise Truncated("scoring batch hit the token ceiling")
    return json.loads(raw)


def gemini_text(prompt: str, max_tokens: int = 16384,
                thinking: int = 2048, temperature: float = 0.4) -> str:
    """Generate text, and refuse to return a silently truncated answer.

    This function used to just concatenate the text parts and return. That is
    how a 57-second episode shipped: three separate things can shorten the
    output with no error raised anywhere.

      * finishReason MAX_TOKENS. The answer stops mid-sentence and looks like
        a complete-but-short script to every downstream check.
      * Thinking tokens. On 2.5-and-later Flash the model reasons before it
        answers, and that reasoning is billed against the SAME
        maxOutputTokens ceiling. Hebrew costs roughly two to three tokens a
        word, so an unbounded thinking budget against the old 8192 ceiling
        could leave almost nothing for the transcript. Capping thinking
        explicitly is what reserves room for the actual answer.
      * An empty candidate. Returns "" and every caller treats it as a very
        short script rather than a failure.

    Raises Truncated on the first two so the caller can retry with more room,
    rather than passing a fragment down the pipeline.
    """
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{TEXT_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingBudget": thinking},
        },
    }
    data = _post_json(url, payload)
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()

    usage = data.get("usageMetadata", {})
    thought = usage.get("thoughtsTokenCount", 0)
    out = usage.get("candidatesTokenCount", 0)
    reason = cand.get("finishReason", "")
    log(f"    [{out} output tokens, {thought} thinking, finish={reason or '?'}]")

    if reason == "MAX_TOKENS":
        raise Truncated(
            f"hit the {max_tokens}-token ceiling "
            f"({thought} of it spent thinking); answer is a fragment"
        )
    if not text:
        raise Truncated(f"empty response (finishReason={reason or '?'})")
    return text


def gemini_text_retry(prompt: str, label: str = "") -> str:
    """gemini_text with escalating room, because truncation is recoverable.

    A fragment is worthless, but the same prompt with a bigger ceiling and a
    smaller thinking allowance usually completes. Only after both attempts
    fail does this give up, and then it says so rather than returning junk.
    """
    for max_tokens, thinking in ((16384, 2048), (32768, 512)):
        try:
            return gemini_text(prompt, max_tokens=max_tokens, thinking=thinking)
        except Truncated as e:
            log(f"  {label} truncated ({e}); retrying with more room")
    raise Truncated(f"{label}: could not get a complete answer")


def gemini_tts_chunk(script_chunk: str) -> bytes:
    """One multi-speaker synthesis call -> raw 24kHz s16le mono PCM.

    The docs flag two real failure modes we guard against here:
      * random 500s where the model emits text tokens instead of audio
      * vague prompts causing the model to READ THE DIRECTIONS ALOUD,
        so we use an explicit preamble + a hard TRANSCRIPT marker.
    """
    # Structured the way Google's own prompting guide recommends: audio
    # profile, scene, director's notes, then a hard transcript marker. A
    # single vague line like "professional and conversational" is what
    # produces either flat recitation or unearned excitement.
    prompt = f"""Synthesize the following conversation as speech.
Do not read any of these instructions aloud.

# AUDIO PROFILE
{SPEAKER_A}: carries the material. She has read the sources and reports what
they say. Steady, unshowy, the authority of someone with nothing to prove.
{SPEAKER_B}: the one who asks. He questions, restates, pushes back. Curious
rather than impressed.

# THE SCENE
Two colleagues in the mobile games industry, mid-conversation about the
week's news. No microphone in the room, no audience being performed for.

# DIRECTOR'S NOTES
{DIRECTION}

Delivery specifics:
- Statements land on a falling tone. Do not lift the end of every sentence.
- Interest is conveyed by attention, not by volume or pitch.
- Surprise is earned, not decorative. At most one genuine moment of it in
  the whole piece, and only where the text clearly warrants it.
- Do not stress every figure. Most numbers are said plainly, in passing.
- Natural breaths. A brief pause before a significant point is welcome.
- Avoid the bright "vocal smile" of morning radio entirely.
- Equally, avoid flat monotone recitation. The register sits between those
  two: engaged, level, human.

Read only the lines after the TRANSCRIPT marker.

TRANSCRIPT:
{script_chunk}"""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{TTS_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": [
                        {
                            "speaker": SPEAKER_A,
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": VOICE_A}
                            },
                        },
                        {
                            "speaker": SPEAKER_B,
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": VOICE_B}
                            },
                        },
                    ]
                }
            },
        },
    }
    # Two distinct failure modes, both documented:
    #   * HTTP 500 -> handled inside _post_json's retry loop
    #   * HTTP 200 with text parts instead of audio -> retried here
    for attempt in range(4):
        data = _post_json(url, payload, tries=4)
        cand = (data.get("candidates") or [{}])[0]
        for part in (cand.get("content") or {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
        reason = cand.get("finishReason", "?")
        if reason == "PROHIBITED_CONTENT":
            raise RuntimeError(
                "TTS classifier rejected the prompt (PROHIBITED_CONTENT). "
                "Strengthen the 'Synthesize the following...' preamble."
            )
        log(f"no audio in response (finishReason={reason}); retrying")
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("TTS returned no audio payload after 4 attempts")


# Free-tier Gemini quota is a per-minute request count, typically 10-15 RPM
# depending on model. A run makes many calls back to back (scoring batches,
# digest, three script passes, each possibly retried) with no natural
# spacing between them, so it can burst past that ceiling in well under a
# minute even though the total for the whole run is modest. Two separate
# guards: pace every call so we do not burst, and on an actual 429 wait a
# full quota window rather than a short exponential backoff.
_MIN_CALL_INTERVAL = float(os.environ.get("MIN_CALL_INTERVAL_SEC", "4.5"))
_last_call_at = [0.0]


def _pace() -> None:
    now = time.monotonic()
    wait = _MIN_CALL_INTERVAL - (now - _last_call_at[0])
    if wait > 0:
        time.sleep(wait)
    _last_call_at[0] = time.monotonic()


def _post_json(url: str, payload: dict, tries: int = 5) -> dict:
    body = json.dumps(payload).encode()
    for attempt in range(tries):
        _pace()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": API_KEY,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            retryable = e.code in (429, 500, 502, 503, 504)
            log(f"API {e.code} (attempt {attempt + 1}/{tries}): {detail}")
            if not retryable or attempt == tries - 1:
                raise
            # A 429 on the free tier means the per-minute bucket is empty,
            # not that anything is wrong. A ~65s wait crosses a full window;
            # a 500/502/503 gets the usual short exponential backoff.
            time.sleep(65 if e.code == 429 else min(30, 4 * 2 ** attempt))
        except Exception as e:  # noqa: BLE001
            log(f"API error (attempt {attempt + 1}/{tries}): {e}")
            if attempt == tries - 1:
                raise
            time.sleep(4 * 2 ** attempt)
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- prompts


def build_digest_prompt(articles: list[dict]) -> str:
    """Faithful condensation. The model is an editor, not an analyst.

    Two things are deliberately separated here:
      * SELECTION  - which articles are worth the listener's time. This is
                     the only editorial judgement the model is allowed.
      * CONTENT    - once selected, the article's own claims and opinions are
                     reported as the article's, never re-framed as fact and
                     never supplemented with the model's own view.
    """
    lang_line = "Write in Hebrew." if LANG == "he" else "Write in English."
    corpus = "\n\n".join(
        f"### {a['title']}\n{a['url']}\n\n{a['text']}" for a in articles
    )
    opinion_block = (
        """
At the very end, add a section headed exactly "## פרשנות מכונה (לא מהמקור)"
containing your own read. Everything above that heading must remain free of it.
"""
        if INCLUDE_OPINION
        else """
Do NOT add any analysis, prediction, recommendation, or opinion of your own,
anywhere, in any form. Not even one sentence. If you find yourself writing
"this means", "worth testing", "the takeaway is", or "expect", delete it.
"""
    )
    return f"""You are a faithful editor condensing industry articles for a
mobile-games company owner who runs UA, monetization, LiveOps and product.
He is a seasoned insider, so do not explain basic terms.

Your job is to compress, not to interpret. He wants what the articles say,
not what you think about it.

{lang_line} Do not use em dashes.

SELECTION (the only judgement you may exercise):
- Include the items with substantive industry content: data, deals, results,
  named-expert argument, market shifts.
- Exclude pure promotion: conference marketing, speaker line-ups, ticket
  offers, "meet us at" posts. If an article is only an event ad, drop it and
  say so in one line under a "## הושמט" heading at the end.

FIDELITY RULES (non-negotiable):
- Every number, percentage, currency figure and date must appear EXACTLY as
  it does in the source. Never round, convert, average, or recompute.
  If the source says 8.7%, you write 8.7%. Keep digits, not words.
- Attribute every claim to its source: "לפי מחקר של ZBD", "זורייה אמר",
  "לפי הכתבה". Never state a sourced claim as bare fact.
- The articles contain opinions and arguments. Keep them, clearly marked as
  the author's or the quoted person's, for example "לטענת הכתבה" or
  "לדעתו של X". Do not adopt them and do not rebut them.
- Where a quote is notable or contested, reproduce it verbatim in quotation
  marks with the speaker's name and role.
- If two sources disagree, present both positions. Do not resolve them.
- If something in the source is vague or unsupported, keep it vague. Do not
  sharpen it into a firmer claim than the source made.
- Never introduce a fact, company, figure or causal link not present in the
  sources below.
{opinion_block}
FORMAT:
- Markdown. One "##" section per included item, titled with the topic.
- Bullets over paragraphs. Under 950 words before the sources list. Do not
  pad to reach that ceiling; a thin week is allowed to produce a short digest.
  This is raw material for a spoken episode downstream, so include enough
  detail per item (the specific numbers, who said what, the mechanism) that a
  script can be built from it without inventing anything.

SOURCE ARTICLES:
{corpus}
"""


_FIDELITY = """FIDELITY (these override every craft instruction below):
- Introduce NO fact, company, figure, causal link or prediction that is not
  in the digest.
- Figures keep their exact value. You may write them as words for speech,
  but eighty-seven tenths stays eighty-seven tenths.
- Attribution stays audible: "לפי המחקר של ZBD", "זורייה מ-GAMEE אמר".
  The listener must always know whose claim it is.
- Claims from the articles are relayed ("הכתבה טוענת ש..."), never asserted
  by the hosts as their own conclusion.
- No advice, no "what you should do", no forecast, no closing recommendation.

METAPHOR RULE (read this twice):
A metaphor may only ILLUMINATE a mechanism the digest already describes.
It may never ADD a claim.
  Allowed:  the digest says 97% of paid users are gone within a month, so a
            host says "אתה ממלא דלי מנוקב" - that is the same fact, made
            concrete.
  Banned:   "וזה אומר שהמודל הזה עומד לקרוס" - that is a prediction wearing
            a metaphor's clothes.
If a metaphor would survive deletion of the underlying fact, it is smuggling
an argument. Cut it."""


_LANG_LINE = (
    """Write the dialogue in natural spoken Hebrew, the way two Israeli
industry people actually talk. Not literary Hebrew, not translated English.
Keep English industry terms in English, because that is how the conversation
really sounds: retention, LiveOps, churn, UA, hybrid monetization. Do not
transliterate them into Hebrew letters."""
    if LANG == "he"
    else "Write the dialogue in natural spoken English."
)


def _hosts() -> str:
    return f"""THE TWO HOSTS ARE NOT SYMMETRIC. This is the most important
craft note.
- {SPEAKER_A} carries the material. She has read the sources and lays out
  what they say.
- {SPEAKER_B} is the listener's proxy. He interrupts, asks the obvious
  question, restates things in plainer words, and raises the objection.
  He is not a yes-man and he is not an idiot. He is the smart colleague who
  says "רגע, אבל זה לא סותר את מה שאמרת קודם?"

Two people alternating facts is not a conversation. It is a list with
costumes. If any exchange would survive being reassigned to the other
speaker, it is not really dialogue."""


def _craft() -> str:
    return f"""CRAFT:
- VARY THE RESPONSE. A conversation where every figure is met with
  astonishment is exhausting and fake. Rotate deliberately through:
    * plain acknowledgement, then move on ("כן, זה בערך מה שציפיתי")
    * a follow-up question about method ("על איזה מדגם זה מבוסס?")
    * scepticism ("זה נשמע גבוה. הם מודדים את זה איך?")
    * a connection to something earlier
    * simply continuing, with no reaction at all
    * and rarely, genuine surprise
  Most numbers should pass without ceremony.
- No more than two question marks in any three consecutive turns.
- Restate the hard parts. After a dense claim, {SPEAKER_B} says it back in
  simpler words. This is the main comprehension device in audio, where the
  listener cannot re-read. Restating is not the same as reacting.
- Give them a stake. They can find something interesting, tedious, or
  overdue without asserting any new fact. "זה כבר שנתיים חוזר על עצמו" is
  attitude, not a claim.
- Vary the rhythm hard. A three-word line after a long one is what makes
  speech sound alive. If every turn is a similar length, it sounds generated.
- Concrete beats abstract. Name the company, the number, the mechanism.
- Light spoken texture only: "תראה", "רגע", "כן, אבל". A little goes far.

BANNED, these are the tells:
- "בואו נצלול", "נשמע מרתק", "זו נקודה מצוינת"
- Any host complimenting the other's question
- Both hosts agreeing three times in a row
- Starting consecutive turns with the same word

FORMAT:
- Exactly two speakers, labelled "{SPEAKER_A}:" and "{SPEAKER_B}:" at the
  start of each line. Never a third speaker. Never bold or markdown on the
  label, just the bare name and a colon.
- Numbers spelled out as words. No digits, no percent signs, no currency
  symbols, they read badly aloud.
- Dialogue lines only. No headings, no stage directions, no commentary
  before or after. Do not restate these instructions."""


# The whole episode, in words. Everything downstream is derived from this,
# so an episode's length is now arithmetic rather than something we ask for
# politely and hope to receive.
TARGET_WORDS = 1000
COLD_OPEN_WORDS = 80
CLOSE_WORDS = 55


PLAN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "through_line": {
            "type": "STRING",
            "description": "One sentence naming the tension connecting these "
                           "items. Not a topic but a tension. If they truly "
                           "do not connect, say so plainly.",
        },
        "orientation": {
            "type": "STRING",
            "description": "One short sentence naming what this week's "
                           "episode covers, so a listener knows where they "
                           "are. Plain and factual, not a sales pitch.",
        },
        "cold_open": {
            "type": "STRING",
            "description": "The single most arresting concrete detail in the "
                           "digest: a number, a reversal, a named company "
                           "doing a specific thing.",
        },
        "segments": {
            "type": "ARRAY",
            "description": "One per story, in the order given, same count.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "claim": {"type": "STRING",
                              "description": "The claim in one sentence."},
                    "figures": {"type": "STRING",
                                "description": "The hard numbers that carry "
                                               "it, exactly as in the digest."},
                    "attribution": {"type": "STRING",
                                    "description": "Whose claim it is."},
                    "image": {"type": "STRING",
                              "description": "ONE concrete image drawn strictly "
                                             "from the mechanism described. "
                                             "'none' if nothing honest fits."},
                    "objection": {"type": "STRING",
                                  "description": "The obvious objection a "
                                                 "smart listener would raise."},
                    "handoff": {"type": "STRING",
                                "description": "How this connects to the next "
                                               "story. 'Second thing' is not a "
                                               "handoff."},
                },
                "propertyOrdering": ["title", "claim", "figures",
                                     "attribution", "image", "objection",
                                     "handoff"],
                "required": ["title", "claim", "figures", "attribution",
                             "objection"],
            },
        },
        "close": {
            "type": "STRING",
            "description": "The one line worth remembering. A restatement of "
                           "the sharpest fact, not a lesson and not advice.",
        },
    },
    "propertyOrdering": ["through_line", "orientation", "cold_open",
                         "segments", "close"],
    "required": ["through_line", "orientation", "cold_open", "segments",
                 "close"],
}


def build_plan_prompt(digest: str, budget: str) -> str:
    """Plan the arc before writing a word.

    Returned as schema-enforced JSON rather than prose, because the plan is
    now consumed by code: each segment drives its own generation call with
    its own word budget. A free-text outline cannot be split up reliably.
    """
    return f"""You are the producer of a mobile-gaming industry podcast.
Plan one episode from the digest below. Do NOT write dialogue yet.

Produce exactly one segment per story listed in the airtime budget, in the
same order. Do not merge stories and do not add any.

AIRTIME BUDGET, decided upstream by relevance scoring:
{budget or "(not supplied; weight by importance)"}

{_FIDELITY}

DIGEST:
{digest}
"""


def build_cold_open_prompt(plan: dict, digest: str, words: int) -> str:
    return f"""Write the OPENING of a mobile-gaming industry podcast episode.

{_LANG_LINE}

{_hosts()}

Structure, in this order and nothing else:
1. {SPEAKER_A} opens with one short orienting line: what this week's episode
   is about. Concretely, not "welcome to the show". Something like naming the
   two or three things on the table this week. The listener has just pressed
   play and needs to know where they are. ONE line only.
2. Straight into the most arresting concrete detail below. No preamble, no
   "let's start with", no agenda-reading.

This is a subscription podcast, so a listener arriving cold must not feel
they have walked in on the middle of a conversation. But nor do they want a
radio host. One orienting line, then the substance.

ORIENTATION: {plan.get('orientation', '')}
THROUGH-LINE: {plan.get('through_line', '')}
COLD OPEN DETAIL: {plan.get('cold_open', '')}

Write about {words} words. That is roughly {max(3, words // 22)} to
{max(4, words // 16)} turns of dialogue.

{_craft()}

{_FIDELITY}

DIGEST (source of truth for every fact):
{digest}
"""


def build_segment_prompt(seg: dict, digest: str, words: int,
                         previous_tail: str, is_last: bool) -> str:
    """One story, one call, one explicit word budget.

    The whole reason this function exists: asking for a 1000-word episode in
    a single call produced 130 words repeatedly, and no amount of insisting
    in the prompt changed that. A 250-word target for one story is a request
    a model actually honours, and five of them add up deterministically.
    """
    handoff = (
        "End on the natural close of this topic. Another segment follows, so "
        f"do not wrap up the episode. Aim toward: {seg.get('handoff', '')}"
        if not is_last
        else "This is the last story before the close. End on its sharpest "
             "point, do not summarise the episode."
    )
    image = seg.get("image", "").strip()
    image_line = (
        f"ONE concrete image is available if it fits naturally: {image}\n"
        "Use it only if it illuminates the mechanism. A forced metaphor is "
        "worse than none."
        if image and image.lower() != "none"
        else "No metaphor for this segment. Stay literal."
    )
    return f"""Write ONE segment of an ongoing podcast conversation. The
hosts are already mid-episode, so do not greet, do not introduce yourselves,
and do not announce the topic as though starting fresh.

{_LANG_LINE}

{_hosts()}

WHAT PRECEDED THIS (continue naturally from it, do not repeat it):
{previous_tail or "(this is the first story)"}

THIS SEGMENT:
- CLAIM: {seg.get('claim', '')}
- FIGURES (exact, do not alter): {seg.get('figures', '')}
- WHOSE CLAIM: {seg.get('attribution', '')}
- THE OBJECTION {SPEAKER_B} SHOULD RAISE: {seg.get('objection', '')}
{image_line}

{handoff}

LENGTH: write {words} words of dialogue, plus or minus fifteen percent.
This is a hard requirement, not a suggestion. Count as you go. That is
roughly {max(4, words // 22)} to {max(5, words // 15)} turns. If you reach
the end of the material before the word count, that means you have not yet
had {SPEAKER_B} restate the claim in plainer words, or press on method, or
raise the objection properly, or let {SPEAKER_A} give the concrete detail
behind the headline figure. Do all of those. Do NOT invent facts to fill
space; extend the conversation around the facts you were given.

{_craft()}

{_FIDELITY}

DIGEST (source of truth for every fact):
{digest}
"""


def build_close_prompt(plan: dict, digest: str, words: int,
                       previous_tail: str) -> str:
    return f"""Write the CLOSING exchange of a podcast episode already in
progress.

{_LANG_LINE}

{_hosts()}

WHAT PRECEDED THIS:
{previous_tail}

THE LINE WORTH REMEMBERING: {plan.get('close', '')}

Rules:
- Do NOT summarise what was discussed. The listener just heard it.
- Do NOT give advice, a forecast, or a recommendation.
- Do NOT sign off with pleasantries, thanks, or "see you next week".
- End on the sharpest fact, restated once, and stop. An abrupt ending is
  better than a polite one.

Write about {words} words. Three or four turns.

{_craft()}

{_FIDELITY}

DIGEST (source of truth for every fact):
{digest}
"""


def build_seam_prompt(script: str, digest: str) -> str:
    """Continuity only. This pass is explicitly forbidden from cutting.

    The previous architecture had a 'polish' pass that was allowed to edit
    freely, and it repeatedly gutted the script. Here the only permitted
    edits are at the joins between independently generated segments.
    """
    return f"""This podcast script was assembled from separately written
segments, so the joins between them can read abruptly or repeat a setup.
Fix ONLY the joins. Return the complete script, nothing else.

Permitted edits:
- Smooth the transition where one topic ends and the next begins.
- Remove a duplicated introduction of the same company or figure.
- Fix a place where a host re-explains something already said.
- Remove any greeting that appears anywhere except the very first line.
- Fix consecutive turns that open on the same word.

FORBIDDEN:
- Do NOT shorten the script. It must come back the same length or longer.
  Returning a condensed version is a failure of this task.
- Do NOT delete whole exchanges.
- Do NOT change any figure, name, or attribution.
- Do NOT add a summary or a sign-off.

Return every line, in the same "Speaker: text" format.

{_FIDELITY}

DIGEST:
{digest}

SCRIPT:
{script}
"""


# The free-editing "polish" pass that used to live here has been removed.
# It was the proximate cause of the 68-second episode: given licence to
# tighten, it condensed a full script into a fragment, and the guards around
# it were always one step behind. Segments are now written to length in the
# first place, and the only post-pass permitted (build_seam_prompt) is
# explicitly forbidden from cutting.


# ---------------------------------------------------------------- fidelity


NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
LIST_MARKER_RE = re.compile(r"^[ \t]*\d+[.)](?=\s)", re.M)


def _numbers(text: str, strip_list_markers: bool = False) -> list[str]:
    """Every numeric token, normalised so 1,000 and 1000 compare equal.

    strip_list_markers drops "1." / "2)" ordinals at line starts, which the
    model generates itself and which must not be mistaken for claims.
    """
    if strip_list_markers:
        text = LIST_MARKER_RE.sub("", text)
    return [m.group(0).replace(",", "") for m in NUM_RE.finditer(text)]


def verify_digest(digest: str, articles: list[dict]) -> list[str]:
    """Deterministic check that the digest invented no figures.

    This is the highest-value guard available, because silent number drift is
    the single most damaging failure mode: a plausible-sounding wrong figure
    is worse than no digest at all. Every numeric token in the digest must
    appear somewhere in the source corpus.

    Deliberately NOT an LLM check. A model grading its own output shares its
    blind spots. This is string matching, so it cannot be talked around.

    KNOWN LIMIT, stated plainly: this catches INVENTED numbers, not MISPLACED
    ones. If the corpus contains both 29 and 30, and the model attaches 30 to
    the metric that was really 29, every token still resolves and nothing is
    flagged. Attribution errors of that kind need a human, or a source-grounded
    reader. Do not read a clean pass as "the digest is correct"; read it as
    "no figure was conjured from nothing".

    Returns a list of human-readable warnings (empty means clean).
    """
    corpus_nums = set()
    for a in articles:
        corpus_nums.update(_numbers(a["text"]))
        corpus_nums.update(_numbers(a["title"]))

    # Only years are allowed through unmatched. Small integers are NOT
    # whitelisted: "9%" standing in for a source's 8.7% is exactly the
    # rounding drift this function exists to catch.
    allowed = {str(y) for y in range(2015, 2036)}

    warnings = []
    for tok in dict.fromkeys(_numbers(digest, strip_list_markers=True)):
        if tok in corpus_nums or tok in allowed:
            continue
        # 8.7 is fine if the source wrote 8.70, and vice versa.
        try:
            if any(abs(float(tok) - float(c)) < 1e-9 for c in corpus_nums):
                continue
        except ValueError:
            pass
        warnings.append(f"figure '{tok}' does not appear in any source article")
    return warnings


def verify_script(script: str, digest: str) -> list[str]:
    """Catch digit-strings in the script that the digest never contained.

    Weaker than verify_digest by design: the script spells numbers out as
    words for the voice model, and matching Hebrew number words against
    digits is not something worth doing with a regex. So this catches leaked
    digits only. Treat it as a smoke alarm, not a proof.
    """
    digest_nums = set(_numbers(digest))
    warnings = []
    for tok in dict.fromkeys(_numbers(script, strip_list_markers=True)):
        if tok in digest_nums or tok in {str(y) for y in range(2015, 2036)}:
            continue
        warnings.append(f"script figure '{tok}' is not in the digest")
    return warnings


# ---------------------------------------------------------------- craft


BANNED_PHRASES = [
    "בואו נצלול", "שאלה מצוינת", "נקודה מצוינת", "בפרק היום",
    "ברוכים הבאים", "להתראות בפרק", "לסיכום", "אז לסיכום",
    "let's dive in", "great question", "welcome back", "in today's episode",
    "to sum up", "that's all for today",
]


INTERROGATIVE = (
    "איך", "למה", "מדוע", "מה ", "מהו", "מי ", "כמה", "האם", "מתי", "איפה",
    "על איזה", "איזה", "באיזה", "ומה", "ואיך", "ולמה", "אז מה", "לפי מה",
    "how", "why", "what", "which", "who", "when", "where", "does", "did",
    "is that", "are they", "based on",
)


def _is_probe(text: str) -> bool:
    """Does this turn interrogate the material?

    A question mark is sufficient but not necessary. Spoken Hebrew regularly
    drops it, and scepticism often arrives as a flat statement.
    """
    if "?" in text:
        return True
    low = text.lower().lstrip("[]abcdefghijklmnopqrstuvwxyz ").strip()
    stripped = re.sub(r"^\[[^\]]*\]\s*", "", text).strip()
    return any(stripped.startswith(w) or f" {w}" in f" {low}"
               for w in INTERROGATIVE)


def parse_turns(script: str) -> list[tuple[str, str]]:
    """Split "Speaker: text" lines into turns.

    Tolerant of the decorations models add unbidden: **Dana:**, - Dana:,
    ## Dana:. A strict matcher here is dangerous rather than merely fussy,
    because every length check in this file is computed from parse_turns.
    If it silently matches nothing, a full script measures as zero words and
    the retry logic concludes the model produced nothing.
    """
    turns: list[tuple[str, str]] = []
    # The trailing [\s*_]* matters as much as the leading one: "**Dana:**"
    # closes its bold AFTER the colon, so without it the markers land at the
    # front of the captured text and inflate every word count.
    pattern = re.compile(
        rf"^[\s>#*_-]*({re.escape(SPEAKER_A)}|{re.escape(SPEAKER_B)})"
        rf"[\s*_]*:[\s*_]*(.+)$"
    )
    for line in script.splitlines():
        line = line.strip()
        if not line:
            continue
        m = pattern.match(line)
        if m:
            turns.append((m.group(1), m.group(2).strip()))
        elif turns:
            turns[-1] = (turns[-1][0], turns[-1][1] + " " + line)
    return turns


def word_count(script: str) -> int:
    return sum(len(t.split()) for _, t in parse_turns(script))


def lint_script(script: str) -> list[str]:
    """Catch the structural tells of generated dialogue.

    Prompts ask for these; linting proves them. Everything here is a pattern
    a model reliably slips back into once it is deep in a long generation.
    """
    issues: list[str] = []
    turns = parse_turns(script)
    if not turns:
        return ["no speaker-labelled turns found"]

    words = [len(t.split()) for _, t in turns]
    total = sum(words)

    if total < 700:
        issues.append(f"only {total} words, thin for a 5-6 minute episode")
    if total > 1250:
        issues.append(f"{total} words, will overrun and quality drifts long")

    longest = max(words)
    if longest > 55:
        i = words.index(longest)
        issues.append(
            f"monologue: {turns[i][0]} runs {longest} words "
            f"(\"{turns[i][1][:45]}...\")"
        )

    # Burstiness. Uniform turn length is the clearest generated-text signature.
    mean = total / len(words)
    var = sum((w - mean) ** 2 for w in words) / len(words)
    if (var ** 0.5) / mean < 0.45:
        issues.append(
            f"turns too uniform (mean {mean:.0f}w, sd/mean "
            f"{(var ** 0.5) / mean:.2f}); needs short reactions between "
            "long explanations"
        )

    # Speaker balance. A 50/50 split is unnatural, but so is a lecture.
    a_words = sum(w for (s, _), w in zip(turns, words) if s == SPEAKER_A)
    share = a_words / total if total else 0
    if not 0.4 <= share <= 0.75:
        issues.append(
            f"{SPEAKER_A} speaks {share:.0%} of the words; one host has "
            "become an audience"
        )

    # The listener-proxy must actually interrogate the material. Counting "?"
    # alone undercounts badly: spoken Hebrew often drops the question mark, so
    # "על איזה מדגם זה מבוסס" reads as a statement to a naive check while
    # doing exactly the probing work we want.
    b_turns = [t for s, t in turns if s == SPEAKER_B]
    probes = sum(1 for t in b_turns if _is_probe(t))
    if b_turns and probes < 3:
        issues.append(
            f"{SPEAKER_B} probes only {probes} time(s); he is the "
            "listener's proxy and should be interrogating the material"
        )

    low = script.lower()
    for phrase in BANNED_PHRASES:
        if phrase.lower() in low:
            issues.append(f"banned filler present: '{phrase}'")

    # Consecutive turns opening on the same word read as a stuck loop.
    for i in range(1, len(turns)):
        w0 = turns[i - 1][1].split()
        w1 = turns[i][1].split()
        if w0 and w1 and w0[0].strip(",.!?[]") == w1[0].strip(",.!?[]"):
            issues.append(f"turns {i} and {i + 1} both open on '{w1[0]}'")
            break

    if re.search(r"\d", script):
        issues.append("digits present; spell numbers out or the voice stumbles")

    tags = re.findall(r"\[([a-z ]+)\]", script)
    if len(tags) > 3:
        issues.append(f"{len(tags)} audio tags, theatrical; three is the cap")
    banned_tags = {"surprised", "amazed", "excited", "gasp", "shouting",
                   "panicked", "trembling"}
    hit = banned_tags & {t.strip() for t in tags}
    if hit:
        issues.append(f"excitable audio tags {sorted(hit)}; these are what "
                      "make it sound artificial")

    # The tell he actually complained about: everything met with astonishment.
    excl = script.count("!")
    if excl > 2:
        issues.append(f"{excl} exclamation marks; the delivery will sound "
                      "permanently startled")
    short_reactions = sum(
        1 for _, t in turns
        if len(t.split()) <= 4 and ("?" in t or "!" in t)
    )
    if short_reactions > len(turns) * 0.25:
        issues.append(
            f"{short_reactions}/{len(turns)} turns are clipped exclamations "
            "or one-line questions; vary the responses"
        )
    q_total = sum(1 for _, t in turns if "?" in t)
    if turns and q_total >= len(turns) * 0.45:
        issues.append(
            f"{q_total}/{len(turns)} turns contain a question; colleagues "
            "make statements too"
        )

    return issues


def _issue_kind(msg: str) -> str:
    """Bucket a lint message so two runs can be compared by problem class."""
    for key in ("thin", "will overrun", "monologue", "uniform", "audience",
                "listener's proxy", "banned", "both open", "digits",
                "audio tags", "excitable", "exclamation", "clipped",
                "contain a question"):
        if key in msg:
            return key
    return "other"


MIN_EPISODE_WORDS = 820          # ~5 minutes spoken; target band is 900-1100


def _tail(script: str, turns: int = 4) -> str:
    """The last few turns, as context for the next segment's generation."""
    return "\n".join(f"{s}: {t}" for s, t in parse_turns(script)[-turns:])


def _write_part(prompt: str, target: int, label: str) -> str:
    """Generate one part and hold it to its own word budget.

    Retrying a 250-word segment is cheap and nearly always converges. That
    is the whole argument for this architecture over one-shot generation:
    when the unit of failure is one segment rather than the episode, a
    failure costs one call and is individually recoverable.
    """
    floor = int(target * 0.7)
    best, best_words = "", -1
    for attempt in (1, 2):
        try:
            part = gemini_text_retry(prompt, label=label)
        except Truncated as e:
            log(f"    {label}: {e}")
            continue
        words = word_count(part)
        log(f"    {label}: {words} words (target {target})")
        if words > best_words:
            best, best_words = part, words
        if words >= floor:
            return part
        log(f"    {label}: under floor {floor}; retrying")
    return best


def write_episode(digest: str, articles: list[dict], budget: str = "") -> str:
    """Build the episode a segment at a time, then smooth the joins.

    The previous design asked one call for a whole 1000-word episode and
    checked afterwards whether it had complied. It repeatedly did not, and
    no amount of stronger wording in the prompt changed that: run #4 came
    back at roughly 130 words. Insisting harder was never going to work,
    because the failure was structural.

    Here the episode's length is arithmetic. Each story gets its own call
    with its own word target derived from the airtime it was allotted
    upstream, and the targets sum to TARGET_WORDS by construction. A short
    segment is visible immediately, costs one cheap retry, and cannot drag
    the rest of the episode down with it.
    """
    log("  plan")
    try:
        plan = gemini_json(build_plan_prompt(digest, budget), PLAN_SCHEMA)
    except Exception as e:                                  # noqa: BLE001
        log(f"  planning failed ({e}); falling back to a flat plan")
        plan = {}
    if not isinstance(plan, dict):
        plan = {}

    segments = plan.get("segments") or []
    # The plan is asked for one segment per article in order, but a model can
    # still merge or drop. Airtime comes from the articles, which are the
    # authority, so pad or trim the plan to match rather than trusting it.
    if len(segments) != len(articles):
        log(f"  plan returned {len(segments)} segments for "
            f"{len(articles)} articles; reconciling")
    shares = [a.get("_airtime") or (1 / max(len(articles), 1))
              for a in articles]
    while len(segments) < len(articles):
        i = len(segments)
        segments.append({"title": articles[i]["title"], "claim": "",
                         "figures": "", "attribution": "", "objection": ""})
    segments = segments[:len(articles)]

    body_words = TARGET_WORDS - COLD_OPEN_WORDS - CLOSE_WORDS
    total_share = sum(shares) or 1.0
    targets = [max(90, int(body_words * s / total_share)) for s in shares]
    log(f"  word targets: open {COLD_OPEN_WORDS}, "
        f"segments {targets}, close {CLOSE_WORDS}")

    parts: list[str] = []

    log("  cold open")
    parts.append(_write_part(
        build_cold_open_prompt(plan, digest, COLD_OPEN_WORDS),
        COLD_OPEN_WORDS, "open"))

    for i, (seg, target) in enumerate(zip(segments, targets)):
        seg = dict(seg)
        seg.setdefault("title", articles[i]["title"])
        log(f"  segment {i + 1}/{len(segments)}: {seg.get('title', '')[:48]}")
        parts.append(_write_part(
            build_segment_prompt(
                seg, digest, target,
                previous_tail=_tail("\n".join(parts)),
                is_last=(i == len(segments) - 1),
            ),
            target, f"seg{i + 1}"))

    log("  close")
    parts.append(_write_part(
        build_close_prompt(plan, digest, CLOSE_WORDS,
                           previous_tail=_tail("\n".join(parts))),
        CLOSE_WORDS, "close"))

    assembled = "\n".join(p for p in parts if p.strip())
    assembled_words = word_count(assembled)
    log(f"  assembled: {assembled_words} words")

    if assembled_words < MIN_EPISODE_WORDS:
        log(f"  WARNING: below the {MIN_EPISODE_WORDS}-word floor even after "
            "per-segment retries; shipping what we have")

    # Seam pass. Allowed to fix joins, forbidden to cut. Anything shorter
    # coming back is rejected outright rather than argued with, which is the
    # lesson from every previous editing pass in this pipeline.
    log("  seams")
    try:
        seamed = gemini_text_retry(build_seam_prompt(assembled, digest),
                                   label="seams")
    except Truncated as e:
        log(f"  seam pass failed ({e}); keeping assembled version")
        return assembled

    seamed_words = word_count(seamed)
    if seamed_words < assembled_words * 0.92:
        log(f"  seam pass shortened {assembled_words} -> {seamed_words}; "
            "rejected, keeping assembled version")
        return assembled
    if verify_script(seamed, digest):
        log("  seam pass introduced unverified figures; keeping assembled")
        return assembled

    log(f"  final: {seamed_words} words")
    for w in lint_script(seamed):
        log(f"    lint: {w}")
    return seamed


# ---------------------------------------------------------------- audio


def split_script(script: str) -> list[str]:
    """Group whole dialogue lines into <=CHUNK_CHARS chunks.

    Splitting only on line boundaries keeps speaker labels intact, which the
    multi-speaker config depends on.
    """
    lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for ln in lines:
        if buf and size + len(ln) > CHUNK_CHARS:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(ln)
        size += len(ln) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


# 24 kHz, 16-bit, mono.
PCM_BYTES_PER_SEC = 24000 * 2

# Conversational Hebrew runs about two and a half words a second. 0.22 s/word
# is roughly 270 wpm, faster than anyone actually speaks, so audio shorter
# than that for a given word count means content is missing rather than that
# the voice was brisk.
MIN_SECONDS_PER_WORD = 0.22


def _pcm_seconds(pcm: bytes) -> float:
    return len(pcm) / PCM_BYTES_PER_SEC


def synthesize(script: str, wav_path: pathlib.Path) -> None:
    """Synthesize, verifying that each chunk produced plausible audio.

    Nothing here used to check that the returned audio corresponded to the
    text sent. gemini_tts_chunk only asserts that SOME audio came back, so a
    chunk that synthesized its first sentence and stopped was indistinguishable
    from a complete one, and the shortfall only became visible as a suspiciously
    small mp3 nobody was checking either.
    """
    chunks = split_script(script)
    log(f"synthesizing {len(chunks)} chunks")
    pcm = bytearray()
    short_chunks = 0

    for i, chunk in enumerate(chunks, 1):
        expect_words = word_count(chunk) or len(chunk.split())
        floor = expect_words * MIN_SECONDS_PER_WORD
        log(f"  chunk {i}/{len(chunks)} ({len(chunk)} chars, "
            f"{expect_words} words, expect >{floor:.0f}s)")

        audio = b""
        for attempt in (1, 2, 3):
            audio = gemini_tts_chunk(chunk)
            got = _pcm_seconds(audio)
            if got >= floor:
                log(f"    {got:.0f}s")
                break
            log(f"    only {got:.0f}s for {expect_words} words "
                f"(attempt {attempt}/3); TTS dropped content, retrying")
            time.sleep(3)
        else:
            short_chunks += 1
            log(f"    WARNING: chunk {i} still short after 3 attempts")

        pcm += audio
        time.sleep(1)  # be polite to preview-tier rate limits

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)      # s16
        wf.setframerate(24000)  # Gemini TTS output rate
        wf.writeframes(bytes(pcm))

    total = _pcm_seconds(bytes(pcm))
    expected = word_count(script) * MIN_SECONDS_PER_WORD
    log(f"wav written: {wav_path} ({wav_path.stat().st_size / 1e6:.1f} MB, "
        f"{total:.0f}s)")

    # Publishing a truncated episode is worse than publishing none: the feed
    # is permanent and the listener has no way to tell a short episode from a
    # broken one. So this fails the build rather than committing the file.
    if expected and total < expected * 0.6:
        raise RuntimeError(
            f"audio is {total:.0f}s for a {word_count(script)}-word script, "
            f"expected at least {expected * 0.6:.0f}s. "
            f"{short_chunks} chunk(s) came back short. Refusing to publish a "
            "truncated episode."
        )


def to_mp3(wav_path: pathlib.Path, mp3_path: pathlib.Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(wav_path),
            "-codec:a", "libmp3lame", "-b:a", "96k",
            str(mp3_path),
        ],
        check=True,
    )
    wav_path.unlink(missing_ok=True)
    log(f"mp3 written: {mp3_path} ({mp3_path.stat().st_size / 1e6:.1f} MB)")


def wav_duration_seconds(path: pathlib.Path) -> int:
    with wave.open(str(path), "rb") as wf:
        return int(wf.getnframes() / wf.getframerate())


# ---------------------------------------------------------------- feed


def build_feed() -> None:
    """Regenerate the whole feed from what is on disk. Idempotent."""
    items = []
    for mp3 in sorted(EPISODES.glob("*.mp3"), reverse=True):
        date = mp3.stem                       # YYYY-MM-DD
        meta_path = mp3.with_suffix(".json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        try:
            pub = dt.datetime.strptime(date, "%Y-%m-%d").replace(
                hour=6, tzinfo=dt.timezone.utc
            )
        except ValueError:
            continue
        notes = meta.get("notes") or meta.get("summary", "")
        plain = re.sub(r"<[^>]+>", " ", notes)
        plain = re.sub(r"\s+", " ", plain).strip()
        items.append(f"""    <item>
      <title>{xml_escape(meta.get('title', f'Gamesforum Digest {date}'))}</title>
      <description><![CDATA[{notes}]]></description>
      <content:encoded><![CDATA[{notes}]]></content:encoded>
      <itunes:summary>{xml_escape(plain[:900])}</itunes:summary>
      <pubDate>{format_datetime(pub)}</pubDate>
      <guid isPermaLink="false">gamesforum-{date}</guid>
      <enclosure url="{BASE_URL}/episodes/{mp3.name}" length="{mp3.stat().st_size}" type="audio/mpeg"/>
      <itunes:duration>{meta.get('duration', 0)}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Gamesforum Digest</title>
    <link>{BASE_URL}</link>
    <description>Weekly mobile-gaming briefing, auto-generated from Gamesforum.</description>
    <language>{'he' if LANG == 'he' else 'en'}</language>
    <itunes:author>Automated</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Technology"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (ROOT / "feed.xml").write_text(feed, encoding="utf-8")
    log(f"feed.xml rebuilt with {len(items)} episodes")


# ---------------------------------------------------------------- main


def main() -> int:
    EPISODES.mkdir(exist_ok=True)
    DIGESTS.mkdir(exist_ok=True)

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    done: set[str] = set(state.get("processed", []))

    # Discovery, blocking, substance signals and relevance scoring all live in
    # discovery.py so they can be tuned in isolation with --dry-run. It writes
    # a ledger/<date>.md recording what it kept, what it dropped, and why.
    from discovery import select

    articles = select()
    if not articles:
        log("nothing cleared the relevance bar; exiting clean")
        # Mark everything seen so a weak week is not re-scored next run.
        seen = {a["url"] for a in discover_articles()}
        state["processed"] = sorted(done | seen)
        state["last_run"] = dt.date.today().isoformat()
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        return 0

    today = dt.date.today().isoformat()

    log("writing digest")
    budget = "\n".join(
        f"- {int(a.get('_airtime', 0) * 100)}% [{a.get('source', '?')}] "
        f"{a['title']}"
        for a in articles
    )
    log("airtime budget:\n" + budget)
    digest = gemini_text_retry(build_digest_prompt(articles), label="digest")

    # Fidelity gate. One free retry, because a regenerated digest usually
    # comes back clean; if it still drifts, ship it but shout about it.
    problems = verify_digest(digest, articles)
    if problems:
        log(f"FIDELITY: {len(problems)} unverified figure(s); regenerating")
        for w in problems:
            log(f"  ! {w}")
        digest = gemini_text_retry(build_digest_prompt(articles),
                                   label="digest retry")
        problems = verify_digest(digest, articles)

    sources = "\n".join(f"- [{a['title']}]({a['url']})" for a in articles)
    banner = ""
    if problems:
        log("FIDELITY: still unverified after retry, flagging in the file")
        for w in problems:
            log(f"  !! {w}")
        listed = "\n".join(f"> - {w}" for w in problems)
        banner = (
            "> **אזהרת אימות:** המספרים הבאים לא נמצאו במקורות. "
            "בדוק מול הקישורים למטה לפני שאתה מסתמך עליהם.\n"
            f"{listed}\n\n"
        )
    (DIGESTS / f"{today}.md").write_text(
        f"# Gamesforum Digest — {today}\n\n{banner}{digest}\n\n"
        f"## Sources\n{sources}\n",
        encoding="utf-8",
    )

    log("writing podcast script")
    script = write_episode(digest, articles, budget)
    for w in verify_script(script, digest):
        log(f"  ! {w}")
    (DIGESTS / f"{today}-script.md").write_text(script, encoding="utf-8")

    wav = EPISODES / f"{today}.wav"
    mp3 = EPISODES / f"{today}.mp3"
    synthesize(script, wav)
    duration = wav_duration_seconds(wav)
    to_mp3(wav, mp3)

    # Show notes. This is what appears under the episode in Apple Podcasts,
    # so the source list has to live here, not only in the repo. Every claim
    # in the audio should be one tap from the article it came from.
    first_para = re.sub(r"[#*>|_`-]", "", digest.strip().split("\n\n")[0])[:320]
    links = "".join(
        f'<li><a href="{xml_escape(a["url"])}">'
        f'{xml_escape(a["title"])}</a>'
        f' <em>({xml_escape(a.get("source", ""))})</em></li>'
        for a in articles
    )
    notes = (
        f"<p>{xml_escape(first_para)}</p>"
        f"<p><strong>מקורות הפרק ({len(articles)}):</strong></p>"
        f"<ol>{links}</ol>"
        f"<p>נבחרו אוטומטית מתוך מקורות שהוגדרו ב-profile.toml. "
        f"פירוט מלא של מה נבחר ומה נזרק: ledger/{today}.md</p>"
    )
    mp3.with_suffix(".json").write_text(
        json.dumps(
            {
                "title": f"Gamesforum Digest {today}",
                "summary": first_para,
                "notes": notes,
                "duration": duration,
                "sources": [
                    {"title": a["title"], "url": a["url"],
                     "source": a.get("source", ""),
                     "airtime": a.get("_airtime")}
                    for a in articles
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    build_feed()

    state["processed"] = sorted(done | {a["url"] for a in articles})
    state["last_run"] = today
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    log(f"done: {len(articles)} articles, {duration}s episode")
    return 0


if __name__ == "__main__":
    sys.exit(main())
