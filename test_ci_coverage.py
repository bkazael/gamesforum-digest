#!/usr/bin/env python3
"""
Every test_*.py in the repo must be run by .github/workflows/test.yaml.

test_email_source.py was written, passed locally, and was never added to
the workflow's hand-typed list -- so for a day CI was green while not
running it at all. A test CI doesn't run only looks like coverage. This
file closes that gap by failing the moment a test file exists that the
workflow doesn't invoke.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
workflow = (ROOT / ".github" / "workflows" / "test.yaml").read_text(encoding="utf-8")

# Ignore commented-out lines: a "# python test_x.py" comment is not a run.
run_lines = [l for l in workflow.splitlines() if not l.lstrip().startswith("#")]
invoked = set(re.findall(r"python\s+(test_\w+\.py)", "\n".join(run_lines)))
present = {p.name for p in ROOT.glob("test_*.py")}

missing = sorted(present - invoked)
stale = sorted(invoked - present)

print("\n--- Testing CI coverage ---")
ok = True
if missing:
    ok = False
    print(f"FAIL  test files CI never runs: {missing}")
else:
    print(f"PASS  all {len(present)} test files are run by test.yaml")
if stale:
    ok = False
    print(f"FAIL  test.yaml runs files that no longer exist: {stale}")
else:
    print("PASS  test.yaml references no missing files")

# A SyntaxWarning (e.g. "invalid escape sequence '\S'") is a future
# SyntaxError. The first live smoke run showed one in sources.py's docstring,
# buried in the Actions log where nobody reads. Compiling every module with
# warnings promoted to errors makes it a red test instead.
import warnings

bad = []
for path in sorted(ROOT.glob("*.py")):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (SyntaxWarning, SyntaxError) as e:
            bad.append(f"{path.name}: {e}")
if bad:
    ok = False
    print(f"FAIL  modules that compile with warnings: {bad}")
else:
    print("PASS  every module compiles without a SyntaxWarning")

print("\n" + ("ALL PASS" if ok else "FAILED"))
sys.exit(0 if ok else 1)
