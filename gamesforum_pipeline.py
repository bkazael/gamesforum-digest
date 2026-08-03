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
TEXT_MODEL = os.environ.get("TEXT_MODEL", "gemini-3.5-flash")

SITE = "https://www.globalgamesforum.com"
LISTINGS = [f"{SITE}/features", f"{SITE}/news"]

ROOT = pathlib.Path(__file__).resolve().parent
EPISODES = ROOT / "episodes"
DIGESTS = ROOT / "digests"
STATE_FILE = ROOT / "state.json"

# Gemini TTS caps at 2 speakers. 30 voices available; these two read well
# as an informative/warm podcast pair.
SPEAKER_A, VOICE_A = "Dana", "Charon"      # informative
SPEAKER_B, VOICE_B = "Yoni", "Sulafat"     # warm

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


def gemini_text(prompt: str) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{TEXT_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192},
    }
    data = _post_json(url, payload)
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def gemini_tts_chunk(script_chunk: str) -> bytes:
    """One multi-speaker synthesis call -> raw 24kHz s16le mono PCM.

    The docs flag two real failure modes we guard against here:
      * random 500s where the model emits text tokens instead of audio
      * vague prompts causing the model to READ THE DIRECTIONS ALOUD,
        so we use an explicit preamble + a hard TRANSCRIPT marker.
    """
    prompt = (
        "Synthesize the following podcast conversation as speech. "
        "Do not read these instructions aloud. "
        f"Two hosts, {SPEAKER_A} and {SPEAKER_B}, recording a concise "
        "mobile-gaming industry briefing for an experienced operator. "
        "Delivery: professional, conversational, brisk but not rushed. "
        "Read only the lines after the TRANSCRIPT marker.\n\n"
        "TRANSCRIPT:\n" + script_chunk
    )
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


def _post_json(url: str, payload: dict, tries: int = 3) -> dict:
    body = json.dumps(payload).encode()
    for attempt in range(tries):
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
            time.sleep(min(60, 4 * 2 ** attempt))
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
- Bullets over paragraphs. Under 800 words before the sources list.

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


def build_outline_prompt(digest: str, budget: str = "") -> str:
    """Plan the arc before writing a word. Scripts written straight from a
    digest inherit the digest's shape, which is a list. Lists read aloud are
    the single biggest reason AI podcasts sound like AI podcasts."""
    return f"""You are the producer of a mobile-gaming industry podcast.
Plan one episode from the digest below. Do not write dialogue yet.

Output a compact plan:

1. THROUGH-LINE: one sentence naming the tension that connects these items.
   Not a topic ("retention"), a tension ("the industry is spending more to
   acquire users it already knows it will lose"). If the items genuinely do
   not connect, say so and order them by importance instead. Do not invent a
   connection that is not really there.

2. COLD OPEN: the single most arresting concrete detail in the whole digest.
   A number, a reversal, a specific company doing a specific thing. This is
   the first fifteen seconds. It is never a greeting and never a summary of
   what is coming.

AIRTIME BUDGET. This was decided upstream by relevance scoring. Respect it.
The lead story gets a narrative; a 10% item gets two or three exchanges and
then you move on. Equal time for every item is what makes an episode sound
like a list read aloud.
{budget or "(not supplied; weight by importance)"}

3. SEGMENTS: 3 or 4 only. Ruthless. For each:
   - the claim in one sentence
   - the hard numbers that carry it
   - whose claim it is
   - ONE concrete image or analogy that makes it land, drawn strictly from
     the mechanism already described. Write "none" if nothing honest fits.
     A forced metaphor is worse than none.
   - the obvious objection a smart listener would raise

4. HANDOFFS: how each segment connects to the next. "Second thing" is not a
   handoff, it is a bullet point spoken aloud.

5. CLOSE: the one line worth remembering. A restatement of the sharpest fact,
   not a lesson and not advice.

{_FIDELITY}

DIGEST:
{digest}
"""


def build_script_prompt(digest: str, outline: str) -> str:
    lang_line = (
        """Write the dialogue in natural spoken Hebrew, the way two Israeli
industry people actually talk. Not literary Hebrew, not translated English.
Keep English industry terms in English, because that is how the conversation
really sounds: retention, LiveOps, churn, UA, hybrid monetization. Do not
transliterate them into Hebrew letters."""
        if LANG == "he"
        else "Write the dialogue in natural spoken English."
    )
    return f"""Write the episode from the plan below.

{lang_line}

THE TWO HOSTS ARE NOT SYMMETRIC. This is the most important craft note.
- {SPEAKER_A} carries the material. She has read the sources and lays out
  what they say.
- {SPEAKER_B} is the listener's proxy. He interrupts, asks the obvious
  question, restates things in plainer words, and raises the objection.
  He is not a yes-man and he is not an idiot. He is the smart colleague who
  says "רגע, אבל זה לא סותר את מה שאמרת קודם?"

Two people alternating facts is not a conversation. It is a list with
costumes. If any exchange would survive being reassigned to the other
speaker, it is not really dialogue.

CRAFT:
- Every number gets a reaction before it gets an explanation. A figure stated
  flatly is a figure the listener forgets.
- Restate the hard parts. After a dense claim, {SPEAKER_B} says it back in
  simpler words. This is the main comprehension device in audio, where the
  listener cannot re-read.
- Vary the rhythm hard. A three-word line after a long one is what makes
  speech sound alive. If every turn is a similar length, it sounds generated.
- Concrete beats abstract. Name the company, the number, the mechanism.
- Signpost transitions, because there is nothing on screen to orient anyone.
- One callback to something said earlier in the episode. Only one.
- Let them disagree once about emphasis, then move on. Never manufacture a
  disagreement about facts.
- Light spoken texture only: "תראה", "רגע", "כן, אבל". A little goes far.

BANNED, these are the tells:
- Greetings, welcomes, intros, sign-offs, "בפרק היום נדבר על"
- "בואו נצלול", "נשמע מרתק", "זו נקודה מצוינת"
- Any host complimenting the other's question
- Summarising at the end what was just said
- Both hosts agreeing three times in a row
- Starting consecutive turns with the same word

{_FIDELITY}

FORMAT:
- Exactly two speakers, labelled "{SPEAKER_A}:" and "{SPEAKER_B}:".
  Never a third.
- 900 to 1100 words, which is 5 to 6 minutes spoken. Under is better
  than over.
- Numbers spelled out as words. No digits, no percent signs, no currency
  symbols, they read badly.
- English audio tags, used maybe four times in the whole script:
  [thoughtful], [emphatic], [dry], [surprised]. Overuse sounds theatrical.
- Dialogue lines only. No headings, no stage directions.

PLAN:
{outline}

DIGEST (source of truth for every fact):
{digest}
"""


def build_polish_prompt(script: str, digest: str) -> str:
    """Self-critique pass. Models are far better at spotting their own tells
    than at avoiding them first time."""
    return f"""Edit this podcast script. Return only the edited script, in the
same "Speaker: line" format, nothing else.

Work through these in order and actually change the text:

1. TURN LENGTH. Find the longest turn. If it is over about forty words, break
   it with a real interruption from the other host, not a filler nod.
2. RHYTHM. Are turns all similar length? Insert short reactions. Merge choppy
   fragments that should be one thought.
3. THE FLAT NUMBER TEST. Every figure: does someone react to it, or is it
   recited? Recited numbers get a reaction or get cut.
4. ASYMMETRY. Could you swap the two speakers' lines without noticing? If so,
   sharpen {SPEAKER_B} into the one who questions and restates.
5. OPENING. Does it begin with the most arresting concrete detail? If it
   begins with any form of greeting or preview, delete that and start again
   at the interesting part.
6. ENDING. If it summarises, or offers advice, or trails off politely, cut
   back to the last genuinely interesting line.
7. TELLS. Remove "בואו נצלול", mutual compliments, three consecutive
   agreements, consecutive turns opening on the same word.
8. LENGTH. Cut to under 1100 words. Cut whole exchanges, not adjectives.
9. HEBREW. Anything that reads like translated English gets rewritten the way
   someone would actually say it out loud.

{_FIDELITY}

Verify against the digest before returning: every figure and attribution must
still be intact and unchanged. Fix drift rather than keeping a nicer line.

DIGEST:
{digest}

SCRIPT TO EDIT:
{script}
"""


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


def parse_turns(script: str) -> list[tuple[str, str]]:
    turns = []
    for line in script.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(rf"^({re.escape(SPEAKER_A)}|{re.escape(SPEAKER_B)})\s*:\s*(.+)$", line)
        if m:
            turns.append((m.group(1), m.group(2).strip()))
        elif turns:
            turns[-1] = (turns[-1][0], turns[-1][1] + " " + line)
    return turns


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

    # The listener-proxy must actually question things.
    b_turns = [t for s, t in turns if s == SPEAKER_B]
    questions = sum(1 for t in b_turns if "?" in t)
    if b_turns and questions < 3:
        issues.append(
            f"{SPEAKER_B} asks only {questions} question(s); he is the "
            "listener's proxy and should be probing"
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

    tags = len(re.findall(r"\[[a-z ]+\]", script))
    if tags > 8:
        issues.append(f"{tags} audio tags, theatrical; four or so is plenty")

    return issues


def _issue_kind(msg: str) -> str:
    """Bucket a lint message so two runs can be compared by problem class."""
    for key in ("thin", "will overrun", "monologue", "uniform", "audience",
                "question", "banned", "both open", "digits", "audio tags"):
        if key in msg:
            return key
    return "other"


def write_script(digest: str, budget: str = "") -> str:
    """Three passes: plan the arc, write it, then edit against the tells.

    One-shot generation reliably produces a list read aloud. The outline pass
    is what buys narrative shape; the polish pass is what removes the tells,
    which models spot far better than they avoid.
    """
    log("  pass 1/3: outline")
    outline = gemini_text(build_outline_prompt(digest, budget))

    log("  pass 2/3: draft")
    script = gemini_text(build_script_prompt(digest, outline))
    before = lint_script(script)
    for w in before:
        log(f"    draft: {w}")

    log("  pass 3/3: polish")
    polished = gemini_text(build_polish_prompt(script, digest))

    after = lint_script(polished)

    # Accept the edit only if it is genuinely not worse. Comparing issue
    # counts alone is not enough: an edit can trade one problem for another
    # and score equal while reading much worse. So a polish that introduces
    # any problem CLASS the draft did not have is rejected outright.
    if verify_script(polished, digest):
        log("    polish introduced unverified figures; keeping draft")
        return script

    new_kinds = {_issue_kind(w) for w in after} - {_issue_kind(w) for w in before}
    if new_kinds:
        log(f"    polish introduced new problems {sorted(new_kinds)}; keeping draft")
        return script
    if len(after) > len(before):
        log(f"    polish regressed ({len(before)} -> {len(after)}); keeping draft")
        return script

    for w in after:
        log(f"    final: {w}")
    return polished


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


def synthesize(script: str, wav_path: pathlib.Path) -> None:
    chunks = split_script(script)
    log(f"synthesizing {len(chunks)} chunks")
    pcm = bytearray()
    for i, chunk in enumerate(chunks, 1):
        log(f"  chunk {i}/{len(chunks)} ({len(chunk)} chars)")
        pcm += gemini_tts_chunk(chunk)
        time.sleep(1)  # be polite to preview-tier rate limits
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)      # s16
        wf.setframerate(24000)  # Gemini TTS output rate
        wf.writeframes(bytes(pcm))
    log(f"wav written: {wav_path} ({wav_path.stat().st_size / 1e6:.1f} MB)")


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
    digest = gemini_text(build_digest_prompt(articles))

    # Fidelity gate. One free retry, because a regenerated digest usually
    # comes back clean; if it still drifts, ship it but shout about it.
    problems = verify_digest(digest, articles)
    if problems:
        log(f"FIDELITY: {len(problems)} unverified figure(s); regenerating")
        for w in problems:
            log(f"  ! {w}")
        digest = gemini_text(build_digest_prompt(articles))
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
    script = write_script(digest, budget)
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
