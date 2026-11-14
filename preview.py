#!/usr/bin/env python3
"""Generate an isolated, reviewable episode without changing the published feed.

python preview.py --discovery-only  # live sources, no model calls
python preview.py                   # complete paid preview
python preview.py --script-only     # paid script pipeline (plan, stories, review, frame, intro, proofread), no audio
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", default="preview_output")
    ap.add_argument("--discovery-only", action="store_true")
    ap.add_argument("--script-only", action="store_true")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent
    output = Path(args.output).resolve()
    if output == root or output in root.parents:
        ap.error("Preview output must be a separate directory")
    output.mkdir(parents=True, exist_ok=True)
    # Set before importing any pipeline modules: all writes share this root.
    os.environ["PODCAST_OUTPUT_DIR"] = str(output)
    os.environ.setdefault("PODCAST_BASE_URL", "https://preview.invalid")
    for name in ("memory.json", "state.json"):
        if not (output / name).exists() and (root / name).exists():
            shutil.copy2(root / name, output / name)
    import gamesforum_pipeline as p
    import discovery
    if args.discovery_only:
        discovery.select(dry_run=True)
        return 0
    if not p.GEMINI_API_KEY:
        ap.error("Set GEMINI_API_KEY for a paid preview; use --discovery-only without one")
    p.preflight(audio=not args.script_only)
    import datetime as dt
    import checkpoint
    import stages
    today = dt.datetime.now(dt.timezone.utc).date()
    # A preview runs every stage now, whatever the day (stages.py), in its own output
    # directory. --script-only stops before the voice.
    os.environ["PIPELINE_FORCE"] = "1"
    if args.script_only:
        os.environ["PIPELINE_SKIP_PUBLISH"] = "1"
    rc = stages.run(today)
    if args.script_only:
        loaded = checkpoint.load_content(stages.week_target(today).isoformat())
        if not loaded or not loaded[1].get("script"):
            raise RuntimeError("the preview did not produce a script; see the log above")
        data = loaded[1]
        from script_quality import count_words
        print("::group::episode summary", flush=True)
        for k, s in sorted((data.get("segments") or {}).items(), key=lambda kv: int(kv[0])):
            print(f"story {int(k) + 1}: {s.get('status')} {len(s.get('script', []))} turns, "
                  f"{count_words(s.get('script', []))} words, {len(s.get('key_points', []))} key points, "
                  f"by {s.get('model')}"
                  + (f", cut: {s.get('reason')}" if s.get("status") == "dropped" else "")
                  + f" | {s.get('headline')}", flush=True)
        total = count_words(data["script"])
        print(f"total: {len(data['script'])} turns, {total} words, about {total / 140:.0f} min; "
              f"{data.get('bridges_used', 0)} bridges; theme: {data.get('through_line') or '-'}", flush=True)
        print("::endgroup::", flush=True)
        print("::group::script", flush=True)
        for t in data["script"]:
            print(f"{t['speaker']}: {t['text']}", flush=True)
        print("::endgroup::", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
