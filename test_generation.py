#!/usr/bin/env python3
"""
Offline tests for how an episode is assembled in the pipeline: the stories
(segments.py, tested on its own in test_segments.py), then the intro written
LAST, then the checkpoint behaviour around them.

Zero network, zero Gemini calls: gemini_json is replaced throughout and told
apart by which SCHEMA it is handed, so these tests also prove each stage asks
for the schema it is supposed to.

What has to hold, and why each is worth a test:
  - The ORDER: stories -> frame (title, outro) -> intro, with the intro built
    from the finished stories. If a later edit quietly moved the intro back in
    front, every other test here would still pass.
  - The optional stages (frame, intro) degrade instead of failing the run.
  - The checkpoint is written as each story finishes, after the assembled
    script, and after the intro, so a hard kill never costs paid work and a
    resumed run never gets a second greeting.
"""

from __future__ import annotations

import copy
import datetime as dt
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import checkpoint as CP  # noqa: E402
import gamesforum_pipeline as P  # noqa: E402

FAILS = []
A, B = P.SPEAKER_A, P.SPEAKER_B


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def words(n: int) -> str:
    return " ".join(["מילה"] * n) + "."


print("\n--- Testing story assembly / intro-last ---")

ARTICLES = [
    {"url": "https://example.test/eu-kids", "title": "Daily log-in bonuses under threat in EU Kids Act",
     "source": "PG", "text": "EU Kids Act text. " * 30, "_airtime": 0.40, "published": dt.date(2026, 10, 2)},
    {"url": "https://example.test/youtube", "title": "YouTube rolls out Playables to 50 markets",
     "source": "MG", "text": "Playables text. " * 30, "_airtime": 0.25, "published": dt.date(2026, 10, 2)},
    {"url": "https://example.test/tyrads", "title": "TyrAds and Nordeus partner on rewarded engagement",
     "source": "GF", "text": "Partnership text. " * 30, "_airtime": 0.35, "published": dt.date(2026, 10, 2)},
]

# What segments.build_episode() hands write_intro() as `prep`.
prep = {"articles": [{"key_points": ["EU Kids Act is a proposal, not yet law"]},
                     {"key_points": ["Playables in 50 markets"]},
                     {"key_points": ["partnership announced, no results"]}],
        "questions": [], "through_line": "platforms are being pushed to prove who is a child"}

# ---------------------------------------------------------------- 6. write_intro

DATA = {"episode_title": "Regulation tightens", "digest_summary": [
    {"title": "EU Kids Act", "key_takeaway": "login streaks under threat"},
    {"title": "YouTube Playables", "key_takeaway": "50 markets"},
    {"title": "Rewarded engagement", "key_takeaway": "claims, no results"},
]}
GOOD_INTRO = {"turns": [
    {"speaker": B, "text": "היי דנה, אני עדיין חושב על רצפי ההתחברות."},
    {"speaker": A, "text": "ידעתי שתתחיל משם, זה הרי הנושא שלך."},
    {"speaker": B, "text": "אני רק אומר שהמספרים לא שקריים, ובכל זאת כולם מתעלמים מהם."},
    {"speaker": A, "text": words(60)},
    {"speaker": B, "text": "נתחיל בחוק הילדים של האיחוד האירופי."},
]}
intro_seen = {}
def fake_intro(prompt, schema=None, **kw):
    intro_seen.setdefault("prompts", []).append(prompt)
    intro_seen["schema"], intro_seen["kw"] = schema, kw
    return GOOD_INTRO

P.gemini_json = fake_intro
turns, source = P.write_intro(DATA, prep)
check("a good intro is welcome + the model's turns", turns[0] == P.welcome_turn() and len(turns) == 6 and source == "model")
check("the welcome line is fixed in code, not left to the model",
      turns[0]["text"].startswith("ברוכים הבאים ל-Ben's Weekly Digest"))
check("write_intro asks for INTRO_SCHEMA with a short bounded call",
      intro_seen["schema"] is P.INTRO_SCHEMA and intro_seen["kw"].get("tries") in (2, 4) and intro_seen["kw"].get("timeout") == 150)
ip = intro_seen["prompts"][0]
check("the intro prompt is built from the FINISHED episode: its story list", "EU Kids Act" in ip and "YouTube Playables" in ip)
check("the intro prompt carries the stories' striking facts and the through-line",
      "EU Kids Act is a proposal" in ip and "Through-line" in ip)
check("the intro prompt asks for banter, a teaser, and a hand-over",
      "banter" in ip and "TEASER" in ip and "hands over" in ip)
check("the intro prompt bans invented personal anecdotes", "invent personal anecdotes" in ip)

# The teaser must lead with what the SCRIPT opens with, not the model's own list order
DATA_WITH_SCRIPT = dict(DATA, script=[
    {"speaker": A, "text": "השבוע AppLovin הגישה בקשה לצו מניעה נגד Unity בקליפורניה."},
    {"speaker": B, "text": "על מה היא מבססת את הטענה הזאת?"},
    {"speaker": A, "text": "TURN THREE MUST NOT APPEAR"},
])
P.write_intro(DATA_WITH_SCRIPT, prep)
ip2 = intro_seen["prompts"][-1]
check("the intro prompt states what the script actually opens with",
      "THE SCRIPT ACTUALLY OPENS WITH" in ip2 and "AppLovin הגישה בקשה לצו מניעה" in ip2)
check("...from the first two turns only", "TURN THREE MUST NOT APPEAR" not in ip2)
check("the intro prompt forbids naming a day of the week", "Never name a day of the week" in ip2)
check("a named weekday in the intro is a STYLE fault",
      any(p.startswith(P.STYLE) and "day of the week" in p
          for p in P.intro_problems([dict(t, text=t["text"] + " איזה כיף שהגענו ליום חמישי.")
                                     if i == 0 else t for i, t in enumerate(GOOD_INTRO["turns"])])))
check("'שני מיליון' (two million) is not mistaken for Monday",
      not any("day of the week" in p for p in P.intro_problems(
          [dict(t, text=t["text"] + " שני מיליון משתמשים.") if i == 0 else t
           for i, t in enumerate(GOOD_INTRO["turns"])])))

# intro_problems
check("a clean intro has no problems", P.intro_problems(GOOD_INTRO["turns"]) == [])
check("too few turns is a problem", any("turns" in p for p in P.intro_problems(GOOD_INTRO["turns"][:2])))
check("the first intro turn must be the analyst's reply",
      any("first turn" in p for p in P.intro_problems([GOOD_INTRO["turns"][1]] + GOOD_INTRO["turns"][1:])))
check("repeating the welcome is a problem",
      any("repeats the welcome" in p for p in P.intro_problems(
          GOOD_INTRO["turns"][:2] + [{"speaker": B, "text": "ברוכים הבאים שוב."}])))
check("a bloated intro is a problem",
      any("words" in p for p in P.intro_problems(
          [dict(t, text=words(120)) for t in GOOD_INTRO["turns"]])))

# style rules -- the first live run (2026-10-04) broke all of these despite
# the prompt asking otherwise: a generic "a week where..." opener with a hype
# word, two turns opening with an agreement word, three Dana turns in a row.
def _variant(i, **over):
    t = [dict(x) for x in GOOD_INTRO["turns"]]
    t[i].update(over)
    return t

check("a hype word in the intro is flagged as a style problem",
      any(p.startswith(P.STYLE) and "hype" in p
          for p in P.intro_problems(_variant(2, text="הסכומים מטורפים, ובכל זאת אף אחד לא שם לב."))))
check("an intro turn opening with an agreement word is flagged",
      any("agreement" in p for p in P.intro_problems(_variant(1, text="בדיוק, ידעתי שתתחיל משם."))))
check("a first reply about 'the week' is flagged -- the exact line the user complained about",
      any("about the week itself" in p for p in P.intro_problems(
          _variant(0, text="שבוע שבו המובייל ממשיך לגלגל סכומים."))))
three_dana = [GOOD_INTRO["turns"][0]] + [dict(GOOD_INTRO["turns"][1]) for _ in range(3)] + [GOOD_INTRO["turns"][4]]
check("three turns in a row by one host is flagged",
      any("more than two turns in a row" in p for p in P.intro_problems(three_dana)))
check("the 10-04 live intro (week opener, no greeting, hype, 2 agreement openers, 3 Dana in a row) trips every rule",
      len([p for p in P.intro_problems([
          {"speaker": B, "text": "אכן, דנה. שבוע שבו המובייל ממשיך לגלגל סכומים מטורפים, ואיפה הוא דורך."},
          {"speaker": A, "text": "בדיוק. ועם כל הכסף הזה שמסתובב, יש מי שרוצה נתח וגם מי שרוצה לווסת."},
          {"speaker": B, "text": "כמו תמיד, רק את יכולה לסדר את כל הבלגן הזה לסיפור קוהרנטי, ודאי."},
          {"speaker": A, "text": "ננסה, ננסה. בטח כשמדובר על מיליארדים של דולרים בשוק הזה כולו."},
          {"speaker": A, "text": "אז מה מחכה לנו השבוע? " + words(30)},
          {"speaker": A, "text": "נתחיל, כמובן, עם עדכון ההכנסות ועם כל מה שקשור בחנויות."},
      ] ) if p.startswith(P.STYLE)]) == 5)
check("the intro prompt carries the tone rules (hype ban, agreement limit, no week mood-line)",
      "No turn opens with an agreement word" in " ".join(ip.split())
      and "mood line about the week" in " ".join(ip.split()))

# ---- the 2026-10-05 production intro: cold open, banter built on an unexplained
# figure, a compound question in the teaser. The spec was the problem, not the model.
check("the intro prompt's most important rule: explain everything in the same breath",
      "THE RULE THAT MATTERS MOST" in ip and "explained in the same" in ip and "the listener has heard nothing yet" in ip)
check("the first reply must be a greeting, with a line that needs no context",
      "greets back" in ip and "needs no context" in ip)
check("the order puts the banter AFTER the teaser, so it reacts to something already explained",
      ip.index("answers in one short, friendly line") < ip.index("reacts in one short, human line"))
check("the prompt shows the shape with an example, marked as shape only",
      "EXAMPLE OF THE SHAPE ONLY" in ip and "never reuse its wording or" in ip)
check("compound questions in the teaser are forbidden",
      "never a compound question" in ip)

COLD_OPEN = [
    {"speaker": B, "text": "אני מודה שהמספר על Pix בברזיל הפתיע אותי מאוד. 96 אחוז מהמבוגרים."},
    {"speaker": A, "text": "אתה חושב שזה יגיע גם אלינו מתישהו?"},
    {"speaker": B, "text": "יש לנו מספיק כאב ראש גם בלי זה, דנה."},
    {"speaker": A, "text": words(70)},
    {"speaker": B, "text": "נראה אם זה מחזיק."},
]
check("the real cold open (no greeting, straight into an unexplained figure) is flagged",
      any("does not greet" in p for p in P.intro_problems(COLD_OPEN)))
fixed = [dict(t) for t in COLD_OPEN]
fixed[0]["text"] = "היי דנה, בוקר טוב."
check("a plain greeting passes that rule",
      not any("does not greet" in p for p in P.intro_problems(fixed)))
for g in ("שלום דנה.", "בוקר טוב, דנה.", "הי דנה, מה העניינים?"):
    fx = [dict(t) for t in COLD_OPEN]; fx[0]["text"] = g
    check(f"greeting accepted: {g}", not any("does not greet" in p for p in P.intro_problems(fx)))
check("more than two question marks in an intro is flagged",
      any("question marks" in p for p in P.intro_problems(
          [dict(t, text=t["text"] + " באמת? כן?") for t in fixed])))
check("a monologue turn over 110 words is flagged",
      any("monologue" in p for p in P.intro_problems(
          fixed[:3] + [{"speaker": A, "text": words(115)}] + fixed[4:])))
check("every one of those is a STYLE fault (retry, then accept), never a fallback",
      all(p.startswith(P.STYLE) for p in P.intro_problems(COLD_OPEN)
          if "greet" in p or "question marks" in p or "monologue" in p))

# style faults earn a retry, but are accepted if they persist (the fallback is worse)
hyped = {"turns": _variant(2, text="הסכומים מטורפים, ובכל זאת אף אחד לא שם לב לזה.")}
n_calls = []
P.gemini_json = lambda *a, **k: (n_calls.append(1) or hyped)
turns, source = P.write_intro(DATA, prep)
check("an intro that stays slightly hyped is still used after two attempts, not discarded",
      source == "model" and len(n_calls) == 2 and len(turns) == len(hyped["turns"]) + 1,
      f"source={source}, calls={len(n_calls)}")

# retry once, then fall back
attempts = []
def flaky(prompt, schema=None, **kw):
    attempts.append(prompt)
    return {"turns": GOOD_INTRO["turns"][:1]} if len(attempts) == 1 else GOOD_INTRO
P.gemini_json = flaky
turns, source = P.write_intro(DATA, prep)
check("a bad first intro is retried once, with the reason in the prompt",
      len(attempts) == 2 and "REJECTED because" in attempts[1] and source == "model")

attempts.clear()
P.gemini_json = lambda *a, **k: (attempts.append(1) or {"turns": GOOD_INTRO["turns"][:1]})
turns, source = P.write_intro(DATA, prep)
check("an intro that stays bad is capped at two attempts, then falls back",
      len(attempts) == 2 and source == "fallback")
check("the fallback is the welcome plus a one-line preview of the first stories",
      turns[0] == P.welcome_turn() and "EU Kids Act" in turns[1]["text"] and len(turns) == 2)

def down(*a, **k):
    raise RuntimeError("API down")
P.gemini_json = down
turns, source = P.write_intro(DATA, prep)
check("an API failure never raises out of write_intro -- it falls back", source == "fallback" and len(turns) == 2)
turns, source = P.write_intro({"digest_summary": []}, None)
check("with nothing to preview, the fallback is the welcome alone", turns == [P.welcome_turn()])

# add_intro
P.gemini_json = lambda *a, **k: GOOD_INTRO
d = P.add_intro({"digest_summary": DATA["digest_summary"], "script": [{"speaker": A, "text": "סיפור ראשון."}],
                 "prep": prep, "episode_title": "x"})
check("add_intro puts the intro in front of the stories",
      d["script"][0] == P.welcome_turn() and d["script"][-1]["text"] == "סיפור ראשון.")
check("add_intro marks the data so a resume never adds a second greeting",
      d["intro_done"] is True and d["intro_source"] == "model")

intro_agree = {"turns": [dict(t, text="בדיוק. " + t["text"]) for t in GOOD_INTRO["turns"]]}
P.gemini_json = lambda *a, **k: copy.deepcopy(intro_agree)
turns, source = P.write_intro(DATA, prep)
check("the intro is trimmed of every agreement opener, so it passes first time",
      source == "model" and not any(t["text"].startswith(P.script_quality.AGREEMENT_OPENERS) for t in turns[1:]))


# ---------------------------------------------------------------- 7. main(): the order, and when the checkpoint is written

def story_turns(n=16, tag="סיפור"):
    return [{"speaker": A if i % 2 == 0 else B, "text": f"{tag} מספר {i} בשיחה על הכתבה."} for i in range(n)]

order, saves, synth = [], [], []

def fake_all(prompt, schema=None, **kw):
    props = (schema or {}).get("properties", {})
    if "key_points" in props:
        order.append("story")
        return {"publishable": True, "stance": "NEUTRAL_REPORTING", "headline": "כותרת",
                "key_points": ["נקודה אחת", "נקודה שנייה"], "caution": "", "takeaway": "לבדוק משהו.",
                "script": story_turns()}
    if "issues" in props:
        order.append("review")
        return {"issues": []}
    if "episode_title" in props:
        order.append("frame")
        return {"episode_title": "פרק לדוגמה",
                "outro": [{"speaker": A, "text": "תודה שהאזנתם."}]}
    if "turns" in props:
        order.append("intro")
        fake_all.intro_prompt = prompt
        return GOOD_INTRO
    raise AssertionError("unexpected schema")

def setup_env(tmp_path, fake):
    saved = (CP.CHECKPOINT_DIR, P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR,
             P.synthesize_audio, P.get_duration, P.build_feed, P.gemini_json,
             P.memory.append_entry, P.memory.load_recent_context, CP.save_content)
    CP.CHECKPOINT_DIR = tmp_path / ".checkpoint"
    P.EPISODES, P.DIGESTS, P.ROOT = tmp_path / "ep", tmp_path / "dg", tmp_path
    P.STATE_FILE, P.ASSETS_DIR = tmp_path / "state.json", tmp_path / "assets"
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    P.gemini_json = fake
    def fake_synthesize(script, _wav, mp3_path, **_kw):
        synth.append(list(script))
        pathlib.Path(mp3_path).write_bytes(b"fake MP3")
    P.synthesize_audio = fake_synthesize
    # A plausible speaking rate (150 words/min) for whatever script was voiced.
    P.get_duration = lambda *_a, **_k: 7 + int(sum(len(x["text"].split()) for x in synth[-1]) * 0.4)
    P.build_feed = lambda: (P.ROOT / "feed.xml").write_text(f"bens-digest-{today}</guid>")
    P.memory.append_entry = lambda *_a, **_k: None
    P.memory.load_recent_context = lambda *_a, **_k: ""
    return saved, today

def restore_env(saved):
    (CP.CHECKPOINT_DIR, P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR,
     P.synthesize_audio, P.get_duration, P.build_feed, P.gemini_json,
     P.memory.append_entry, P.memory.load_recent_context, CP.save_content) = saved

import discovery as _D
_real_select = _D.select

with tempfile.TemporaryDirectory() as tmp:
    saved, today = setup_env(pathlib.Path(tmp), fake_all)
    real_save = CP.save_content
    persisted = []

    def _recording_save(date, arts, data):
        saves.append("script" if data.get("script") else "segments:%d" % len(data.get("segments") or {}))
        if data.get("script"):
            saves[-1] += ":intro" if data.get("intro_done") else ":no-intro"
        real_save(date, arts, data)
        # The 2026-10-04 live run printed "could not save content (Object of
        # type date is not JSON serializable)" twice and still went green --
        # a save that fails quietly looks exactly like one that worked unless
        # the test reads the file back. The articles here carry real dates.
        persisted.append(CP.load_content(date) is not None)

    CP.save_content = _recording_save
    _D.select = lambda *a, **k: [dict(a_) for a_ in ARTICLES]
    os.environ.pop("FORCE_REGENERATE", None)

    try:
        rc = P.main()
        check("main() completes with the story, frame and intro stages", rc == 0)
        check("the stages run in the designed order: every story and its review, then the frame, then the intro",
              order[-2:] == ["frame", "intro"] and order.count("story") == 3 and order.count("review") == 3
              and order.index("frame") > max(i for i, o in enumerate(order) if o in ("story", "review")),
              str(order))
        check("the intro prompt was built from the finished stories' list",
              "כותרת" in fake_all.intro_prompt)
        check("the checkpoint is saved after selection, as each story finishes, after the script "
              "(intro owed), then after the intro",
              saves[:4] == ["segments:0", "segments:1", "segments:2", "segments:3"]
              and saves[4:] == ["script:no-intro", "script:intro"], str(saves))
        check("...and every save really landed on disk, with real date-bearing articles",
              all(persisted), str(persisted))
        final = synth[0] if synth else []
        check("the audio script opens with the fixed welcome, then the model's intro, then the stories",
              final and final[0] == P.welcome_turn() and final[1] == GOOD_INTRO["turns"][0]
              and final[len(GOOD_INTRO["turns"]) + 1]["text"].startswith("סיפור"),
              str(final[:2]))
        check("the script ends with the frame's outro", final and final[-1]["text"] == "תודה שהאזנתם.")
        script_md = P.DIGESTS / f"{today}-script.md"
        check("the saved script file starts with the welcome line",
              script_md.exists() and script_md.read_text(encoding="utf-8").startswith(A + ": ברוכים הבאים"))
        report = P.ROOT / "reports" / f"{today}-editorial.json"
        check("the per-story review is written to reports/", report.exists())

        # A resumed run with two of three stories written pays for the third only.
        order.clear(); saves.clear(); synth.clear()
        for f in P.EPISODES.glob("*"):
            f.unlink()
        done = {str(i): {"status": "ok", "publishable": True, "stance": "NEUTRAL_REPORTING",
                         "headline": "כותרת", "key_points": ["נקודה"], "caution": "",
                         "takeaway": "משהו.", "script": story_turns(16, "ישן"), "review": []}
                for i in (0, 1)}
        CP.save_content(today, ARTICLES, {"segments": done})
        P.main()
        check("a resumed run with two stories done writes only the third (plus its review, the frame, the intro)",
              order == ["story", "review", "frame", "intro"], str(order))

        # A resumed run that still owes the intro writes ONLY the intro.
        order.clear(); saves.clear(); synth.clear()
        for f in P.EPISODES.glob("*"):
            f.unlink()
        CP.save_content(today, ARTICLES, {"episode_title": "T", "script": story_turns(10),
                                          "kept": [0, 1, 2], "prep": prep, "intro_done": False,
                                          "digest_summary": [{"title": "t", "key_takeaway": "k"}]})
        P.main()
        check("a resumed run that still owes the intro makes exactly one text call: the intro",
              order == ["intro"], str(order))

        # A legacy checkpoint (no intro_done key) already has its greeting.
        order.clear(); synth.clear()
        for f in P.EPISODES.glob("*"):
            f.unlink()
        CP.save_content(today, ARTICLES, {"episode_title": "T", "script": story_turns(10),
                                          "digest_summary": [{"title": "t", "key_takeaway": "k"}]})
        P.main()
        check("a legacy checkpoint without the key is never given a second greeting",
              order == [] and synth and synth[0][0]["text"] != P.welcome_turn()["text"], str(order))
    except Exception as e:                                  # noqa: BLE001
        import traceback; traceback.print_exc()
        check("main() runs the story/frame/intro flow", False, f"{type(e).__name__}: {e}")
    finally:
        _D.select = _real_select
        restore_env(saved)

# ---------------------------------------------------------------- 8. a story cut in review is not published

FOUR = ARTICLES + [dict(ARTICLES[0], url="https://example.test/fourth", title="A fourth story")]
reviews = {"n": 0}
def one_bad_story(prompt, schema=None, **kw):
    props = (schema or {}).get("properties", {})
    if "issues" in props and "A fourth story" in prompt:
        return {"issues": [{"turn": 1, "kind": "FACT", "blocking": True,
                            "problem": "invented number", "fix": "remove it"}]}
    return fake_all(prompt, schema, **kw)

order.clear(); synth.clear()
with tempfile.TemporaryDirectory() as tmp:
    saved, today = setup_env(pathlib.Path(tmp), one_bad_story)
    _D.select = lambda *a, **k: [dict(a_) for a_ in FOUR]
    try:
        rc = P.main()
        check("a story that stays factually wrong after one rewrite is cut, the episode still publishes",
              rc == 0 and bool(synth))
        meta = __import__("json").loads((P.EPISODES / f"{today}.json").read_text(encoding="utf-8"))
        check("...and the cut story is not credited in the published sources",
              [s["title"] for s in meta["sources"]] == [a["title"] for a in ARTICLES], str(meta["sources"]))
    except Exception as e:                                  # noqa: BLE001
        import traceback; traceback.print_exc()
        check("a cut story must not fail the run", False, f"{type(e).__name__}: {e}")
    finally:
        _D.select = _real_select
        restore_env(saved)

# ---------------------------------------------------------------- 9. the frame and the intro failing still produce an episode

order2 = []
def stories_only(prompt, schema=None, **kw):
    props = (schema or {}).get("properties", {})
    if "episode_title" in props or "turns" in props:
        order2.append("failed:" + ("frame" if "episode_title" in props else "intro"))
        raise RuntimeError("503 simulated")
    return fake_all(prompt, schema, **kw)

order.clear(); synth.clear()
with tempfile.TemporaryDirectory() as tmp:
    saved, today = setup_env(pathlib.Path(tmp), stories_only)
    _D.select = lambda *a, **k: [dict(a_) for a_ in ARTICLES]
    _real_sleep = P.time.sleep
    P.time.sleep = lambda *_a, **_k: None
    try:
        rc = P.main()
        check("frame AND intro failing still produces a finished episode", rc == 0 and bool(synth))
        check("...each optional stage was tried and failed",
              "failed:frame" in order2 and "failed:intro" in order2, str(order2))
        check("...the episode still opens with the fixed welcome (the fallback intro)",
              synth and synth[0][0] == P.welcome_turn())
        check("...and closes with the plain sign-off (the fallback outro)",
              synth and "ונתראה" in synth[0][-1]["text"], str(synth[0][-1] if synth else ""))
    except Exception as e:                                  # noqa: BLE001
        import traceback; traceback.print_exc()
        check("a failing optional stage must never fail the run", False, f"{type(e).__name__}: {e}")
    finally:
        P.time.sleep = _real_sleep
        _D.select = _real_select
        restore_env(saved)

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
