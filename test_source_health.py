#!/usr/bin/env python3
"""
Offline tests for source_health.py.
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import source_health as SH

FAILS = []

def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

print("\n--- Testing source_health.py ---")

SOURCES = [
    {"name": "PocketGamer.biz", "kind": "rss"},
    {"name": "MobileGamer.biz", "kind": "rss"},
    {"name": "Gamigion (Mobile Gaming Today)", "kind": "rss"},
    {"name": "Gamesforum", "kind": "html"},
]

# 1. Everyone producing content: no alerts, first run, no waiting period.
alerts = SH.check(SOURCES, {"PocketGamer.biz": 5, "MobileGamer.biz": 3,
                             "Gamigion (Mobile Gaming Today)": 2, "Gamesforum": 4})
check("a fully healthy run has no alerts", alerts == [])

# 2. One RSS source empty while others are fine -- alerts immediately (no
# waiting for a second bad week), and blames that source specifically.
alerts = SH.check(SOURCES, {"PocketGamer.biz": 5, "MobileGamer.biz": 3,
                             "Gamigion (Mobile Gaming Today)": 0, "Gamesforum": 4})
check("a single failing RSS source alerts on the very first miss",
      len(alerts) == 1, f"got {alerts}")
check("the alert names the specific source and the ones that worked",
      "Gamigion (Mobile Gaming Today)" in alerts[0]
      and "PocketGamer.biz" in alerts[0] and "MobileGamer.biz" in alerts[0],
      alerts[0] if alerts else "no alert")

# 3. Every RSS source fails together -- this is the "it's probably us, not
# them" case, and the message must say that instead of blaming each site.
alerts = SH.check(SOURCES, {"PocketGamer.biz": 0, "MobileGamer.biz": 0,
                             "Gamigion (Mobile Gaming Today)": 0, "Gamesforum": 4})
check("all RSS sources failing together produces one alert per RSS source",
      len(alerts) == 3, f"got {len(alerts)}")
check("the message says this looks systemic, not source-specific",
      all("EVERY RSS source failed together" in a for a in alerts),
      alerts)

# 4. An HTML source failing gets its own distinct message pointing at
# link_pattern, not conflated with the RSS-specific wording.
alerts = SH.check(SOURCES, {"PocketGamer.biz": 5, "MobileGamer.biz": 3,
                             "Gamigion (Mobile Gaming Today)": 2, "Gamesforum": 0})
check("an HTML source failing alone produces exactly one alert",
      len(alerts) == 1, f"got {alerts}")
check("the HTML alert points at link_pattern, not the RSS wording",
      "link_pattern" in alerts[0] and "RSS" not in alerts[0],
      alerts[0] if alerts else "no alert")

# 5. record()/pending_alerts() round-trip through the scratch file, and a
# missing/corrupt file behaves like "nothing to report" rather than crashing
# the workflow step that reads it.
with tempfile.TemporaryDirectory() as tmp:
    real_file = SH.ALERTS_FILE
    SH.ALERTS_FILE = pathlib.Path(tmp) / "source_alerts.json"
    try:
        check("pending_alerts() is empty before anything is recorded",
              SH.pending_alerts() == [])
        SH.record(["X: 0 articles this week."])
        check("pending_alerts() returns exactly what record() just wrote",
              SH.pending_alerts() == ["X: 0 articles this week."])
        SH.ALERTS_FILE.write_text("not json")
        check("a corrupt alerts file is treated as empty, not a crash",
              SH.pending_alerts() == [])
    finally:
        SH.ALERTS_FILE = real_file


# 6. An email source (kind = "email") is exempt from the "0 articles" alert
# entirely -- a newsletter genuinely not publishing this week is normal and
# indistinguishable from a broken source without more context, and treating
# it as a fault would recreate the exact cry-wolf pattern that got
# Gamigion's original RSS attempt disabled (CHANGELOG, 2026-09-22).
EMAIL_SOURCES = SOURCES + [{"name": "Gamigion (Mobile Gaming Today) email",
                            "kind": "email"}]
alerts = SH.check(EMAIL_SOURCES, {"PocketGamer.biz": 5, "MobileGamer.biz": 3,
                                   "Gamigion (Mobile Gaming Today)": 2, "Gamesforum": 4,
                                   "Gamigion (Mobile Gaming Today) email": 0})
check("a quiet week for an email source produces no alert at all",
      alerts == [], f"got {alerts}")

# 7. -1 (collect() couldn't even run the adapter -- see sources.py) always
# alerts, for any kind, with wording distinct from "zero results."
alerts = SH.check(EMAIL_SOURCES, {"PocketGamer.biz": 5, "MobileGamer.biz": 3,
                                   "Gamigion (Mobile Gaming Today)": 2, "Gamesforum": 4,
                                   "Gamigion (Mobile Gaming Today) email": -1})
check("a hard adapter failure (-1) alerts even for an email source",
      len(alerts) == 1, f"got {alerts}")
check("the -1 alert says the fetch itself failed, not that it was quiet",
      "fetch itself failed" in alerts[0], alerts[0] if alerts else "no alert")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
