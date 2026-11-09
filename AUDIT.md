# Production and listening-quality audit — 2026-10-13

Scope: ingestion, ranking, story selection, preparation, Hebrew dialogue,
speech synthesis, audio assembly, checkpoint recovery, RSS publication and CI.
Reviewed archived scripts from September 22/28 and October 5/12, along with
episode source lists and selection ledgers. Publisher claims in those archived
scripts were not independently fact-checked.

## Findings and changes

| Finding | Listener/operational impact | Change |
| --- | --- | --- |
| October 12 gave 40% planned airtime to an AI opinion interview elevated from 8 to 10 by a format bonus | Opinion crowded out mobile-business developments; six other stories got about 5% each | Bound source bonuses, prioritize new evidence, six-story ceiling and 30/20/50 airtime |
| September 22 describes proposed EU restrictions as requirements, while its own source title says proposed | Claim status can become stronger in narration than in source material | Review complete draft against source text; block material factual/attribution errors |
| Long roadmap intros and repetitive question/answer/affirmation patterns | Slow opening and interchangeable host roles | Shorter intro, varied exchanges, reviewer checks for repetition and omitted stories |
| Article text extraction used all content after the first h1; HTML source dates were unknown | Sidebar noise and old articles could reach ranking | Structured article extraction, publication metadata, explicit date gate |
| A 250-word minimum rejected concise mobile announcements | Useful short news was filtered before relevance scoring | Lower the floor to 120 and let relevance/evidence decide |
| Sequential downloads and first-source-first fetch cap | Added sources would slow runs and later sources could be starved | Four-worker retrieval and source-balanced fetch budget |
| All URL query parameters removed; whole URLs lowercased for dedupe | Distinct query-addressed stories could collide | Strip tracking only, preserve meaningful IDs and path case |
| Newsletter parsing marked messages read before episode success | A later failure could permanently lose an article | Read-only date-bounded IMAP collection |
| Audio checkpoints used only date + position | A changed script/voice could reuse incompatible speech | Content/config fingerprint and corruption detection |
| Oversized turns escaped the chunk limit; output assumed raw PCM | Oversized requests or corrupt mixed-format audio | Sentence-aware splitting with word fallback; explicit PCM/WAV validation |
| Jingle mixing used automatic amix normalization and unbounded outro | Speech level could change after the intro; excessive outro | Explicit mix gains, bounded fades and mono loudness normalization |
| ffprobe failure returned a fabricated five-minute duration | Invalid audio could look publishable | Probe failure blocks publication; coarse speech-rate sanity check |
| MP3 existence alone skipped reruns | Failure after audio but before RSS/state could strand an episode | Completion manifest only after all output/state steps |
| API retries included permanent errors; request timeouts ignored remaining budget | Wasted quota/time and hard kills | Fail fast on permanent errors, bound timeouts/backoff by remaining budget |
| API key in URL; text usage counters never updated | Easier accidental key logging, misleading cost diagnostics | Auth header and response usage counters |
| Workflow cache key omitted run attempt; publishing staged every file | Rerun caches could be immutable hits; unrelated files might be committed | Attempt/code-aware cache keys and explicit publishing paths |

## Validation so far

- Original ten test scripts pass under Python 3.12.
- New production regression suite covers extraction, Atom links, canonical
  URLs, audio chunk boundaries/format/cache integrity, API behavior, deadline
  budget, editorial revision/failure/coverage, atomic writes and memory retries.
- Real local ffmpeg test rendered and probed a playable MP3 with the jingle.
- Live discovery on October 13 fetched all four configured sources and found
  65 readable candidates after blocking and date checks, without model calls.
- All eleven test scripts pass under Python 3.12; GitHub zero-token CI passed.
- Full GitHub preview succeeded: six stories, 1,570 script words, 55 dialogue
  turns, 12:21 audio duration, 128.3 words/minute, 8.5 MB MP3, 24 kHz mono.
- The editorial review rated naturalness 4/5 and focus 4/5 and found all six
  selected stories present. It logged four non-blocking follow-ups, including
  one inference about unbanked users and a disconnected transition. The review
  did not call the draft exceptional, and these issues are visible for human
  review in the downloadable preview artifact.
- Decoded MP3 measured -19.1 dB mean and -3.2 dB peak, without clipping.

These checks do not constitute a human listening test or independent fact
verification. Final listening quality must be assessed on the generated preview;
the production gate provides a repeatable minimum, not a guarantee of perfection.

See [OPERATIONS.md](OPERATIONS.md) for configuration, isolated previews, adding
sources, retry behavior and rollback.
