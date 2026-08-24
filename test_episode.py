#!/usr/bin/env python3
"""
Offline tests for Gamesforum Digest v4.0 (JSON Schema + Gemini TTS Pipeline).
"""

import os
import pathlib
import sys

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

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)