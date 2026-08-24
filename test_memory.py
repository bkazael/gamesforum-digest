#!/usr/bin/env python3
"""
Offline tests for memory.py. No network, no Gemini calls, and the real
memory.json is never touched -- everything below runs against a scratch
file in a temp directory.
"""

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import memory

FAILS = []

def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

print("\n--- Testing memory.py ---")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = pathlib.Path(tmp)
    # Redirect memory.py's module-level paths at scratch files, so this
    # test can never read or write the project's real memory.json.
    memory.MEMORY_FILE = tmp_path / "memory.json"
    memory.PROFILE_FILE = tmp_path / "profile.toml"   # absent -> default lookback

    # 1. First run ever: no file on disk yet.
    check("load_recent_context on a missing file returns empty string",
          memory.load_recent_context() == "")

    # 2. build_entry derives everything from digest_summary -- no LLM call,
    #    nothing here can time out or need a fake API response.
    digest_summary = [
        {"title": "Apple cuts EU fees", "key_takeaway": "5% core commission", "metrics_mentioned": ["5%"]},
        {"title": "Xsolla D2C push", "key_takeaway": "40%+ revenue from D2C", "metrics_mentioned": []},
    ]
    entry = memory.build_entry("Episode One", digest_summary, date="2026-08-24")
    check("build_entry assigns ep_001 to the first entry",
          entry["episode_id"] == "ep_001", entry["episode_id"])
    check("build_entry pulls topics from digest_summary titles",
          entry["topics_covered"] == ["Apple cuts EU fees", "Xsolla D2C push"])
    check("build_entry joins the key_takeaways",
          "5% core commission" in entry["key_takeaways"]
          and "D2C" in entry["key_takeaways"])

    # 3. append_entry persists to disk, and the next build_entry sees it.
    memory.append_entry(entry)
    check("append_entry writes memory.json", memory.MEMORY_FILE.exists())
    on_disk = json.loads(memory.MEMORY_FILE.read_text(encoding="utf-8"))
    check("memory.json holds exactly one episode after one append",
          len(on_disk) == 1, len(on_disk))

    entry2 = memory.build_entry("Episode Two", digest_summary, date="2026-08-31")
    check("build_entry increments episode_id from what's already on disk",
          entry2["episode_id"] == "ep_002", entry2["episode_id"])
    memory.append_entry(entry2)

    # 4. The script prompt sees both, most recent last.
    ctx = memory.load_recent_context()
    check("load_recent_context is non-empty once entries exist", bool(ctx))
    check("load_recent_context includes both episode dates",
          "2026-08-24" in ctx and "2026-08-31" in ctx)

    # 5. This is the cost control: the archive on disk can grow forever,
    #    but the prompt only ever sees `limit` of them.
    ctx_capped = memory.load_recent_context(limit=1)
    check("load_recent_context(limit=1) drops the older episode",
          "2026-08-24" not in ctx_capped and "2026-08-31" in ctx_capped)

    # 6. [memory].lookback_episodes in profile.toml overrides the default
    #    of 3 without gamesforum_pipeline.py needing to know the number.
    memory.PROFILE_FILE.write_text(
        "[memory]\nlookback_episodes = 1\n", encoding="utf-8"
    )
    ctx_from_profile = memory.load_recent_context()
    check("profile.toml's lookback_episodes is honored when no limit is passed",
          "2026-08-24" not in ctx_from_profile and "2026-08-31" in ctx_from_profile)

    # 7. A corrupt memory.json degrades to "no history", not a crash --
    #    losing continuity for one episode beats failing the whole run.
    memory.MEMORY_FILE.write_text("{not valid json", encoding="utf-8")
    check("a corrupt memory.json is treated as empty, not a crash",
          memory.load_recent_context() == "")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
