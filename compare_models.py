#!/usr/bin/env python3
"""
Side-by-side comparison of Gemini models on the two stages where wording is
the product: the script and the intro.

Run by .github/workflows/model_compare.yaml (manual only). It spends real
Gemini quota -- about 1 request for the shared editorial prep plus 2 per model
(script + intro), more if a retry fires -- so it is for answering one question,
"would a different model write this episode better?", with evidence rather than
a guess. It writes nothing to the repo.

How it works: it takes the newest published episode, re-fetches that episode's
real source articles (episodes/<date>.json lists them), runs the same editorial
prep once, then for each model in $MODELS generates the script and the intro
from identical inputs, and prints both next to a few measurable numbers. The
numbers are only a screen (length, hype words, agreement openers, longest
turn); what actually decides is reading the Hebrew.

It also lists the models the API key can use for generateContent. That listing
costs no generation quota, and it is how you find out whether a stronger model
is even available to this key before comparing anything.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.request

import discovery as D
import gamesforum_pipeline as P

ROOT = pathlib.Path(__file__).resolve().parent


def say(msg: str = "") -> None:
    print(msg, flush=True)


def list_models() -> list[str]:
    url = ("https://generativelanguage.googleapis.com/v1beta/models"
           f"?pageSize=200&key={P.GEMINI_API_KEY}")
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.loads(r.read())
    names = []
    for m in data.get("models", []):
        if "generateContent" in (m.get("supportedGenerationMethods") or []):
            names.append(m["name"].removeprefix("models/"))
    return sorted(names)


def newest_episode() -> tuple[str, dict]:
    metas = sorted(p for p in (ROOT / "episodes").glob("20??-??-??.json"))
    if not metas:
        sys.exit("no published episode to take articles from")
    path = metas[-1]
    return path.stem, json.loads(path.read_text(encoding="utf-8"))


def load_articles(meta: dict) -> list[dict]:
    arts = []
    for s in meta.get("sources", []):
        got = P.fetch_article(s["url"])
        if not got:
            say(f"  (could not fetch {s['url']}; skipped)")
            continue
        got["source"] = s.get("source", "")
        got["title"] = s.get("title") or got["title"]
        arts.append(got)
    return arts


def describe(script: list[dict]) -> str:
    q = P.script_quality
    words = q.count_words(script)
    longest = max((len((t.get("text") or "").split()) for t in script), default=0)
    hype = sum(" ".join(t.get("text", "") for t in script).lower().count(s) for s in q.HYPE_STEMS)
    openers = sum(1 for t in script if (t.get("text") or "").strip().startswith(q.AGREEMENT_OPENERS))
    questions = sum(1 for t in script if (t.get("text") or "").rstrip().endswith("?"))
    return (f"{len(script)} turns, {words} words | longest turn {longest} words | "
            f"hype words {hype} | agreement openers {openers} | question turns {questions}")


SCRIPT_FOOTER = (
    "\n\n---\nOUTPUT FORMAT (for this manual test only): plain text, nothing else. "
    "First line: `TITLE: <episode title>`. Then the whole dialogue, one turn per "
    "line, every line starting with `Dana:` or `Yoni:`. No JSON, no markdown, no "
    "commentary before or after."
)
INTRO_FOOTER = (
    "\n\n---\nOUTPUT FORMAT (for this manual test only): plain text, nothing else. "
    "One turn per line, every line starting with `Dana:` or `Yoni:`, in order, "
    "starting with Yoni's reply to the welcome line. No JSON, no markdown, no "
    "commentary before or after."
)


class _Captured(Exception):
    """Raised by the recording stub to stop right after the prompt is built."""


def parse_digest(date: str) -> list[dict]:
    """The published episode's per-story summary, from digests/<date>.md --
    what write_intro() would have been given had it run on that script."""
    import re
    text = (ROOT / "digests" / f"{date}.md").read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"^## (.+)\n\*\*Key Takeaway:\*\* (.+)$", text, re.M):
        if m.group(1).startswith(("Discussion", "Sources")):
            continue
        out.append({"title": m.group(1).strip(), "key_takeaway": m.group(2).strip(),
                    "metrics_mentioned": []})
    return out


def dump_prompts(articles: list[dict], prep: dict, date: str, meta: dict) -> int:
    """Write the EXACT prompts the pipeline sends, without calling the model,
    so they can be pasted into any other model (AI Studio, another provider)
    and the answers compared. Nothing here is a paraphrase: the prompts come
    out of generate_podcast_content() and write_intro() themselves, with the
    network call replaced by a stub that records the prompt and stops."""
    out = ROOT / "prompts_out"
    out.mkdir(exist_ok=True)
    cap: dict[str, str] = {}

    def recorder(prompt, schema=None, **kw):
        cap["prompt"] = prompt
        raise _Captured()

    real = P.gemini_json
    P.gemini_json = recorder
    try:
        try:
            P.generate_podcast_content(articles, "dump", "", prep=prep)
        except _Captured:
            pass
        script_prompt = cap.pop("prompt")
        data = {"episode_title": meta.get("title", "").replace(" | Ben's Weekly Digest", ""),
                "digest_summary": parse_digest(date)}
        P.write_intro(data, prep)
        intro_prompt = cap.pop("prompt")
    finally:
        P.gemini_json = real
    (out / "prompt_1_script.txt").write_text(script_prompt + SCRIPT_FOOTER, encoding="utf-8")
    (out / "prompt_2_intro.txt").write_text(intro_prompt + INTRO_FOOTER, encoding="utf-8")
    for name in ("prompt_1_script.txt", "prompt_2_intro.txt"):
        size = (out / name).stat().st_size
        say(f"  wrote {name}: {size:,} bytes")
    return 0


def main() -> int:
    if not P.GEMINI_API_KEY:
        sys.exit("set GEMINI_API_KEY")
    models = [m.strip() for m in os.environ.get("MODELS", "").split(",") if m.strip()]
    if not models:
        models = [P.GEMINI_TEXT_MODEL]

    say("=== models this API key can use for generateContent ===")
    try:
        available = list_models()
        for name in available:
            if "tts" in name or "image" in name or "embed" in name:
                continue
            say(f"  {name}")
    except Exception as e:                                  # noqa: BLE001
        say(f"  (could not list models: {type(e).__name__}: {e})")
        available = []

    date, meta = newest_episode()
    say(f"\n=== material: episode {date}: {meta.get('title', '')} ===")
    articles = load_articles(meta)
    say(f"  {len(articles)} of {len(meta.get('sources', []))} source articles fetched")
    if len(articles) < 3:
        sys.exit("too few articles fetched to compare on")
    D.assign_airtime(articles, D.load_profile())

    say("\n=== shared editorial prep (default model, run once) ===")
    prep = P.prepare_episode(articles, "")
    if not prep:
        if not os.environ.get("DUMP_PROMPTS"):
            sys.exit("editorial prep failed; nothing to compare on")
        say("  WARNING: editorial prep failed (quota?); the dumped prompts will "
            "have no EDITORIAL PREP block")

    if os.environ.get("DUMP_PROMPTS"):
        say("\n=== DUMP_PROMPTS: writing the exact prompts, no model call ===")
        return dump_prompts(articles, prep, date, meta)

    for model in models:
        say("\n" + "=" * 78)
        say(f"MODEL: {model}" + ("" if not available or model in available
                                else "   (NOT in this key's model list)"))
        say("=" * 78)
        P.GEMINI_CREATIVE_MODEL = model
        started = time.monotonic()
        try:
            data = P.generate_podcast_content(articles, "compare", "", prep=prep)
        except Exception as e:                              # noqa: BLE001
            say(f"SCRIPT FAILED on {model}: {type(e).__name__}: {str(e)[:300]}")
            continue
        script_secs = time.monotonic() - started
        try:
            intro, source = P.write_intro(data, prep)
        except Exception as e:                              # noqa: BLE001
            say(f"INTRO FAILED on {model}: {type(e).__name__}: {str(e)[:300]}")
            intro, source = [], "failed"
        say(f"\n[{model}] script generated in {script_secs:.0f}s: {describe(data['script'])}")
        say(f"[{model}] intro source: {source}")
        say(f"\n--- INTRO ({model}) ---")
        for t in intro:
            say(f"{t['speaker']}: {t['text']}")
        say(f"\n--- SCRIPT, first 10 turns ({model}) ---")
        for t in data["script"][:10]:
            say(f"{t['speaker']}: {t['text'][:420]}")

    say("\n=== done; nothing was written to the repo ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
