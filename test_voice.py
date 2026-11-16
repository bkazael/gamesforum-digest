"""Zero-token tests for the voice fixes of 2026-11-16: the pitch check that catches a chunk without
two distinct voices, re-voicing such a chunk, no two turns in a row by one speaker, and vowel marks."""
import math
import pathlib
import struct
import sys
import tempfile
import types
from unittest.mock import patch

import gamesforum_pipeline as P
import nikud as N
import speech
import voice_check as V

FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def voice(freqs, seconds=8, rate=24000):
    """Alternating one-second stretches of buzzy tones at the given pitches (16-bit mono PCM)."""
    out = bytearray()
    for sec in range(seconds):
        f = freqs[sec % len(freqs)]
        for i in range(rate):
            t = i / rate
            v = sum(math.sin(2 * math.pi * f * h * t) / h for h in (1, 2, 3))
            out += struct.pack("<h", int(9000 * v / 1.8))
    return bytes(out)


TWO = voice([85, 235])          # a male and a female register
SAME = voice([150, 165])        # what the bad chunk of 2026-11-16 looked like

# ---------------------------------------------------------------- the pitch check
if not V.available():
    check("numpy is installed for the voice check (requirements-dev.txt)", False)
else:
    ok, note, spread = V.judge(TWO)
    check("two distinct voices pass", ok and spread > 100, note)
    ok2, note2, spread2 = V.judge(SAME)
    check("two voices in the same register fail", not ok2 and spread2 < spread, note2)
    check("a chunk where only one host speaks is not judged", V.judge(SAME, both_speak=False)[0])
    check("too little speech is not judged (never a false alarm)", V.judge(b"\0\0" * 24000)[0])

    # ------------------------------------------------------------ a bad chunk is voiced again
    takes = []

    def tts(text, tries=4):
        takes.append(text)
        return SAME if len(takes) == 1 else TWO

    def ffmpeg_ok(cmd, **kw):
        pathlib.Path(cmd[-1]).write_bytes(b"mp3")
        return types.SimpleNamespace(returncode=0, stderr="")

    turns = [{"speaker": "Dana", "text": "שלום לכולם."}, {"speaker": "Yoni", "text": "היי דנה."}]
    with tempfile.TemporaryDirectory() as tmp, patch.object(P, "gemini_tts_chunk", tts), \
            patch.object(P.subprocess, "run", ffmpeg_ok), patch.object(P, "VOICE_REPORT", []) as report:
        P.synthesize_audio(turns, pathlib.Path(tmp) / "e.wav", pathlib.Path(tmp) / "e.mp3")
        check("a chunk without two distinct voices is voiced again, and the retake is recorded",
              len(takes) == 2 and len(report) == 1 and report[0]["chunk"] == 1, f"{len(takes)} takes, {report}")
    takes.clear()

    def always_bad(text, tries=4):
        takes.append(text)
        return SAME

    with tempfile.TemporaryDirectory() as tmp, patch.object(P, "gemini_tts_chunk", always_bad), \
            patch.object(P.subprocess, "run", ffmpeg_ok), patch.object(P, "VOICE_REPORT", []):
        P.synthesize_audio(turns, pathlib.Path(tmp) / "e.wav", pathlib.Path(tmp) / "e.mp3")
        check("retakes are bounded: three takes, then the best one ships (an episode is never blocked)",
              len(takes) == 1 + P.VOICE_RETRIES, str(len(takes)))

# ---------------------------------------------------------------- one speaker never twice in a row
merged = speech.merge_same_speaker([
    {"speaker": "Dana", "text": "סוף הסיפור."}, {"speaker": "Yoni", "text": "מסקנה."},
    {"speaker": "Yoni", "text": "וקישור לסיפור הבא."}, {"speaker": "Dana", "text": "פתיחה."}])
check("consecutive turns of one speaker are joined for the voice",
      [t["speaker"] for t in merged] == ["Dana", "Yoni", "Dana"] and merged[1]["text"] == "מסקנה. וקישור לסיפור הבא.")
sent = []
with tempfile.TemporaryDirectory() as tmp, patch.object(P, "gemini_tts_chunk", lambda text, tries=4: (sent.append(text), TWO)[1]), \
        patch.object(P.subprocess, "run", lambda cmd, **kw: (pathlib.Path(cmd[-1]).write_bytes(b"mp3"),
                                                              types.SimpleNamespace(returncode=0, stderr=""))[1]):
    P.synthesize_audio([{"speaker": "Dana", "text": "א."}, {"speaker": "Yoni", "text": "ב."},
                        {"speaker": "Yoni", "text": "ג."}, {"speaker": "Dana", "text": "ד."}],
                       pathlib.Path(tmp) / "e.wav", pathlib.Path(tmp) / "e.mp3")
lines = sent[0].split("\n")
check("the speech model never receives two lines in a row by the same host",
      all(a.split(":")[0] != b.split(":")[0] for a, b in zip(lines, lines[1:])), sent[0])

# ---------------------------------------------------------------- vowel marks
script = [{"speaker": "Dana", "text": "החברה פרסמה ספר חדש, והמספר עלה."}, {"speaker": "Yoni", "text": "זה דבר טוב."}]
items = [{"turn": 1, "word": "החברה", "vocalized": "הַחֶבְרָה"}, {"turn": 1, "word": "ספר", "vocalized": "סֵפֶר"},
         {"turn": 1, "word": "עלה", "vocalized": "עלו"},          # different letters
         {"turn": 2, "word": "חדש", "vocalized": "חָדָשׁ"},        # not in that turn
         {"turn": 9, "word": "x", "vocalized": "y"},              # no such turn
         {"turn": 1, "word": "ספר", "vocalized": "סָפַר"}]         # a second reading of the same word
words = N.accept(script, items)
check("a pointed form is accepted only with the same letters, in its own turn, once per word",
      words == [{"turn": 1, "word": "החברה", "vocalized": "הַחֶבְרָה"}, {"turn": 1, "word": "ספר", "vocalized": "סֵפֶר"}], str(words))
spoken = N.apply(script, words)
check("the pointed word replaces the whole word only (not the letters inside a longer word)",
      spoken[0]["text"] == "הַחֶבְרָה פרסמה סֵפֶר חדש, והמספר עלה." and script[0]["text"].startswith("החברה"))
check("removing the marks gives back the published text", N.strip_marks(spoken[0]["text"]) == script[0]["text"])
check("a failed call never raises and points nothing",
      N.vocalize(script, call=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503")), model="m") == [])
check("no list means the plain script is voiced", N.apply(script, None) == script)

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
