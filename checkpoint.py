#!/usr/bin/env python3
"""
Resume support for a partially-completed run.

The problem this exists to solve, concretely: on 2026-09-21 the pipeline
spent ~3 minutes on discovery and scoring (4 batched Gemini calls), ~2
minutes generating a 1915-word script (1 more Gemini call), then
successfully synthesized 2 of 4 audio chunks -- and threw *all* of it away
because chunk 3 hung and the CI step hit its timeout. The next attempt
would have started from zero and spent the same quota again. That is also
exactly how 2026-09-14/15 burned through a whole day's free-tier request
allowance on a single episode: two full attempts, one published episode.

So: each expensive stage writes its result under CHECKPOINT_DIR, keyed by
the episode date, the moment it succeeds. A later run for the same date
picks up whatever is already there and only does what's missing. A run
that finishes cleanly calls clear() and leaves nothing behind.

Design notes:
  - Keyed by date, so yesterday's half-finished run can never be mistaken
    for today's. A new day starts genuinely fresh.
  - Audio chunks are stored as raw PCM, indexed by position, because
    synthesize_audio() concatenates them in order -- a chunk is only
    reusable if we know exactly where it belongs.
  - Nothing here may raise into the caller's happy path: a corrupt or
    unreadable checkpoint means "no checkpoint", never a failed run. The
    worst case has to stay "redo the work", which is just today's
    behaviour.
  - CHECKPOINT_DIR is gitignored. It is scratch state for one episode in
    flight, not project history.
"""

from __future__ import annotations

import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent
CHECKPOINT_DIR = ROOT / ".checkpoint"


def _content_path(date: str) -> pathlib.Path:
    return CHECKPOINT_DIR / f"{date}-content.json"


def _chunk_path(date: str, index: int) -> pathlib.Path:
    return CHECKPOINT_DIR / f"{date}-chunk-{index:02d}.pcm"


def save_content(date: str, articles: list[dict], data: dict) -> None:
    """Store the selected articles + generated script for `date`.

    Called right after generate_podcast_content() succeeds -- i.e. after
    every Gemini *text* call the episode needs. Everything downstream of
    this point (TTS, ffmpeg, feed) can be retried without spending another
    text token.
    """
    try:
        CHECKPOINT_DIR.mkdir(exist_ok=True)
        _content_path(date).write_text(
            json.dumps({"articles": articles, "data": data},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:                              # noqa: BLE001
        # Failing to *save* a checkpoint must never fail the run that
        # produced the thing worth saving.
        print(f"[pipeline]   checkpoint: could not save content ({e})", flush=True)


def load_content(date: str) -> tuple[list[dict], dict] | None:
    """Return (articles, data) from an earlier attempt at `date`, or None."""
    path = _content_path(date)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        articles, data = payload["articles"], payload["data"]
        # A checkpoint missing the one field the whole rest of the run is
        # built on is worse than no checkpoint -- treat it as absent rather
        # than half-resuming into a confusing failure further downstream.
        if not data.get("script"):
            return None
        return articles, data
    except Exception:
        return None


def save_chunk(date: str, index: int, pcm: bytes) -> None:
    """Store one synthesized audio chunk, by its position in the script."""
    try:
        CHECKPOINT_DIR.mkdir(exist_ok=True)
        _chunk_path(date, index).write_bytes(pcm)
    except Exception as e:                              # noqa: BLE001
        print(f"[pipeline]   checkpoint: could not save chunk {index} ({e})", flush=True)


def load_chunk(date: str, index: int) -> bytes | None:
    """Return a previously synthesized chunk, or None if we must redo it."""
    path = _chunk_path(date, index)
    if not path.exists():
        return None
    try:
        pcm = path.read_bytes()
        # A zero-byte chunk means an interrupted write, not silence worth
        # reusing -- redo it rather than splicing a gap into the episode.
        return pcm or None
    except Exception:
        return None


def clear(date: str) -> None:
    """Drop every checkpoint for `date`. Call only after a full success."""
    try:
        for path in CHECKPOINT_DIR.glob(f"{date}-*"):
            path.unlink(missing_ok=True)
        # Tidy the directory away too once it has nothing left in it, so a
        # healthy repo has no .checkpoint/ lying around at all.
        if CHECKPOINT_DIR.exists() and not any(CHECKPOINT_DIR.iterdir()):
            shutil.rmtree(CHECKPOINT_DIR, ignore_errors=True)
    except Exception:
        pass
