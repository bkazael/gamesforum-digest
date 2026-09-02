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

import memory

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

UA = "Mozilla/5.0 (compatible; bens-digest/6.3)"
HTTP_TIMEOUT = int(os.environ.get("API_TIMEOUT_SEC", "300"))
RUN_DEADLINE_SEC = int(os.environ.get("RUN_DEADLINE_SEC", "2400"))

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
    if time.monotonic() - _run_started > RUN_DEADLINE_SEC:
        raise RuntimeError("Run exceeded deadline; aborting.")

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

def gemini_json(prompt: str, schema: dict | None = None) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is required.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={GEMINI_API_KEY}"

    gen_config = {"responseMimeType": "application/json"}
    if schema:
        gen_config["responseSchema"] = schema

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_config
    }

    body = json.dumps(payload).encode()
    for attempt in range(6):
        _check_deadline()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                data = json.loads(r.read())
            cand = (data.get("candidates") or [{}])[0]
            text = (cand.get("content") or {}).get("parts", [{}])[0].get("text", "")
            log(f"    [Gemini responded in {time.monotonic() - started:.1f}s]")
            return json.loads(text)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            log(f"    Gemini API attempt {attempt + 1}/6 failed: HTTP {e.code} - {err_body}")
            if attempt == 5:
                raise
            time.sleep(10 * (attempt + 1))
        except Exception as e:
            log(f"    Gemini API attempt {attempt + 1}/6 failed: {e}")
            if attempt == 5:
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

def generate_podcast_content(articles: list[dict], today_date: str, memory_context: str = "",
                              target_words: int | None = None) -> dict:
    lang_inst = f"Write in natural Hebrew as spoken by Israeli mobile gaming executives (use {SPEAKER_A} [Female Anchor] and {SPEAKER_B} [Male Analyst]). Keep English terms like UA, CPI, ROAS, LTV, SKAN, DTC, IAP in English." if LANG == "he" else "Write in natural spoken English."
    corpus = "\n\n".join(f"ARTICLE {i+1}: {a['title']}\nURL: {a['url']}\n\n{a['text']}" for i, a in enumerate(articles))

    # target_words exists only for live_smoke.py (Tier 2): production
    # (main(), below) never passes it, so this branch never runs for a real
    # episode and the show's target length is untouched.
    if target_words:
        length_inst = f"Target Script Length: about {target_words} words. This is a short smoke-test run -- keep it brief."
    else:
        length_inst = "Target Script Length: 1,500 to 1,900 words. Keep it detailed, engaging, and professional."

    # Continuity is opt-in in the prompt itself: the section only exists when
    # there is real history to draw on, and the instruction is explicit about
    # not forcing a callback. A memory feature that makes every episode open
    # with "as we discussed last week" would work against the calm, unforced
    # tone the show is already tuned for -- so this is additive, not a
    # rewrite of how the hosts talk.
    memory_block = ""
    if memory_context:
        memory_block = f"""
PREVIOUS EPISODES (for natural continuity -- mention only when genuinely
relevant to today's stories; never force a callback):
{memory_context}
"""

    prompt = f"""You are the lead executive producer of a top-tier mobile gaming industry podcast.

Your goal is an in-depth, highly structured episode covering key developments.
{memory_block}
STRUCTURE OF THE SHOW:
1. FORMAL INTRO & GREETING:
   - Start smoothly as background music fades out.
   - {SPEAKER_A} opens warmly: "ברוכים הבאים ל-Ben's Weekly Digest. אני דנה, ואיתי יוני."
   - {SPEAKER_B} responds naturally: "היי דנה, שבוע מרתק בתעשייה."
   - {SPEAKER_A} outlines the main topics briefly.
2. DEEP DIVE SEGMENTS (Spend 3-5 dialogue turns PER ARTICLE):
   - Break down metrics, deals, and strategic implications.
   - Debate mechanics and UA/LTV impact.
3. SHOW OUTRO: Summarize the actionable takeaway and sign off.
4. EPISODE METADATA: Generate a highly engaging, catchy episode title based on the stories covered.

CHARACTER DYNAMICS:
- {SPEAKER_A} (Dana - Female Anchor): Leads strategy, numbers, and overarching market trends.
- {SPEAKER_B} (Yoni - Male Analyst): Analytical, questions assumptions, probes UA/LTV realities.

SPEECH NATURALISM:
- {lang_inst}
- {length_inst}

SOURCE ARTICLES:
{corpus}
"""
    log("generating podcast content via Gemini text call...")
    data = gemini_json(prompt, PODCAST_SCHEMA)

    total_words = sum(len(turn.get("text", "").split()) for turn in data.get("script", []))
    log(f"generated script: {len(data.get('script', []))} turns, {total_words} words")

    if total_words < 600:
        log(f"Warning: Script is short ({total_words} words), but proceeding.")

    return data

# ---------------------------------------------------------------- Gemini TTS

def gemini_tts_chunk(script_chunk_text: str, tries: int = 8) -> bytes:
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
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                data = json.loads(r.read())

            cand = (data.get("candidates") or [{}])[0]
            for part in (cand.get("content") or {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])

            log(f"    TTS chunk returned no audio data (attempt {attempt + 1}/{tries}); retrying...")
        except urllib.error.HTTPError as e:
            log(f"    TTS HTTP {e.code} (attempt {attempt + 1}/{tries})")
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
        except Exception as e:
            log(f"    TTS error (attempt {attempt + 1}/{tries}): {e}")
            if attempt == tries - 1:
                raise

        time.sleep(25 * (attempt + 1))

    raise RuntimeError("Gemini TTS returned no audio payload after retries.")

def synthesize_audio(script_turns: list[dict], wav_path: pathlib.Path, mp3_path: pathlib.Path):
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

    pcm = bytearray()
    for idx, chunk_text in enumerate(chunks, 1):
        log(f"  processing TTS chunk {idx}/{len(chunks)} ({len(chunk_text)} chars)...")
        audio_bytes = gemini_tts_chunk(chunk_text)
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

def main() -> int:
    EPISODES.mkdir(exist_ok=True)
    DIGESTS.mkdir(exist_ok=True)
    ASSETS_DIR.mkdir(exist_ok=True)

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    done: set[str] = set(state.get("processed", []))

    from discovery import select
    articles = select()
    if not articles:
        log("No articles cleared relevance threshold. Exiting clean.")
        state["last_run"] = dt.date.today().isoformat()
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        return 0

    today = dt.date.today().isoformat() + "-v2"

    # Recent-episode context for the script prompt. Reads memory.json
    # directly (no LLM call), so this costs nothing and cannot itself fail
    # the run -- see memory.py for why.
    memory_context = memory.load_recent_context()

    # 1. Gemini Single-Pass Text & Script Generation
    data = generate_podcast_content(articles, today, memory_context)

    episode_title = data.get("episode_title", "Weekly Gaming Digest")
    full_title = f"{episode_title} | Ben's Weekly Digest"

    # 2. Save Digest & Script
    (DIGESTS / f"{today}.md").write_text(render_digest_md(data, articles, today, full_title), encoding="utf-8")
    script_text = "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in data["script"])
    (DIGESTS / f"{today}-script.md").write_text(script_text, encoding="utf-8")

    # 3. Audio Synthesis via Gemini TTS + Ducking Jingle Assembly
    wav_path = EPISODES / f"{today}.wav"
    mp3_path = EPISODES / f"{today}.mp3"
    synthesize_audio(data["script"], wav_path, mp3_path)
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

    log("Finished run cleanly using Gemini API.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
