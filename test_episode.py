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

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)