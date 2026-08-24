#!/usr/bin/env python3
"""
Episode memory. Keeps the show sounding like a continuing program instead of
six unrelated one-off episodes.

The design constraint that shapes this file: it must never cost a Gemini
call, and it must never be able to fail a run. Both follow from the same
decision -- every memory entry is derived deterministically from
`digest_summary`, which generate_podcast_content() already returns as part
of the normal script-generation call. There is nothing left to ask an LLM
for, so nothing here can time out, get rate-limited, or drift out of schema.

Two different sizes, on purpose:
  - memory.json on disk keeps every episode ever recorded. It is the show's
    archive, and it costs nothing to keep all of it.
  - The prompt only ever sees the last `lookback_episodes` (default 3) of
    them. That number is what actually matters for cost: it is what caps
    token spend on every future episode, regardless of how large the
    archive on disk grows.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
MEMORY_FILE = ROOT / "memory.json"
PROFILE_FILE = ROOT / "profile.toml"

DEFAULT_LOOKBACK = 3


def _lookback_episodes() -> int:
    """[memory].lookback_episodes from profile.toml, defaulting to 3.

    Reads the file directly rather than taking a parameter, so the caller in
    gamesforum_pipeline.py doesn't need to know this config exists -- the
    same pattern _load_voice() already uses there for [voice].
    """
    if PROFILE_FILE.exists():
        try:
            import tomllib
            with PROFILE_FILE.open("rb") as f:
                cfg = tomllib.load(f).get("memory", {})
                n = cfg.get("lookback_episodes")
                if isinstance(n, int) and n > 0:
                    return n
        except Exception:
            pass
    return DEFAULT_LOOKBACK


def _load_all() -> list[dict]:
    if not MEMORY_FILE.exists():
        return []
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        # A corrupt memory file should never take the show down with it --
        # worst case is one episode loses continuity, not that generation
        # fails outright.
        return []


def load_recent_context(limit: int | None = None) -> str:
    """Format the last N episodes for the script prompt.

    Returns "" (not a placeholder sentence) when there is no history yet, so
    generate_podcast_content() can skip the whole prompt section on the
    first run after this ships, rather than telling the model about an
    empty past.
    """
    episodes = _load_all()
    if not episodes:
        return ""
    n = limit if limit is not None else _lookback_episodes()
    recent = episodes[-n:] if n > 0 else episodes
    lines = [
        f"- פרק מ-{ep.get('date', '?')} ({ep.get('title', '')}): "
        f"{ep.get('key_takeaways', '')}"
        for ep in recent
    ]
    return "\n".join(lines)


def build_entry(episode_title: str, digest_summary: list[dict],
                 date: str | None = None) -> dict:
    """Derive a memory entry from digest_summary. No LLM call.

    digest_summary is the structured per-topic breakdown
    generate_podcast_content() already produces for the episode's show
    notes (title, key_takeaway, metrics_mentioned per topic). Reusing it
    here means a memory entry costs nothing extra to produce and can never
    itself fail to generate.
    """
    topics = [item.get("title", "") for item in digest_summary if item.get("title")]
    takeaways = " | ".join(
        item.get("key_takeaway", "") for item in digest_summary if item.get("key_takeaway")
    )
    existing = _load_all()
    return {
        "episode_id": f"ep_{len(existing) + 1:03d}",
        "date": date or dt.date.today().isoformat(),
        "title": episode_title,
        "topics_covered": topics,
        "key_takeaways": takeaways,
    }


def append_entry(entry: dict) -> None:
    """Append one entry and persist. Call only after a full run succeeds --
    gamesforum_pipeline.py does this last, so a failed run never leaves a
    phantom episode in the archive."""
    episodes = _load_all()
    episodes.append(entry)
    MEMORY_FILE.write_text(
        json.dumps(episodes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
