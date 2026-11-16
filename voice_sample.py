#!/usr/bin/env python3
"""A short A/B sample for the owner's ears: the same passage voiced without and with vowel
marks on the ambiguous words (nikud.py). Writes samples/plain.mp3, samples/pointed.mp3 and
samples/README.md (the words that were pointed). Costs one text call and two speech calls.

    GEMINI_API_KEY=... python voice_sample.py [digests/<date>-script.md] [first turn] [turns]
"""
from __future__ import annotations

import pathlib
import sys

import gamesforum_pipeline as P
import llm
import nikud

OUT = pathlib.Path(__file__).resolve().parent / "samples"


def main() -> int:
    scripts = sorted((P.DIGESTS).glob("*-script.md"))
    path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else scripts[-1]
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 14
    turns = [{"speaker": line.split(": ", 1)[0], "text": line.split(": ", 1)[1]}
             for line in path.read_text(encoding="utf-8").splitlines() if ": " in line][start:start + count]
    llm.start_run()
    words = nikud.vocalize(turns, call=lambda p, s, **kw: llm.call_model(p, s, **kw),
                           model=llm.GEMINI_CREATIVE_MODEL, log=P.log)
    OUT.mkdir(exist_ok=True)
    P.synthesize_audio(turns, OUT / "plain.wav", OUT / "plain.mp3")
    P.synthesize_audio(nikud.apply(turns, words), OUT / "pointed.wav", OUT / "pointed.mp3")
    lines = [f"# Voice sample: {path.name}, turns {start + 1}-{start + count}", "",
             "`plain.mp3` is the passage as it was voiced until now; `pointed.mp3` has vowel marks on "
             "the words below. Listen to both and say which reads Hebrew better.", "",
             "| turn | word | pointed |", "| --- | --- | --- |"]
    lines += [f"| {w['turn']} | {w['word']} | {w['vocalized']} |" for w in words]
    lines += ["", "## The passage", ""] + [f"{i}. **{t['speaker']}:** {t['text']}" for i, t in enumerate(turns, 1)]
    (OUT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    P.log(f"sample written: {len(words)} words pointed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
