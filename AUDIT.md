# System review: 2026-11-13

Replaces the 2026-10-13 audit (in git history). Written for the developers who
will work on this next, with a short section for the owner at the end.

**Scope.** All production code on `main` at `bbe6f39`, the workflows, the
profile, every ledger and run report in the repo, and the history of all 79
runs of the weekly workflow. **Not checked:** the sources could not be fetched
live from the review environment (its network blocks them), the repo
variables could not be read, and nobody listened to the audio. Findings about
sources come from the committed ledgers.

## 1. Verdict

The idea is sound and the parts that matter exist: collection, scoring,
per-story writing, review, speech, feed. The system is unreliable for three
reasons, in this order:

1. **It spends more model calls per episode than the free tier safely gives.**
   The free tier allows about 20 requests a day per model, and failed attempts
   count. One 8-story episode needs 10 to 27 calls on the writing model alone
   (section 4). Every recent failure traces back to this or to a fix for it.
2. **It is changed on the day it runs.** 93 commits in 12 weeks, most pushed to
   `main` on a Monday or during a failing run, with manual re-runs that spend
   the same daily quota the scheduled run needs.
3. **It grew by incident.** Each failure added a mechanism (retry, fallback,
   catch-up, adoption, variant). Few were removed. The result is about 4,200
   lines of production Python that one person can no longer hold in their head.

Against the owner's five goals:

| Goal | State | Main gap |
| --- | --- | --- |
| Good sources | Partly | Three sources feed every episode. The flagship source has been silently dead for four episodes (3.1). |
| Easy to add sources | Yes for RSS | Five lines in `profile.toml`. No way to see whether a new source ever contributes. |
| Get past "no RSS / subscription" | Weak | The HTML scraper is the dead source. The email adapter is built and disabled. Paid content is not reachable and should not be (3.3). |
| Free | Yes, at the cost of reliability | See reason 1. |
| Enjoyable to listen to | Not yet measurable | Stories are written independently, so the episode is a list. Weak fallback models write most stories on bad days. There is no feedback loop from the listener (section 6). |

## 2. Evidence

**Runs (all 79).** 34 succeeded, 40 failed, 5 were cancelled. Manual runs: 14
succeeded, 32 failed. Scheduled runs: 20 succeeded, 8 failed, 2 cancelled, and
many of the 20 are the second and third Monday firings that exit because an
episode already exists.

| Period | What happened |
| --- | --- |
| Aug 17 to Sep 7 | One scheduled run a week, all succeeded. |
| Sep 14, Sep 21 | Scheduled run failed or was cancelled; published by hand. |
| Sep 28 to Oct 19 | Four clean weeks. |
| Oct 26 | 8 failures; published Tuesday. A new API key met retired models. |
| Nov 2 | 2 failures; third Monday firing succeeded. |
| Nov 9 | 10 failures, 2 cancelled; published Wednesday Nov 11. |

The system was stable when it was left alone and has failed every Monday since
the API key changed to a fresh free-tier project on Oct 27.

**Episodes.** Recent lengths: 1,179 words (9.3 min), 1,258 (11 min), 902
(7.8 min), 1,496 (11.8 min). All were capped at six stories while 8 to 15
candidates a week scored 8 or higher.

**Size.** 4,200 lines of production Python, 3,400 lines of tests (361 checks in
13 script-style files), 1,124 lines of changelog, five overlapping documents.
`gamesforum_pipeline.py` is 1,351 lines, 187 of them comments, with 22 dated
incident references.

**GitHub scheduling.** The Monday 03:00 UTC cron has started between 03:52 and
11:03 UTC. One Tuesday catch-up never started.

## 3. Sources

### 3.1 Gamesforum contributes nothing and nothing says so (P0)

Since Oct 19, every Gamesforum candidate (14 to 18 a week) is rejected as
`UNDATED`. The Oct 13 change set `require_publication_date = true` in
`profile.toml`; the HTML adapter (`sources.from_html`) has no dates, and
`article_parser.extract` finds none on those pages. `source_health.check` only
alerts when a source returns zero links, so four episodes went out with no
alert. Before Oct 13 the same source lost most candidates to the 250-word
minimum.

Fix, in order of preference:
- Extract a date from the article page (visible date text, `<time>` element);
  add a fixture of a real Gamesforum page to the tests.
- Failing that, record a first-seen date per URL in `state.json` and treat an
  undated article as published when first seen. On the first run after the
  change, mark every listed link as seen without making it eligible.

### 3.2 No yield monitoring (P0)

A source is healthy only if its articles can reach an episode. Add to the run
report, per source: listed, blocked by title, unreadable, undated or stale,
too short, scored, selected. Alert when a source lists candidates and none is
scored for two consecutive runs.

### 3.3 What each source is worth

Per-episode contribution over the last six ledgers:

| Source | Candidates a week | Selected | Note |
| --- | --- | --- | --- |
| PocketGamer.biz | 33 to 47 | 2 to 4 | Carries the show. |
| MobileGamer.biz | 8 to 9 | 2 | Best ratio. |
| Gamigion (site feed) | 10 | 2 | Good since Oct 19. |
| Gamesforum | 14 to 18 | 0 since Oct 19 | See 3.1. |
| PocketGamer.com | 25 | 0, ever | 16 blocked by title, the rest score 0.6 to 2.8. Consumer site. Costs fetches and part of a scoring batch every week. |
| Gamigion Substack (email) | n/a | disabled | Free posts arrive as previews; the Deconstructions are paid. |

- Remove PocketGamer.com, or keep it only behind a title allow-list (owner
  decision, section 8).
- The email adapter is about 200 lines plus 324 lines of tests for a disabled
  feature. Either enable it for free newsletters that have no feed, or move it
  out of the main path until it is used.
- "Bypass subscription" has a hard limit: paid content is not reachable
  without paying, and the pipeline should not try. What is reachable: sites
  with no feed (HTML listing, sitemap), feeds blocked from GitHub's addresses
  (newsletter email, or a fetch from another network), and free newsletter
  issues.
- New sources: `probe_source.py` already tests reachability from the runner.
  Make it the documented first step, and have it print a ready `[[sources]]`
  block. Candidates worth probing are in section 8; none was verified here.

## 4. Model calls and the free tier

Calls per run, derived from the code (estimates, not measured; the pipeline
does not count calls per model, which is itself a gap):

| Stage | Model variable | Calls for 8 stories |
| --- | --- | --- |
| Scoring, about 70 readable candidates in batches of 20 | `GEMINI_SCORING_MODEL` | 4, plus 0 to 3 duplicate checks |
| Story writing, one per story, plus rewrites | `GEMINI_CREATIVE_MODEL` | 8 to 24 |
| Intro, proofreading | creative | 2 to 3 |
| Fact review, one per story, repeated after a rewrite | `GEMINI_REVIEW_MODEL` | 8 to 16 |
| Title and outro | review | 1 |
| Speech | `TTS_MODEL` | 3 to 5 chunks |

With 20 a day per model, one clean run fits and nothing else does: a retry,
a manual run, or a preview on the same day pushes the writing onto the weaker
fallback models. Raising the cap to 8 stories on Nov 11 made this tighter.

Changes, in order of value:
1. **Count calls per model per run** and write them to `reports/<date>-run.json`.
   Refuse to start a stage whose worst case does not fit what is left.
2. **Batch the fact review**: one call for all stories on the review model
   (8 to 16 calls become 1 or 2). Stories are short; the articles fit.
3. **Fold title, outro and intro into one call**, after the stories.
4. **Limit rewrites** to one per story in total (structural or factual), and
   drop the story after that.
5. **Split the run across quota days.** Collect and score on Sunday evening
   Pacific; write on Monday after 08:00 UTC. The 03:00 UTC cron already lands
   on Sunday's quota by accident; make it a deliberate separate stage.
6. Default model names in code are retired models (`gemini-2.5-flash`,
   `gemini-2.5-flash-lite`). The system runs only because repo variables
   override them. Set working defaults in one config table and delete
   `GEMINI_TEXT_MODEL`, which nothing uses but which the run report prints.

Target: at most 10 calls per model in a clean run, so that two full attempts
fit in one day.

## 5. Code and architecture

### 5.1 Structure

- `gamesforum_pipeline.py` holds configuration, HTTP, the Gemini client, model
  routing, the intro, speech, ffmpeg, the feed and the orchestration.
  `sources.py` imports from it, and it imports `discovery` lazily, so the
  import graph is circular.
- Model fallback is implemented twice: `gemini_json_quota` in the pipeline and
  `_scoring_call` in `discovery.py`, which reaches into the pipeline's private
  `_QUOTA_SPENT`.
- Stage state lives in the Actions cache (`.checkpoint`), keyed by ISO week,
  with `adopt_recent` renaming files across days. It works, but it cannot be
  inspected, and a failed run commits nothing: the Nov 11 ledger is not in the
  repo because the run that selected the stories failed later.

Proposed shape, same behaviour, fewer moving parts:

```
collect   -> candidates.json       (no model)
select    -> selection.json        (scoring model)
write     -> script.json           (creative + review)
voice     -> episode.mp3           (TTS, ffmpeg)
publish   -> feed.xml, state.json
```

Each stage is a subcommand that reads the previous file and writes its own.
A stage that finished is never run again for that week. Keep the files on a
`work` branch or as workflow artifacts so they are visible; delete
`adopt_recent`. Put the Gemini client, the routing table and the call counter
in one `llm.py`.

### 5.2 Leftovers to delete or fix

| Item | Where |
| --- | --- |
| Airtime shares are computed, logged, and written to the ledger but nothing uses them; story size comes from `role_for(index)` | `discovery.assign_airtime`, `[airtime]` in `profile.toml` |
| `next_title` is passed through three functions and never read | `segments.py` |
| The story prompt says "the previous story has just ended with a hand-over" and "end on ... the hand-over", while the ending rule forbids hand-overs | `segments.segment_prompt`, `_style_rules` |
| The style rule forbids ending with "operators should", while `takeaway` and the coverage standard ask for exactly that | `segments.py` |
| `prep` is built "in the shape the old prep step produced"; `render_digest_md` still prints prep questions that are always empty | `segments.build_episode`, pipeline |
| Docstrings refer to `generate_podcast_content()`, removed in the rewrite | `checkpoint.py`, `memory.py`, `test_contracts.py` |
| Comments cite a 20-minute timeout and "6 x 300s" tries; real values are 55 minutes and 4 | `discovery.py`, `gemini_json` |
| `from_email` docstring says there is no sender filter; there is one | `sources.py` |
| A Gmail address is written in `profile.toml`, in a repo served by GitHub Pages | `profile.toml`, email source comment |
| `SITE` constant unused | pipeline |

### 5.3 Workflow

- Every failed attempt sends a failure email, including attempts followed by a
  success minutes later. Replace with one check late on Wednesday that alerts
  only if the week has no episode.
- "Check source health" turns a run red after the episode was published. Report
  it as a warning, or in the weekly check.
- A failed run should still commit `ledger/` and `reports/`.
- Actions are on versions that target a deprecated Node runtime; update them.
- Episodes are committed to git: 109 MB so far, about 8.5 MB a week. Move
  audio to release assets or object storage before the Pages size limit
  becomes a problem; keep the feed URL stable.

### 5.4 Tests and documents

- Tests are plain scripts with a home-made `check()`. Move to `pytest` so a
  single failure can be run and read. Keep the contract tests; drop tests of
  removed behaviour.
- There is no test that runs the whole pipeline against recorded responses of
  a real week. One such test would have caught 3.1.
- Collapse the documents: `README.md` (owner: what it is, how to add a
  source), `OPERATIONS.md` (runbook), `ARCHITECTURE.md` (developers). Fold
  `PLAN.md` into the last two. Move incident narratives from code comments to
  the changelog and leave one line with the reason in the code.

### 5.5 Process

- No pushes to `main` from Sunday 18:00 to Monday 18:00 Pacific unless the
  week's episode is missing.
- Changes are validated with the `preview` workflow on Thursday or Friday, on
  a day with no production run, so they do not spend Monday's quota.
- After a failed scheduled run, do not start a manual run the same day. The
  Tuesday and Wednesday catch-ups exist for this.

## 6. Listening quality

What is in place: per-story fact review against the source, a numbers check,
a Hebrew-share check, and since Nov 11 a whole-episode proofreading pass that
has not yet run in production.

What is missing:

1. **No episode-level editing.** Stories are written in parallel with no
   knowledge of each other, then concatenated. Nothing orders them for flow,
   links related stories (three of the five on Nov 11 were about layoffs and
   closures), or removes overlap. Add one editor call that sees all headlines
   and key points before writing and returns order, grouping and a one-line
   bridge for each story to open with.
2. **The fallback model decides quality on bad days.** Record in the run
   report which model wrote each story. If more than half were written by the
   last fallback, hold the episode for the next catch-up instead of
   publishing. The owner has said a later episode beats a weak one.
3. **No listener feedback.** Add `feedback.md`: the owner writes two or three
   lines after an episode; the last few entries go into the writing and
   proofreading prompts. This is the cheapest quality lever in the project.
4. **Pronunciation.** `[speech.replacements]` has four entries. Have the
   proofreader return names it expects the voice to misread, log them, and
   let the owner add the ones that were wrong.
5. **Speech model.** `gemini-2.5-flash-preview-tts` is a preview model and the
   only one configured. The automatic swap handles retirement only when the
   error names a replacement. Document a tested second choice.
6. **Raw titles.** Titles from sources reach the script and the show notes in
   English. The proofreader now translates them in the script; the show notes
   still list English titles under a Hebrew summary. Decide which is wanted.

## 7. Work plan

**P0, small, before the next Monday run**
1. Gamesforum dates (3.1) with a real-page fixture.
2. Per-source yield in the run report and the alert (3.2).
3. Call counter per model in the run report (4.1).
4. Fix the contradictory prompt lines (5.2).
5. Remove the Gmail address from `profile.toml`.
6. Verify on the first real run that proofreading applied edits and that an
   8-story episode stayed inside quota; if not, set `max_articles = 7`.

**P1, two to three working days, on a branch, validated by preview**
1. Batch the review; merge title, outro and intro (4.2, 4.3, 4.4).
2. One `llm.py`; remove the second fallback chain; one model table.
3. Stage subcommands with visible files; remove `adopt_recent` (5.1).
4. Weekly "no episode" alert instead of per-run emails; source health as a
   warning; failed runs commit ledger and reports (5.3).
5. Delete the leftovers (5.2); decide the email adapter's fate (3.3).
6. `pytest`; one recorded end-to-end test (5.4).

**P2, quality**
1. Episode editor call for order and bridges (6.1).
2. Hold-back rule for fallback-written episodes (6.2).
3. `feedback.md` loop (6.3) and pronunciation log (6.4).
4. Audio out of git (5.3). Documents collapsed (5.4).

Stop rule: after P1, no new mechanism is added in response to a single failed
run. A failure gets a line in the changelog; a mechanism needs two failures
with the same cause.

## 8. For the owner

1. **Stop pressing "Run workflow" after a failure.** 32 of 49 manual runs
   failed, and each one spends the quota the next scheduled run needs. The
   Tuesday and Wednesday catch-ups do the retry.
2. **Decide on PocketGamer.com.** It has never contributed a story.
   Recommendation: remove it.
3. **Check three repository variables exist** (Settings, Secrets and
   variables, Actions, Variables): `GEMINI_CREATIVE_MODEL`,
   `GEMINI_SCORING_MODEL`, `GEMINI_REVIEW_MODEL`. Without them the code falls
   back to retired models.
4. **After each episode, write two or three lines** on what you liked and
   what bored you. Until `feedback.md` exists, send them in chat.
5. **Name the sources you miss.** Candidates to probe, none verified:
   Deconstructor of Fun, Naavik (free issues), GameDev Reports,
   GamesIndustry.biz, Game World Observer, Sensor Tower and AppMagic blogs,
   the two-and-a-half-gamers newsletter. For real-money skill games, name the
   outlets you read; the current sources cover that area only by chance.
6. **The free-tier decision stands, with a condition.** The plan above is
   built to work without paying. If two Mondays fail on quota after P1 is
   done, the remaining fix is a paid key with a low monthly cap; check
   Google's current price list before deciding.
7. **GitHub Pages requires a public repository on a free account.** Confirm
   you are comfortable that `profile.toml`, which describes your business
   focus, is public; if not, the profile can move to a repository secret.
