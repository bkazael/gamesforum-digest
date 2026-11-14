"""pytest entry point: every test_*.py is run as its own test, so one failure can be run and read.

    pytest -q                     # everything (this is what CI runs)
    pytest -q -k stages           # one file
    python test_stages.py         # the same file on its own, with all its PASS/FAIL lines

The test files are plain scripts with a small `check()` helper (361+ checks): they print a PASS or
FAIL line per check and exit non-zero on any FAIL. This wrapper runs each in a fresh process and
reports the FAIL lines, so a red test says what failed without opening the log.
"""
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent
FILES = sorted(p.name for p in ROOT.glob("test_*.py"))


@pytest.mark.parametrize("name", FILES)
def test_script(name):
    done = subprocess.run([sys.executable, name], cwd=ROOT, capture_output=True, text=True, timeout=300,
                          env={**__import__("os").environ, "GEMINI_API_KEY": "test-key"})
    out = done.stdout + done.stderr
    failed = [line for line in out.splitlines()
              if line.startswith(("FAIL", "ERROR", "Traceback", "AssertionError"))]
    assert done.returncode == 0 and not failed, "\n".join(failed[:20]) or out[-2500:]
