#!/usr/bin/env python3
"""The weekly check: does this week's episode exist? Fails (red run, owner emailed) if not.

Run late on the last chance day (see weekly-check.yml). Also warns about things that will
become problems slowly: the size of the repository's audio, and a stage that failed.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import checkpoint
import runtime
import stages

AUDIO_WARN_MB = 600       # GitHub Pages sites should stay under about 1 GB


def main(today: dt.date | None = None) -> int:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    target = stages.week_target(today)
    import gamesforum_pipeline as P
    published = P.episode_published_for(target)
    audio_mb = sum(p.stat().st_size for p in (runtime.OUTPUT_ROOT / "episodes").glob("*.mp3")) / 1e6
    if audio_mb > AUDIO_WARN_MB:
        print(f"::warning::episodes/ holds {audio_mb:.0f} MB of audio (limit to watch: {AUDIO_WARN_MB} MB); "
              "move older audio out of git before the site hits its size limit (see AUDIT.md 5.3)")
    plan = stages.load_plan(target)
    for h in plan.get("history", [])[-5:]:
        print(f"{h['date']}: {'; '.join(h['did']) or 'nothing'} ({h['status']})")
    if published:
        print(f"OK: the episode for the week of {target} was published on {published}")
        return 0
    err = (plan.get("last_error") or {}).get("error", "no error recorded")
    held = plan.get("held")
    print(f"::error::No episode for the week of {target}. Last error: {err}. "
          + (f"Held back: {held['reason']}. " if held else "")
          + "See work/plan.json and the latest daily run. Re-run 'Gamesforum weekly digest' "
            "with 'force' only after the model quota resets (08:00 UTC).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
