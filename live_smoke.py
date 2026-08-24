#!/usr/bin/env python3
"""
Tier 2: a minimal REAL Gemini smoke test.

This spends actual GEMINI_API_KEY budget -- unlike test_episode.py,
test_memory.py and test_contracts.py (Tier 0/1), which are all mocked and
free. Run this manually, on purpose, before trusting a model/schema/prompt
change in production -- not routinely, and never on a schedule. It is
triggered only by the "Live smoke test (manual, small cost)" workflow
(.github/workflows/manual_test.yaml), which is workflow_dispatch-only.

Kept deliberately cheap:
  - One hardcoded fixture article. No network fetch, no discovery run.
  - generate_podcast_content(..., target_words=150) asks for a short
    script instead of the usual 1,500-1,900 words.
  - A short script fits in one TTS chunk (well under TTS_CHUNK_CHAR_LIMIT),
    so this makes exactly one text-generation call and one TTS call --
    nowhere near the 6-8 articles / multiple TTS chunks a real weekly
    episode costs.

What it proves: the real API accepts the schemas this codebase sends it
right now (this is what would have caught the SCORE_SCHEMA casing bug
before test_contracts.py's offline check existed to catch it for free) and
that end-to-end text + TTS generation still works against the live models.

What it deliberately does NOT touch: state.json, memory.json, feed.xml,
digests/, episodes/. Output goes to ./smoke_output/, which nothing else in
the pipeline reads.
"""

from __future__ import annotations

import pathlib
import sys

import gamesforum_pipeline as P

OUTPUT_DIR = pathlib.Path(__file__).resolve().parent / "smoke_output"

FIXTURE_ARTICLE = {
    "url": "https://example.test/smoke-fixture",
    "title": "Smoke Test: Mobile IAP Revenue Update",
    "source": "LiveSmoke",
    "text": (
        "Global mobile IAP revenue reached $43.6bn in the fixture quarter, "
        "up 5.3% year on year, while downloads fell 12%. A hybrid-casual "
        "title crossed $10m in its first month using a web shop alongside "
        "in-game ads. This is placeholder text for a smoke test and is not "
        "a real news article."
    ),
}


def main() -> int:
    if not P.GEMINI_API_KEY:
        sys.exit("set GEMINI_API_KEY -- this test calls the real Gemini API")

    OUTPUT_DIR.mkdir(exist_ok=True)
    P.log("=== Tier 2 live smoke test: this spends real Gemini tokens ===")

    P.log("1/2: text generation (target_words=150, one fixture article)...")
    data = P.generate_podcast_content(
        [FIXTURE_ARTICLE], "smoke-test", memory_context="", target_words=150
    )
    words = sum(len(t.get("text", "").split()) for t in data.get("script", []))
    P.log(f"  got {len(data.get('script', []))} turns, {words} words")

    (OUTPUT_DIR / "smoke-script.json").write_text(
        __import__("json").dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    P.log("2/2: TTS synthesis (should be a single chunk)...")
    wav_path = OUTPUT_DIR / "smoke.wav"
    mp3_path = OUTPUT_DIR / "smoke.mp3"
    P.synthesize_audio(data["script"], wav_path, mp3_path)

    P.log(f"=== done. Output in {OUTPUT_DIR}/ -- nothing in the repo's "
          f"production paths (feed.xml, state.json, memory.json, "
          f"episodes/, digests/) was touched. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
