# Operations

Runbook for running, tuning and fixing the podcast. For what it is, see [README.md](README.md); for
how it is built, [ARCHITECTURE.md](ARCHITECTURE.md).

## Run it

Python 3.12 and `ffmpeg`/`ffprobe`; the production code uses only the standard library. `pytest` is
needed only for the tests.

```sh
pip install -r requirements-dev.txt
pytest -q                          # every test, no network, no model cost (also: python test_stages.py)
python preview.py --discovery-only # live sources, no model calls
GEMINI_API_KEY=... python preview.py --script-only   # real model calls: plan, stories, review, frame, intro, proofread
GEMINI_API_KEY=... python preview.py                 # the whole episode, voiced, in preview_output/
```

A preview runs every stage now, whatever the day, in `preview_output/` (git-ignored); it never
publishes. In GitHub Actions the **Live smoke test** workflow does the same with mode `smoke`,
`script` or `preview` and uploads the result. **Do previews on a day with no production run**
(Thursday or Friday morning, before the 09:17 UTC run): they spend the same daily model quota.

Production is `python gamesforum_pipeline.py`, run daily by **Gamesforum weekly digest**
(`weekly-digest.yml`). It does only what is due that day (see ARCHITECTURE.md). Environment:

| Variable | Meaning |
| --- | --- |
| `GEMINI_API_KEY` (secret) | The Gemini key |
| `PODCAST_BASE_URL` | Public URL of the site; the feed's enclosure URLs are built from it |
| `SHOW_NAME` | The show's name in the feed, titles and the spoken welcome |
| `GEMINI_CREATIVE_MODEL` / `_SCORING_MODEL` / `_REVIEW_MODEL` | Writing and intro, scoring, review and title |
| `GEMINI_QUOTA_FALLBACKS` | Models used, in order, when one is spent or not answering |
| `GEMINI_DAILY_BUDGET` | Requests per model per quota day this pipeline allows itself (default 16 of the free 20) |
| `TTS_MODEL`, `TTS_FALLBACK_MODEL` | Speech model, and the one used if the first is gone |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (secrets) | Only for an `email` source |
| `PIPELINE_FORCE=1` | Every stage now, ignoring the calendar (the workflow's `force` input) |
| `PIPELINE_SKIP_PUBLISH=1` | Stop after editing (previews) |
| `PODCAST_OUTPUT_DIR`, `PODCAST_PROFILE` | Redirect outputs; use another profile |

## What to inspect

| File | Tells you |
| --- | --- |
| `work/plan.json` | The week: what each day did, the last error, whether it was held back, calls per model |
| `work/<monday>-content.json` | The selection, the plan, every story and the script so far |
| `work/usage.json` | Model requests made today per model (the budget meter) |
| `ledger/<date>.md` | Every candidate and why it was chosen or dropped |
| `reports/source_yield.json` | Per source: listed, blocked, undated, stale, scored, selected, over the last runs |
| `reports/<date>-editorial.json` | Per story: kept or cut and why, the reviewer's notes, edits applied, which model wrote it; the proofreading edits (applied and refused), the names to check |
| `reports/<date>-run.json` | The published run: models, calls, the week's history, duration, words per minute |
| `digests/<date>-script.md` | The exact text that was voiced |

## When something fails

Daily failures are warnings: the next day retries from `work/`. The run turns red only on
Wednesday (the last chance day), and `weekly-check.yml` emails you on Wednesday afternoon only if
there is still no episode. To diagnose, read `work/plan.json` (`last_error`, `history`) first.

| Message | Meaning | Do |
| --- | --- | --- |
| `paused: model budget spent` | The day's model budget is used (by design) | Nothing: the next quota day (08:00 UTC) continues |
| `429 ... per day` | Gemini's own daily quota is spent | Nothing; the next model in the chain is used |
| `404 ... use models/X` | Model retired for this key | Automatic switch; set the repo variable to X |
| `503 high demand` repeated | Google overload; failed attempts still count against the quota | Nothing; models are skipped after two failed calls |
| `held back: most stories were written by the weakest model` | A better model will rewrite them on the next quota day | Nothing (at most twice; never past Wednesday) |
| `story N cut from the episode` | A factual error survived one rewrite | Read the editorial report |
| `Only N stories survived review` | Fewer than three left | Read the editorial report; the next run retries from the saved stories |
| `source alert: ... listed but none scored` | A source fetches but cannot feed an episode | `reports/source_yield.json`; probe the source |
| `ffmpeg ... failed` / `assembled as:` | The mix fell back (without slow-down, or without the jingle) | The episode still published; look at the ffmpeg line |
| Any other traceback | A bug | Fix with a test; do not re-run the same day on the free tier |

To redo the voice only, run the workflow with `force` after the saved script exists: the speech
chunks are cached by their text, so only changed chunks cost anything.

## Tune `profile.toml`

- `[schedule]`: the days of the weekly plan. Changing `publish_on` also needs the cron lines in
  `weekly-digest.yml` to fire on that day.
- `[thresholds]`: `min_score`, `max_articles` (8) with `core_articles` (6) and `extra_slot_min_score`
  (8.0): six places for anything over the bar, two more only for 8+ stories, so a weak week stays at
  six; `max_age_days`, `min_words`, `require_publication_date`, `min_scoring_coverage`.
- `[identity]`, `[interests]`: the taste the scorer and writers use.
- `[voice]`: speaker names, voices, delivery direction; `[speech] tempo` (0.94 = slightly slower);
  `[speech.replacements]`: pronunciation glossary, applied to speech only.
- Sources: below.

## Sources

`kind = "rss"`: a feed (RSS or Atom). Add `full_text_in_feed = true` if the feed carries the whole
post. `kind = "html"`: a listing page plus a `link_pattern` regex; article pages need readable
text, and if they carry no date the link's first-seen day is used (`seen.json`; the first time a
source is seen, everything listed counts as old). `kind = "email"`: a newsletter in a mailbox you
own, read-only (`GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` secrets; set `sender` if the inbox is shared);
Substack previews arrive as partial articles and never lead. Common keys: `weight`,
`max_per_episode`, `block` (title patterns), `boost`, `roundup_patterns`, `enabled`.

The steps for a new source: run **Probe a source from GitHub Actions** (it prints a ready block, the
number of items, whether the page's text and date are readable from the runner), paste the block,
run `preview.py --discovery-only`, check the ledger for `UNREADABLE`, `UNDATED`, `STALE`, `THIN`,
then watch `reports/source_yield.json` for a few weeks. Paid content is not reachable without paying
and the system does not try.

## Rules for changing the system

1. **No pushes to `main` from Sunday 18:00 to Monday 18:00 Pacific** unless the week's episode is
   missing; validate changes with a preview on Thursday or Friday.
2. **A failure gets a line in the changelog; a new mechanism needs the same cause twice.**
3. After a failed scheduled run, do not start a manual run the same day.
4. Rollback: revert the commit on `main`; keep published episode files and GUIDs; never reset history.

## Privacy

Your email address and name are not written in any file: the mailbox address is a secret, the show's
name is a variable (`SHOW_NAME`). Git history is another matter: commits made from a personal
account carry that account's name and email. To stop it for future commits, turn on **Keep my email
addresses private** and **Block command line pushes that expose my email** in GitHub, Settings,
Emails. Existing commits can only be changed by rewriting history and force-pushing `main`, which
rewrites every commit hash; that needs your decision.

## Scheduled reviews

Claude Code scheduled tasks (set up in the session that maintains this repo): a Monday run check, a
monthly content and voice review, a quarterly architecture review. They read `work/plan.json`, the
reports and the logs, and change code only for a clear bug with a test. Set that session to a small
model for the routine checks.
