# Ben's Weekly Digest — Gamesforum Digest

An automated weekly Hebrew podcast for mobile-game operators, hosted by Dana
and Yoni. It finds recent reporting, selects relevant developments, prepares
discussion notes, writes and reviews a conversational script, synthesizes
two-speaker audio, and publishes an RSS feed.

The audience works in casual/puzzle, hybrid-casual and real-money skill games.
Topics emphasize competitors, monetization, UA, LiveOps, payments, store
policies and useful market developments.

## Quick start

Requires Python 3.12+ and `ffmpeg` / `ffprobe`. Runtime modules use the standard
library only. Paid generation needs `GEMINI_API_KEY` in the environment.

```sh
# Offline tests, no model cost. Each legacy test runs as its own script.
for test in test_*.py; do python3.12 "$test" || exit 1; done

# Live sources, no model calls or changes to the published artifacts.
python3.12 preview.py --discovery-only

# Generate and review a script without paying for audio.
python3.12 preview.py --script-only

# Complete isolated episode, resuming saved paid work where available.
python3.12 preview.py
```

Previews go to the Git-ignored `preview_output/`. In GitHub Actions, run **Live
smoke test** on the desired branch with `smoke`, `script`, or `preview` mode.
The output becomes a downloadable artifact; that workflow never publishes.

Production uses `python3.12 gamesforum_pipeline.py` with `PODCAST_BASE_URL`
set to the absolute public hosting URL. The weekly publishing workflow is
limited to `main` and retries automatically. Completion requires the full
publication manifest, not just an MP3 on disk.

## Components

| Module | Responsibility |
| --- | --- |
| `sources.py`, `article_parser.py` | RSS/Atom, HTML and read-only email; article text and publication dates |
| `discovery.py` | Bounded retrieval, freshness, relevance scoring, dedupe, source mix and airtime |
| `gamesforum_pipeline.py` | Evidence preparation, dialogue, intro, speech, audio mixing and RSS |
| `editorial.py`, `script_quality.py` | Final evidence/coverage/naturalness gate and deterministic text checks |
| `audio_tools.py` | Bounded text chunks and strict audio-format decoding |
| `checkpoint.py`, `runtime.py` | Verified speech reuse, crash-safe writes, paths and validated configuration |
| `memory.py`, `source_health.py` | Episode continuity and source diagnostics |

Edit `profile.toml` for the audience, sources, interests, thresholds, airtime
and voices. Standard RSS/Atom additions need only another `[[sources]]` entry.
Current settings select at most six stories: 30% planned airtime for the lead,
20% for the second, and 50% shared by the remainder.

Inspect `ledger/`, `digests/`, `reports/<date>-editorial.json` and
`reports/<date>-run.json` to understand selection, the exact spoken text,
review decisions, models, duration and usage. A reviewer model can miss errors;
listen to the preview before approving a major prompt, voice or model change.

[OPERATIONS.md](OPERATIONS.md) documents all environment variables, tuning,
source onboarding, previews, failure recovery and rollback.
[AUDIT.md](AUDIT.md) records the review findings and validation evidence.
[CHANGELOG.md](CHANGELOG.md) retains the earlier incident history.
