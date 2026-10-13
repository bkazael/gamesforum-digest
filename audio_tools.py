"""Text chunking and strict decoding for the audio transport contract."""
from __future__ import annotations

import io
import re
import wave


def chunks_for_turns(turns: list[dict], limit: int) -> list[str]:
    if limit < 100:
        raise ValueError("TTS chunk limit must be at least 100 characters")
    lines = []
    for turn in turns:
        prefix = f"{turn['speaker']}: "
        text = " ".join(turn["text"].split())
        # Prefer complete sentences; a very long sentence falls back to words.
        segments = re.split(r"(?<=[.!?…])\s+", text)
        current = prefix
        for segment in segments:
            units = [segment] if len(prefix + segment) <= limit else segment.split()
            for unit in units:
                if len(prefix + unit) > limit:
                    raise ValueError("A single TTS token exceeds the chunk limit")
                addition = (" " if current != prefix else "") + unit
                if len(current + addition) > limit:
                    lines.append(current)
                    current = prefix + unit
                else:
                    current += addition
        if current != prefix:
            lines.append(current)
    chunks, current = [], ""
    for line in lines:
        combined = f"{current}\n{line}" if current else line
        if len(combined) > limit:
            chunks.append(current)
            current = line
        else:
            current = combined
    if current:
        chunks.append(current)
    if not chunks:
        raise ValueError("Cannot synthesize an empty script")
    return chunks


def decode_pcm(payload: bytes, mime: str = "audio/L16;rate=24000") -> bytes:
    """Return mono little-endian 24 kHz s16 PCM, rejecting incompatible audio."""
    if payload.startswith(b"RIFF") or "wav" in mime.lower():
        with wave.open(io.BytesIO(payload), "rb") as f:
            if (f.getnchannels(), f.getsampwidth(), f.getframerate()) != (1, 2, 24000):
                raise ValueError("TTS WAV must be mono, 16-bit, 24 kHz")
            pcm = f.readframes(f.getnframes())
    else:
        if not re.match(r"audio/(?:l16|pcm)(?:;|$)", mime, re.I):
            raise ValueError(f"Unsupported TTS audio format: {mime}")
        rate = re.search(r"rate\s*=\s*(\d+)", mime, re.I)
        if rate and int(rate.group(1)) != 24000:
            raise ValueError(f"Unsupported TTS sample rate: {rate.group(1)}")
        pcm = payload
    if not pcm or len(pcm) % 2:
        raise ValueError("Empty or incomplete 16-bit PCM response")
    return pcm
