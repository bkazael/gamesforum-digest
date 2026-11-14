# Weekly mobile-games podcast

An automated weekly Hebrew podcast for mobile-game operators, hosted by two voices (Dana and
Yoni). It reads recent industry reporting from the sources you choose, selects what matters,
writes and fact-checks a conversation about each story, ties the stories into one episode,
voices it, and publishes an RSS feed. It runs on GitHub Actions and the Gemini free tier.

The listener owns a company in casual/puzzle, hybrid-casual and real-money skill games; the
editorial taste is in `profile.toml`.

## How a week works

The episode is published on Monday but built over the days before, so no single day carries all
the model calls (the free tier allows about 20 requests a day per model):

| Day (UTC, 09:17) | What happens |
| --- | --- |
| Fri | Collect, score and choose the stories; plan the running order and the week's theme; start writing |
| Fri, Sat, Sun | A few stories are written each day |
| Sun | One fact review of all stories, one rewrite round; title, outro and the links between stories; intro; proofreading |
| Mon | Voice, mix and publish |
| Tue, Wed | Catch-up if anything did not finish. Wednesday afternoon a check emails you only if there is still no episode |

Everything is saved in `work/` after each step, so a day that fails or runs out of quota is
simply continued by the next run. See [ARCHITECTURE.md](ARCHITECTURE.md).

## What you do

1. **After you listen, write two or three lines in `feedback.md`.** The writers get your last three
   notes as taste (never as facts). This is the cheapest way to improve the show.
2. **Don't press "Run workflow" after a failure.** Every failed attempt spends quota the next run
   needs; the daily runs retry on their own. Use the `force` option only after the quota resets
   (08:00 UTC).
3. **Add a source** with a few lines in `profile.toml` (below).
4. **Set the repository variables** (Settings, Secrets and variables, Actions, Variables):
   `PODCAST_BASE_URL`, and optionally `SHOW_NAME`, `GEMINI_CREATIVE_MODEL`, `GEMINI_SCORING_MODEL`,
   `GEMINI_REVIEW_MODEL`, `TTS_MODEL`. The secret is `GEMINI_API_KEY`.
5. **Pronunciation:** after each episode the log lists names the voice may misread. If you hear one
   wrong, add it to `[speech.replacements]` in `profile.toml`.

## Add a source

Most sites have an RSS or Atom feed. Add:

```toml
[[sources]]
name = "Example Mobile News"
kind = "rss"
urls = ["https://example.com/feed"]
max_per_episode = 2
```

Then run **Probe a source** (GitHub Actions) to see whether the feed is reachable from the runner and
whether the articles carry readable text and dates. Sources without a feed: `kind = "html"` (a
listing page and a link pattern; articles without a date count from the day their link first
appeared) or `kind = "email"` (a newsletter delivered to a mailbox you read with an app password).
Paid content is not reachable without paying, and the system does not try. Details in
[OPERATIONS.md](OPERATIONS.md).

## Where things are

| Path | What |
| --- | --- |
| `profile.toml` | Taste, sources, thresholds, voices, the weekly schedule, the pronunciation glossary |
| `feedback.md` | Your notes on recent episodes |
| `work/` | The week in progress: plan, stories, script, model-call counts |
| `episodes/`, `digests/`, `feed.xml` | What was published |
| `ledger/<date>.md` | Every candidate article and why it was chosen or dropped |
| `reports/` | Per episode: the run, the editorial review (what was cut or fixed), per-source yield |

Developers: [ARCHITECTURE.md](ARCHITECTURE.md). Runbook: [OPERATIONS.md](OPERATIONS.md). History:
[CHANGELOG.md](CHANGELOG.md). The latest review and what was done about it: [AUDIT.md](AUDIT.md).
