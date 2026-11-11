"""Zero-token tests for proofread.py, built on the 2026-11-11 episode's real slips."""
import sys

import proofread as PR

FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


A, B = "Dana", "Yoni"
SCRIPT = [
    {"speaker": A, "text": "ברוכים הבאים. נפתח הפעם ב-Take-Two."},
    {"speaker": B, "text": "המובייל הסביר 45 אחוז מסך ההזמנות, ענייה מ-42 אחוז בשנה שעברה."},
    {"speaker": A, "text": "משקיע יחיד גדול מהווה נקודת כשل ריכוזית."},
    {"speaker": B, "text": "מעניין איך זה מתחבר ל-Monopoly Go ולמכניקות עונתיות."},
    {"speaker": A, "text": "הסמנכ''לית הכספית מעריכה ירידה נוספת."},
    {"speaker": B, "text": "בוא נראה מה קורה בפרשת Mass Layoffs at Rovio Türkiye: Ruby Games."},
]

# ---------------------------------------------------------------- free cleanup
clean, n = PR.deterministic_cleanup(SCRIPT, "he")
check("an Arabic letter inside a Hebrew word is fixed for free", clean[2]["text"].endswith("נקודת כשל ריכוזית."))
check("a doubled apostrophe in a Hebrew acronym becomes a gershayim", 'סמנכ"לית' in clean[4]["text"])
check("names with Latin accents are untouched", "Türkiye" in clean[5]["text"])
check("the count of free fixes is reported", n == 2, str(n))
check("the input is not modified in place", "ل" in SCRIPT[2]["text"])

# ---------------------------------------------------------------- guards
check("a one-word spelling fix is accepted",
      PR.accept_replacement(SCRIPT[1]["text"], SCRIPT[1]["text"].replace("ענייה", "עלייה"), "he") is None)
check("a fix that changes a number is refused",
      PR.accept_replacement(SCRIPT[1]["text"], SCRIPT[1]["text"].replace("45", "46"), "he") == "changes a number")
check("a fix that adds a company name is refused",
      PR.accept_replacement("החברה דיווחה על ירידה.", "Zynga דיווחה על ירידה.", "he")
      == "adds a name or English word")
check("translating a raw English headline into Hebrew is accepted (names may go, none may come)",
      PR.accept_replacement(SCRIPT[5]["text"], "בוא נראה מה קורה עם הפיטורים ב-Ruby Games של Rovio בטורקיה.", "he",
                            "ENGLISH_LEFT") is None)
check("a rewrite of the whole turn is refused",
      PR.accept_replacement(SCRIPT[1]["text"], "זה נושא אחר לגמרי שאין בו שום קשר למה שנאמר קודם.", "he")
      is not None)

# ---------------------------------------------------------------- applying edits
edits = [
    {"turn": 2, "category": "SPELLING", "action": "REPLACE",
     "corrected": SCRIPT[1]["text"].replace("ענייה", "עלייה"), "reason": "typo"},
    {"turn": 4, "category": "OFF_TOPIC", "action": "DELETE", "corrected": "", "reason": "Monopoly Go"},
    {"turn": 1, "category": "REPEAT", "action": "DELETE", "corrected": "", "reason": "never the opening"},
    {"turn": 3, "category": "SPELLING", "action": "DELETE", "corrected": "", "reason": "not deletable"},
    {"turn": 2, "category": "GRAMMAR", "action": "REPLACE",
     "corrected": "המובייל הסביר 50 אחוז.", "reason": "bad"},
    {"turn": 99, "category": "SPELLING", "action": "REPLACE", "corrected": "x", "reason": "out of range"},
]
out, record = PR.apply_edits(SCRIPT, edits, "he")
check("the spelling fix is applied", "עלייה" in out[1]["text"] and "ענייה" not in out[1]["text"])
check("the off-topic turn is deleted", not any("Monopoly" in t["text"] for t in out) and len(out) == 5)
check("the opening turn and a non-deletable category are never deleted",
      out[0]["text"] == SCRIPT[0]["text"] and any("כש" in t["text"] for t in out))
check("a bad replacement is refused and recorded with the reason",
      any(r["category"] == "GRAMMAR" and not r["applied"] and r.get("refused") for r in record))
check("out-of-range turns are ignored", all(r["turn"] <= len(SCRIPT) for r in record))

# ---------------------------------------------------------------- the stage
seen = {}


def call_ok(prompt, schema, *, model, tries, timeout):
    seen["prompt"], seen["model"] = prompt, model
    return {"edits": edits[:2]}


out, report = PR.proofread_episode(SCRIPT, call=call_ok, model="m-creative", lang="he")
check("the stage reads the whole episode in one call, with the model it is given",
      seen["model"] == "m-creative" and all(f"{i}. " in seen["prompt"] for i in range(1, 7)))
check("the stage applies free fixes and model fixes together",
      report["status"] == "done" and report["applied"] == 2 and report["deterministic_fixes"] == 2
      and any("נקודת כשל" in t["text"] for t in out), str(report))


def call_down(prompt, schema, *, model, tries, timeout):
    raise RuntimeError("429 quota")


warnings = []
out, report = PR.proofread_episode(SCRIPT, call=call_down, model="m", lang="he", log=warnings.append)
check("a failed proofreading call never fails the run; the free fixes still apply",
      report["status"] == "unavailable" and len(out) == len(SCRIPT) and "ل" not in out[2]["text"]
      and any("::warning::" in w for w in warnings))

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
