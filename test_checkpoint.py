#!/usr/bin/env python3
"""
Offline tests for checkpoint.py.
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import checkpoint as C

FAILS = []

def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

print("\n--- Testing checkpoint.py ---")

ARTICLES = [{"url": "https://example.test/a", "title": "A", "text": "t", "source": "S"}]
DATA = {"episode_title": "Ep", "digest_summary": [], "script": [{"speaker": "Dana", "text": "hi"}]}

with tempfile.TemporaryDirectory() as tmp:
    real_dir, real_work = C.CHECKPOINT_DIR, C.WORK_DIR
    C.CHECKPOINT_DIR = pathlib.Path(tmp) / ".checkpoint"
    C.WORK_DIR = pathlib.Path(tmp) / "work"
    try:
        # 1. Nothing saved yet -> nothing to resume from. This is the
        # normal weekly case and must never look like a resume.
        check("load_content on a fresh date returns None",
              C.load_content("2026-09-22") is None)
        check("load_chunk on a fresh date returns None",
              C.load_chunk("2026-09-22", 1) is None)

        # 2. Round-trip: what goes in comes back out intact, because the
        # whole resume path trusts this to replace a real Gemini call.
        C.save_content("2026-09-22", ARTICLES, DATA)
        resumed = C.load_content("2026-09-22")
        check("save_content/load_content round-trips", resumed is not None)
        if resumed:
            arts, data = resumed
            check("resumed articles survive the round trip", arts == ARTICLES)
            check("resumed script survives the round trip",
                  data["script"] == DATA["script"])

        # 3. Chunks are keyed by position -- reusing a chunk at the wrong
        # index would splice the episode together out of order.
        C.save_chunk("2026-09-22", 1, b"first")
        C.save_chunk("2026-09-22", 2, b"second")
        check("chunk 1 reads back exactly", C.load_chunk("2026-09-22", 1) == b"first")
        check("chunk 2 reads back exactly", C.load_chunk("2026-09-22", 2) == b"second")
        check("an unsynthesized chunk is still None",
              C.load_chunk("2026-09-22", 3) is None)

        # 4. Dates are isolated: yesterday's abandoned half-run must never
        # be mistaken for today's work.
        check("another date sees no content", C.load_content("2026-09-23") is None)
        check("another date sees no chunks", C.load_chunk("2026-09-23", 1) is None)

        # 5. A zero-byte chunk is an interrupted write, not silence worth
        # keeping -- reusing it would splice a gap into the audio.
        C.save_chunk("2026-09-22", 4, b"")
        check("an empty chunk is treated as missing, not as silence",
              C.load_chunk("2026-09-22", 4) is None)

        # 6. A corrupt checkpoint must degrade to "no checkpoint" (redo the
        # work), never raise into the run that's trying to resume.
        (C.WORK_DIR / "2026-09-24-content.json").write_text("not json")
        check("a corrupt content checkpoint reads as None, not a crash",
              C.load_content("2026-09-24") is None)

        # 7. A checkpoint with no script is half-written -- resuming from
        # it would fail confusingly much further downstream.
        C.save_content("2026-09-25", ARTICLES, {"episode_title": "x", "script": []})
        check("a scriptless checkpoint is rejected rather than half-resumed",
              C.load_content("2026-09-25") is None)

        # 8. clear() removes only the finished episode's files.
        C.clear("2026-09-22")
        check("clear() drops that date's content", C.load_content("2026-09-22") is None)
        check("clear() drops that date's chunks", C.load_chunk("2026-09-22", 1) is None)
        check("clear() leaves other dates alone",
              (C.WORK_DIR / "2026-09-24-content.json").exists()
              or not C.CHECKPOINT_DIR.exists())
    finally:
        C.CHECKPOINT_DIR, C.WORK_DIR = real_dir, real_work

# 9. REAL articles, not fixtures. Every article select() returns carries
# `published` as a datetime.date (sources.py parses it), and json.dumps
# raised on it -- "Object of type date is not JSON serializable" -- so
# save_content() failed silently on every real run and the checkpoint,
# with the cheap-retry design built on it, had never saved anything in
# production. Every fixture above has no date at all, which is exactly why
# nothing caught it; the first full end-to-end run (2026-10-04) did.
import datetime as dt

with tempfile.TemporaryDirectory() as tmp:
    real_dir, real_work = C.CHECKPOINT_DIR, C.WORK_DIR
    C.CHECKPOINT_DIR = pathlib.Path(tmp) / ".checkpoint"
    C.WORK_DIR = pathlib.Path(tmp) / "work"
    try:
        real_articles = [
            {"url": "https://example.test/b", "title": "B", "text": "t", "source": "PG",
             "published": dt.date(2026, 10, 2), "signals": {"words": 300},
             "_also": [{"title": "dup", "url": "u", "source": "x"}]},
            {"url": "https://example.test/c", "title": "C", "text": "t", "source": "MG",
             "published": dt.datetime(2026, 10, 3, 8, 30)},
            {"url": "https://example.test/d", "title": "D", "text": "t", "source": "GF",
             "published": None, "tags": {"a", "b"}},
        ]
        C.save_content("2026-10-04", real_articles, DATA)
        check("a checkpoint with real date-bearing articles actually gets written",
              (C.WORK_DIR / "2026-10-04-content.json").exists())
        back = C.load_content("2026-10-04")
        check("...and loads back", back is not None)
        if back:
            arts, data = back
            check("a date comes back as an ISO string (nothing after selection reads it as a date)",
                  arts[0]["published"] == "2026-10-02"
                  and arts[1]["published"].startswith("2026-10-03T08:30"),
                  f"{arts[0]['published']!r} / {arts[1]['published']!r}")
            check("everything else about the articles survives untouched",
                  arts[0]["_also"] == real_articles[0]["_also"]
                  and arts[2]["published"] is None)
            check("a set (not JSON-native) is saved as a sorted list rather than failing the save",
                  arts[2]["tags"] == ["a", "b"])
    finally:
        C.CHECKPOINT_DIR, C.WORK_DIR = real_dir, real_work

# ---------------------------------------------------------------- the week's text work lives in work/, audio in .checkpoint/
import tempfile as _tf
with _tf.TemporaryDirectory() as _tmp:
    _saved = (C.CHECKPOINT_DIR, C.WORK_DIR)
    C.CHECKPOINT_DIR, C.WORK_DIR = pathlib.Path(_tmp) / ".checkpoint", pathlib.Path(_tmp) / "work"
    try:
        C.save_content("2026-11-16", [{"title": "a"}], {"segments": {"0": {"status": "ok"}}})
        C.save_chunk("2026-11-16", 1, b"pcm")
        check("text work goes to work/, audio chunks to .checkpoint/",
              (C.WORK_DIR / "2026-11-16-content.json").exists()
              and not (C.CHECKPOINT_DIR / "2026-11-16-content.json").exists()
              and (C.CHECKPOINT_DIR / "2026-11-16-chunk-01.pcm").exists())
        C.clear("2026-11-16")
        check("clear() removes both after a published episode",
              not (C.WORK_DIR / "2026-11-16-content.json").exists() and C.load_chunk("2026-11-16", 1) is None)
        check("adopt_recent is gone: a catch-up run reads the same work/ file, no renaming",
              not hasattr(C, "adopt_recent"))
    finally:
        C.CHECKPOINT_DIR, C.WORK_DIR = _saved

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
