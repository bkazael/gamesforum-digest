#!/usr/bin/env python3
"""
Offline tests for the reordered episode generation: editorial prep, then the
script, then the intro written LAST (gamesforum_pipeline.py).

Zero network, zero Gemini calls: gemini_json is replaced throughout and told
apart by which SCHEMA it is handed, so these tests also prove each stage asks
for the schema it is supposed to.

What has to hold, and why each is worth a test:
  - The ORDER: prep -> script -> intro, with the script prompt built from the
    prep and the intro prompt built from the finished script. That ordering
    is the whole design; if a later edit quietly moved the intro back in
    front, every other test here would still pass.
  - Every new stage degrades instead of failing the run. A new stage that can
    take down Monday's episode is a regression no matter how good its output
    is when it works.
  - The checkpoint is written after the script AND after the intro, so a hard
    kill in between never costs the script, and a resumed run never gets a
    second greeting.
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


print("\n--- Testing editorial prep / script / intro-last ---")

ARTICLES = [
    {"url": "https://example.test/eu-kids", "title": "Daily log-in bonuses under threat in EU Kids Act",
     "source": "PG", "text": "EU Kids Act text. " * 30, "_airtime": 0.40, "published": dt.date(2026, 10, 2)},
    {"url": "https://example.test/youtube", "title": "YouTube rolls out Playables to 50 markets",
     "source": "MG", "text": "Playables text. " * 30, "_airtime": 0.25, "published": dt.date(2026, 10, 2)},
    {"url": "https://example.test/tyrads", "title": "TyrAds and Nordeus partner on rewarded engagement",
     "source": "GF", "text": "Partnership text. " * 30, "_airtime": 0.35, "published": dt.date(2026, 10, 2)},
]

# ---------------------------------------------------------------- 1. _clean_prep

raw = {
    "articles": [
        {"id": 1, "stance": "NEUTRAL_REPORTING", "key_points": ["a", "  ", "b"], "caution": "c", "takeaway": "t"},
        {"id": 1, "stance": "NEUTRAL_REPORTING", "key_points": ["dup"], "caution": "", "takeaway": ""},
        {"id": 9, "stance": "NEUTRAL_REPORTING", "key_points": ["out of range"], "caution": "", "takeaway": ""},
        {"id": "x", "stance": "NEUTRAL_REPORTING", "key_points": [], "caution": "", "takeaway": ""},
        {"id": 3, "stance": "SOMETHING_ELSE", "key_points": ["p"], "caution": "", "takeaway": ""},
    ],
    "questions": [
        {"question": "Answered?", "about": [1, 7], "answer_basis": "ARTICLE", "answer": "Yes, per article 1."},
        {"question": "Claims answered but says nothing", "about": [2], "answer_basis": "ARTICLE", "answer": ""},
        {"question": "Open one", "about": [3], "answer_basis": "OPEN", "answer": "invented anyway"},
        {"question": "Third open", "about": [], "answer_basis": "OPEN", "answer": ""},
        {"question": "   ", "about": [], "answer_basis": "ARTICLE", "answer": "x"},
    ],
    "through_line": "  regulation  ",
}
clean = P._clean_prep(raw, len(ARTICLES))
check("duplicate, out-of-range and non-numeric article ids are dropped",
      [a["id"] for a in clean["articles"]] == [1, 3], str([a["id"] for a in clean["articles"]]))
check("blank key points are dropped", clean["articles"][0]["key_points"] == ["a", "b"])
check("an unknown stance falls back to NEUTRAL_REPORTING",
      clean["articles"][1]["stance"] == "NEUTRAL_REPORTING")
check("an 'answered' question with no answer text is demoted to OPEN",
      clean["questions"][1]["answer_basis"] == "OPEN" and clean["questions"][1]["answer"] == "")
check("an OPEN question never keeps an answer the model invented anyway",
      clean["questions"][2]["answer"] == "")
check("at most MAX_OPEN_QUESTIONS open questions survive",
      sum(1 for q in clean["questions"] if q["answer_basis"] == "OPEN") == P.MAX_OPEN_QUESTIONS)
check("a blank question is dropped", all(q["question"].strip() for q in clean["questions"]))
check("'about' keeps only real article numbers", clean["questions"][0]["about"] == [1])
check("the through-line is trimmed", clean["through_line"] == "regulation")

many = {"articles": [], "questions": [
    {"question": f"q{i}", "about": [], "answer_basis": "ARTICLE", "answer": "a"} for i in range(20)
], "through_line": ""}
check("questions are capped at MAX_QUESTIONS",
      len(P._clean_prep(many, 3)["questions"]) == P.MAX_QUESTIONS)

# ---------------------------------------------------------------- 2. prepare_episode

GOOD_PREP = {
    "articles": [
        {"id": 1, "stance": "NEUTRAL_REPORTING", "key_points": ["EU Kids Act is a proposal, not yet law"],
         "caution": "status unclear", "takeaway": "audit which of your games use streaks"},
        {"id": 2, "stance": "NEUTRAL_REPORTING", "key_points": ["Playables in 50 markets"],
         "caution": "", "takeaway": "watch the IAP pilot in 2027"},
        {"id": 3, "stance": "VENDOR_MARKETING", "key_points": ["partnership announced, no results"],
         "caution": "no outcome figures", "takeaway": "ask for retention data before believing it"},
    ],
    "questions": [
        {"question": "Does the EU act cover login streaks for adults?", "about": [1],
         "answer_basis": "ARTICLE", "answer": "Only for minors, per the article."},
        {"question": "When would YouTube IAP launch?", "about": [2],
         "answer_basis": "OPEN", "answer": ""},
    ],
    "through_line": "platforms are being pushed to prove who is a child",
}

seen = {}
def fake_prep(prompt, schema=None, **kw):
    seen["prompt"], seen["schema"], seen["kw"] = prompt, schema, kw
    return GOOD_PREP

P.gemini_json = fake_prep
prep = P.prepare_episode(ARTICLES, memory_context="- 2026-09-22: something earlier")
check("prepare_episode asks for PREP_SCHEMA", seen.get("schema") is P.PREP_SCHEMA)
check("prepare_episode is bounded: 2 tries and a short timeout, not a whole run's budget",
      seen["kw"].get("tries") == 2 and seen["kw"].get("timeout") and seen["kw"]["timeout"] <= 300,
      str(seen.get("kw")))
check("the prep prompt contains every article's text", all(a["title"] in seen["prompt"] for a in ARTICLES))
check("the prep prompt forbids inventing answers", "never invent an answer" in seen["prompt"])
check("previous episodes reach the prep prompt, with the vendor-name guard",
      "something earlier" in seen["prompt"] and "must not name a company" in seen["prompt"])
check("a good response is returned cleaned", prep and len(prep["articles"]) == 3 and len(prep["questions"]) == 2)

P.prepare_episode.__globals__["gemini_json"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503"))
check("a failing prep returns None instead of raising",
      P.prepare_episode(ARTICLES) is None)
P.gemini_json = lambda *a, **k: {"articles": [], "questions": [], "through_line": ""}
check("a prep with no usable articles is ignored (None)", P.prepare_episode(ARTICLES) is None)

# ---------------------------------------------------------------- 3. _format_prep

block = P._format_prep(prep, ARTICLES)
check("the briefing marks each article's stance", "[stance: VENDOR_MARKETING]" in block)
check("the briefing lists answered questions with their basis",
      "answer [ARTICLE]: Only for minors" in block)
check("an unanswerable question is marked OPEN in the briefing", "OPEN" in block)
check("the through-line is included", "THROUGH-LINE" in block)

# ---------------------------------------------------------------- 4. the script prompt

script_resp = {"episode_title": "T",
               "digest_summary": [{"title": a["title"], "key_takeaway": "k", "metrics_mentioned": []}
                                  for a in ARTICLES],
               "script": [{"speaker": A if i % 2 == 0 else B, "text": words(330)} for i in range(4)]}
prompts = []
def fake_script(prompt, schema=None, **kw):
    prompts.append(prompt)
    return copy.deepcopy(script_resp)

P.gemini_json = fake_script
P.generate_podcast_content(ARTICLES, "2026-10-05", prep=prep)
sp = prompts[0]
check("with a prep, the script prompt carries the EDITORIAL PREP", "EDITORIAL PREP" in sp)
check("the script prompt tells the model there is no intro in its part",
      "NO INTRO" in sp and "recorded separately" in sp)
check("the old fixed greeting is no longer part of the script's instructions",
      "opens warmly" not in sp)
check("airtime reaches the script prompt (it never did before)",
      "airtime: ~40%" in sp and "airtime: ~25%" in sp and "airtime: ~35%" in sp)
check("a vendor/company source must be attributed, not endorsed", "ATTRIBUTED, never" in sp)
check("open questions must be said to be open, never invented", "OPEN" in sp and "never answered with something invented" in sp)
check("the hype ban and the agreement-opener limit are in the prompt",
      "no stacked" in sp.lower() or "stacked" in sp.lower())
check("a question may not be left hanging", "never left" in sp and "hanging" in sp)

prompts.clear()
P.generate_podcast_content(ARTICLES, "2026-10-05", prep=None)
check("with no prep there is no EDITORIAL PREP block, and the script is still asked for",
      "EDITORIAL PREP" not in prompts[0] and "NO INTRO" in prompts[0])

# ---------------------------------------------------------------- 5. the word floor shares the single retry

short = dict(script_resp, script=[{"speaker": A, "text": words(200)}, {"speaker": B, "text": words(200)}])
long_ = script_resp
queue = [short, long_]
prompts.clear()
def fake_floor(prompt, schema=None, **kw):
    prompts.append(prompt)
    return copy.deepcopy(queue[min(len(prompts), len(queue)) - 1])
P.gemini_json = fake_floor
d = P.generate_podcast_content(ARTICLES, "2026-10-05", prep=prep)
check("a script under the word floor gets exactly one retry", len(prompts) == 2, f"{len(prompts)} calls")
check("the retry prompt says it was too short", "only" in prompts[1] and "words" in prompts[1])
check("the longer second attempt is what comes back", P.script_quality.count_words(d["script"]) >= P.SCRIPT_FLOOR_WORDS)

prompts.clear()
queue = [short]
P.gemini_json = fake_floor
d = P.generate_podcast_content(ARTICLES, "2026-10-05", prep=prep)
check("a script that stays short is still accepted after the one retry (never an exception)",
      len(prompts) == 2 and bool(d["script"]))

prompts.clear()
queue = [short]
P.gemini_json = fake_floor
P.generate_podcast_content(ARTICLES, "2026-10-05", target_words=150)
check("the live-smoke path (target_words) is exempt from the word floor", len(prompts) == 1)

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
      intro_seen["schema"] is P.INTRO_SCHEMA and intro_seen["kw"].get("tries") == 2)
ip = intro_seen["prompts"][0]
check("the intro prompt is built from the FINISHED episode: its story list", "EU Kids Act" in ip and "YouTube Playables" in ip)
check("the intro prompt carries the prep's striking facts and questions",
      "EU Kids Act is a proposal" in ip and "Does the EU act cover login streaks" in ip)
check("the intro prompt asks for banter, a teaser, and a hand-over",
      "banter" in ip and "TEASER" in ip and "hands over" in ip)
check("the intro prompt bans invented personal anecdotes", "invent personal anecdotes" in ip)

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

# ---------------------------------------------------------------- 6b. enforcement the prompt alone could not hold
#
# 2026-10-04: six of 36 turns opened with an agreement word against a limit of
# two (09-15 had thirteen of 35). A retry costs a whole Gemini request, so the
# limit is enforced deterministically instead.

AGREE = ["בדיוק.", "בהחלט,", "נכון.", "לגמרי,", "אכן.", "בדיוק,"]
agree_script = dict(script_resp, script=[
    {"speaker": A if i % 2 == 0 else B,
     "text": f"{w} כך שהמפתחים צריכים לבדוק את כל הנתונים האלה ואת ההשלכות שלהם. " + words(300)}
    for i, w in enumerate(AGREE)
])
P.gemini_json = lambda *a, **k: copy.deepcopy(agree_script)
d = P.generate_podcast_content(ARTICLES, "2026-10-05", prep=prep)
opening = sum(1 for t in d["script"] if t["text"].startswith(P.script_quality.AGREEMENT_OPENERS))
check("generate_podcast_content enforces the agreement-opener limit (2) without a retry",
      opening == 2, f"{opening} turns still open with an agreement word")

intro_agree = {"turns": [dict(t, text="בדיוק. " + t["text"]) for t in GOOD_INTRO["turns"]]}
P.gemini_json = lambda *a, **k: copy.deepcopy(intro_agree)
turns, source = P.write_intro(DATA, prep)
check("the intro is trimmed of every agreement opener, so it passes first time",
      source == "model" and not any(t["text"].startswith(P.script_quality.AGREEMENT_OPENERS) for t in turns[1:]))

# prompt rules added after the first full real episode
P.gemini_json = fake_script
prompts.clear()
P.generate_podcast_content(ARTICLES, "2026-10-05", prep=prep, memory_context="- 2026-09-22 (x): earlier")
sp = prompts[0]
check("the script prompt forbids claiming an earlier episode covered something it didn't",
      "ONLY if it appears in the" in sp and "as we remember from previous" in sp)
prompts.clear()
P.generate_podcast_content(ARTICLES, "2026-10-05", prep=prep, memory_context="")
check("with no history the prompt says not to refer to earlier episodes at all, "
      "and never mentions a PREVIOUS EPISODES block that isn't there",
      "Do not refer to earlier episodes at all" in prompts[0] and "PREVIOUS EPISODES" not in prompts[0])
check("the script prompt caps turn length and asks for plain questions",
      "No turn longer than about 80 words" in sp and "compound interview question" in sp)
check("the script prompt stops every story ending on the same 'developers should' line",
      "at most\n  two stories may end on an explicit" in sp.replace("  two", "\n  two") or "two stories may end on an explicit" in sp)

# ---------------------------------------------------------------- 6c. which model writes what
#
# Scoring, dedupe and editorial prep are classification/extraction -- the fast
# model is the right tool. The script and the intro are writing, where a
# stronger model is the lever for how natural the Hebrew sounds, so those two
# (and only those) take GEMINI_CREATIVE_MODEL. Unset, it is GEMINI_TEXT_MODEL
# and nothing changes.

check("with GEMINI_CREATIVE_MODEL unset, the creative model IS the text model",
      os.environ.get("GEMINI_CREATIVE_MODEL") or P.GEMINI_CREATIVE_MODEL == P.GEMINI_TEXT_MODEL)

kws = {}
def route(prompt, schema=None, **kw):
    kws[("prep" if schema is P.PREP_SCHEMA else "intro" if schema is P.INTRO_SCHEMA else "script")] = kw
    if schema is P.PREP_SCHEMA:
        return GOOD_PREP
    if schema is P.INTRO_SCHEMA:
        return GOOD_INTRO
    return copy.deepcopy(script_resp)

_real_creative = P.GEMINI_CREATIVE_MODEL
P.GEMINI_CREATIVE_MODEL = "gemini-creative-test"
P.gemini_json = route
P.prepare_episode(ARTICLES)
P.generate_podcast_content(ARTICLES, "2026-10-05", prep=prep)
P.write_intro(DATA, prep)
P.GEMINI_CREATIVE_MODEL = _real_creative
check("the script call is sent to the creative model", kws["script"].get("model") == "gemini-creative-test", str(kws["script"]))
check("the intro call is sent to the creative model", kws["intro"].get("model") == "gemini-creative-test", str(kws["intro"]))
check("editorial prep stays on the default (fast) model", "model" not in kws["prep"], str(kws["prep"]))

# ---------------------------------------------------------------- 7. main(): the order, and when the checkpoint is written

order, saves, synth = [], [], []

def fake_all(prompt, schema=None, **kw):
    if schema is P.PREP_SCHEMA:
        order.append("prep")
        return GOOD_PREP
    if schema is P.PODCAST_SCHEMA:
        order.append("script")
        fake_all.script_prompt = prompt
        return copy.deepcopy(script_resp)
    if schema is P.INTRO_SCHEMA:
        order.append("intro")
        fake_all.intro_prompt = prompt
        return GOOD_INTRO
    raise AssertionError("unexpected schema")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = pathlib.Path(tmp)
    saved = (CP.CHECKPOINT_DIR, P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR,
             P.synthesize_audio, P.get_duration, P.build_feed, P.gemini_json,
             P.memory.append_entry, P.memory.load_recent_context, CP.save_content)
    CP.CHECKPOINT_DIR = tmp_path / ".checkpoint"
    P.EPISODES, P.DIGESTS, P.ROOT = tmp_path / "ep", tmp_path / "dg", tmp_path
    P.STATE_FILE, P.ASSETS_DIR = tmp_path / "state.json", tmp_path / "assets"
    P.gemini_json = fake_all
    P.synthesize_audio = lambda script, *a, **k: synth.append(list(script))
    P.get_duration = lambda *_a, **_k: 600
    P.build_feed = lambda *_a, **_k: None
    P.memory.append_entry = lambda *_a, **_k: None
    P.memory.load_recent_context = lambda *_a, **_k: ""
    real_save = CP.save_content
    persisted = []

    def _recording_save(date, arts, data):
        saves.append(data.get("intro_done"))
        real_save(date, arts, data)
        # The 2026-10-04 live run printed "could not save content (Object of
        # type date is not JSON serializable)" twice and still went green --
        # a save that fails quietly looks exactly like one that worked unless
        # the test reads the file back. The articles here carry real dates.
        persisted.append(CP.load_content(date) is not None)

    CP.save_content = _recording_save
    import discovery as _D
    _real_select = _D.select
    _D.select = lambda *a, **k: [dict(a_) for a_ in ARTICLES]
    os.environ.pop("FORCE_REGENERATE", None)

    try:
        rc = P.main()
        check("main() completes with all three text stages", rc == 0)
        check("the stages run in the designed order: prep, then script, then intro",
              order == ["prep", "script", "intro"], str(order))
        check("the script prompt was built from the prep", "EDITORIAL PREP" in fake_all.script_prompt)
        check("the intro prompt was built from the finished script's story list",
              ARTICLES[0]["title"] in fake_all.intro_prompt)
        check("the checkpoint is saved after the script (intro still owed), then after the intro",
              saves == [False, True], str(saves))
        check("...and both saves really landed on disk, with real date-bearing articles",
              persisted == [True, True], str(persisted))
        final = synth[0] if synth else []
        check("the audio script opens with the fixed welcome, then the model's intro, then the stories",
              final and final[0] == P.welcome_turn() and final[1] == GOOD_INTRO["turns"][0]
              and final[len(GOOD_INTRO["turns"]) + 1]["text"].startswith("מילה"),
              str(final[:2]))
        script_md = (P.DIGESTS / f"{pathlib.Path(str(P.DIGESTS)).name and __import__('datetime').date.today().isoformat()}-script.md")
        check("the saved script file starts with the welcome line",
              script_md.exists() and script_md.read_text(encoding="utf-8").startswith(A + ": ברוכים הבאים"))
        digest_md = (P.DIGESTS / f"{__import__('datetime').date.today().isoformat()}.md").read_text(encoding="utf-8")
        check("the digest shows the discussion questions, answered and open",
              "Discussion questions" in digest_md and "Only for minors" in digest_md and "Open:" in digest_md)

        # A resumed run that still owes the intro writes ONLY the intro.
        order.clear(); saves.clear(); synth.clear()
        for f in (P.EPISODES.glob("*")):
            f.unlink()
        today = __import__("datetime").date.today().isoformat()
        CP.save_content(today, ARTICLES, dict(script_resp, prep=prep, intro_done=False))
        saves.clear()
        P.main()
        check("a resumed run that still owes the intro makes exactly one text call: the intro",
              order == ["intro"], str(order))

        # A legacy checkpoint (no intro_done key) already has its greeting.
        order.clear(); synth.clear()
        for f in (P.EPISODES.glob("*")):
            f.unlink()
        CP.save_content(today, ARTICLES, dict(script_resp))
        P.main()
        check("a legacy checkpoint without the key is never given a second greeting",
              order == [] and synth and synth[0][0]["text"] != P.welcome_turn()["text"], str(order))
    except Exception as e:                                  # noqa: BLE001
        import traceback; traceback.print_exc()
        check("main() runs the three-stage flow", False, f"{type(e).__name__}: {e}")
    finally:
        _D.select = _real_select
        (CP.CHECKPOINT_DIR, P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR,
         P.synthesize_audio, P.get_duration, P.build_feed, P.gemini_json,
         P.memory.append_entry, P.memory.load_recent_context, CP.save_content) = saved

# ---------------------------------------------------------------- 8. main(): both new stages failing still produces an episode

order2 = []
def only_script(prompt, schema=None, **kw):
    if schema is P.PODCAST_SCHEMA:
        order2.append("script")
        return copy.deepcopy(script_resp)
    order2.append("failed:" + ("prep" if schema is P.PREP_SCHEMA else "intro"))
    raise RuntimeError("503 simulated")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = pathlib.Path(tmp)
    saved = (CP.CHECKPOINT_DIR, P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR,
             P.synthesize_audio, P.get_duration, P.build_feed, P.gemini_json,
             P.memory.append_entry, P.memory.load_recent_context)
    CP.CHECKPOINT_DIR = tmp_path / ".checkpoint"
    P.EPISODES, P.DIGESTS, P.ROOT = tmp_path / "ep", tmp_path / "dg", tmp_path
    P.STATE_FILE, P.ASSETS_DIR = tmp_path / "state.json", tmp_path / "assets"
    P.gemini_json = only_script
    synth2 = []
    P.synthesize_audio = lambda script, *a, **k: synth2.append(list(script))
    P.get_duration = lambda *_a, **_k: 600
    P.build_feed = lambda *_a, **_k: None
    P.memory.append_entry = lambda *_a, **_k: None
    P.memory.load_recent_context = lambda *_a, **_k: ""
    _D.select = lambda *a, **k: [dict(a_) for a_ in ARTICLES]
    try:
        rc = P.main()
        check("prep AND intro failing still produces a finished episode", rc == 0 and bool(synth2))
        check("...the script stage ran, and each optional stage was tried and failed",
              order2[0].startswith("failed:prep") and "script" in order2 and order2[-1].startswith("failed:intro"),
              str(order2))
        check("...and the episode still opens with the fixed welcome (the fallback intro)",
              synth2 and synth2[0][0] == P.welcome_turn())
    except Exception as e:                                  # noqa: BLE001
        check("a failing optional stage must never fail the run", False, f"{type(e).__name__}: {e}")
    finally:
        _D.select = _real_select
        (CP.CHECKPOINT_DIR, P.EPISODES, P.DIGESTS, P.ROOT, P.STATE_FILE, P.ASSETS_DIR,
         P.synthesize_audio, P.get_duration, P.build_feed, P.gemini_json,
         P.memory.append_entry, P.memory.load_recent_context) = saved

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
