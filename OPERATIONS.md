# Operating and tuning the podcast

Use Python 3.12+ and `ffmpeg`/`ffprobe`. Runtime code uses the Python standard
library only. No database, web server or extra package service is required.

## Run safely

```sh
# Zero model cost: test logic and contracts (each legacy test is a script).
for test in test_*.py; do python3.12 "$test" || exit 1; done

# Live source check, no model calls, no changes to the published artifacts.
python3.12 preview.py --discovery-only

# Real model calls, script and editorial report, without paying for audio.
python3.12 preview.py --script-only

# Full preview: resumes any saved script and generates audio + preview RSS.
python3.12 preview.py
```

Paid local previews require `GEMINI_API_KEY` in the environment. Do not put keys
in source files. Alternatively run the existing **Live smoke test** GitHub
workflow on your branch with mode `script` or `preview`; it uses the existing
Actions secret and uploads the output as a seven-day artifact. That workflow
has read-only repository permissions and never commits or publishes. The
weekly publishing workflow is restricted to `main`.

Preview writes to `preview_output/`, which is ignored by Git. It starts with
copies of the current article state and episode memory, so the selection
reflects what listeners have already heard. `--output another-directory`
starts a separate experiment. A preview on the day after publication can
legitimately contain fewer stories. This is a preview of *new* material, not
a recreation of the previous week's episode.

The production entry point remains `python3.12 gamesforum_pipeline.py`.
Set `PODCAST_BASE_URL` to the public hosting URL; relative enclosure URLs are
rejected by that entry point. `PODCAST_OUTPUT_DIR` redirects all generated
artifacts, checkpoint files, memory and state. `PODCAST_PROFILE` selects an
alternative TOML profile. Set these before starting Python.

## What to inspect

| Artifact | What it tells you |
| --- | --- |
| `ledger/<date>.md` | Every candidate's selection or rejection reason, including stale/undated articles |
| `digests/<date>-script.md` | The exact text sent to speech synthesis |
| `reports/<date>-editorial.json` | Evidence, attribution, coverage and naturalness review; both attempts if revised |
| `reports/<date>-run.json` | Completion marker, models, duration, speech rate, text-token usage, source dates and scores |
| `source_alerts.json` | Sources that failed or returned no items in this run |
| `.checkpoint/<date>-content.json` | Selected source text and the paid draft for recovery; never committed |
| `.checkpoint/<date>-chunk-*` | PCM chunks and identity/integrity metadata |

Editorial review performs one review, at most one whole-draft revision, and a
second review. An unresolved material factual error, missing selected story or
rating below `[editorial].minimum_rating` stops the run **before TTS**. This is
a model-assisted check, not independent verification of a publisher's claims.
Inspect the saved report when it fails; do not solve factual failures by simply
lowering the threshold. The reviewer uses the configured creative model.

The run's token counters cover text responses received in that process, not
previous attempts restored from checkpoints and not TTS billing. They are
diagnostics, not a dollar estimate. Speech-rate checks are coarse completeness
checks; they do not prove pronunciation or perfect transcript adherence.

## Tune the product in `profile.toml`

- `identity` / `interests`: audience and editorial priorities. This remains a
  Hebrew show for an experienced mobile-game operator.
- `thresholds.max_articles`: currently six, to allow more useful discussion.
- `airtime`: 30% lead, 20% second, 50% shared among remaining stories. Shares
  are targets given to the writer, not measured guarantees of speech time.
- `thresholds.min_words`: 120, so a concise announcement is eligible for
  relevance scoring instead of being discarded just for being short.
- `thresholds.max_source_bonus`: 0.5, so preferred formats/outlets cannot
  turn an average opinion piece into a 10/10 lead story.
- `thresholds.require_publication_date`: true. Uses feed dates or Article
  JSON-LD / publication metadata; an update date is not a publication date.
  Unknown dates are visible in the ledger rather than silently treated as new.
- `thresholds.fetch_workers`: four (validated range 1–8). Requests stay
  bounded; round-robin allocation prevents a prolific feed from consuming
  the entire candidate limit.
- `thresholds.min_scoring_coverage`: 0.8. Failed scoring cannot silently
  produce a full episode selected only by how many numbers articles contain.
- `voice`: two speaker names, two voices and delivery instructions. Voice
  changes invalidate cached speech automatically.

The intro targets 60–100 words and previews two or three stories. The script
prompt favors 15–55-word turns, with no obligatory interview question in every
item. Editorial review checks the complete script including intro and summary.

Model environment variables remain `GEMINI_TEXT_MODEL`,
`GEMINI_CREATIVE_MODEL`, and `TTS_MODEL`. Changing a model is not automatically
an improvement: run a script preview and full audio preview before publishing.
The existing defaults are preserved pending evidence from comparisons.

## Add a source

For a normal RSS or Atom feed, add only configuration:

```toml
[[sources]]
name = "New mobile games publication"
kind = "rss"
urls = ["https://publisher.example/feed"]
enabled = true
weight = 1.0
max_per_episode = 2
block = ["register now", "tickets"]
```

Run `preview.py --discovery-only`. Verify actual article text and dates, not
just a nonzero feed count. Use the ledger to spot `UNREADABLE`, `UNDATED`,
`STALE`, `THIN` or unexpected title blocks. Then run a script preview to check
whether relevance scores and spoken attribution match your intent.

For HTML listings use `kind = "html"`, listing `urls`, and the existing
`link_pattern` format (a regex matching relative article paths). Article pages
need accessible publication metadata. HTML extraction prefers Article JSON-LD,
then `<article>`/`<main>`, then the existing body fallback. Sites rendered only
through JavaScript may need a dedicated adapter; the fallback is not a browser.

For a dedicated newsletter mailbox use `kind = "email"`, `mailbox`,
`imap_host` (default Gmail), and the newsletter's base URL in `urls`.
`GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` are environment secrets. Collection is
read-only and date-bounded (`SINCE`, `BODY.PEEK[]`), including already-read mail.
Publication state deduplicates selected URLs. Collection, previews and failed
runs never mark mail as consumed. The current email parser expects a Substack
HTML post with a `/p/` permalink; other newsletter formats need an adapter.

For a new adapter type, add its function to `sources.ADAPTERS`, allow the kind
in `runtime.load_profile`, and provide a fixture test for its output contract:
`url`, `title`, `source`, `published` (date or None), `summary`, optional `text`.
Return an empty list for a genuinely quiet source and raise on hard failures.
Never perform publication or inbox mutations in an adapter.

## Failures, retries and rollback

An MP3 alone no longer means a run is complete. Retry resumes its saved draft,
checks the editorial fingerprint, verifies audio chunk hashes and finishes
metadata, RSS, state and memory before writing the completion report. Writes
use temporary files and atomic replacement. An interruption can leave multiple
artifacts at different stages; the completion marker and retry make that state
recoverable. They do not make a whole directory transaction atomic.

PCM reuse requires matching text, speaker names, model, voices, direction and
audio format, plus a byte checksum. In GitHub Actions, checkpoint cache keys
also include code/profile hashes, run ID and attempt number. A code change
deliberately starts a fresh cache; an ordinary retry of the same code resumes.

`FORCE_REGENERATE=1` intentionally clears that date's checkpoint and regenerates
it. A normal retry is cheaper. Publishing replacements with the same RSS GUID
may not cause podcast players to download them again; use an explicit versioned
episode filename when intentionally republishing an already-heard episode.

No live audio, feed or archived episode is changed by this improvement branch.
Merge after reviewing the preview and validation. To roll back after a merge,
revert the merge commit on `main`; retain already-published episode files and
GUIDs. For a squash merge, revert that squash commit. Don't reset history.
