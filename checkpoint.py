#!/usr/bin/env python3
"""
Saved work for the episode being built.

Two kinds of state, kept in two places on purpose:

  WORK_DIR (`work/`, committed to git by the daily run)
      The week's text work: the selected articles, the plan, every story, the
      assembled script. Small JSON, visible in the repository, and it survives
      across the days the episode takes. It is what makes "write three stories
      today, three tomorrow" possible.

  CHECKPOINT_DIR (`.checkpoint/`, kept in the GitHub Actions cache, gitignored)
      Synthesized audio chunks (tens of MB of PCM). Only the publish day needs
      them, to survive a failed ffmpeg or a timeout without paying for speech again.

Keyed by the episode's target date. Nothing here may raise into the caller's
happy path: a corrupt or unreadable file means "nothing saved", never a failed run.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import runtime

ROOT = runtime.OUTPUT_ROOT
CHECKPOINT_DIR = ROOT / ".checkpoint"
WORK_DIR = ROOT / "work"


def _content_path(date: str) -> pathlib.Path:
    return WORK_DIR / f"{date}-content.json"


def _chunk_path(date: str, index: int) -> pathlib.Path:
    return CHECKPOINT_DIR / f"{date}-chunk-{index:02d}.pcm"


def _json_default(obj):
    """Serialize what json can't: every article carries `published` as a
    datetime.date (sources.py parses it), and json.dumps raised on it --
    "Object of type date is not JSON serializable" -- so save_content()
    failed, silently by design, on EVERY real run. The checkpoint, and with
    it the cheap-retry design the 06:00 and 10:00 reruns depend on, had
    never actually saved anything in production. Only the first real
    end-to-end run (2026-10-04) showed it: the test fixtures all had
    published=None. A date comes back as an ISO string, which nothing after
    selection reads as a date.
    """
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


def save_content(date: str, articles: list[dict], data: dict) -> None:
    """Store the selected articles + generated script for `date`.

    Called right after generate_podcast_content() succeeds -- i.e. after
    every Gemini *text* call the episode needs. Everything downstream of
    this point (TTS, ffmpeg, feed) can be retried without spending another
    text token.
    """
    try:
        WORK_DIR.mkdir(exist_ok=True)
        runtime.atomic_write(_content_path(date),
            json.dumps({"articles": articles, "data": data},
                       ensure_ascii=False, indent=2, default=_json_default),
        )
    except Exception as e:                              # noqa: BLE001
        # Failing to *save* a checkpoint must never fail the run that
        # produced the thing worth saving.
        print(f"::warning::checkpoint: could not save content ({e}) -- a failed run will restart from scratch", flush=True)


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
        # Either the assembled script, or the finished stories of an episode that
        # was interrupted mid-way (segments.py saves each one as it completes).
        if not data.get("script") and "segments" not in data:
            return None
        return articles, data
    except Exception:
        return None


def save_chunk(date: str, index: int, pcm: bytes, identity: str | None = None) -> None:
    """Store one synthesized audio chunk, by its position in the script."""
    try:
        CHECKPOINT_DIR.mkdir(exist_ok=True)
        path = _chunk_path(date, index)
        runtime.atomic_write(path, pcm)
        runtime.atomic_write(path.with_suffix(".json"), json.dumps({
            "identity": identity, "sha256": runtime.fingerprint(pcm.hex()), "bytes": len(pcm)}))
    except Exception as e:                              # noqa: BLE001
        print(f"::warning::checkpoint: could not save audio chunk {index} ({e}) -- a failed run will re-synthesize it", flush=True)


def load_chunk(date: str, index: int, identity: str | None = None) -> bytes | None:
    """Return a previously synthesized chunk, or None if we must redo it."""
    path = _chunk_path(date, index)
    if not path.exists():
        return None
    try:
        pcm = path.read_bytes()
        if identity is not None:
            meta = json.loads(path.with_suffix(".json").read_text())
            if (meta.get("identity") != identity or meta.get("bytes") != len(pcm)
                    or meta.get("sha256") != runtime.fingerprint(pcm.hex())):
                return None
        # A zero-byte chunk means an interrupted write, not silence worth
        # reusing -- redo it rather than splicing a gap into the episode.
        return pcm or None
    except Exception:
        return None


def clear(date: str) -> None:
    """Drop every saved file for `date`. Call only after a full success."""
    try:
        path = _content_path(date)
        path.unlink(missing_ok=True)
        for p in CHECKPOINT_DIR.glob(f"{date}-*"):
            p.unlink(missing_ok=True)
        if CHECKPOINT_DIR.exists() and not any(CHECKPOINT_DIR.iterdir()):
            shutil.rmtree(CHECKPOINT_DIR, ignore_errors=True)
    except Exception:                                   # noqa: BLE001
        pass
