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

# ---------------------------------------------------------------- 6. soft caps: score outranks the source mix, within bounds
#
# Modelled on the real 2026-09-28 ledger: room for 8, seven taken, while
# 9.0s sat rejected "תקרת ... מלאה" and two 7.0s (a vendor press release and
# a roundup) stayed in.

CAP_SOURCES = [
    {"name": "PG", "max_per_episode": 3},
    {"name": "MG", "max_per_episode": 2},
    {"name": "GF", "max_per_episode": 2},
]
CAP_THR = {"min_score": 6, "max_articles": 8}


def cap_field():
    return [
        art("pg log-in bonuses under threat in eu kids act", "login bonuses", 10.0, "PG"),
        art("pg roblox loses bid over los angeles lawsuit", "roblox lawsuit", 9.0, "PG"),
        art("pg august spending report honor of kings", "august spend", 9.0, "PG"),
        art("pg newzoo no single model for growth cpi", "newzoo cpi", 9.0, "PG"),     # capped
        art("pg flexion friction around alternative distribution", "flexion", 9.0, "PG"),  # capped
        art("pg jest hits one million run rate after iap", "jest iap", 8.0, "PG"),    # capped
        art("mg youtube rolls out playables in fifty markets", "youtube playables", 10.0, "MG"),
        art("mg horizon create publish games with a prompt", "horizon create", 9.0, "MG"),
        art("mg chillbase onestate web store revenue", "chillbase webstore", 9.0, "MG"),   # capped
        art("mg data digest fastest growing games", "data digest", 9.0, "MG"),             # capped
        art("gf tiktok and roblox three digital policy stories", "digital policy", 7.0, "GF"),
        art("gf tyrads nordeus announce exclusive partnership", "tyrads press release", 7.0, "GF"),
        art("gf weekly round-up android seventeen", "roundup", 6.0, "GF"),
    ]


D.gemini_json = lambda prompt, schema=None: {"same_story": False, "reasoning": "distinct"}

field = cap_field()
result = D.select_stories(field, CAP_SOURCES, CAP_THR)
titles = [a["title"] for a in result]
by_src = {}
for a in result:
    by_src[a["source"]] = by_src.get(a["source"], 0) + 1

check("the episode is full (8) instead of stopping at 7 with strong articles waiting",
      len(result) == 8, f"got {len(result)}")
check("a 9.0 that a cap used to drop now takes the free slot (Newzoo CPI benchmarks)",
      any("newzoo" in t for t in titles), str(titles))
check("a 9.0 capped candidate beats a 7.0 pick by the swap margin (ChillBase over a 7.0)",
      any("chillbase" in t for t in titles) and sum(1 for a in result if a["_score"] == 7.0) == 1,
      str([(a["title"][:18], a["_score"]) for a in result]))
check("no source ends up more than cap_overflow (1) over its cap",
      by_src.get("PG", 0) <= 4 and by_src.get("MG", 0) <= 3 and by_src.get("GF", 0) <= 3,
      str(by_src))
check("a source is never swapped down to zero",
      by_src.get("GF", 0) >= 1, str(by_src))
scores = [a["_score"] for a in result]
check("the result is strongest-first (assign_airtime gives chosen[0] the lead share)",
      scores == sorted(scores, reverse=True), str(scores))
swapped_out = [a for a in field if str(a.get("_reject", "")).startswith("הוחלף")]
check("a swapped-out article is explained in the ledger, not silently missing",
      len(swapped_out) == 1, f"{len(swapped_out)} swapped")
check("an overflow/swap pick carries a ledger note saying why it got in",
      sum(1 for a in result if a.get("_note")) == 2,
      str([a.get("_note") for a in result if a.get("_note")]))

# cap_overflow = 0 restores the old hard cap exactly.
result = D.select_stories(cap_field(), CAP_SOURCES, dict(CAP_THR, cap_overflow=0))
check("cap_overflow = 0 restores the strict cap (7 picks, no Newzoo)",
      len(result) == 7 and not any("newzoo" in a["title"] for a in result),
      f"{len(result)} picks")

# The free-slot step needs a real score, not just any capped candidate.
weak_capped = [
    art("x1 strong pick", "strong", 9.0, "X"),
    art("x2 capped but mediocre", "mediocre", 7.5, "X"),
]
result = D.select_stories(weak_capped, [{"name": "X", "max_per_episode": 1}], CAP_THR)
check("a capped candidate below cap_overflow_min_score does not take a free slot",
      len(result) == 1, f"got {len(result)}")

# A source's only article is protected from the swap pass.
protected = [
    art("y1 top story", "top", 9.0, "Y"),
    art("y2 also top, capped", "top2", 9.0, "Y"),
    art("x1 lone weak pick", "weak", 6.5, "X"),
]
result = D.select_stories(protected, [{"name": "Y", "max_per_episode": 1},
                                      {"name": "X", "max_per_episode": 1}],
                          dict(CAP_THR, max_articles=2))
check("the sole representative of a source is never swapped out",
      any(a["source"] == "X" for a in result) and len(result) == 2,
      str([(a["title"][:10], a["source"]) for a in result]))

# ---------------------------------------------------------------- 7. press-release filtering
#
# 2026-09-28 gave 3 turns to "Tyrads & Nordeus Announce Exclusive
# Partnership to Drive Player-First Engagement" -- no figures, the company's
# own slogan quoted back as insight. The same article had scored 3.0 and
# 2.0 in the 08-03 and 08-13 ledgers and 7.0 on 09-28, so the model's score
# alone was never going to hold the line; the headline pattern is the
# deterministic backstop.

import tomllib  # noqa: E402

with (pathlib.Path(__file__).resolve().parent / "profile.toml").open("rb") as _f:
    _profile = tomllib.load(_f)
_gf = next(s for s in _profile["sources"] if s["name"] == "Gamesforum")


def _gf_blocked(slug_title: str):
    url = "https://www.globalgamesforum.com/news-media/" + slug_title.replace(" ", "-")
    return D.blocked_by_title(slug_title, url, _gf.get("block", []))


check("the TyrAds/Nordeus press-release headline is blocked on Gamesforum",
      _gf_blocked("tyrads nordeus announce exclusive partnership to drive player first "
                  "engagement in top eleven") is not None)

# Real Gamesforum headlines from the ledgers that must keep flowing.
for keep in (
    "sensor tower acquires appmagic to expand smb gaming intelligence offering",
    "mistplay mychips acquisition",
    "why nostalgia is driving mobile gamings biggest success stories",
    "technology digital policy three stories shaping the future",
    "the paywall how much is it a marketing problem",
):
    check(f"real headline not blocked: {keep[:48]!r}", _gf_blocked(keep) is None)

check("the Gamesforum patterns apply only to Gamesforum (a trade outlet's "
      "partnership story is untouched)",
      not any(s.get("block") and any("exclusive partnership" in p for p in s["block"])
              for s in _profile["sources"] if s["name"] != "Gamesforum"))

PR_TEXT = ("Tyrads is excited to announce an exclusive partnership with Nordeus. "
           "The strategic partnership brings player-first rewarded engagement and "
           "sustainable growth. For more information contact our media contact.")
sig = D.substance_signals(PR_TEXT)
check("a press-release body registers several PR-style phrases",
      sig["pr_phrases"] >= 4, f"pr_phrases={sig['pr_phrases']}")
check("the count reaches the scorer's SIGNALS line",
      "press-release phrases" in D.substance_note(sig))

NEWS_TEXT = ("Newzoo reports a 25% drop in mobile downloads and a 30% rise in CPI to "
             "$0.56 across 2025, with the market reaching $121.1bn.")
check("a plain data story registers no PR-style phrases",
      D.substance_signals(NEWS_TEXT)["pr_phrases"] == 0)
check("a plain data story's SIGNALS line does not mention press releases",
      "press-release" not in D.substance_note(D.substance_signals(NEWS_TEXT)))

_prompt = D.build_scoring_prompt(_profile, [{
    "_idx": 0, "title": "t", "text": "x " * 50, "signals": D.substance_signals("x " * 50),
}])
check("the scoring rubric states that intentions without a measured result cap at 4",
      "cannot score above 4" in _prompt and "a claim, not evidence" in _prompt)
check("the rubric keeps completed deals with amounts as valid MARKET news",
      "judged as MARKET news" in _prompt)

# ---------------------------------------------------------------- 8. repeats of already-covered stories
#
# 2026-09-28 re-covered August's market data 13 days after 2026-09-15 did,
# from a different outlet and with different totals (4.2bn downloads down 7%
# vs 3.76bn up 1%). Selection can't see that on its own; scoring has to be
# told what the listener already heard.

import json as _json  # noqa: E402
import tempfile as _tempfile  # noqa: E402
import memory as _memory  # noqa: E402

_cand = [{"_idx": 0, "title": "t", "text": "x " * 50, "signals": D.substance_signals("x " * 50)}]

_no_hist = D.build_scoring_prompt(_profile, _cand)
check("no recent topics adds no ALREADY COVERED section",
      "ALREADY COVERED" not in _no_hist)

_with = D.build_scoring_prompt(dict(_profile, _recent_topics=[
    "2026-09-15: August 2026 mobile game charts: Fate/Grand Order's big anniversary",
    "2026-09-22: Google Settles UK App Developer Class-Action for £260M",
]), _cand)
check("recent topics reach the scoring prompt, dated",
      "ALREADY COVERED IN RECENT EPISODES" in _with
      and "2026-09-15: August 2026 mobile game charts" in _with)
check("the prompt says a repeat of the same dataset caps at 4 unless it adds a new fact",
      "cannot score above 4" in _with.split("ALREADY COVERED")[1]
      and "materially new" in _with)
check("a genuine follow-up development stays welcome (not a blanket ban on revisits)",
      "follow-up development" in _with)

# memory.load_recent_topics(): reads real-shaped entries, windowed, dated.
with _tempfile.TemporaryDirectory() as _tmp:
    _real = _memory.MEMORY_FILE
    _memory.MEMORY_FILE = pathlib.Path(_tmp) / "memory.json"
    try:
        check("no memory file -> no topics", _memory.load_recent_topics() == [])
        _memory.MEMORY_FILE.write_text(_json.dumps([
            {"date": f"2026-0{m}-01", "title": "x", "topics_covered": [f"topic {m}a", f"topic {m}b"]}
            for m in range(1, 8)
        ]), encoding="utf-8")
        _t = _memory.load_recent_topics(limit=2)
        check("load_recent_topics(limit=2) returns the last two episodes' topics, dated",
              _t == ["2026-06-01: topic 6a", "2026-06-01: topic 6b",
                     "2026-07-01: topic 7a", "2026-07-01: topic 7b"], str(_t))
        check("the default window is 4 episodes (a month), wider than the script's 3",
              len(_memory.load_recent_topics()) == 8, f"{len(_memory.load_recent_topics())}")
        _memory.MEMORY_FILE.write_text("not json", encoding="utf-8")
        check("a corrupt memory file degrades to no topics instead of raising",
              _memory.load_recent_topics() == [])
    finally:
        _memory.MEMORY_FILE = _real

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
