#!/usr/bin/env python3
"""
Wiring/contract tests. Zero network, zero Gemini calls, zero cost.

This file exists because of a specific, real outage: a rewrite replaced
discovery.py with a version that no longer exported select(), and nothing
in CI imported the two modules together to notice. gamesforum_pipeline.py
ran `from discovery import select` in production, unchecked, until someone
looked at the traceback by hand.

Three things this file checks that a plain unit test (see test_episode.py,
test_memory.py) does not:

  1. The modules actually import together and the names each one expects
     from the other actually exist. This alone would have caught the
     outage above on the first push.
  2. Every Gemini responseSchema in the codebase uses the type names the
     API actually accepts. SCORE_SCHEMA used lowercase JSON-Schema type
     names ("object", "integer") left over from when this code called a
     different API; Gemini's schema enum is uppercase. That mismatch was
     silent -- score_all()'s per-batch try/except would have swallowed the
     resulting error and fallen back to the substance-signal score for
     every single article, forever, with nothing in the logs louder than a
     warning. This check would have caught it without a live API call.
  3. discovery.select()'s actual return shape -- run end to end against
     fixture data, with only the network and the two Gemini calls
     mocked -- has the keys generate_podcast_content() requires. That
     contract (url, title, text, source) is exactly the one that broke.

What this file does NOT cover: TTS synthesis, ffmpeg mixing, and
build_feed()'s XML rendering. Those need real audio or touch the live
episodes/ directory and belong in the Tier 2 manual smoke test
(live_smoke.py) instead, not in something that runs on every push.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

FAILS = []

def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

print("\n--- Testing module wiring & data contracts ---")

# ---------------------------------------------------------------- 1. imports

try:
    import gamesforum_pipeline as P
    import discovery as D
    import sources as S
    import memory as M
    check("gamesforum_pipeline, discovery, sources, memory all import together", True)
except Exception as e:
    check("gamesforum_pipeline, discovery, sources, memory all import together",
          False, f"{type(e).__name__}: {e}")
    print("\nFAILED: " + str(["module import"]))
    sys.exit(1)   # nothing else below can run without this

check("discovery.select exists and is callable",
      hasattr(D, "select") and callable(D.select))
check("gamesforum_pipeline.gemini_json exists (no leftover claude_json alias)",
      hasattr(P, "gemini_json") and not hasattr(P, "claude_json"))
check("discovery imports gemini_json directly (not the old claude_json alias)",
      D.gemini_json is P.gemini_json)

# ---------------------------------------------------------------- 2. schema casing

GEMINI_TYPES = {"OBJECT", "STRING", "ARRAY", "INTEGER", "NUMBER", "BOOLEAN"}

def schema_problems(node, path="root") -> list[str]:
    """Recursively find any 'type' value that isn't a real Gemini schema type."""
    problems = []
    if isinstance(node, dict):
        t = node.get("type")
        if t is not None and t not in GEMINI_TYPES:
            problems.append(
                f"{path}: type={t!r} is not a valid Gemini schema type "
                f"(expected one of {sorted(GEMINI_TYPES)})"
            )
        for k, v in node.items():
            problems.extend(schema_problems(v, f"{path}.{k}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            problems.extend(schema_problems(v, f"{path}[{i}]"))
    return problems

for schema_name, schema in (("PODCAST_SCHEMA", P.PODCAST_SCHEMA),
                            ("discovery.SCORE_SCHEMA", D.SCORE_SCHEMA)):
    problems = schema_problems(schema, schema_name)
    check(f"{schema_name} uses only valid Gemini type names",
          not problems, "; ".join(problems))

# ---------------------------------------------------------------- 3. full mocked run

FIXTURE_TEXT = " ".join(["מילה"] * 300)   # >= profile.toml's min_words (250)

FIXTURE_CANDIDATES = [
    {"url": "https://example.test/apple-eu-fees", "title": "Apple cuts EU app store fees",
     "source": "FixtureWire", "published": None, "summary": "5% core commission"},
    {"url": "https://example.test/mobile-iap-q3", "title": "Mobile IAP revenue hits new high in Q3",
     "source": "FixtureWire", "published": None, "summary": "$40bn quarterly IAP"},
]

def fake_collect(sources, max_age_days):
    return list(FIXTURE_CANDIDATES), set()

def fake_fetch_article(url):
    cand = next(c for c in FIXTURE_CANDIDATES if c["url"] == url)
    return {"url": url, "title": cand["title"], "text": FIXTURE_TEXT}

def fake_gemini_json_score(prompt, schema=None):
    # Mirrors SCORE_SCHEMA's shape: one row per candidate in the batch,
    # scored comfortably above profile.toml's min_score (6).
    return {"articles": [
        {"id": i, "reasoning": "fixture", "axis": "MARKET", "score": 9,
         "why": "fixture candidate", "topic": "fixture"}
        for i in range(len(FIXTURE_CANDIDATES))
    ]}

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = pathlib.Path(tmp)

    # Redirect discovery's state/ledger at scratch paths so this run can
    # never touch the real state.json or write into ledger/. profile.toml
    # itself is read for real -- that's a feature here, since it also
    # proves the checked-in config is valid TOML with the sections
    # select() expects.
    D.STATE_FILE = tmp_path / "state.json"
    D.LEDGER = tmp_path / "ledger"
    D.collect = fake_collect
    D.fetch_article = fake_fetch_article
    D.gemini_json = fake_gemini_json_score

    try:
        chosen = D.select()
        check("select() returns articles for a fixture run that should clear every gate",
              len(chosen) == len(FIXTURE_CANDIDATES), f"got {len(chosen)}")
    except Exception as e:
        check("select() runs end to end against fixtures without raising",
              False, f"{type(e).__name__}: {e}")
        chosen = []

REQUIRED_KEYS = {"url", "title", "text", "source"}
if chosen:
    missing = [REQUIRED_KEYS - set(a) for a in chosen]
    check("every article select() returns has the keys generate_podcast_content() needs",
          all(not m for m in missing), f"missing: {missing}")

# ---------------------------------------------------------------- 4. select() -> generate_podcast_content()

def fake_gemini_json_script(prompt, schema=None):
    return {
        "episode_title": "Fixture Episode",
        "digest_summary": [
            {"title": c["title"], "key_takeaway": c["summary"], "metrics_mentioned": []}
            for c in FIXTURE_CANDIDATES
        ],
        "script": [
            {"speaker": P.SPEAKER_A, "text": "פתיחה " * 40},
            {"speaker": P.SPEAKER_B, "text": "תגובה " * 40},
        ],
    }

if chosen:
    P.gemini_json = fake_gemini_json_script
    try:
        data = P.generate_podcast_content(chosen, "2026-08-31-test", memory_context="")
        check("generate_podcast_content() runs on select()'s real output",
              bool(data.get("script")), "empty script")

        md = P.render_digest_md(data, chosen, "2026-08-31-test", data["episode_title"])
        check("render_digest_md() includes every source article",
              all(c["title"] in md for c in chosen))

        # Same limit production uses -- imported, not copied, so this can't
        # drift the way test_episode.py's copy once did.
        lines = [f"{t['speaker']}: {t['text']}" for t in data["script"]]
        over_limit = [ln for ln in lines if len(ln) > P.TTS_CHUNK_CHAR_LIMIT]
        check("no single script turn alone exceeds the TTS chunk limit",
              not over_limit, f"{len(over_limit)} oversized turn(s)")
    except Exception as e:
        check("generate_podcast_content() + render_digest_md() run on select()'s output",
              False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------- 5. memory_context actually reaches the prompt

if chosen:
    captured_prompt = {}
    def capture_prompt(prompt, schema=None):
        captured_prompt["text"] = prompt
        return fake_gemini_json_script(prompt, schema)
    P.gemini_json = capture_prompt
    P.generate_podcast_content(chosen, "2026-08-31-test",
                               memory_context="- פרק מ-2026-08-24 (Fixture Prior): כלום.")
    check("a non-empty memory_context is inserted into the actual prompt sent to Gemini",
          "Fixture Prior" in captured_prompt.get("text", ""))
    P.gemini_json = capture_prompt
    P.generate_podcast_content(chosen, "2026-08-31-test", memory_context="")
    check("an empty memory_context adds no PREVIOUS EPISODES section",
          "PREVIOUS EPISODES" not in captured_prompt.get("text", ""))

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
