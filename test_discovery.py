#!/usr/bin/env python3
"""
Offline tests for discovery.py's selection logic -- no network, no real
Gemini calls (gemini_json is monkeypatched throughout).

Written after a real production incident on 2026-08-31: a 7-article episode
came out as 5 because the old two-pass design (apply_caps on a widened
limit, then dedupe_stories to trim) could fill a source's cap with a pick
that dedup then discarded, with nothing behind it to backfill the freed
slot. The fix merged both checks into one pass (select_stories()) and
replaced the raw keyword-overlap heuristic, which had already produced a
real false positive in production (two unrelated Google stories matched on
"google"+"play"+"impacting" alone), with an LLM confirmation step
(confirm_same_story()) that only fires when the heuristic flags a pair.

Both failure modes get a test below, built directly from the real
production data that exposed them.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import discovery as D

FAILS = []

def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

print("\n--- Testing discovery.select_stories() ---")

SOURCES = [{"name": "TestWire", "max_per_episode": 2}]
THR = {"min_score": 6, "max_articles": 8}


def art(title, why, score, source="TestWire"):
    return {"title": title, "url": f"https://example.test/{hash(title) & 0xffff}",
            "source": source, "_score": score, "_why": why}


# Reproduces the real 2026-08-31 collision almost verbatim: two distinct
# Google stories that only share the company name and the model's own
# formulaic connector word.
GOOGLE_SETTLEMENT = art(
    "Google settles UK app developers class action lawsuit for $353m",
    "Google settles a UK class-action lawsuit for $353m over Play Store "
    "fees and app distribution policies, impacting platform economics.",
    9.0,
)
GOOGLE_REQUIREMENTS = art(
    "Google Play reveals new performance requirements for apps and games",
    "Google Play is introducing new performance requirements by 2027, "
    "impacting app visibility and publishing.",
    8.5,
)
ROBLOX_SAFETY = art(
    "Roblox announces new safety features",
    "Roblox rolls out new safety and parental control features globally.",
    7.0,
)

# ---------------------------------------------------------------- 1. heuristic pre-filter

clash = D._heuristic_clash(GOOGLE_REQUIREMENTS, [GOOGLE_SETTLEMENT])
check("the real collision still trips the cheap heuristic pre-filter "
      "(this is expected -- it's only a pre-filter, not a verdict)",
      clash is GOOGLE_SETTLEMENT)

no_clash = D._heuristic_clash(ROBLOX_SAFETY, [GOOGLE_SETTLEMENT])
check("an unrelated article does not trip the heuristic",
      no_clash is None)

# ---------------------------------------------------------------- 2. false positive is NOT rejected

D.gemini_json = lambda prompt, schema=None: {
    "same_story": False,
    "reasoning": "One is a lawsuit settlement, the other is a technical "
                 "policy change; different announcements.",
}
result = D.select_stories(
    [GOOGLE_SETTLEMENT, GOOGLE_REQUIREMENTS], SOURCES, THR
)
check("when Gemini says two heuristically-flagged articles are NOT the "
      "same story, both are kept (fixes the 2026-08-31 false positive)",
      len(result) == 2 and GOOGLE_SETTLEMENT in result
      and GOOGLE_REQUIREMENTS in result,
      f"got {len(result)} article(s)")

# ---------------------------------------------------------------- 3. confirmed duplicate IS rejected, and the freed cap slot backfills

D.gemini_json = lambda prompt, schema=None: {
    "same_story": True,
    "reasoning": "Both describe the same Google Play settlement.",
}
result = D.select_stories(
    [GOOGLE_SETTLEMENT, GOOGLE_REQUIREMENTS, ROBLOX_SAFETY], SOURCES, THR
)
check("a confirmed duplicate is dropped",
      GOOGLE_REQUIREMENTS not in result)
check("this is the actual bug fix: the cap slot the duplicate would have "
      "used goes to the next-best candidate from the same source instead "
      "of being lost -- 2026-08-31's episode landed on 5 articles instead "
      "of 7 precisely because this backfill did not happen",
      len(result) == 2 and ROBLOX_SAFETY in result,
      f"got {len(result)} article(s): {[a['title'][:30] for a in result]}")

# ---------------------------------------------------------------- 4. confirmation failure fails open (keeps both, never silently drops a topic)

def _raise(prompt, schema=None):
    raise RuntimeError("simulated API error")
D.gemini_json = _raise
result = D.select_stories(
    [GOOGLE_SETTLEMENT, GOOGLE_REQUIREMENTS], SOURCES, THR
)
check("a dedupe-confirmation API error fails open -- both articles are "
      "kept rather than risking another silently-dropped topic",
      len(result) == 2)

# ---------------------------------------------------------------- 5. per-source cap still enforced with confirmed non-duplicates

THIRD = art("Google Play adds new safety controls for kids accounts",
            "Google Play rolls out parental controls and age verification.",
            6.5)
D.gemini_json = lambda prompt, schema=None: {
    "same_story": False, "reasoning": "distinct announcements",
}
result = D.select_stories(
    [GOOGLE_SETTLEMENT, GOOGLE_REQUIREMENTS, THIRD], SOURCES, THR
)
check("the per-source cap (2) is still enforced once it's genuinely full "
      "of non-duplicate picks",
      len(result) == 2 and THIRD not in result,
      f"got {len(result)} article(s)")
check("THIRD was rejected for being over the cap, not mistaken for a "
      "duplicate",
      THIRD.get("_reject", "").startswith("תקרת"))

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
