#!/usr/bin/env python3
"""
Offline tests for script_quality.py.

The fixtures are not invented: they are the broken turns actually found in
digests/*-script.md (13 of them across 6 of the 11 episodes recorded so far),
reproduced with the sentence tail the model really produced and the next
turn that really followed. If the repair logic ever "fixes" something these
don't expect, that is the signal to look at the real scripts again.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import script_quality as Q  # noqa: E402

FAILS = []
G = Q.GERSHAYIM


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


print("\n--- Testing script_quality.py ---")

# ---------------------------------------------------------------- 1. normalize_quotes

check("an ASCII quote inside a Hebrew acronym becomes gershayim",
      Q.normalize_quotes('ארה"ב') == f"ארה{G}ב")
check("a quote around an English phrase is left alone",
      Q.normalize_quotes('איך "global launch" הוא') == 'איך "global launch" הוא')
check("a quote after a space (a quoted phrase) is left alone",
      Q.normalize_quotes('הוא אמר "כן" והלך') == 'הוא אמר "כן" והלך')

# ---------------------------------------------------------------- 2. the real 2026-09-28 case: same host, tail re-opened as a new turn

script = [
    {"speaker": "Dana", "text": "ירידה קלה של 1% חודש בחודש. ארה"},
    {"speaker": "Dana", "text": "במובילה עם 28.6% מההכנסות, אחריה סין עם 16.7%."},
]
fixed, notes = Q.repair_script(script)
check("09-28: the two Dana turns are stitched into one", len(fixed) == 1, f"got {len(fixed)}")
check("09-28: the acronym is restored and the tail keeps its words",
      fixed and fixed[0]["text"] == f"ירידה קלה של 1% חודש בחודש. ארה{G}ב מובילה עם 28.6% מההכנסות, אחריה סין עם 16.7%.",
      fixed[0]["text"] if fixed else "")
check("09-28: the repair is reported", len(notes) == 1, str(notes))

# ---------------------------------------------------------------- 3. the common real case: the rest of the sentence is simply gone

script = [
    {"speaker": "Dana", "text": "ההוצאות של צרכנים ב-App Store בארה"},
    {"speaker": "Yoni", "text": "חבר'ה, אפל רושמת ירידה חסרת תקדים."},
]
fixed, notes = Q.repair_script(script)
check("08-31: a different next speaker means the turn is closed, not stitched",
      len(fixed) == 2 and fixed[0]["text"] == f"ההוצאות של צרכנים ב-App Store בארה{G}ב.",
      fixed[0]["text"])
check("08-31: the other host's turn is untouched",
      fixed[1]["text"] == "חבר'ה, אפל רושמת ירידה חסרת תקדים.")

script = [
    {"speaker": "Yoni", "text": "נכון. המנכ"},
    {"speaker": "Dana", "text": "בואו נסיים עם Mistplay."},
]
fixed, _ = Q.repair_script(script)
check("08-31: a prefixed acronym (המנכ) is restored with its prefix kept",
      fixed[0]["text"] == f"נכון. המנכ{G}ל.", fixed[0]["text"])

for tail, want in (("אוליבר בולוס, מנכ", f"אוליבר בולוס, מנכ{G}ל."),
                   ("ברוב מדינות ארה", f"ברוב מדינות ארה{G}ב.")):
    fixed, _ = Q.repair_script([{"speaker": "Dana", "text": tail},
                                 {"speaker": "Yoni", "text": "כן."}])
    check(f"08-24: {tail[-6:]!r} restored", fixed[0]["text"] == want, fixed[0]["text"])

# ---------------------------------------------------------------- 4. things that must NOT be touched

healthy = [
    {"speaker": "Dana", "text": "עלייה של 44%"},          # ends in %, legitimate
    {"speaker": "Yoni", "text": "זה גדל ב-2026."},
    {"speaker": "Dana", "text": "מה דעתך על Mistplay?"},
    {"speaker": "Yoni", "text": "הם אמרו 'player first'"},   # ends in a quote mark
]
fixed, notes = Q.repair_script(healthy)
check("healthy turns pass through unchanged", fixed == healthy and notes == [], str(notes))
check("healthy turns raise no problems", Q.script_problems(healthy) == [])

unknown = [{"speaker": "Dana", "text": "הם הכריזו על שותפות עם חברת פלונ"}]
fixed, notes = Q.repair_script(unknown)
check("an unrecognised cut is left exactly as it was (never guessed at)",
      fixed == unknown and notes == [], str(fixed))
check("...and is reported as a problem so the caller can retry",
      len(Q.script_problems(fixed)) == 1)

# ---------------------------------------------------------------- 5. close_remaining is the last resort

closed, n = Q.close_remaining(unknown)
check("close_remaining ends a leftover fragment with a full stop",
      n == 1 and closed[0]["text"].endswith("."), str(closed))
check("close_remaining leaves healthy turns alone", Q.close_remaining(healthy)[1] == 0)

# ---------------------------------------------------------------- 6b. hype detection survives Hebrew final letters
#
# Hebrew swaps ף/ם for פ/מ when a suffix is added, so a stem written in its
# final form ("מטורף") silently misses "מטורפים". The first live intro said
# "סכומים מטורפים" and sailed past exactly that.

def _hype(text):
    return Q.style_report([{"speaker": "Dana", "text": text}])[0].split()[0]

for word in ("מטורף", "מטורפים", "עצום", "עצומה", "עצומים", "מדהים", "מדהימה", "מהפכה", "דרמטי"):
    check(f"hype word detected in every form: {word}", _hype(f"זה {word} באמת.") == "1")
check("a similar-looking ordinary word is not hype (עצור = stop)", _hype("עצור רגע.") == "0")
check("style_report counts agreement openers",
      "2 of 3 turns" in Q.style_report([
          {"speaker": "Dana", "text": "בדיוק, כן."}, {"speaker": "Yoni", "text": "לגמרי."},
          {"speaker": "Dana", "text": "מה דעתך?"}])[1])

# ---------------------------------------------------------------- 6c. the agreement-opener trim
#
# The six real openers from the 2026-10-04 episode.
REAL = [
    "נכון, ו-AppLovin דחו את ההשוואה, וציינו ש-Ad Review שלהם משמש למיתון מודעות בלבד.",
    "בהחלט. נכון לעכשיו, זו סוגיה משפטית מתמשכת עם טענות וטענות נגד, ועדיין אין פסיקה.",
    "בהחלט. NetEase תטמיע את שיטות התשלום הללו בתחילה בכותרים כמו Where Winds Meet.",
    "בדיוק. למרות שעדיין אין הפרות חוק ספציפיות שאותרו, אי התייחסות עלולה להוביל לאכיפה.",
    "בדיוק. ולגבי משחקי IP גדולים כמו זה, יש לנו מקרה מבחן מהעבר הקרוב.",
    "בדיוק. המודעות הללו חייבות לספק את חווית הליבה של המשחק במהירות ובאופן אותנטי.",
]
out, n = Q.trim_agreement_openers([{"speaker": "Dana", "text": t} for t in REAL])
check("the first two agreement openers are kept, the other four stripped", n == 4, f"changed {n}")
check("the first two turns are untouched", out[0]["text"] == REAL[0] and out[1]["text"] == REAL[1])
check("a stripped turn reads as a normal sentence",
      out[2]["text"].startswith("NetEase תטמיע") and out[4]["text"].startswith("ולגבי משחקי IP"))
check("after trimming, no more than two turns open with an agreement word",
      sum(1 for t in out if t["text"].startswith(Q.AGREEMENT_OPENERS)) == 2)

out, n = Q.trim_agreement_openers([{"speaker": "Dana", "text": "בהחלט."}] * 4, keep=0)
check("a bare one-word answer is never stripped to nothing", n == 0 and out[0]["text"] == "בהחלט.")
out, n = Q.trim_agreement_openers([{"speaker": "Dana", "text": "בדיוק כמו שאמרנו קודם על כל העניין הזה."}], keep=0)
check("an opener that is part of the sentence (no punctuation after it) is left alone", n == 0)
out, n = Q.trim_agreement_openers([{"speaker": "Dana", "text": "נכון, זה גדול."}], keep=0)
check("a short remainder (under six words) is left alone", n == 0)
check("keep=0 strips even the first", Q.trim_agreement_openers(
    [{"speaker": "Dana", "text": "בדיוק. " + "מילה " * 8}], keep=0)[1] == 1)

# "דרמת ענק" (a huge drama) was in the 2026-10-04 intro and slipped past: the
# stem 'דרמט' needs a ט, and 'דרמת' has a ת.
for word in ("דרמה", "דרמת ענק", "דרמטי", "דרמטית"):
    check(f"hype detected: {word}", _hype(f"זו {word} באמת.") == "1")

# ---------------------------------------------------------------- 6. empty turns

check("an empty turn is reported",
      len(Q.script_problems([{"speaker": "Dana", "text": "  "}])) == 1)

# ---------------------------------------------------------------- style problems (voice, not form)
heb = [{"speaker": "Dana", "text": "הרשת העלתה את העמלה והמחיר קפץ."}]
check("a Hebrew script raises no style problem", Q.script_style_problems(heb) == [])
eng = [{"speaker": "Dana", "text": "The network raised its fee and prices jumped sharply."}]
check("an English script is flagged when Hebrew was required",
      any("not in Hebrew" in p for p in Q.script_style_problems(eng, "he")))
check("...but not when English is the show language", Q.script_style_problems(eng, "en") == [])
mixed = [{"speaker": "Dana", "text": "החברה AppLovin תבעה את Unity על נתוני הפרסום של המפעילים."}]
check("English company names inside Hebrew do not trip it", Q.script_style_problems(mixed) == [])
formula = [{"speaker": "Yoni", "text": f"בסוף מפעילים צריכים לבדוק את זה {i}."} for i in range(3)]
check("three 'operators should' endings are flagged",
      any("formula" in p for p in Q.script_style_problems(formula)))
check("two are allowed", Q.script_style_problems(formula[:2]) == [])
check("'המפעילים אמרו' is not the formula",
      Q.script_style_problems([{"speaker": "Dana", "text": "המפעילים אמרו שזה עובד."}] * 5) == [])

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
