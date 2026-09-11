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

with tempfile.TemporaryDirectory() as tmp:
    real_file = SH.HEALTH_FILE
    SH.HEALTH_FILE = pathlib.Path(tmp) / "source_health.json"
    try:
        # 1. A source with items every run never alerts.
        SH.record({"Gamesforum": 5})
        SH.record({"Gamesforum": 3})
        check("a consistently-producing source has no pending alerts",
              SH.pending_alerts() == [])

        # 2. One quiet week alone is not an alert (a real run can legitimately
        # have nothing new from a source that week).
        SH.record({"Gamesforum": 0})
        check("a single miss does not alert yet",
              SH.pending_alerts() == [])

        # 3. FAILURE_THRESHOLD consecutive misses (2, by default) does alert --
        # this is the real incident this module exists to catch: a source
        # whose page markup changed or feed died silently for weeks.
        SH.record({"Gamesforum": 0})
        check(f"{SH.FAILURE_THRESHOLD} consecutive misses trips the alert",
              SH.pending_alerts() == ["Gamesforum"])

        # 4. Recovery clears it -- the alert must not stick around once the
        # source is producing again, or every future run stays permanently
        # red for a problem that's already fixed.
        SH.record({"Gamesforum": 1})
        check("a recovered source is no longer in pending_alerts()",
              SH.pending_alerts() == [])

        # 5. Sources are tracked independently -- one broken source must not
        # mask or get confused with another that's healthy.
        SH.record({"SourceA": 0, "SourceB": 5})
        SH.record({"SourceA": 0, "SourceB": 5})
        check("only the actually-broken source is reported",
              SH.pending_alerts() == ["SourceA"])

        # 6. A missing/corrupt health file behaves like "no history yet",
        # same reasoning as memory.py's corrupt-file handling -- this must
        # never be able to fail a run by itself.
        SH.HEALTH_FILE.write_text("not json")
        check("a corrupt health file is treated as empty, not a crash",
              SH.pending_alerts() == [])
    finally:
        SH.HEALTH_FILE = real_file

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
