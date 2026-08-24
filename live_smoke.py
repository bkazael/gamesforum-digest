#!/usr/bin/env python3
"""
Tier 2: a minimal REAL Gemini smoke test.

This spends actual GEMINI_API_KEY budget -- unlike test_episode.py,
test_memory.py and test_contracts.py (Tier 0/1), which are all mocked and
free. Run this manually, on purpose, before trusting a model/schema/prompt
change in production -- not routinely, and never on a schedule. It is
triggered only by the "Live smoke test (manual, small cost)" workflow
(.github/workflows/manual_test.yaml), which is workflow_dispatch-only.

Three real API calls, kept deliberately cheap:
  1. discovery.score_all() against ONE fixture candidate -- this is the
     call that uses SCORE_SCHEMA, and is the only one of the three that
     actually round-trips that schema against the live API. (The
     text-generation and TTS calls below use PODCAST_SCHEMA, which was
     already correctly cased -- they prove the pipeline still works
     end-to-end, but on their own they would NOT have caught the
     SCORE_SCHEMA casing bug. This step exists specifically to close that
     gap.)
  2. generate_podcast_content(..., target_words=150) on the same fixture,
     asking for a short script instead of the usual 1,500-1,900 words.
  3. One TTS call -- a short script fits in a single chunk (well under
     TTS_CHUNK_CHAR_LIMIT), so this is nowhere near the 6-8 articles /
     multiple TTS chunks a real weekly episode costs.

What it deliberately does NOT touch: state.json, memory.json, feed.xml,
digests/, episodes/. It also never runs discovery.select() itself, so it
does not scrape any real site -- only score_all() runs, against one
hardcoded fixture. Output goes to ./smoke_output/, which nothing else in
the pipeline reads.
"""

from __future__ import annotations

import json
import pathlib
import sys

import discovery as D
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

    P.log("1/3: discovery scoring (SCORE_SCHEMA, real API call)...")
    profile = D.load_profile()
    fixture_candidate = {
        "_idx": 0,
        "title": FIXTURE_ARTICLE["title"],
        "text": FIXTURE_ARTICLE["text"],
        "signals": D.substance_signals(FIXTURE_ARTICLE["text"]),
    }
    scores = D.score_all(profile, [fixture_candidate])
    row = scores.get(0)
    if not row or not isinstance(row.get("score"), int):
        sys.exit(
            "SCORE_SCHEMA did not round-trip against the real API -- "
            f"score_all() returned {scores!r}. This is the exact failure "
            "mode the casing fix in discovery.py was meant to close; if "
            "you see this, that fix did not hold and scoring is still "
            "silently broken in production."
        )
    P.log(f"  SCORE_SCHEMA round-trip OK: score={row['score']}, axis={row['axis']!r}")

    P.log("2/3: text generation (target_words=150, one fixture article)...")
    data = P.generate_podcast_content(
        [FIXTURE_ARTICLE], "smoke-test", memory_context="", target_words=150
    )
    words = sum(len(t.get("text", "").split()) for t in data.get("script", []))
    P.log(f"  got {len(data.get('script', []))} turns, {words} words")

    (OUTPUT_DIR / "smoke-script.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    P.log("3/3: TTS synthesis (should be a single chunk)...")
    wav_path = OUTPUT_DIR / "smoke.wav"
    mp3_path = OUTPUT_DIR / "smoke.mp3"
    P.synthesize_audio(data["script"], wav_path, mp3_path)

    P.log(f"=== done. Output in {OUTPUT_DIR}/ -- nothing in the repo's "
          f"production paths (feed.xml, state.json, memory.json, "
          f"episodes/, digests/) was touched. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
