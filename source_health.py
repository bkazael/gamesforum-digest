#!/usr/bin/env python3
"""
Per-run source-alert detection.

sources.py's own adapters already log *why* a fetch produced nothing
(network error, zero links after a markup change, malformed feed) -- but
that line only reaches whoever happens to be reading a GitHub Actions run
log, and nobody does that until an episode already looks thin or wrong.

The key diagnostic idea: every RSS source is fetched the exact same way
(one HTTP GET, one XML parse), so if SOME RSS sources come back with
articles and only one doesn't, that is specific to that one feed -- its
URL likely moved, or it stopped serving RSS. But if EVERY RSS source comes
back empty in the same run, that is the opposite signal: it's far more
likely something broke in our own network path or fetch code than that
every independently-run site failed at the exact same moment. check()
tells these two cases apart and phrases the alert accordingly, in plain
language, so whoever reads it doesn't have to work it out themselves.

Alerts fire the first time a source comes back empty -- no waiting for a
second bad week, on request (a source either failed this run or it
didn't; there is nothing to gain by staying quiet about it once). The one
file this module writes (ALERTS_FILE) is scratch space for a single CI
run: gamesforum_pipeline.py's run writes it, and weekly-digest.yml's
"Check source health" step -- which runs later in the *same* job, after
the episode is already committed -- reads it back and fails the job on
purpose if it's non-empty, which is what makes GitHub send its own
default failure-notification email. ALERTS_FILE is never committed to the
repo (see .gitignore) and carries no history across runs; that is exactly
why it can't grow stale or drift out of sync with reality the way a
persisted streak counter could.
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
ALERTS_FILE = ROOT / "source_alerts.json"


def check(sources: list[dict], raw_item_counts: dict[str, int]) -> list[str]:
    """Return one plain-language alert line per source that got zero raw
    items this run.

    sources: the same list from profile.toml's [[sources]], for each
    entry's "kind" and "name".
    raw_item_counts: {source_name: count}, straight from sources.collect().
    A count of -1 (see collect()) means the adapter itself raised, not that
    it fetched cleanly and found nothing -- that always alerts, whatever
    the kind, with wording that says so plainly.
    """
    kind_by_name = {s["name"]: s.get("kind", "rss") for s in sources}
    rss_names = [s["name"] for s in sources if s.get("kind", "rss") == "rss"]
    rss_ok = [n for n in rss_names if raw_item_counts.get(n, 0) > 0]
    rss_failed = [n for n in rss_names if raw_item_counts.get(n, 0) == 0]
    all_rss_down = bool(rss_names) and not rss_ok

    alerts: list[str] = []
    for name, count in raw_item_counts.items():
        if count == -1:
            alerts.append(
                f"{name}: the fetch itself failed (an exception, not just "
                f"zero results) -- check the pipeline log for this source's "
                f"error. For an email source this usually means the mailbox "
                f"login or connection failed, not that the newsletter went "
                f"quiet."
            )
            continue
        if count > 0:
            continue
        # A newsletter genuinely not publishing that week is normal, not a
        # fault -- unlike an RSS feed or scraped page, which should always
        # have *something* if the source is healthy. Alerting on this every
        # quiet week is exactly the cry-wolf pattern that got Gamigion's old
        # RSS attempt disabled in the first place (see CHANGELOG, 2026-09-22).
        if kind_by_name.get(name) == "email":
            continue
        if name in rss_names and all_rss_down:
            alerts.append(
                f"{name}: 0 articles this week, and EVERY RSS source failed "
                f"together ({', '.join(rss_failed)}). That almost never "
                f"means every independent site went down at the same "
                f"moment -- check the pipeline's own network access or "
                f"fetch code before assuming any one feed broke."
            )
        elif name in rss_names:
            alerts.append(
                f"{name}: 0 articles this week, while other RSS sources "
                f"({', '.join(rss_ok)}) fetched fine. This looks specific "
                f"to {name} -- its feed URL may have moved or stopped "
                f"serving RSS."
            )
        else:
            alerts.append(
                f"{name}: 0 articles this week from its listing page(s). "
                f"This is an HTML-scraped source, so the site's markup may "
                f"have changed -- check link_pattern in profile.toml for "
                f"{name}."
            )
    return alerts


def record(alerts: list[str]) -> None:
    """Persist this run's alerts so the workflow's later step can read them."""
    ALERTS_FILE.write_text(
        json.dumps(alerts, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def pending_alerts() -> list[str]:
    if not ALERTS_FILE.exists():
        return []
    try:
        data = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        # A corrupt/missing alerts file should never take the show down
        # with it -- worst case this run's health check is silently skipped.
        return []
