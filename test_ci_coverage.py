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
run_lines = "\n".join(l for l in workflow.splitlines() if not l.lstrip().startswith("#"))
suite = (ROOT / "check_suite.py").read_text(encoding="utf-8") if (ROOT / "check_suite.py").exists() else ""

print("\n--- Testing CI coverage ---")
ok = True
# CI runs `pytest`, and check_suite.py runs every test_*.py by glob, so a new test file is
# covered the moment it exists. What can still go wrong is the wiring itself.
if re.search(r"^\s*run:\s*pytest\b", run_lines, re.M):
    print("PASS  test.yaml runs pytest")
else:
    ok = False
    print("FAIL  test.yaml does not run pytest")
if 'glob("test_*.py")' in suite and "python_files = check_suite.py" in (ROOT / "pytest.ini").read_text(encoding="utf-8"):
    print("PASS  check_suite.py runs every test_*.py by glob and pytest.ini points at it")
else:
    ok = False
    print("FAIL  check_suite.py / pytest.ini no longer cover every test_*.py")
print(f"PASS  {len(list(ROOT.glob('test_*.py')))} test files are covered") if ok else None
sys.exit(0 if ok else 1)
