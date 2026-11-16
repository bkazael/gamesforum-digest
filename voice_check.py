"""Does a voiced chunk really contain the two hosts' two voices?

Why (2026-11-16): the episode is voiced in several calls, and the speech model gave one
of them (minutes 5-8.5) two voices in the same low register; the listener heard the voices
change and then "one person asking and answering herself". Nothing in the pipeline listens,
so nothing noticed.

The check is a pitch measurement, no model: with a female and a male voice the pitch of
the voiced frames spreads from about 75-90 Hz (10th percentile) to 220-270 Hz (90th).
Measured on three published episodes, good passages never fell below 200 at the top; the
bad one sat at 182 with its bottom at 108. A chunk whose spread is too narrow is voiced
again (synthesize_audio), and the widest take is kept.

Needs numpy; without it the check is skipped with a warning (the production code is
otherwise stdlib-only, so the workflow installs numpy explicitly).
"""
from __future__ import annotations

HIGH_MIN_HZ = 195.0      # 90th percentile of voiced pitch must reach the female register
LOW_MAX_HZ = 102.0       # 10th percentile must reach the male register
MIN_VOICED_FRAMES = 150  # below this there is not enough speech to judge


def available() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def pitch_percentiles(pcm: bytes, rate: int = 24000) -> tuple[float, float, int] | None:
    """(10th percentile, 90th percentile, voiced frames) of the pitch in 16-bit mono PCM,
    or None when numpy is missing or there is too little voiced audio."""
    try:
        import numpy as np
    except ImportError:
        return None
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
    step = max(1, rate // 8000)                    # 8 kHz is plenty for 70-320 Hz
    x, r = x[::step], rate // step
    n, hop = int(0.04 * r), int(0.05 * r)
    lo, hi = int(r / 320), int(r / 70)
    pitches = []
    for s in range(0, len(x) - n - hi, hop):
        f = x[s:s + n]
        if float(np.sqrt((f * f).mean())) < 0.02:
            continue
        f = f - f.mean()
        seg = x[s:s + n + hi]
        seg = seg - seg.mean()
        ac = np.array([float((f * seg[lag:lag + n]).sum()) for lag in range(lo, hi)])
        k = int(ac.argmax())
        if ac[k] / (float((f * f).sum()) + 1e-9) > 0.45:
            pitches.append(r / (lo + k))
    if len(pitches) < MIN_VOICED_FRAMES:
        return None
    return (float(np.percentile(pitches, 10)), float(np.percentile(pitches, 90)), len(pitches))


def judge(pcm: bytes, both_speak: bool = True, rate: int = 24000) -> tuple[bool, str, float]:
    """(ok, note, spread). A chunk in which only one host speaks is not judged."""
    if not both_speak:
        return True, "one speaker only, not judged", 0.0
    got = pitch_percentiles(pcm, rate)
    if got is None:
        return True, "not measured (numpy missing or too little speech)", 0.0
    low, high, _ = got
    ok = high >= HIGH_MIN_HZ and low <= LOW_MAX_HZ
    return ok, f"pitch {low:.0f}-{high:.0f} Hz", high - low
