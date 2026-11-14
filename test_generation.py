#!/usr/bin/env python3
"""
Offline tests for the intro (write_intro / add_intro): its rules, its retries and its
fallbacks. The stories are tested in test_segments.py and the week's flow, day by day,
in test_stages.py. Zero network, zero Gemini calls.
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
import llm

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

llm.gemini_json = fake_intro
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
llm.gemini_json = lambda *a, **k: (n_calls.append(1) or hyped)
turns, source = P.write_intro(DATA, prep)
check("an intro that stays slightly hyped is still used after two attempts, not discarded",
      source == "model" and len(n_calls) == 2 and len(turns) == len(hyped["turns"]) + 1,
      f"source={source}, calls={len(n_calls)}")

# retry once, then fall back
attempts = []
def flaky(prompt, schema=None, **kw):
    attempts.append(prompt)
    return {"turns": GOOD_INTRO["turns"][:1]} if len(attempts) == 1 else GOOD_INTRO
llm.gemini_json = flaky
turns, source = P.write_intro(DATA, prep)
check("a bad first intro is retried once, with the reason in the prompt",
      len(attempts) == 2 and "REJECTED because" in attempts[1] and source == "model")

attempts.clear()
llm.gemini_json = lambda *a, **k: (attempts.append(1) or {"turns": GOOD_INTRO["turns"][:1]})
turns, source = P.write_intro(DATA, prep)
check("an intro that stays bad is capped at two attempts, then falls back",
      len(attempts) == 2 and source == "fallback")
check("the fallback is the welcome plus a one-line preview of the first stories",
      turns[0] == P.welcome_turn() and "EU Kids Act" in turns[1]["text"] and len(turns) == 2)

def down(*a, **k):
    raise RuntimeError("API down")
llm.gemini_json = down
turns, source = P.write_intro(DATA, prep)
check("an API failure never raises out of write_intro -- it falls back", source == "fallback" and len(turns) == 2)
turns, source = P.write_intro({"digest_summary": []}, None)
check("with nothing to preview, the fallback is the welcome alone", turns == [P.welcome_turn()])

# add_intro
llm.gemini_json = lambda *a, **k: GOOD_INTRO
d = P.add_intro({"digest_summary": DATA["digest_summary"], "script": [{"speaker": A, "text": "סיפור ראשון."}],
                 "prep": prep, "episode_title": "x"})
check("add_intro puts the intro in front of the stories",
      d["script"][0] == P.welcome_turn() and d["script"][-1]["text"] == "סיפור ראשון.")
check("add_intro marks the data so a resume never adds a second greeting",
      d["intro_done"] is True and d["intro_source"] == "model")

intro_agree = {"turns": [dict(t, text="בדיוק. " + t["text"]) for t in GOOD_INTRO["turns"]]}
llm.gemini_json = lambda *a, **k: copy.deepcopy(intro_agree)
turns, source = P.write_intro(DATA, prep)
check("the intro is trimmed of every agreement opener, so it passes first time",
      source == "model" and not any(t["text"].startswith(P.script_quality.AGREEMENT_OPENERS) for t in turns[1:]))


print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
