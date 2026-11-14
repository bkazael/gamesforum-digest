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

import os
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
    import source_health as SH
    import llm
    check("gamesforum_pipeline, discovery, sources, memory, source_health all import together", True)
except Exception as e:
    check("gamesforum_pipeline, discovery, sources, memory, source_health all import together",
          False, f"{type(e).__name__}: {e}")
    print("\nFAILED: " + str(["module import"]))
    sys.exit(1)   # nothing else below can run without this

check("discovery.select exists and is callable",
      hasattr(D, "select") and callable(D.select))
check("llm.gemini_json exists and the pipeline re-exports it (no leftover claude_json alias)",
      hasattr(llm, "gemini_json") and P.gemini_json is llm.gemini_json and not hasattr(P, "claude_json"))
check("discovery scores through llm.call_model, the one fallback chain (no private copy of it)",
      hasattr(D, "llm") and not hasattr(D, "gemini_json") and not hasattr(D, "_pipeline"))

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

import segments as SEG
_schemas = SEG.schemas((P.SPEAKER_A, P.SPEAKER_B))
for schema_name, schema in (("segments.SEGMENT_SCHEMA", _schemas["segment"]),
                            ("segments.REVIEW_SCHEMA", _schemas["review"]),
                            ("segments.FRAME_SCHEMA", _schemas["frame"]),
                            ("INTRO_SCHEMA", P.INTRO_SCHEMA),
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
    # Stands in for an email-sourced candidate (sources.from_email(), kind =
    # "email" in profile.toml): it already carries "text", exactly the shape
    # that lets select()'s fetch stage skip fetch_article() entirely. That
    # skip is the whole point of reading a blocked source's newsletter
    # instead of its site -- re-fetching the URL would just hit the same
    # block one stage later. fake_fetch_article below raises if this
    # candidate's URL is ever passed to it, so a regression that starts
    # re-fetching an already-fetched item fails loudly here instead of
    # quietly 403ing in production.
    {"url": "https://gamigion.substack.com/p/fixture-issue",
     "title": "Deconstruction: a fixture issue", "source": "FixtureMail",
     "published": None, "summary": FIXTURE_TEXT[:200], "text": FIXTURE_TEXT},
]

def fake_collect(sources, max_age_days):
    counts = {c["source"]: 0 for c in FIXTURE_CANDIDATES}
    for c in FIXTURE_CANDIDATES:
        counts[c["source"]] += 1
    return [dict(c, published=__import__("datetime").date.today())
            for c in FIXTURE_CANDIDATES], set(), counts

def fake_fetch_article(url):
    if url == "https://gamigion.substack.com/p/fixture-issue":
        raise AssertionError(
            "fetch_article() was called for a candidate that already had "
            "text -- select()'s stage 3 should have skipped the re-fetch"
        )
    cand = next(c for c in FIXTURE_CANDIDATES if c["url"] == url)
    return {"url": url, "title": cand["title"], "text": FIXTURE_TEXT}

def fake_gemini_json_score(prompt, schema=None, **kwargs):
    check("story scoring selects the configured classification model",
          kwargs.get("model") == P.GEMINI_SCORING_MODEL, str(kwargs))
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
    # never touch the real state.json or write into ledger/. Same reasoning
    # for source_health.ALERTS_FILE: select() calls source_health.record()
    # for real, and a test run has no business writing into the repo's
    # scratch alerts file. profile.toml itself is read for real -- that's
    # a feature here, since it also proves the checked-in config is valid
    # TOML with the sections select() expects.
    D.STATE_FILE = tmp_path / "state.json"
    D.SEEN_FILE = tmp_path / "seen.json"
    D.YIELD_FILE = tmp_path / "reports" / "source_yield.json"
    D.LEDGER = tmp_path / "ledger"
    SH.ALERTS_FILE = tmp_path / "source_alerts.json"
    D.collect = fake_collect
    D.fetch_article = fake_fetch_article
    llm.gemini_json = fake_gemini_json_score

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

email_survivor = next((a for a in chosen if a["url"].startswith("https://gamigion.substack.com")), None)
check("the already-fetched (email) candidate survived stage 3 without a re-fetch",
      email_survivor is not None and email_survivor["text"] == FIXTURE_TEXT,
      f"got {email_survivor}")

# ---------------------------------------------------------------- 4. select() -> the script

def turns(n, text="פתיחה"):
    """A well-formed, terminated conversation of n turns."""
    return [{"speaker": P.SPEAKER_A if i % 2 == 0 else P.SPEAKER_B,
             "text": f"{text} מספר {i} בשיחה על הכתבה."} for i in range(n)]

import segments as SG


def produce_script(articles, today, memory_context, resume, min_segments=None):
    """What stages.py does over several days, here all at once (segments.build_episode)."""
    return SG.build_episode(
        articles, call=lambda p, s, *, model, tries, timeout: llm.call_model(
            p, s, model=model, tries=tries, timeout=timeout),
        save=lambda d: None, models={"creative": llm.GEMINI_CREATIVE_MODEL, "review": llm.GEMINI_REVIEW_MODEL},
        speakers=(P.SPEAKER_A, P.SPEAKER_B), lang=P.LANG, memory_context=memory_context, resume=resume,
        log=lambda m: None, min_segments=min_segments or SG.MIN_SEGMENTS)


def model_stub(script=None, key_points=None, issues=None, log=None):
    """gemini_json stand-in that answers each kind of call by its schema:
    a story (key_points), its review (issues), or the frame (episode_title)."""
    def fn(prompt, schema=None, **kw):
        props = (schema or {}).get("properties", {})
        if log is not None:
            log.append((sorted(props)[0] if props else "", prompt))
        if "key_points" in props:
            return {"publishable": True, "stance": "NEUTRAL_REPORTING", "headline": "כותרת",
                    "key_points": key_points if key_points is not None else ["נקודה אחת", "נקודה שנייה"],
                    "caution": "", "takeaway": "משהו לבדוק.",
                    "script": script if script is not None else turns(16)}
        if "issues" in props:
            return {"issues": issues or []}
        if "episode_title" in props:
            return {"episode_title": "פרק לדוגמה", "outro": [
                {"speaker": P.SPEAKER_A, "text": "זה היה הפרק, תודה שהאזנתם."}], "bridges": []}
        raise AssertionError(f"unexpected schema {sorted(props)}")
    return fn

if chosen:
    llm.gemini_json = model_stub()
    try:
        data = produce_script(chosen, "2026-08-31-test", "", None, min_segments=1)
        check("produce_script() runs on select()'s real output",
              bool(data.get("script")), "empty script")
        check("every selected story becomes a segment of the script",
              len(data["kept"]) == len(chosen) and not data["dropped"],
              f"kept {data['kept']}, dropped {data['dropped']}")
        check("the title and outro come from the frame", data["episode_title"] == "פרק לדוגמה"
              and "תודה שהאזנתם" in data["script"][-1]["text"])

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
        check("produce_script() + render_digest_md() run on select()'s output",
              False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------- 4b. a cut-off turn (Hebrew acronym quote bug)
#
# script_quality.py explains the bug: 13 turns across 6 of 11 real episodes
# stop mid-word at ארה"ב / מנכ"ל. Three behaviours matter, and each costs
# real money if wrong: a repairable cut must cost NO extra Gemini request, an
# unrecognisable one must cost exactly ONE rewrite of that story, and if the
# rewrite is also broken the run must still produce a script instead of failing.

def story_calls(log):
    return [p for kind, p in log if kind == "caution"]

if chosen:
    one = chosen[:1]
    # (a) repairable: restored locally, one request for the story.
    broken = turns(15) + [{"speaker": P.SPEAKER_A, "text": "ההוצאות ירדו בארה"}]
    log = []
    llm.gemini_json = model_stub(script=broken, log=log)
    d = produce_script(one, "2026-10-05-test", "", None, min_segments=1)
    check("a repairable cut-off acronym costs no rewrite", len(story_calls(log)) == 1, f"{len(story_calls(log))} story calls")
    check("...and the acronym is restored in the output",
          any(t["text"] == "ההוצאות ירדו בארה״ב." for t in d["script"]))

    # (b) unrecognisable on attempt 1, clean on attempt 2: exactly one rewrite,
    # and the rewrite prompt says why and includes the draft.
    bad = turns(15) + [{"speaker": P.SPEAKER_A, "text": "הם הכריזו על שותפות עם חברת פלונ"}]
    good = turns(16, "טוב")
    seq, log = [bad, good], []
    inner = model_stub(log=log)
    def two_drafts(prompt, schema=None, **kw):
        out = inner(prompt, schema, **kw)
        if "key_points" in (schema or {}).get("properties", {}):
            out["script"] = seq.pop(0)
        return out
    llm.gemini_json = two_drafts
    d = produce_script(one, "2026-10-05-test", "", None, min_segments=1)
    calls = story_calls(log)
    check("an unrecognised cut-off turn costs exactly one rewrite", len(calls) == 2, f"{len(calls)} story calls")
    check("the rewrite prompt names what was wrong and carries the draft",
          "NOT ACCEPTED because" in calls[1] and "PREVIOUS DRAFT:" in calls[1])
    check("the first prompt has no rejection note", "NOT ACCEPTED" not in calls[0])
    check("the clean second draft is what is returned", any("טוב" in t["text"] for t in d["script"]))

    # (c) broken both times: never a third story request, never an exception.
    log = []
    llm.gemini_json = model_stub(script=bad, log=log)
    d = produce_script(one, "2026-10-05-test", "", None, min_segments=1)
    check("a story that stays broken is capped at two requests", len(story_calls(log)) == 2, f"{len(story_calls(log))} calls")
    check("...and still ships, with the fragment closed instead of left hanging",
          any(t["text"].endswith(".") and t["text"].startswith("הם הכריזו") for t in d["script"]))

# ---------------------------------------------------------------- 5. memory_context actually reaches the prompt

if chosen:
    log = []
    llm.gemini_json = model_stub(log=log)
    produce_script(chosen[:1], "2026-08-31-test", "- פרק מ-2026-08-24 (Fixture Prior): כלום.", None, min_segments=1)
    check("a non-empty memory_context is inserted into the actual prompt sent to Gemini",
          any("Fixture Prior" in p for p in story_calls(log)))
    log = []
    llm.gemini_json = model_stub(log=log)
    produce_script(chosen[:1], "2026-08-31-test", "", None, min_segments=1)
    check("an empty memory_context adds no PREVIOUS EPISODES section",
          not any("PREVIOUS EPISODES" in p for p in story_calls(log)))

# ---------------------------------------------------------------- 6. a finished script is voiced and published without a single model call
#
# The expensive half of a run is text generation. When the week's script is already
# saved (work/), the Monday run must reach the voice without touching the model or the
# selection. Getting this wrong is silent and expensive: the episode still comes out
# right, it just spends a second set of quota. So this asserts the thing that matters:
# the model and select() are never called.

import checkpoint as CP
import datetime as _dt
import stages as ST

MON = _dt.date(2026, 11, 23)
TUE = _dt.date(2026, 11, 24)


def _env(tmp_path):
    saved = (CP.WORK_DIR, CP.CHECKPOINT_DIR, P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR,
             P.synthesize_audio, P.get_duration, P.build_feed, P.preflight, llm.gemini_json,
             P.memory.append_entry, ST.FEEDBACK_FILE)
    CP.WORK_DIR, CP.CHECKPOINT_DIR = tmp_path / "work", tmp_path / ".checkpoint"
    P.EPISODES, P.DIGESTS, P.ROOT = tmp_path / "ep", tmp_path / "dg", tmp_path
    P.STATE_FILE, P.ASSETS_DIR = tmp_path / "state.json", tmp_path / "assets"
    ST.FEEDBACK_FILE = tmp_path / "feedback.md"
    P.preflight = lambda audio=True: None
    P.memory.append_entry = lambda *_a, **_k: None
    return saved


def _restore(saved):
    (CP.WORK_DIR, CP.CHECKPOINT_DIR, P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR,
     P.synthesize_audio, P.get_duration, P.build_feed, P.preflight, llm.gemini_json,
     P.memory.append_entry, ST.FEEDBACK_FILE) = saved


import discovery as _D
_real_select, _real_observe = _D.select, _D.observe_sources


def boom(*_a, **_k):
    raise AssertionError("a model call or a selection was made")


with tempfile.TemporaryDirectory() as tmp:
    tmp_path = pathlib.Path(tmp)
    saved = _env(tmp_path)
    audio_calls = []
    llm.gemini_json = boom
    _D.select, _D.observe_sources = boom, lambda: 0

    def fake_audio(_script, _wav, mp3_path, date=None):
        audio_calls.append(date)
        pathlib.Path(mp3_path).write_bytes(b"fake MP3")

    P.synthesize_audio = fake_audio
    P.get_duration = lambda *_a, **_k: 20
    P.build_feed = lambda: (P.ROOT / "feed.xml").write_text("feed")
    CP.save_content(MON.isoformat(), FIXTURE_CANDIDATES,
                    {"episode_title": "Resumed", "digest_summary": [{"title": "t", "key_takeaway": "k"}],
                     "intro_done": True, "proofread_done": True, "kept": [0, 1, 2],
                     "script": [{"speaker": P.SPEAKER_A, "text": "שלום חברים, " * 10 + "."},
                                {"speaker": P.SPEAKER_B, "text": "שלום."}],
                     "segments": {str(i): {"status": "ok", "headline": "כותרת"} for i in range(3)}})
    try:
        rc = ST.run(MON)
        check("a Monday run with a saved script publishes without any model call or selection", rc == 0)
        check("...and reaches audio synthesis under the week's key", audio_calls == [MON.isoformat()],
              f"synthesize_audio called with {audio_calls}")
        check("...and clears the week's saved work", CP.load_content(MON.isoformat()) is None)
        check("the episode is credited with the three kept stories",
              len(__import__("json").loads((P.EPISODES / f"{MON.isoformat()}.json").read_text())["sources"]) == 3)
    except AssertionError as e:
        check("a Monday run with a saved script makes no model call", False, str(e))
    except Exception as e:
        check("a Monday run with a saved script runs", False, f"{type(e).__name__}: {e}")
    finally:
        _D.select, _D.observe_sources = _real_select, _real_observe
        _restore(saved)

# ---------------------------------------------------------------- 7. a published week is never rebuilt
#
# The daily runs fire Friday to Wednesday. A run after a published episode must be a
# cheap no-op, and the guard's other failure direction is far worse: if it triggers when
# it shouldn't, no episode is ever published again and everything still looks green.

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = pathlib.Path(tmp)
    saved = _env(tmp_path)
    P.EPISODES.mkdir(parents=True, exist_ok=True)
    select_calls = []
    _D.select = lambda *a, **k: select_calls.append(1) or []
    _D.observe_sources = lambda: 0
    llm.gemini_json = boom
    try:
        (P.EPISODES / f"{MON.isoformat()}.mp3").write_bytes(b"already published")
        (P.EPISODES / f"{MON.isoformat()}.json").write_text("{}")
        check("a Tuesday run after Monday's episode exits 0 without discovery",
              ST.run(TUE) == 0 and not select_calls, f"select calls={len(select_calls)}")
        check("an orphan mp3 without its metadata is not a publication",
              P.episode_published_for(_dt.date(2026, 11, 30)) is None)
        (P.EPISODES / f"{MON.isoformat()}.json").unlink()
        check("...so a Tuesday run with only an orphan mp3 goes on to select",
              ST.run(TUE) == 0 and len(select_calls) == 1, f"select calls={len(select_calls)}")
    except Exception as e:
        check("the published-week guard behaves", False, f"{type(e).__name__}: {e}")
    finally:
        _D.select, _D.observe_sources = _real_select, _real_observe
        _restore(saved)

# ---------------------------------------------------------------- 7b. which episode belongs to which week

with tempfile.TemporaryDirectory() as tmp:
    saved_ep = P.EPISODES
    P.EPISODES = pathlib.Path(tmp)
    try:
        for d in ("2026-11-09", "2026-11-11"):
            (P.EPISODES / f"{d}.mp3").write_bytes(b"x")
            (P.EPISODES / f"{d}.json").write_text("{}")
        check("an episode published on the Monday belongs to that week", P.episode_published_for(_dt.date(2026, 11, 9)) == "2026-11-09")
        check("a Wednesday catch-up still counts for its week", P.episode_published_for(_dt.date(2026, 11, 9)) is not None
              and P.episode_published_for(_dt.date(2026, 11, 11)) == "2026-11-11")
        check("the next week's Monday is not covered by last week's episodes",
              P.episode_published_for(_dt.date(2026, 11, 16)) is None)
        check("an episode from before the target Monday never counts",
              P.episode_published_for(_dt.date(2026, 11, 12)) is None)
    finally:
        P.EPISODES = saved_ep

# ---------------------------------------------------------------- 8. the episode title
#
# A published item's guid is permanent, so the title is the one part of it
# still worth getting right after the fact -- and it is what a subscriber
# actually reads in their player. The 2026-09-15 episode went out as
# "... | Ben's Weekly Digest | Ben's Weekly Digest" because the suffix was
# appended unconditionally while the script prompt has the hosts say the
# show name out loud, so Gemini sometimes folds it into episode_title too.

for src, want in [
    ("הגיימינג מתפצל", "הגיימינג מתפצל | Ben's Weekly Digest"),
    ("D2C | Ben's Weekly Digest", "D2C | Ben's Weekly Digest"),
    ("D2C | Ben's Weekly Digest | Ben's Weekly Digest", "D2C | Ben's Weekly Digest"),
    ("  spaced  ", "spaced | Ben's Weekly Digest"),
    ("Ben's Weekly Digest", "Ben's Weekly Digest"),
    ("", "Ben's Weekly Digest"),
]:
    got = P.show_title(src)
    check(f"show_title({src!r}) appends the show name exactly once",
          got == want, f"got {got!r}, wanted {want!r}")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
