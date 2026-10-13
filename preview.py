#!/usr/bin/env python3
"""Generate an isolated, reviewable episode without changing the published feed.

python preview.py --discovery-only  # live sources, no model calls
python preview.py                   # complete paid preview
python preview.py --script-only     # paid editorial pipeline, no audio
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
    if args.script_only:
        import datetime as dt
        import checkpoint
        import runtime
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        resumed = checkpoint.load_content(today)
        if resumed:
            articles, data = resumed
        else:
            articles = discovery.select()
            if not articles:
                raise RuntimeError("No new stories qualify for a preview")
            context = p.memory.load_recent_context()
            prep = p.prepare_episode(articles, context)
            data = p.generate_podcast_content(articles, today, context, prep=prep)
            data.update(prep=prep, intro_done=False)
            checkpoint.save_content(today, articles, data)
        if data.get("intro_done") is False:
            data = p.add_intro(data)
            checkpoint.save_content(today, articles, data)
        data = p.review_episode(data, articles, today)
        checkpoint.save_content(today, articles, data)
        runtime.atomic_write(output / "digests" / f"{today}-script.md",
            "\n".join(f"{t['speaker']}: {t['text']}" for t in data["script"]))
        return 0
    return p.main()


if __name__ == "__main__":
    raise SystemExit(main())
