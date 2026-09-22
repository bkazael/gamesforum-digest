#!/usr/bin/env python3
"""
Offline tests for Gamesforum Digest v4.0 (JSON Schema + Gemini TTS Pipeline).
"""

import base64
import json
import os
import pathlib
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "test-key")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import gamesforum_pipeline as P

FAILS = []

def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

print("\n--- Testing v4.0 Architecture ---")

# 1. Test Script Word Count Calculation
mock_data = {
    "digest_summary": [{"title": "Test", "key_takeaway": "Takeaway", "metrics_mentioned": ["10%"]}],
    "script": [
        {"speaker": P.SPEAKER_A, "text": " " .join(["מילה"] * 300)},
        {"speaker": P.SPEAKER_B, "text": " " .join(["מילה"] * 250)}
    ]
}
words = sum(len(t["text"].split()) for t in mock_data["script"])
check("Word count calculation", words == 550, f"Got {words} words")

# 2. Test Length Safety Gate
check("Script passes word count floor (>=500 words)", words >= 500)

# 3. Test Text Chunking for Gemini TTS Batching
# Imports the real limit from gamesforum_pipeline instead of hand-copying
# the number, so this test can't silently drift out of sync with
# production the way it previously did (it used to test against 1500 while
# synthesize_audio() was already using 3800).
lines = [f"{turn['speaker']}: {turn['text']}" for turn in mock_data["script"]]
chunks, current_chunk, current_len = [], [], 0
for line in lines:
    if current_len + len(line) > P.TTS_CHUNK_CHAR_LIMIT and current_chunk:
        chunks.append("\n".join(current_chunk))
        current_chunk, current_len = [], 0
    current_chunk.append(line)
    current_len += len(line)
if current_chunk:
    chunks.append("\n".join(current_chunk))

check("Script turns grouped into small number of TTS chunks", len(chunks) <= 3, f"Got {len(chunks)} chunks")

# 4. build_feed() must recover an episode's date from ANY version suffix,
# not just "-v2". Written after a real incident (2026-08-31): the old code
# stripped the literal string "-v2" from the filename, so a "-v3" episode
# (created to get a fresh RSS guid after the original "-v2" guid had
# already gone out live once) failed strptime() and was silently dropped
# from the feed -- build_feed()'s except ValueError: continue swallowed it
# with no warning. This drives build_feed() end to end against a fixture
# episodes/ directory instead of testing the regex in isolation, so it
# would have caught the silent-drop, not just a parsing detail.
with tempfile.TemporaryDirectory() as tmp:
    tmp_path = pathlib.Path(tmp)
    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    real_episodes, real_root = P.EPISODES, P.ROOT
    P.EPISODES = episodes_dir
    P.ROOT = tmp_path
    try:
        for stem in ["2026-08-31-v3", "2026-08-24-v2", "2026-09-07"]:
            (episodes_dir / f"{stem}.mp3").write_bytes(b"fake audio")
            (episodes_dir / f"{stem}.json").write_text(
                json.dumps({"title": f"Episode {stem}", "summary": "s",
                            "notes": "<p>n</p>", "duration": 600}),
                encoding="utf-8",
            )
        P.build_feed()
        feed_text = (tmp_path / "feed.xml").read_text(encoding="utf-8")
        item_count = feed_text.count("<item>")
        check("build_feed() keeps a -v3 episode instead of silently dropping it",
              "bens-digest-2026-08-31-v3" in feed_text)
        check("build_feed() also keeps a plain, no-suffix dated filename",
              "bens-digest-2026-09-07" in feed_text)
        check("build_feed() produced all 3 fixture episodes, not fewer",
              item_count == 3, f"got {item_count} <item> entries")
    finally:
        P.EPISODES, P.ROOT = real_episodes, real_root

# 5. gemini_tts_chunk() must not silently hand back truncated audio.
# Real incident (2026-09-07): a chunk's audio hit the model's own output
# ceiling mid-sentence, the API returned finishReason=MAX_TOKENS along with
# whatever partial inlineData it had produced, and the old code returned
# that partial audio as if it were the complete chunk -- nothing checked
# finishReason at all. This mocks urlopen (not gemini_tts_chunk itself) so
# it actually exercises the response-parsing code that had the bug.
class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def _tts_response(finish_reason):
    payload = {
        "candidates": [{
            "finishReason": finish_reason,
            "content": {"parts": [{"inlineData": {"data": base64.b64encode(b"fake-pcm").decode()}}]},
        }]
    }
    return _FakeResponse(json.dumps(payload).encode())

with patch("time.sleep"):
    with patch("urllib.request.urlopen", return_value=_tts_response("STOP")):
        result = P.gemini_tts_chunk("some script text", tries=2)
        check("gemini_tts_chunk() returns audio when finishReason is STOP", result == b"fake-pcm")

    with patch("urllib.request.urlopen", return_value=_tts_response("MAX_TOKENS")):
        try:
            P.gemini_tts_chunk("some script text", tries=3)
            check("gemini_tts_chunk() raises instead of returning truncated audio", False,
                  "no exception was raised")
        except P.TTSTruncatedError:
            check("gemini_tts_chunk() raises instead of returning truncated audio", True)
        except Exception as e:
            check("gemini_tts_chunk() raises instead of returning truncated audio", False,
                  f"wrong exception type: {type(e).__name__}: {e}")

# 6. synthesize_audio() must actually REUSE checkpointed chunks rather
# than re-synthesizing them. This is the whole point of checkpoint.py and
# the failure mode would be silent: a resumed run that quietly pays for
# every chunk again still produces a correct episode, so only a test that
# counts API calls can tell the difference. Written after 2026-09-21,
# where a run finished 2 of 4 chunks, died on the third, and kept nothing.
import checkpoint as CP

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = pathlib.Path(tmp)
    real_ckpt, real_sleep = CP.CHECKPOINT_DIR, P.time.sleep
    real_tts, real_run = P.gemini_tts_chunk, P.subprocess.run
    CP.CHECKPOINT_DIR = tmp_path / ".checkpoint"
    P.time.sleep = lambda *_a, **_k: None          # no 25s waits in tests
    # fake_run below stands in for ffmpeg so the suite needs no audio tools

    calls = []
    def fake_tts(text, *_a, **_k):
        calls.append(text)
        return b"NEW" + bytes([len(calls)])
    P.gemini_tts_chunk = fake_tts

    # Stand in for ffmpeg: capture the wav it would read (synthesize_audio
    # deletes it straight after) and produce the mp3 it would write, so the
    # function can finish and we can still assert on the audio it built.
    produced = {}
    def fake_run(cmd, *_a, **_k):
        for i, arg in enumerate(cmd):
            if arg == "-i" and str(cmd[i + 1]).endswith(".wav"):
                produced["pcm"] = pathlib.Path(cmd[i + 1]).read_bytes()
        pathlib.Path(cmd[-1]).write_bytes(b"fake mp3")
    P.subprocess.run = fake_run

    try:
        # One turn per chunk: long enough that each exceeds the limit on
        # its own, so the chunker can't merge them and indices are stable.
        turns = [{"speaker": P.SPEAKER_A, "text": "x" * (P.TTS_CHUNK_CHAR_LIMIT + 10)},
                 {"speaker": P.SPEAKER_B, "text": "y" * (P.TTS_CHUNK_CHAR_LIMIT + 10)}]

        # Pretend a previous attempt finished chunk 1 only.
        CP.save_chunk("2026-09-22", 1, b"CACHED")

        wav, mp3 = tmp_path / "a.wav", tmp_path / "a.mp3"
        P.synthesize_audio(turns, wav, mp3, date="2026-09-22")

        check("a checkpointed chunk is not re-synthesized",
              len(calls) == 1, f"made {len(calls)} TTS call(s), expected 1")

        pcm = produced.get("pcm", b"")
        check("the reused chunk's audio is actually in the output",
              b"CACHED" in pcm)
        check("the newly synthesized chunk is in the output too",
              b"NEW" in pcm)
        check("chunk 2 was checkpointed after being synthesized",
              CP.load_chunk("2026-09-22", 2) is not None)

        # With no date, checkpointing is off entirely -- the stateless
        # behaviour the older tests and any ad-hoc use rely on.
        calls.clear()
        P.synthesize_audio(turns, wav, mp3)
        check("without a date, nothing is reused and both chunks are synthesized",
              len(calls) == 2, f"made {len(calls)} TTS call(s), expected 2")
    finally:
        CP.CHECKPOINT_DIR = real_ckpt
        P.time.sleep, P.gemini_tts_chunk, P.subprocess.run = real_sleep, real_tts, real_run

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)