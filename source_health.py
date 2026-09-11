#!/usr/bin/env python3
"""
Per-source health tracking.

sources.py's own adapters already log *why* a fetch produced nothing
(network error, zero links after a markup change, malformed feed) -- but
that line only reaches whoever happens to be reading a GitHub Actions run
log, and nobody does that until an episode already looks thin or wrong.
This module turns "a source has been silent for FAILURE_THRESHOLD
consecutive weekly runs" into something that cannot be missed: main() in
gamesforum_pipeline.py checks pending_alerts() and, if anything is
returned, the workflow's final step fails on purpose -- *after* the
episode has already been committed and pushed by the step before it. A
broken source degrades the show (fewer candidate articles) without ever
blocking it, but the run still turns red and GitHub emails the repo owner
about it by default. No new infrastructure, no new secret, no third-party
notification service -- just reusing what a failed Actions run already
does on its own.

A single quiet week from a source is not itself suspicious (a source can
legitimately have nothing new to say); FAILURE_THRESHOLD consecutive
misses in a row is the line between "quiet week" and "this is actually
broken." Deliberately dependency-free and side-effect-free beyond its own
JSON file, same reasoning as memory.py: this must never be able to fail a
run by itself.
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
HEALTH_FILE = ROOT / "source_health.json"

FAILURE_THRESHOLD = 2


def _load() -> dict:
    if not HEALTH_FILE.exists():
        return {}
    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # A corrupt health file should never take the show down with it --
        # worst case is one run's health tracking resets to zero.
        return {}


def record(raw_item_counts: dict[str, int]) -> None:
    """Update each source's consecutive-miss streak and persist.

    raw_item_counts: {source_name: how many raw items collect() got from it
    this run, before date filtering or cross-source dedup}. 0 means this
    run's fetch produced nothing at all, whatever the underlying reason --
    sources.py has already logged the specific one.
    """
    state = _load()
    for name, count in raw_item_counts.items():
        entry = state.setdefault(name, {"consecutive_misses": 0})
        entry["consecutive_misses"] = (
            0 if count > 0 else entry.get("consecutive_misses", 0) + 1
        )
    HEALTH_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def pending_alerts() -> list[str]:
    """Source names currently at or beyond FAILURE_THRESHOLD consecutive
    misses. Call after record() has run for this week.

    Keeps alerting every run until the source recovers or is removed from
    profile.toml, on purpose: a broken source should stay impossible to
    ignore, not fire once and go quiet while still broken.
    """
    state = _load()
    return sorted(
        name for name, entry in state.items()
        if entry.get("consecutive_misses", 0) >= FAILURE_THRESHOLD
    )
