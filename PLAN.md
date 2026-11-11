# Operating plan: a weekly episode that ships itself

Written 2026-11-10 after a full review of the code, the CI, the last eight
production runs and their logs. It covers what goes wrong today and why,
the fixes in order, which model does which job (and why that saves money),
and a small set of cheap periodic checks with an owner role for each.

## 1. Where the project stands

**What works.** Five sources collect reliably (Gamesforum, PocketGamer.biz,
PocketGamer.com, MobileGamer.biz, Gamigion's site). Selection, the ledger,
memory, the checkpoint, TTS, the jingle mix and the RSS feed have been stable
for weeks. CI runs 12 zero-cost test files on every push.

**What has gone wrong in the last four weeks** (from the run logs, not guesses):

| Date | Run(s) | Root cause | Class |
| --- | --- | --- | --- |
| 10-26 | 53, 54 | 402: the API key's project was on prepaid billing with no credit | account |
| 10-27 | 56-58 | 404: a new key could not use the retired 2.5 models | account / Google |
| 10-27 | 59, 60 | 503 overload on the script call, four tries in one minute | design (one giant call) |
| 10-19 to 11-02 | published | episodes of 902-1,258 words (8-11 min) | design (one giant call) |
| 11-09 | 65-70 | 503s and timeouts, then **429: free tier, 20 requests/day per model**, spent by manual re-runs | quota |
| 11-10 | 72 | one scoring batch failed after 10 min of 503s and was never retried; stopped at 75% coverage | bug (fixed in f2559e8) |
| 11-10 | 73 | 35 min of 503s on scoring (all scored), then 503s on every story until the run's deadline | Google overload |
| 11-10 | 74 | a code push changed the checkpoint cache key, so the paid selection was lost; the writing model's quota was already spent **by the 503 retries of run 73**; one reviewer outage then ended the run | design + quota (fixed in this commit) |

**The most important finding of the day:** on the free tier, requests that
come back 503 still count against the 20-a-day quota. Run 73's story calls
returned nothing but 503s and still used up `gemini-3.8-flash` for the day.
On the free tier, patient retries during an outage burn the quota they're
waiting for.

Three of the six causes are about the Google account and quota rather than
the code, and the code made each of them worse. It retried too fast, put
everything into one call that any failure threw away, and had no answer for
a daily quota.

**What changed on 11-09/10** (in `main`):

- The script is written as one segment per story (`segments.py`): one small
  call per article, three at a time, each checkpointed when it finishes.
- Length comes from coverage. Every number the model lists as a key point must
  be said, and a headline-only story is rewritten once. There is no word target.
- A per-story fact review checks each story against its own article. A story
  whose factual error survives one rewrite is cut, and the episode still
  publishes.
- Calls wait out overload with patient retries (30s, 1, 2, 4, 5 min).
- When a model's daily quota is spent, calls move to the next model.
- A failed scoring batch is retried once, and the selection is checkpointed.
- Scoring, review and writing use three different models, so each draws on
  its own daily quota.
- About 600 lines were deleted, including the whole-draft editorial
  rewrite and the fallback chains.

## 2. The decision that matters most: the free tier

The free tier allows **about 20 requests a day per model**. One episode uses:

| Stage | Calls per episode | Model (now) |
| --- | --- | --- |
| Relevance scoring + dedupe | 5-8 | scoring (flash-lite) |
| Stories (6) + rewrites | 6-9 | creative (3.8-flash) |
| Fact review per story (+ re-review) | 6-8 | review (3.5-flash) |
| Title + outro | 1 | review (3.5-flash) |
| Intro | 1-2 | creative (3.8-flash) |
| Speech | 3-6 chunks | TTS model |

Spread over three models that is at most ~13 of 20 on any one model, which
leaves room for a resume but not for a full second attempt the same day. Every manual re-run or preview spends the
same budget. Two ways forward:

- **A. Stay free.** This works if nobody re-runs by hand on Mondays. Spread
  the load over three models (P1-1 below), and accept that a bad Google day
  delays the episode to the next scheduled run.
- **B. Attach a normal billing account (pay-as-you-go, not prepaid).**
  Quotas rise by orders of magnitude and the whole 429 class disappears. My
  estimate is ~150k input and ~45k output text tokens plus ~15 min of speech
  per episode, roughly **$0.30-0.60 an episode, about $2 a month**. Verify on
  the AI Studio billing page after the first paid week. A spending cap in the
  Google Cloud console removes the risk of surprises.

**Recommendation: B, and more strongly after 11-10.** It is the single change
that removes the most failure modes for the least money. On the free tier a
Google overload day cannot be waited out, because the waiting itself spends
the quota. Everything below works under either option.

## 3. Which model does which job

The rule is to use the cheapest model that is good enough for the job, and
the strong model only where wording is the product.

| Job | Model | Why |
| --- | --- | --- |
| Relevance scoring, dedupe | `gemini-3.5-flash-lite` (`GEMINI_SCORING_MODEL`) | Classification on short summaries; volume is the cost |
| Story writing | `gemini-3.8-flash` (`GEMINI_CREATIVE_MODEL`) | The Hebrew dialogue is the product |
| Intro | creative model | Same reason; 1-2 calls |
| Fact review per story | `gemini-3.5-flash` (`GEMINI_REVIEW_MODEL`, added 11-10) | Better at catching wrong numbers than lite, and has its own quota |
| Title + outro | review model | Small, structured |
| Proofreading, whole episode (added 11-11) | creative model, falls back down the chain | One call; Hebrew spelling and grammar need the strongest Hebrew model; never blocks the run |
| Speech | `gemini-2.5-flash-preview-tts` (`TTS_MODEL`) | Only TTS model in use; swap if retired (automatic on a 404 that names a replacement) |
| Pro models | not used | Slower, pricier, and the quality gap is not where our failures are |

The Claude side (this assistant) uses the same idea: a small model for
routine checks, a strong one only for design.

| Role | Claude model | When |
| --- | --- | --- |
| QA: Monday run check | Sonnet | Every Monday, scheduled (section 5) |
| Dev: fixes, CI, model/API changes | Sonnet | When the QA check finds a bug |
| Content editor: script quality | Sonnet | Monthly, scheduled |
| Voice specialist: pronunciation, pace | Sonnet + your ears | Monthly, with the content review |
| Architect: design review, Google deprecations | Opus | Quarterly, or when something new breaks twice |

## 4. Work plan, in order

### P0: this week (done or running)

1. Fix the scoring retry, and checkpoint the selection: **done** (f2559e8).
2. Publish this week's episode: **running** (manual run after the fix).
3. You decide on billing (section 2). If B: AI Studio, project, attach
   billing, set a monthly cap. No code change is needed.

### P1: next two weeks (small, each with tests)

1. **Spread quota over three models: done 11-10.** `GEMINI_REVIEW_MODEL`
   (default `gemini-3.5-flash`) does review, title and outro, so scoring,
   review and writing each draw on their own daily quota.
2. **Read the first real segment episode.** Check turns, cuts, review notes
   and the Hebrew, then tune `ROLES` floors and the coverage wording if the
   stories run long or thin. Keep `preview.py`'s per-story summary as the
   yardstick.
3. **Make failure visible beyond email.** On a failed run, write one line to
   `source_alerts.json`-style `run_alerts.json` (cause class: quota / overload
   / bug / account) so the Monday check reads one file instead of a 60k log.
4. **Budget guard.** Count calls per model during the run, and log
   `requests: creative 9, review 8, scoring 6` in `reports/<date>-run.json`.
   This is the quota and cost meter.
5. **Don't burn Monday's quota.** Document, and enforce in the
   `manual_test.yaml` description, that previews run on any day but Monday.

### P2: next month

1. **Cross-story moment.** The frame call may add one short bridge where two
   stories share a theme or contradict each other. This is what the old prep
   did well and segments lost.
2. **Pronunciation pass.** Collect Latin-script names from the last four
   scripts, and add the hard ones to `[speech.replacements]` after a listen.
3. **Gamigion Deconstructions:** only if you subscribe (paid posts arrive by
   email; the reader exists and needs the two GMAIL secrets).
4. **Retire dead weight.** Prune `AUDIT.md` into the CHANGELOG, and drop
   `[voice].direction` lines the TTS ignores (check with one A/B listen).

### Not doing

- **Pro models:** cost and latency, and no evidence they fix our failure modes.
- **A second TTS provider:** one more key, account and format to maintain;
  revisit only if Gemini TTS is retired without a replacement.
- **Scraping the Substack site:** it is blocked by design, and paid content is
  paid.

## 5. Periodic checks (cheap, scheduled)

| Check | When | Who (model) | Does | Cost |
| --- | --- | --- | --- | --- |
| CI | every push | GitHub, no model | 13 test files, zero Gemini calls | free |
| Monday run check | Mon 15:47 UTC | QA (Sonnet) | Did the episode publish? If not: classify cause from the log, fix only a clear bug, re-run once if quota allows, report in Hebrew | a few cents of Claude, 0-1 Gemini runs |
| Monthly content + voice review | 2nd of the month | Content + voice (Sonnet) | Read the last 4 scripts and editorial reports: length, cuts, recurring review notes, hype words, English leaks, names to add to the glossary; open one PR with prompt/glossary tweaks | small |
| Quarterly architecture review | Jan/Apr/Jul/Oct 5th | Architect (Opus) | Google model deprecations, quota/billing state, source health trend, dead code; update this plan | moderate, 4x a year |

**How they are wired.** The three Claude checks are scheduled tasks that
wake the Claude Code session this plan was written in (they need its GitHub
access), so they run on whatever model that session is set to. Set it to
Sonnet with `/model` after design work, so the routine checks stay cheap.

The Monday check runs after all three scheduled firings (03:00, 06:00, 10:00
UTC), which GitHub sometimes delays by hours. It never re-runs blindly: on
the free tier one full re-run spends a day's quota. A Monday lost to a Google
outage is retried automatically on Tuesday and Wednesday (09:17 and 15:17
UTC, after the free-tier quota resets at midnight Pacific); those runs exit at once when the week already has an episode, and
otherwise continue the week's unfinished episode (the checkpoint is kept per
ISO week, and `checkpoint.adopt_recent()` moves it to the run's date).

## 6. Runbook: when an episode fails

| Log says | Meaning | Do |
| --- | --- | --- |
| `402 ... prepayment credits are depleted` | The project's prepaid credit is gone | Top up, or switch to normal billing |
| `429 ... per day ... limit: 20` | Free-tier daily quota spent | Nothing today; it resets in ~16h. The next model is used automatically if configured. Avoid manual re-runs |
| `404 ... use models/X` | Model retired for this key | Automatic switch to X. Set the repo variable to X to make it permanent |
| `503 high demand` repeated | Google overload. On the free tier these attempts **do** spend the daily quota | Nothing; the Tue/Wed catch-up runs continue the week's checkpoint. Avoid same-day manual re-runs on the free tier |
| `story N cut from the episode` | A factual error survived one rewrite | Read `reports/<date>-editorial.json`; usually fine as is |
| `Only N stories survived review` | Fewer than 3 stories left | Check the editorial report; if the reviewer is wrong, tune the review prompt |
| Python traceback anywhere else | A bug | Dev fix with a test, push, re-run once |
