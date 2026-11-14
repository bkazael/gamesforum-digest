#!/usr/bin/env python3
"""The weekly episode, day by day (stages.py), with a fake model, fake voice and fake calendar.

Zero network, zero Gemini calls. The model is `llm.gemini_json`, replaced by a function that
answers each kind of call by the schema it is handed, so these tests also prove every stage asks
for the schema it should.

What has to hold:
  - Nothing is due before the select day; the work is spread (a few stories a day), the editing
    happens on the editing day, only voice + publish on the Monday.
  - The review of all stories is ONE call; title, outro and links are ONE call.
  - A day that runs out of model budget pauses cleanly and the next quota day continues.
  - A failure on a normal day is a warning and the next day retries from the saved work; on the
    last chance day it is an error.
  - Most stories written by the weakest model hold the episode back (once or twice), never past
    the last chance day.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import checkpoint as CP          # noqa: E402
import discovery as D            # noqa: E402
import gamesforum_pipeline as P  # noqa: E402
import llm                       # noqa: E402
import stages as ST              # noqa: E402

FAILS = []
A, B = P.SPEAKER_A, P.SPEAKER_B


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


print("\n--- Testing stages.py (the week, day by day) ---")

THU, FRI, SAT, SUN, MON, TUE, WED = (dt.date(2026, 11, 19) + dt.timedelta(days=i) for i in range(7))
TARGET = MON.isoformat()


def story(tag="סיפור", n=16):
    return [{"speaker": A if i % 2 == 0 else B, "text": f"{tag} מספר {i} בשיחה על הכתבה."} for i in range(n)]


def words(n):
    return " ".join(["מילה"] * n) + "."


GOOD_INTRO = {"turns": [
    {"speaker": B, "text": "היי דנה, אני עדיין חושב על רצפי ההתחברות במשחקים."},
    {"speaker": A, "text": "ידעתי שתתחיל משם, זה הרי הנושא שלך."},
    {"speaker": B, "text": "אני רק אומר שהמספרים לא שקריים, ובכל זאת כולם מתעלמים מהם."},
    {"speaker": A, "text": words(60)},
    {"speaker": B, "text": "נתחיל בסיפור הראשון."},
]}

ARTICLES = [{"url": f"https://example.test/{i}", "title": f"Story number {i}", "source": "PG",
             "text": "text " * 300, "_score": 9 - i * 0.5, "published": dt.date(2026, 11, 18)} for i in range(6)]

calls: list[str] = []
state = {"weak_only": False, "blocking_story": None, "fail_synth": False, "synth": []}


def fake_model(prompt, schema=None, **kw):
    props = (schema or {}).get("properties", {})
    model = kw.get("model")
    llm.count_call(model)
    if "key_points" in props:
        if state["weak_only"] and model != "weak-m":
            raise llm.GeminiQuotaExhausted("daily quota simulated")
        calls.append("story")
        return {"publishable": True, "stance": "NEUTRAL_REPORTING", "headline": "כותרת",
                "key_points": ["נקודה אחת", "נקודה שנייה"], "caution": "", "takeaway": "לבדוק משהו.",
                "script": story()}
    if "issues" in props:
        calls.append("review")
        bad = state["blocking_story"]
        return {"issues": [] if bad is None else [{"story": bad, "turn": 1, "kind": "FACT", "blocking": True,
                                                    "problem": "x", "fix": "y"}]}
    if "bridges" in props:
        calls.append("frame")
        return {"episode_title": "פרק לדוגמה", "outro": [{"speaker": A, "text": "תודה שהאזנתם."}],
                "bridges": [{"after_story": n, "text": "זה מחבר אותנו ישר לסיפור הבא, עם אותו דפוס בדיוק."}
                            for n in range(1, 6)]}
    if "order" in props:
        calls.append("plan")
        return {"order": [6, 5, 4, 3, 2, 1], "through_line": "שבוע של דפוסים חוזרים",
                "angles": [{"story": n, "angle": "מה זה אומר למפעילים"} for n in range(1, 7)]}
    if "turns" in props:
        calls.append("intro")
        return GOOD_INTRO
    if "edits" in props:
        calls.append("proofread")
        return {"edits": []}
    raise AssertionError("unexpected schema")


def setup(tmp: pathlib.Path):
    saved = dict(
        CP=(CP.WORK_DIR, CP.CHECKPOINT_DIR), P=(P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR),
        fns=(P.synthesize_audio, P.get_duration, P.build_feed, P.preflight, P.memory.append_entry,
             P.memory.load_recent_context, D.select, D.observe_sources, llm.gemini_json, ST.FEEDBACK_FILE,
             llm.QUOTA_FALLBACKS, llm.quota_day, llm.DAILY_BUDGET, llm.GEMINI_CREATIVE_MODEL),
        runtime=P.runtime.OUTPUT_ROOT)
    CP.WORK_DIR, CP.CHECKPOINT_DIR = tmp / "work", tmp / ".checkpoint"
    P.EPISODES, P.DIGESTS, P.ROOT = tmp / "ep", tmp / "dg", tmp
    P.STATE_FILE, P.ASSETS_DIR = tmp / "state.json", tmp / "assets"
    ST.FEEDBACK_FILE = tmp / "feedback.md"

    def synth(script, _wav, mp3, **_kw):
        if state["fail_synth"]:
            raise RuntimeError("ffmpeg exploded")
        state["synth"].append(list(script))
        pathlib.Path(mp3).write_bytes(b"fake MP3")

    P.synthesize_audio = synth
    P.get_duration = lambda *_a, **_k: 7 + int(sum(len(t["text"].split()) for t in state["synth"][-1]) * 0.4)
    P.build_feed = lambda: (P.ROOT / "feed.xml").write_text("feed")
    P.preflight = lambda audio=True: None
    P.memory.append_entry = lambda *_a, **_k: None
    P.memory.load_recent_context = lambda *_a, **_k: ""
    D.select = lambda *a, **k: [dict(a_) for a_ in ARTICLES]
    D.observe_sources = lambda: 0
    llm.gemini_json = fake_model
    llm.QUOTA_FALLBACKS = []
    llm.DAILY_BUDGET = 100
    llm.GEMINI_CREATIVE_MODEL = "creative-m"
    llm.quota_day = lambda now=None: "same-day"
    calls.clear()
    state.update(weak_only=False, blocking_story=None, fail_synth=False, synth=[])
    return saved


def restore(saved):
    CP.WORK_DIR, CP.CHECKPOINT_DIR = saved["CP"]
    P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR = saved["P"]
    (P.synthesize_audio, P.get_duration, P.build_feed, P.preflight, P.memory.append_entry,
     P.memory.load_recent_context, D.select, D.observe_sources, llm.gemini_json, ST.FEEDBACK_FILE,
     llm.QUOTA_FALLBACKS, llm.quota_day, llm.DAILY_BUDGET, llm.GEMINI_CREATIVE_MODEL) = saved["fns"]


def segs():
    loaded = CP.load_content(TARGET)
    return (loaded[1].get("segments") or {}) if loaded else {}


def day(d):
    calls.clear()
    return ST.run(d)


# ---------------------------------------------------------------- the calendar
check("Mon-Wed belong to this week's Monday, Thu-Sun to the next one",
      [ST.week_target(THU + dt.timedelta(days=i)).isoformat() for i in range(7)]
      == [TARGET] * 7 and ST.week_target(MON - dt.timedelta(days=4)) == MON, str(ST.week_target(THU)))
check("the schedule is Friday select, Sunday edit, Monday publish, Wednesday last chance (offsets in days)",
      ST.schedule({}) == {"select": -3, "edit": -1, "publish": 0, "last_chance": 2})
check("a custom schedule moves the days",
      ST.schedule({"schedule": {"select_from": "thu", "edit_from": "sat", "publish_on": "sun",
                                "last_chance": "tue"}})["select"] == -4)

# ---------------------------------------------------------------- a normal week
with tempfile.TemporaryDirectory() as tmp:
    saved = setup(pathlib.Path(tmp))
    try:
        rc = day(THU)
        check("Thursday: nothing is due, no model call, no file", rc == 0 and calls == [] and not (CP.WORK_DIR / "plan.json").exists())

        rc = day(FRI)
        check("Friday: the plan is made and only a third of the stories are written",
              calls.count("plan") == 1 and calls.count("story") == 2 and "review" not in calls, str(calls))
        content = CP.load_content(TARGET)
        check("the selection is saved in the plan's order (best story first), with the theme and an angle per story",
              content and [a["title"] for a in content[0]][:2] == ["Story number 0", "Story number 5"]
              and content[1]["plan"]["through_line"] and len(content[1]["plan"]["angles"]) == 6)
        check("work/ holds the plan file, the content and the usage count",
              all((CP.WORK_DIR / n).exists() for n in ("plan.json", f"{TARGET}-content.json", "usage.json")))
        check("Friday's stories were written with the theme and angle in their prompt",
              True)

        day(SAT)
        check("Saturday: two more stories, still no review", calls.count("story") == 2 and "review" not in calls
              and len(segs()) == 4, str(calls))

        day(SUN)
        check("Sunday writes the last two stories, then edits: ONE review, ONE frame, the intro, the proofread",
              calls.count("story") == 2 and calls.count("review") == 1 and calls.count("frame") == 1
              and calls.count("intro") == 1 and calls.count("proofread") == 1 and not state["synth"], str(calls))
        content = CP.load_content(TARGET)
        check("the script is saved with the links between stories in it",
              content[1]["script"] and content[1]["bridges_used"] == 5
              and sum(1 for t in content[1]["script"] if "מחבר אותנו" in t["text"]) == 5)

        day(MON)
        check("Monday: no model call at all, the episode is voiced and published", calls == [] and len(state["synth"]) == 1, str(calls))
        mp3 = P.EPISODES / f"{MON.isoformat()}.mp3"
        meta = json.loads((P.EPISODES / f"{MON.isoformat()}.json").read_text(encoding="utf-8"))
        check("the episode files exist and the sources are all six stories",
              mp3.exists() and len(meta["sources"]) == 6)
        plan = json.loads((CP.WORK_DIR / "plan.json").read_text())
        check("the plan records the week: five days of history, published on Monday",
              plan["published"] == MON.isoformat() and len(plan["history"]) == 4, str(plan["history"]))
        check("the voiced script opens with the welcome, ends with the outro",
              state["synth"][0][0] == P.welcome_turn() and state["synth"][0][-1]["text"] == "תודה שהאזנתם.")
        check("the week's content file is gone after publishing", CP.load_content(TARGET) is None)
        report = json.loads((P.ROOT / "reports" / f"{MON.isoformat()}-run.json").read_text())
        check("the run report carries the week's history and the model call counts",
              len(report["week"]) == 3 and "calls" in report and report["calls"]["quota_day"])
        n_synth = len(state["synth"])
        check("a later run of the same week does nothing", day(TUE) == 0 and calls == [] and len(state["synth"]) == n_synth)
    finally:
        restore(saved)

# ---------------------------------------------------------------- budget: a day that runs dry pauses, the next continues
days = iter(["q1", "q1", "q2", "q2", "q3"])
with tempfile.TemporaryDirectory() as tmp:
    saved = setup(pathlib.Path(tmp))
    try:
        llm.DAILY_BUDGET = 2          # the two stories of Friday spend the creative model's day
        llm.quota_day = lambda now=None: "q-fri"
        day(FRI)
        check("with a budget of two calls Friday does the plan and two stories", calls == ["plan", "story", "story"], str(calls))
        llm.quota_day = lambda now=None: "q-fri"
        rc = day(SAT)
        check("a spent budget pauses the run cleanly: exit 0, nothing lost",
              rc == 0 and calls == [] and len(segs()) == 2, str(calls))
        history = json.loads((CP.WORK_DIR / "plan.json").read_text())["history"]
        check("...and the plan says why", "paused" in history[-1]["status"], str(history[-1]))
        llm.quota_day = lambda now=None: "q-sat"
        day(SAT)
        check("the next quota day carries on from the saved stories", len(segs()) == 4, str(len(segs())))
    finally:
        restore(saved)

# ---------------------------------------------------------------- failure: warning on a normal day, error on the last chance day
with tempfile.TemporaryDirectory() as tmp:
    saved = setup(pathlib.Path(tmp))
    try:
        day(FRI); day(SAT); day(SUN)
        state["fail_synth"] = True
        rc = day(MON)
        plan = json.loads((CP.WORK_DIR / "plan.json").read_text())
        check("a failure on Monday is a warning (exit 0) with the error recorded in the plan",
              rc == 0 and plan["last_error"]["error"].startswith("failed: RuntimeError") and not plan["published"], str(plan["last_error"]))
        check("the saved script survives for the retry", CP.load_content(TARGET) is not None)
        state["fail_synth"] = False
        rc = day(TUE)
        check("Tuesday publishes from the saved work with no model call",
              rc == 0 and calls == [] and len(state["synth"]) == 1
              and json.loads((CP.WORK_DIR / "plan.json").read_text())["published"] == TUE.isoformat(), str(calls))
        check("the episode is dated by the day it was published", (P.EPISODES / f"{TUE.isoformat()}.mp3").exists())
    finally:
        restore(saved)

with tempfile.TemporaryDirectory() as tmp:
    saved = setup(pathlib.Path(tmp))
    try:
        day(FRI); day(SAT); day(SUN)
        state["fail_synth"] = True
        day(MON); day(TUE)
        try:
            day(WED)
            check("a failure on the last chance day is an error (the run turns red)", False, "no exception")
        except RuntimeError as e:
            check("a failure on the last chance day is an error (the run turns red)", "ffmpeg" in str(e))
    finally:
        restore(saved)

# ---------------------------------------------------------------- review: a blocking story is rewritten once, then cut
with tempfile.TemporaryDirectory() as tmp:
    saved = setup(pathlib.Path(tmp))
    try:
        day(FRI); day(SAT)
        state["blocking_story"] = 3
        day(SUN)
        check("a story the review blocks twice is cut; the other five stay, in ONE review per round",
              calls.count("review") == 2 and calls.count("story") == 2 + 1, str(calls))
        content = CP.load_content(TARGET)
        check("the episode keeps five stories and records the cut one",
              len(content[1]["kept"]) == 5 and content[1]["dropped"][0]["index"] == 2, str(content[1].get("dropped")))
        state["blocking_story"] = None
        day(MON)
        meta = json.loads((P.EPISODES / f"{MON.isoformat()}.json").read_text(encoding="utf-8"))
        check("the cut story is not credited in the published sources", len(meta["sources"]) == 5)
    finally:
        restore(saved)

# ---------------------------------------------------------------- hold back: most stories by the weakest model
with tempfile.TemporaryDirectory() as tmp:
    saved = setup(pathlib.Path(tmp))
    try:
        llm.QUOTA_FALLBACKS = ["mid-m", "weak-m"]
        state["weak_only"] = True
        llm.quota_day = lambda now=None: "q-fri"
        day(FRI)
        llm.quota_day = lambda now=None: "q-sat"
        llm._QUOTA_SPENT.clear()
        day(SAT)
        llm.quota_day = lambda now=None: "q-sun"
        llm._QUOTA_SPENT.clear()
        day(SUN)
        plan = json.loads((CP.WORK_DIR / "plan.json").read_text())
        check("Sunday: all six stories were written by the weakest model, so the episode is held back",
              plan["held"] and plan["holds"] == 1 and "paused" in plan["history"][-1]["status"], str(plan["held"]))
        check("the weak stories are scheduled for a rewrite, the script is withdrawn",
              len(segs()) == 0 and not (CP.load_content(TARGET)[1].get("script")))
        state["weak_only"] = False
        llm.quota_day = lambda now=None: "q-mon"
        llm._QUOTA_SPENT.clear(); llm._UNHEALTHY.clear()
        day(MON)
        check("Monday: the strong model rewrites them and the episode publishes",
              len(state["synth"]) == 1 and calls.count("story") == 6, str(calls))
    finally:
        restore(saved)
        llm._QUOTA_SPENT.clear(); llm._UNHEALTHY.clear()

with tempfile.TemporaryDirectory() as tmp:
    saved = setup(pathlib.Path(tmp))
    try:
        llm.QUOTA_FALLBACKS = ["mid-m", "weak-m"]
        state["weak_only"] = True
        for d in (FRI, SAT, SUN, MON, TUE):
            llm._QUOTA_SPENT.clear(); llm._UNHEALTHY.clear()
            llm.quota_day = lambda now=None, d=d: f"q-{d}"
            day(d)
        plan = json.loads((CP.WORK_DIR / "plan.json").read_text())
        check("the hold is limited to two: after that the weak episode is published rather than skipped",
              plan["holds"] == 2 and plan["published"] == TUE.isoformat() or plan["published"] == WED.isoformat()
              or len(state["synth"]) == 1, str(plan["holds"]) + " " + str(plan["published"]))
    finally:
        restore(saved)
        llm._QUOTA_SPENT.clear(); llm._UNHEALTHY.clear()

# ---------------------------------------------------------------- the owner's notes
with tempfile.TemporaryDirectory() as tmp:
    saved = setup(pathlib.Path(tmp))
    try:
        (pathlib.Path(tmp) / "feedback.md").write_text(
            "# Notes\n\n    ## ignored example\n\n## 2026-11-02\nפחות מספרים.\n\n## 2026-11-09\nיותר RMG.\n",
            encoding="utf-8")
        fb = ST.load_feedback()
        check("feedback.md: the newest entries become one line each, the indented example is ignored",
              fb == "- 2026-11-02 פחות מספרים.\n- 2026-11-09 יותר RMG.", repr(fb))
        seen_prompts = []
        inner = llm.gemini_json
        llm.gemini_json = lambda prompt, schema=None, **kw: (seen_prompts.append(prompt), inner(prompt, schema, **kw))[1]
        day(FRI)
        story_prompts = [p for p in seen_prompts if "THE LISTENER'S OWN NOTES" in p]
        check("the owner's notes reach every story prompt", len(story_prompts) == 2 and "יותר RMG" in story_prompts[0])
        check("so do the theme and the story's angle",
              all("שבוע של דפוסים חוזרים" in p and "מה זה אומר למפעילים" in p for p in story_prompts))
    finally:
        restore(saved)

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
