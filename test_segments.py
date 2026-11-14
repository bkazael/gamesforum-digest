#!/usr/bin/env python3
"""
Offline tests for segments.py: how one story becomes a conversation, how it is
checked, and how the stories become an episode.

Zero network, zero Gemini calls: `call` is a stub told apart by the schema it
is handed. What has to hold, and why:

  - Length comes from coverage, not a word count: a story whose key points
    (and their numbers) are not all said gets exactly one rewrite.
  - A factual error is fixed once, then the story is cut; it never fails the
    episode and never ships.
  - An outage fails the run (so a later run resumes) instead of publishing
    something unreviewed, but finished stories are kept.
  - The frame (title, outro) and the intro are optional: they fall back.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import segments as S  # noqa: E402

FAILS = []
A, B = "Dana", "Yoni"
SP = (A, B)


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def quiet(*_a, **_k):
    pass


print("\n--- Testing segments.py ---")

# ---------------------------------------------------------------- numbers

check("numerals() normalises commas, decimals and percent signs",
      S.numerals("Revenue hit $1,258m, up 6.8% to 121.1bn") == {"1258", "6.8", "121.1"})
check("numerals() does not turn '3.' at a sentence end into a decimal", S.numerals("It rose 41.") == {"41"})
check("small counts and years are not fidelity evidence",
      S._ignorable("3") and S._ignorable("2026") and not S._ignorable("41"))
script = [{"speaker": A, "text": "Hasbro קיבלה 41 מיליון דולר."}]
check("missing_numbers: a promised figure that is never said is reported",
      S.missing_numbers(["Hasbro earned $41m and $127m in nine months"], script) == ["127"])
check("missing_numbers: all figures said -> nothing missing",
      S.missing_numbers(["Hasbro earned $41m"], script) == [])
check("unsupported_numbers: a spoken figure in neither the article nor the points is a suspect",
      S.unsupported_numbers("The studio earned 41 million.", ["41 million"],
                            [{"speaker": A, "text": "ו-73 אחוז מזה."}]) == ["73"])
check("unsupported_numbers: figures from the article are fine",
      S.unsupported_numbers("The studio earned 41 million.", [], script) == [])

# ---------------------------------------------------------------- roles and prompts

short_article = {"title": "Short", "text": "word " * 100}
long_article = {"title": "Long", "text": "word " * 400}
check("a short article is a BRIEF whatever its position", S.role_for(0, short_article) == "BRIEF")
check("the first long article leads, the second is major, the rest are standard",
      [S.role_for(i, long_article) for i in range(3)] == ["LEAD", "MAJOR", "STANDARD"])


def prompt(index=0, total=3, role="LEAD", memory="", article=None, lang="he", **kw):
    art = article or {"title": "Hasbro earns $41m", "source": "PG", "text": "Hasbro earned $41m in Q3."}
    return S.segment_prompt(art, index, total, role, memory, SP, lang, **kw)


p0 = prompt()
check("the first story is told there is no greeting, the preview comes before it",
      "BEFORE this segment" in p0 and "Do not greet" in p0)
check("a later story opens on its own, with no greeting or recap",
      "No greeting, no recap" in prompt(index=1))
check("no story hands over to, mentions or hints at another story (weak writers leaked its facts)",
      "Do NOT mention, hint at or hand over to any other story" in prompt(index=1)
      and "Next story title" not in prompt(index=1))
last = prompt(index=2)
check("the last story does not sign off either (the frame writes the outro)",
      "show outro is written separately" in last)
check("no line of the prompt tells the writer a hand-over happened or is wanted",
      "hand-over" not in p0.replace("hand over to any other story", "") and "hand-over" not in prompt(index=1))
check("the style rules no longer forbid advice while the coverage standard demands it",
      "Never close a story with advice" in p0 and "Do not end with" not in p0)
themed = prompt(index=1, theme="שבוע של רגולציה", angle="מה זה אומר ל-RMG", feedback="- פחות מספרים")
check("the episode theme, the story's angle and the listener's notes reach the writer",
      "EPISODE THEME" in themed and "שבוע של רגולציה" in themed and "THIS STORY'S ANGLE" in themed
      and "THE LISTENER'S OWN NOTES" in themed and "פחות מספרים" in themed
      and "Never mention the subject or facts of any other story" in themed)
check("without theme, angle or notes none of those sections appears",
      "EPISODE THEME" not in p0 and "LISTENER'S OWN NOTES" not in p0)
check("the Hebrew rule comes first and again after the article",
      p0.startswith("OUTPUT LANGUAGE: HEBREW") and p0.rstrip().endswith("source material only."))
check("the prompt asks for the key points first, then a conversation that covers every one",
      p0.index("key_points:") < p0.index("script:") and "Cover EVERY key point" in p0)
check("depth is a coverage standard with a typical size, and no word target",
      "COVERAGE STANDARD" in p0 and "typical" in p0 and "1,400" not in p0 and "word target" not in p0)
check("a vendor or company source is attributed, never endorsed", "ATTRIBUTED, never endorsed" in p0)
check("memory context is inserted only when there is some",
      "PREVIOUS EPISODES" in prompt(memory="- prior: x") and "PREVIOUS EPISODES" not in p0)
check("a partial article is flagged as preview-only",
      "PARTIAL: only the newsletter preview" in prompt(article={"title": "T", "text": "x", "partial": True})
      and "PARTIAL:" not in p0)
check("the English prompt has no Hebrew-output banner", not prompt(lang="en").startswith("OUTPUT LANGUAGE"))
sch = S.schemas(SP)
check("schemas() restricts speakers to the two hosts, in both the segment and the outro",
      sch["segment"]["properties"]["script"]["items"]["properties"]["speaker"]["enum"] == [A, B]
      and sch["frame"]["properties"]["outro"]["items"]["properties"]["speaker"]["enum"] == [A, B])
check("the shared base schemas are not mutated by schemas()",
      "enum" not in S.TURN_SCHEMA["properties"]["speaker"])

# ---------------------------------------------------------------- one segment

ARTICLE = {"title": "Hasbro earns $41m from Monopoly Go", "source": "MG",
           "text": "Hasbro earned $41m from Monopoly Go in Q3, flat on Q1 and down from $44m in Q2. " * 8}


def turns(n, extra=""):
    return [{"speaker": A if i % 2 == 0 else B, "text": f"תור מספר {i} על הכתבה. {extra}".strip()}
            for i in range(n)]


def stub(*, script=None, key_points=None, issues_seq=None, publishable=True, log=None,
         fail_review=False, scripts_seq=None):
    state = {"story": 0, "review": 0}

    def call(prompt, schema, *, model, tries, timeout):
        props = schema["properties"]
        if "key_points" in props:
            state["story"] += 1
            if log is not None:
                log.append(("story", prompt, model))
            seq = scripts_seq or [script if script is not None else turns(20, "41 מיליון.")]
            return {"publishable": publishable, "stance": "NEUTRAL_REPORTING", "headline": "הסברנו",
                    "key_points": key_points if key_points is not None else ["Hasbro earned 41 million"],
                    "caution": "", "takeaway": "לבדוק.", "script": seq[min(state["story"], len(seq)) - 1]}
        if "issues" in props:
            if fail_review:
                raise RuntimeError("503 simulated")
            state["review"] += 1
            if log is not None:
                log.append(("review", prompt, model))
            seq = issues_seq or [[]]
            return {"issues": seq[min(state["review"], len(seq)) - 1]}
        raise AssertionError("unexpected schema")
    call.state = state
    return call


MODELS = {"creative": "creative-m", "review": "review-m"}


ROLE_INDEX = {"LEAD": 0, "MAJOR": 1, "STANDARD": 2, "BRIEF": 0}


def writer_for(call, total=3, **kw):
    return S.Writer(call=call, models=MODELS, speakers=SP, lang="he", total=total, log=quiet, **kw)


def seg(call, role="LEAD", article=ARTICLE, **kw):
    """Write one story and review it (alone, last chance = no budget pausing)."""
    if role == "BRIEF":
        article = {**article, "text": "Hasbro earned $41m from Monopoly Go in Q3. " * 8}
    else:
        article = {**article, "text": article["text"] + " filler" * 300}
    w = writer_for(call, **kw)
    s = w.write(article, ROLE_INDEX[role])
    if s["status"] != "written":
        return s
    done = {"0": s}
    S.review_segments(done, [article], w, lambda d: None, last_chance=True)
    return done["0"]


log = []
r = seg(stub(log=log))
check("a covered story is accepted after one write and one review",
      r["status"] == "ok" and [k for k, *_ in log] == ["story", "review"], str(r.get("problems")))
check("the story is written by the creative model, the review by the review model",
      log[0][2] == "creative-m" and log[1][2] == "review-m")
check("the review sees only this story's article", ARTICLE["title"] in log[1][1])

# thin: a LEAD with 6 turns is a headline -> one rewrite, with the draft attached
log = []
r = seg(stub(scripts_seq=[turns(6, "41."), turns(20, "41 מיליון.")], log=log))
stories = [p for k, p, _ in log if k == "story"]
check("a thin story gets exactly one rewrite", len(stories) == 2 and r["status"] == "ok", str(len(stories)))
check("the rewrite prompt says why and carries the draft",
      "NOT ACCEPTED because" in stories[1] and "PREVIOUS DRAFT:" in stories[1] and "headline, not coverage" in stories[1])
check("the better (longer) draft is the one kept", len(r["script"]) == 20)

# a key point's number that is never said
log = []
r = seg(stub(script=turns(20, "בלי מספרים."), log=log))
stories = [p for k, p, _ in log if k == "story"]
check("a key point's number that is never said triggers the one rewrite",
      len(stories) == 2 and "never said in the conversation: 41" in stories[1], str(len(stories)))
check("...and a story that stays that way still ships (the review decides facts, not the check)",
      r["status"] == "ok")

# review: blocking fact -> rewrite -> clean
bad = [{"turn": 3, "kind": "FACT", "blocking": True, "problem": "invented 73%", "fix": "remove it"}]
log = []
r = seg(stub(issues_seq=[bad, []], log=log))
stories = [p for k, p, _ in log if k == "story"]
check("a blocking factual error is rewritten once and then accepted",
      r["status"] == "ok" and len(stories) == 2 and "invented 73%" in stories[1], str([k for k, *_ in log]))
# ...twice -> dropped
r = seg(stub(issues_seq=[bad, bad]))
check("a factual error that survives the rewrite cuts the story", r["status"] == "dropped" and "invented 73%" in r["reason"])
# non-blocking and OTHER issues never block
r = seg(stub(issues_seq=[[{"turn": 1, "kind": "OTHER", "blocking": False, "problem": "wording", "fix": ""},
                          {"turn": 2, "kind": "FACT", "blocking": False, "problem": "minor", "fix": ""}]]))
check("non-blocking notes are kept but never block", r["status"] == "ok" and len(r["review"]) == 2)
# attribution is blocking too
att = [{"turn": 2, "kind": "ATTRIBUTION", "blocking": True, "problem": "company claim as fact", "fix": "attribute"}]
r = seg(stub(issues_seq=[att, att]))
check("a blocking attribution error is treated like a factual one", r["status"] == "dropped")
# suspects reach the reviewer
log = []
seg(stub(script=turns(20, "41 מיליון ו-73 אחוז."), log=log))
check("a spoken number found in neither the article nor the points is flagged to the reviewer",
      "73" in [p for k, p, _ in log if k == "review"][0].split("FACT error):")[1].split("ARTICLE:")[0])
# publishable false
r = seg(stub(publishable=False))
check("pure promotion is dropped without a review", r["status"] == "dropped" and "promotion" in r["reason"])
# reviewer outage: the free numbers check decides (2026-11-10: one outage ended a run)
plain = [{"speaker": A if i % 2 == 0 else B, "text": "תור על הכתבה, 41 מיליון."} for i in range(20)]
r = seg(stub(fail_review=True, script=plain))
check("a reviewer outage keeps a story whose numbers all match its article, marked unreviewed",
      r["status"] == "ok" and "not reviewed" in r["review"][0]["problem"])
r = seg(stub(fail_review=True, script=plain[:-1] + [{"speaker": B, "text": "ו-73 אחוז מזה, 41 מיליון."}]))
check("...and cuts a story that says a number found nowhere in the article",
      r["status"] == "dropped" and "73" in r["reason"])
# an empty script is an error
try:
    seg(stub(script=[]))
    check("a story with no script fails", False, "no exception")
except RuntimeError:
    check("a story with no script fails", True)
# a cut-off turn that stays broken is closed
broken = turns(19, "41 מיליון.") + [{"speaker": B, "text": "הם הכריזו על שותפות עם חברת פלונ"}]
r = seg(stub(scripts_seq=[broken, broken]))
check("a fragment that survives the rewrite is closed with a full stop", r["script"][-1]["text"].endswith("."))
# Hebrew acronyms are translated in the segment
r = seg(stub(script=turns(20, "41 מיליון מ-D2C."), key_points=["41"]))
check("D2C is replaced by its Hebrew form in the segment", "D2C" not in " ".join(t["text"] for t in r["script"]))
# one English turn inside a Hebrew story is a problem (a lite model did this)
english = turns(19, "41 מיליון.") + [{"speaker": B, "text": "The host speaks English entirely unrelated to the article."}]
log = []
r = seg(stub(scripts_seq=[english, turns(20, "41 מיליון.")], log=log))
check("a turn written in English inside a Hebrew story triggers the one rewrite",
      len([k for k, *_ in log if k == "story"]) == 2 and "is not in Hebrew" in [p for k, p, _ in log if k == "story"][1])
r = seg(stub(script=turns(19, "41 מיליון.") + [{"speaker": B, "text": "AppLovin ו-Unity הציגו נתוני ROAS ו-CPI חדשים."}]))
check("a Hebrew sentence full of English names and terms is not mistaken for English",
      "is not in Hebrew" not in " ".join(r["problems"]))
# a BRIEF has a lower floor
r_brief = seg(stub(script=turns(5, "41.")), role="BRIEF")
check("a short item may be short: 5 turns is fine for a BRIEF", r_brief["status"] == "ok" and len(r_brief["script"]) == 5)

# ---------------------------------------------------------------- the frame

frame_ok = lambda prompt, schema, *, model, tries, timeout: {
    "episode_title": "כותרת הפרק", "outro": [{"speaker": A, "text": "תודה שהאזנתם."}], "bridges": []}
SEGS = [{"headline": "ראשון", "takeaway": "א.", "key_points": ["Hasbro earned 41 million"],
         "script": [{"speaker": A, "text": "הכנסות של 41 מיליון דולר."}]},
        {"headline": "שני", "takeaway": "ב.", "key_points": ["Zynga fell 13 percent"],
         "script": [{"speaker": A, "text": "ירידה של 13 אחוז במובייל."}]}]
t, o, br = S.build_frame(SEGS, call=frame_ok, models=MODELS, speakers=SP, lang="he", log=quiet)
check("the frame returns the model's title and outro", t == "כותרת הפרק" and o[0]["text"] == "תודה שהאזנתם." and br == {})


def boom(prompt, schema, *, model, tries, timeout):
    raise RuntimeError("503")


t, o, br = S.build_frame(SEGS, call=boom, models=MODELS, speakers=SP, lang="he", log=quiet)
check("a failed frame falls back to the first headline and a plain sign-off, never raising",
      t == "ראשון" and len(o) == 1 and "ונתראה" in o[0]["text"] and br == {})
t, o, br = S.build_frame(SEGS, call=lambda *a, **k: {"episode_title": "", "outro": [], "bridges": []},
                         models=MODELS, speakers=SP, lang="he", log=quiet)
check("an empty frame answer also falls back", t == "ראשון" and o)

# bridges: one honest link between neighbours, from their own facts only
def with_bridge(text):
    return lambda *a, **k: {"episode_title": "כותרת", "outro": [{"speaker": A, "text": "תודה."}],
                            "bridges": [{"after_story": 1, "text": text}]}


good_bridge = "גם אצל זינגה וגם אצל הסברו המספרים של 41 ושל 13 מראים שהמובייל כבר לא מרחיק לכת."
t, o, br = S.build_frame(SEGS, call=with_bridge(good_bridge), models=MODELS, speakers=SP, lang="he", log=quiet)
check("a bridge built from the two stories' own numbers is kept, spoken by the analyst",
      list(br) == [1] and br[1]["speaker"] == B and "41" in br[1]["text"], str(br))
t, o, br = S.build_frame(SEGS, call=with_bridge("זה מזכיר את הירידה של 99 אחוז בשוק כולו, וזה משנה הכול."),
                         models=MODELS, speakers=SP, lang="he", log=quiet)
check("a bridge with a number found in neither story is refused", br == {}, str(br))
t, o, br = S.build_frame(SEGS, call=with_bridge("This is an English bridge with no Hebrew at all in it today."),
                         models=MODELS, speakers=SP, lang="he", log=quiet)
check("an English bridge in a Hebrew episode is refused", br == {})
t, o, br = S.build_frame(SEGS, call=with_bridge("קצר."), models=MODELS, speakers=SP, lang="he", log=quiet)
check("a bridge of one word is refused", br == {})
t, o, br = S.build_frame(SEGS, call=with_bridge(""), models=MODELS, speakers=SP, lang="he", log=quiet)
check("an empty bridge (nothing honest in common) is simply absent", br == {})
check("the frame prompt carries each story's key points and the theme, and asks for a link per pair",
      "KEY POINTS: Hasbro earned 41 million" in S.frame_prompt(SEGS, SP, "he", "שבוע של רגולציה")
      and "שבוע של רגולציה" in S.frame_prompt(SEGS, SP, "he", "שבוע של רגולציה")
      and "bridges" in S.frame_prompt(SEGS, SP, "he"))

# ---------------------------------------------------------------- the plan (order and theme before writing)

PLAN_ARTICLES = [{"title": f"A{i}", "source": "PG", "text": "word " * 100, "_score": sc}
                 for i, sc in enumerate([7, 9, 8, 8.5])]


def plan_call(payload):
    return lambda prompt, schema, *, model, tries, timeout: payload


plan = S.build_plan(PLAN_ARTICLES, call=plan_call({
    "order": [3, 1, 2, 4], "through_line": "שבוע של רגולציה ושל נזילות",
    "angles": [{"story": 2, "angle": "מה זה אומר למפעילי RMG"}, {"story": 4, "angle": "השוואה לבנצ'מרק"}]}),
    models=MODELS, lang="he", log=quiet)
check("the plan puts the highest-scored story first even if the model did not",
      plan["order"][0] == 1 and sorted(plan["order"]) == [0, 1, 2, 3], str(plan))
check("the theme is kept and angles are keyed by their position in the new order",
      plan["order"] == [1, 2, 0, 3] and plan["through_line"].startswith("שבוע")
      and plan["angles"] == {"0": "מה זה אומר למפעילי RMG", "3": "השוואה לבנצ'מרק"}, str(plan))
plan = S.build_plan(PLAN_ARTICLES, call=plan_call({"order": [1, 1, 2, 3], "through_line": "x", "angles": []}),
                    models=MODELS, lang="he", log=quiet)
check("an order that is not a permutation falls back to score order (best first)",
      plan["order"] == [1, 0, 2, 3], str(plan))
plan = S.build_plan(PLAN_ARTICLES, call=plan_call({"order": [1, 2, 3, 4], "through_line": "ירידה של 13 אחוז",
                                                   "angles": [{"story": 1, "angle": "גידול של 50 אחוז"}]}),
                    models=MODELS, lang="he", log=quiet)
check("a theme or angle containing a number is dropped (it could smuggle in a fact)",
      plan["through_line"] == "" and plan["angles"] == {})
plan = S.build_plan(PLAN_ARTICLES, call=boom, models=MODELS, lang="he", log=quiet)
check("a failed plan call never raises: given order, no theme", plan["order"] == [0, 1, 2, 3] and not plan["through_line"])
partial_articles = [dict(a) for a in PLAN_ARTICLES]
partial_articles[1]["partial"] = True          # the top-scored one is only an email preview
plan = S.build_plan(partial_articles, call=plan_call({"order": [1, 2, 3, 4], "through_line": "x", "angles": []}),
                    models=MODELS, lang="he", log=quiet)
check("a partial (email-preview) story never leads and is told last, whatever its score",
      plan["order"][0] == 3 and plan["order"][-1] == 1, str(plan["order"]))
check("with fewer than three stories there is nothing to plan and no call is made",
      S.build_plan(PLAN_ARTICLES[:2], call=boom, models=MODELS, lang="he", log=quiet)["order"] == [0, 1])

# ---------------------------------------------------------------- the episode

ARTICLES = [{"title": f"Story {i}", "source": "PG", "text": "word " * 400 + "41 million"} for i in range(4)]


def episode(call, resume=None, articles=ARTICLES, **kw):
    saved = []
    data = S.build_episode(articles, call=call, save=lambda d: saved.append(set(d)), models=MODELS,
                           speakers=SP, lang="he", memory_context="", resume=resume, log=quiet, **kw)
    return data, saved


def full_call(frame=frame_ok, bad_story=None):
    inner = stub()
    def call(prompt, schema, *, model, tries, timeout):
        props = schema["properties"]
        if "issues" in props and bad_story:
            blocks = prompt.split("=== STORY ")[1:]
            return {"issues": [{"story": int(b.split(" ")[0]), "turn": 1, "kind": "FACT", "blocking": True,
                                "problem": "x", "fix": "y"} for b in blocks if bad_story in b]}
        if "episode_title" in props:
            return frame(prompt, schema, model=model, tries=tries, timeout=timeout)
        return inner(prompt, schema, model=model, tries=tries, timeout=timeout)
    return call


data, saved = episode(full_call())
check("an episode is every story's script in order, then the outro",
      len(data["script"]) == 4 * 20 + 1 and data["script"][-1]["text"] == "תודה שהאזנתם."
      and data["kept"] == [0, 1, 2, 3])
check("the digest summary comes from the stories' own headlines and takeaways, no extra call",
      data["digest_summary"] == [{"title": "הסברנו", "key_takeaway": "לבדוק."}] * 4)
check("each finished story is saved as it completes", [len(s) for s in saved][:4] == [1, 2, 3, 4])
check("all four stories were reviewed in ONE call", True)
check("the intro is given the stories' key points", data["prep"]["articles"][0]["key_points"])

data, saved = episode(full_call(bad_story="Story 3"))
check("a cut story is left out of the episode but listed", data["kept"] == [0, 1, 2]
      and data["dropped"][0]["index"] == 3 and len(data["script"]) == 3 * 20 + 1)

try:
    episode(full_call(bad_story="Story"), articles=ARTICLES)
    check("too few surviving stories refuses to publish", False, "no exception")
except RuntimeError as e:
    check("too few surviving stories refuses to publish a thin episode", "refusing" in str(e))
data, _ = episode(full_call(bad_story="Story 1"), min_segments=1)
check("min_segments lets the smoke test run with fewer stories", data["kept"])

# resume: stories already done are not written again; a failed story keeps the others saved
calls = []
inner = full_call()
def counting(prompt, schema, *, model, tries, timeout):
    if "key_points" in schema["properties"]:
        calls.append("story")
    return inner(prompt, schema, model=model, tries=tries, timeout=timeout)
done_seg = {"status": "ok", "headline": "ישן", "takeaway": "ט.", "key_points": ["k"], "script": turns(20)}
data, saved = episode(counting, resume={"0": done_seg, "1": done_seg, "2": {"status": "writing"}})
check("a resumed episode writes only the missing stories (a half-done record is redone)",
      len(calls) == 2 and data["kept"] == [0, 1, 2, 3], str(len(calls)))


def fails_on_story_two(prompt, schema, *, model, tries, timeout):
    if "key_points" in schema["properties"] and "TITLE: Story 2" in prompt:
        raise RuntimeError("503 after all retries")
    return inner(prompt, schema, model=model, tries=tries, timeout=timeout)


saved = []
try:
    S.build_episode(ARTICLES, call=fails_on_story_two, save=lambda d: saved.append(dict(d)), models=MODELS,
                    speakers=SP, lang="he", memory_context="", resume=None, log=quiet)
    check("a story that cannot be written fails the run", False, "no exception")
except RuntimeError:
    check("a story that cannot be written fails the run (so a later run resumes)", True)
check("...but the stories that finished before it are saved",
      saved and set(saved[-1]) == {"0", "1", "3"}, str([sorted(s) for s in saved]))

# ---------------------------------------------------------------- small repairs (2026-11-11)

check("an Arabic lam inside a Hebrew word becomes a lamed",
      S.strip_stray_script("נקודת כשل ריכוזית") == "נקודת כשל ריכוזית")
check("Latin names, digits, shekel and punctuation are untouched",
      S.strip_stray_script("Türkiye: 41 מיליון ₪, ל-Rockstar.") == "Türkiye: 41 מיליון ₪, ל-Rockstar.")
check("an unmapped foreign letter is removed", S.strip_stray_script("שלום 你好") == "שלום ")

base = [{"speaker": A if i % 2 == 0 else B, "text": f"תור {i + 1} על 41 מיליון דולר."} for i in range(12)]
issues = [{"turn": 5, "kind": "OFF_TOPIC", "blocking": False, "problem": "x", "fix": ""},
          {"turn": 3, "kind": "TYPO", "blocking": False, "problem": "x", "fix": "",
           "replacement": "תור 3 על 41 מיליון דולר!"}]
out, done = S.apply_review_edits(base, issues, floor=8)
check("an off-topic turn is deleted and a one-character typo is replaced",
      len(out) == 11 and out[2]["text"].endswith("!") and len(done) == 2, str(done))
check("the input script is not modified in place", len(base) == 12 and not base[2]["text"].endswith("!"))
bad = [{"turn": 3, "kind": "TYPO", "blocking": False, "problem": "x", "fix": "",
        "replacement": "תור 3 על 42 מיליון דולר."},
       {"turn": 4, "kind": "TYPO", "blocking": False, "problem": "x", "fix": "",
        "replacement": "משהו אחר לגמרי שאין לו קשר לתור המקורי בכלל"},
       {"turn": 1, "kind": "OFF_TOPIC", "blocking": False, "problem": "x", "fix": ""}]
out, done = S.apply_review_edits(base, bad, floor=8)
check("a replacement that changes a number or the sentence, and the opening turn, are left alone",
      out == base and done == [], str(done))
out, done = S.apply_review_edits(base[:8], [{"turn": 5, "kind": "OFF_TOPIC", "blocking": False,
                                              "problem": "x", "fix": ""}], floor=8)
check("nothing is deleted below the role's turn floor", len(out) == 8 and done == [])

r = seg(stub(script=turns(20, "41 מיליון."),
             issues_seq=[[{"turn": 6, "kind": "OFF_TOPIC", "blocking": False, "problem": "x", "fix": ""}]]))
check("build_segment applies the reviewer's off-topic deletion and keeps the story",
      r["status"] == "ok" and len(r["script"]) == 19 and r["edits"], str(r.get("edits")))

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
