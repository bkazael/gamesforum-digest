#!/usr/bin/env python3
"""
Tier 2: a minimal REAL Gemini smoke test.

This spends actual GEMINI_API_KEY budget -- unlike test_episode.py,
test_memory.py and test_contracts.py (Tier 0/1), which are all mocked and
free. Run this manually, on purpose, before trusting a model/schema/prompt
change in production -- not routinely, and never on a schedule. It is
triggered only by the "Live smoke test (manual, small cost)" workflow
(.github/workflows/manual_test.yaml), which is workflow_dispatch-only.

Five real API calls, kept deliberately cheap:
  1. discovery.score_all() against ONE fixture candidate, with a fixture
     "already covered" topic -- this is the call that uses SCORE_SCHEMA, and
     it also exercises the scoring prompt's ALREADY COVERED section.
  2. prepare_episode() on two fixture articles -- round-trips PREP_SCHEMA.
  3. generate_podcast_content(..., prep=..., target_words=250) -- the
     stories-and-outro script, built from the prep, with no intro.
  4. write_intro() -- round-trips INTRO_SCHEMA. A *fallback* here is treated
     as a failure: it means the real model's answer did not pass the
     validators (or the call failed), which is exactly what this test exists
     to surface before a Monday does.
  5. One TTS call over intro + script (short enough for a single chunk).
  Every schema the pipeline sends has to be accepted by the live API at least
  once before it ships -- SCORE_SCHEMA's lowercase type names were the
  reason this test exists.

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


SECOND_ARTICLE = {
    "url": "https://example.test/smoke-fixture-2",
    "title": "Smoke Test: Proposed Rules On Daily Login Rewards For Minors",
    "source": "LiveSmoke",
    "text": (
        "A proposed rule would restrict daily login bonuses and activity "
        "streaks for players under 16 in the fixture region, while leaving "
        "them untouched for adults. The proposal is not yet law and no entry "
        "date has been set. Two fixture studios said age verification would "
        "cost them an estimated 4% of monthly revenue. This is placeholder "
        "text for a smoke test and is not a real news article."
    ),
    "_airtime": 0.4,
}
FIXTURE_ARTICLE["_airtime"] = 0.6


def main() -> int:
    if not P.GEMINI_API_KEY:
        sys.exit("set GEMINI_API_KEY -- this test calls the real Gemini API")

    OUTPUT_DIR.mkdir(exist_ok=True)
    P.log("=== Tier 2 live smoke test: this spends real Gemini tokens ===")

    P.log("1/5: discovery scoring (SCORE_SCHEMA + ALREADY COVERED section, real API call)...")
    profile = D.load_profile()
    profile["_recent_topics"] = ["2026-09-28: Fixture topic that was covered last week"]
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

    articles = [FIXTURE_ARTICLE, SECOND_ARTICLE]

    P.log("2/5: editorial prep (PREP_SCHEMA, real API call)...")
    prep = P.prepare_episode(articles, memory_context="")
    if not prep or not prep["articles"] or not prep["questions"]:
        sys.exit(
            "PREP_SCHEMA did not round-trip against the real API (prepare_episode "
            f"returned {prep!r}). In production this would silently fall back to "
            "writing the script without the editorial prep."
        )
    P.log(f"  PREP_SCHEMA round-trip OK: {len(prep['articles'])} articles, "
          f"{len(prep['questions'])} questions")

    P.log("3/5: script from the prep (target_words=250, no intro)...")
    data = P.generate_podcast_content(
        articles, "smoke-test", memory_context="", target_words=250, prep=prep
    )
    words = P.script_quality.count_words(data.get("script", []))
    P.log(f"  got {len(data.get('script', []))} turns, {words} words")
    leftovers = P.script_quality.script_problems(data["script"])
    if leftovers or any('"' in t.get("text", "") for t in data["script"]):
        P.log(f"  WARNING: script still has problems after repair: {leftovers}")

    P.log("4/5: intro, written last (INTRO_SCHEMA, real API call)...")
    data["prep"] = prep
    intro, source = P.write_intro(data, prep)
    if source != "model":
        sys.exit(
            "the intro fell back to the plain welcome: the real model's intro "
            "did not pass intro_problems() (or the call failed) -- see the "
            "'intro problem' lines above. Fix the prompt or the validator "
            "before this reaches a Monday."
        )
    for turn in intro:
        P.log(f"    {turn['speaker']}: {turn['text']}")
    data["script"] = intro + data["script"]

    (OUTPUT_DIR / "smoke-script.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    P.log("5/5: TTS synthesis (should be a single chunk)...")
    wav_path = OUTPUT_DIR / "smoke.wav"
    mp3_path = OUTPUT_DIR / "smoke.mp3"
    P.synthesize_audio(data["script"], wav_path, mp3_path)

    P.log(f"=== done. Output in {OUTPUT_DIR}/ -- nothing in the repo's "
          f"production paths (feed.xml, state.json, memory.json, "
          f"episodes/, digests/) was touched. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
