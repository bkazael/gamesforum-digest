# Architecture

For developers. What the system is for: [README.md](README.md). How to run and fix it:
[OPERATIONS.md](OPERATIONS.md). Why it looks like this: [CHANGELOG.md](CHANGELOG.md) and
[AUDIT.md](AUDIT.md).

## The idea in one paragraph

Once a week, publish a Hebrew two-voice episode about the week's mobile-games news, from sources the
owner chooses, at no cost, in a quality the owner enjoys. The constraint that shapes everything is
the Gemini free tier: about 20 requests a day per model, resetting at midnight Pacific, with failed
attempts counted. So the episode is built over several days, a few stories at a time, each unit of
work is saved the moment it is done, and every model call is counted and budgeted.

## The week

`stages.py` decides, from the date and from what is already saved, what a daily run does. Day offsets
are from the target Monday and come from `[schedule]` in `profile.toml`.

```
Fri  select   collect -> block -> fetch -> score -> choose (discovery.py)
              plan: order, theme, an angle per story (segments.build_plan)
Fri  write    stories_per_run = remaining stories / writing days left, so about a third a day
Sat  write
Sun  write    the rest
Sun  edit     review_segments: ONE call for all stories; blocking issues -> one rewrite -> one more
              review -> else cut;  assemble_episode: ONE call for title, outro and bridges;
              intro (pipeline); proofread (proofread.py);  hold-back check
Mon  publish  voice (Gemini TTS chunks, cached) -> ffmpeg mix -> feed, state, memory, report
Tue, Wed      catch-up of whatever is missing; Wednesday is the last chance (failures are errors,
              quality holds are lifted)
```

Rules that make it robust:
- A stage is due by date *and* by state, never by which run it is: a missed day is picked up by the next.
- A day that exhausts a model's budget raises `GeminiQuotaExhausted`, the run exits 0, the next quota
  day continues from the saved work. A failure on a normal day is a warning with `last_error` in the
  plan; on the last chance day it is raised (a red run).
- `weekly-check.yml` (Wednesday 21:43 UTC) is the only alert: it fails if no episode exists for the week.
- Idempotent publish: an episode for the target week (mp3 and metadata) means every later run exits early.

## Modules

| Module | Responsibility |
| --- | --- |
| `stages.py` | The calendar, the plan file, the daily run; calls everything below |
| `llm.py` | The only Gemini text client: one call (`gemini_json`), the model chain (`call_model`), retired-model swap, retry/deadline budget, call counter and per-quota-day budget (`work/usage.json`) |
| `segments.py` | Plan, `Writer` (one story), `review_segments` (one review), `build_frame` (title, outro, bridges), `assemble_episode`; prompts and free checks (numbers, Hebrew share, stray scripts) |
| `proofread.py` | One call over the whole episode plus free cleanup; every edit guarded (same numbers, no new names, close to the original, limited deletes) |
| `discovery.py` | Stages 1-4 of selection: collect, title block, fetch and date, score; source mix and caps; ledger; first-seen dates; per-source yield |
| `sources.py`, `article_parser.py` | RSS/Atom, HTML listing and read-only email adapters; article text and the publication date |
| `gamesforum_pipeline.py` | Intro, TTS, ffmpeg, feed, show notes, `publish_episode`; re-exports the `llm` names tests patch |
| `checkpoint.py` | `work/` (committed text work) and `.checkpoint/` (cached speech chunks) |
| `script_quality.py`, `speech.py`, `audio_tools.py` | Deterministic text repair, term translation, chunking and PCM decoding |
| `memory.py`, `source_health.py`, `runtime.py` | Episode memory, source alerts (fetch and yield), atomic writes, profile validation |
| `check_week.py` | The Wednesday check |
| `preview.py`, `live_smoke.py`, `probe_source.py` | Isolated previews; a paid smoke test; the source probe that prints a ready block |

## Cohesion: how the pieces become one episode

Stories are written independently (a failure costs one story, they can be written on different days)
but are tied together in four places:
1. **Plan before writing**: one call orders the stories (best first, related ones adjacent; a partial
   email-preview story last), and gives a theme and an angle per story. They contain no digits, so
   they cannot smuggle a fact into a story. The writers see the theme and their angle, never another
   story's subject.
2. **Bridges after review**: the frame call writes one 12-30 word turn per neighbouring pair, from the
   two stories' verified key points only. `validate_bridge` refuses a number not in those two stories,
   a wrong length, English, an agreement opener. A refused bridge is simply absent.
3. **Outro** from the same call, using the theme.
4. **Intro** written last from the finished stories and the theme (separate call: its prompt and
   validators are tuned, so it was not merged into the frame call).

`feedback.md` (the owner's notes) goes to every story prompt as taste.

## Model routing and budget

Defaults are in `llm.py`; repo variables override them. Calls for an 8-story episode, per model
(most are never needed; rewrites are the variable part):

| Job | Model variable | Calls |
| --- | --- | --- |
| Scoring (about 70 candidates, batches of 20) | scoring | 4, plus a few duplicate checks |
| Plan | review | 1 |
| Story writing and rewrites | creative | 8-12 |
| Review of all stories (twice if something was blocked) | review | 1-2 |
| Frame (title, outro, bridges) | review | 1 |
| Intro, proofreading | creative | 2-3 |
| Speech | TTS | 3-5 chunks |

Each model's requests per quota day are counted (every HTTP attempt) and capped at
`GEMINI_DAILY_BUDGET` (16). `call_model` walks the chain `model, *GEMINI_QUOTA_FALLBACKS`, skipping
models that are spent, skipped for the run (two failed calls), or out of budget. A story records which
model wrote it; if more than half of the kept stories were written by the last-resort model the
episode is held back and those stories are rewritten by a better one on the next quota day (twice at
most, never on the last chance day).

## Data files

| Path | Written by | Committed |
| --- | --- | --- |
| `work/plan.json`, `work/<monday>-content.json`, `work/usage.json` | `stages`, `checkpoint`, `llm` | yes, by every run |
| `seen.json` | `discovery` (first-seen dates of undated links) | yes |
| `ledger/<date>.md`, `reports/source_yield.json`, `reports/<date>-*.json` | `discovery`, `stages`, `publish_episode` | yes |
| `episodes/*.mp3|json`, `digests/*`, `feed.xml`, `state.json`, `memory.json` | `publish_episode` | yes |
| `.checkpoint/<monday>-chunk-NN.pcm` | `checkpoint` | no (Actions cache) |

## Tests

`pytest -q` runs `check_suite.py`, which runs every `test_*.py` as its own test (they are scripts with a
small `check()` helper; `python test_x.py` shows every PASS/FAIL line). No network, no model.
`test_stages.py` replays whole weeks with a fake model, fake voice and fake calendar: spreading, budgets,
failures, hold-back, feedback. `test_segments.py` covers the writing/review/frame/plan logic,
`test_proofread.py` the guards, `test_dates_and_yield.py` dates, first-seen and per-source yield
(including the 2026-10 undated-source failure replayed end to end), `test_production.py` the client,
budget and audio contracts, `test_contracts.py` module wiring and schemas.

## Decisions and known gaps

- The intro stays a separate call (its prompt and validators are tuned). Title, outro and bridges are one call.
- Audio is committed to git (about 8.5 MB a week; 109 MB so far). `check_week.py` warns at 600 MB.
  Moving it to release assets needs a feed-URL migration that cannot be tested from here; do it when the warning appears.
- Git history contains commits made from a personal account (name and email); only a history rewrite changes that (see OPERATIONS.md, Privacy).
- Not done, on purpose: Pro models; a second speech provider beyond `TTS_FALLBACK_MODEL`; scraping paid or blocked content.
- Speech is a preview model (`gemini-2.5-flash-preview-tts`); `TTS_FALLBACK_MODEL` is the second choice, untested on audio quality.
- `[voice].direction` has not been A/B tested against the TTS model; keep it short.
