"""Zero-token tests: publication dates, first-seen dates, per-source yield (2026-11-13 audit, 3.1/3.2)."""
import datetime as dt
import re
import sys

import article_parser as AP
import discovery as D
import source_health as SH

FAILS = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def strip(s):
    return re.sub(r"<[^>]+>", " ", s)


BODY = "<p>Body words of the article here. </p>" * 60

# ---------------------------------------------------------------- dates on the page
check("a <time datetime> element is read",
      AP.extract(f'<main><h1>T</h1><time datetime="2026-11-12T09:30:00+00:00">x</time>{BODY}</main>',
                 "https://e.test/a", strip)["published"] == dt.date(2026, 11, 12))
check("a date written in words next to the headline is read",
      AP.extract(f"<article><h1>T</h1><div>By A. Writer, 9 Nov 2026</div>{BODY}</article>",
                 "https://e.test/a", strip)["published"] == dt.date(2026, 11, 9))
check("'November 3rd, 2026' and '03.11.2026' both parse",
      AP._text_date("November 3rd, 2026") == dt.date(2026, 11, 3)
      and AP._text_date("03.11.2026") == dt.date(2026, 11, 3))
check("an 'updated' <time> is never taken for the publication date",
      AP.extract(f'<article><h1>T</h1><p>Updated: <time datetime="2026-11-12">x</time></p>{BODY}</article>',
                 "https://e.test/a", strip)["published"] is None)
check("a page with no date at all has none (the caller falls back to first-seen)",
      AP.extract(f"<article><h1>T</h1>{BODY}</article>", "https://e.test/a", strip)["published"] is None)

# ---------------------------------------------------------------- first-seen
today = dt.date(2026, 11, 13)
items = [{"source": "Wire", "url": "https://w.test/a", "published": None},
         {"source": "Wire", "url": "https://w.test/b", "published": None},
         {"source": "Dated", "url": "https://d.test/x", "published": dt.date(2026, 11, 1)}]
seen = {}
n = D.record_first_seen(items, seen, today, 7)
check("the first sight of a source records all its links as already old",
      n == 2 and all(dt.date.fromisoformat(d) < today - dt.timedelta(days=7) for d in seen["Wire"].values()),
      str(seen))
check("dated sources are not tracked", "Dated" not in seen)
later = items + [{"source": "Wire", "url": "https://w.test/c", "published": None}]
D.record_first_seen(later, seen, today + dt.timedelta(days=2), 7)
check("a link that appears later counts from the day it appeared",
      D.first_seen_date(seen, "Wire", "https://w.test/c") == today + dt.timedelta(days=2))
check("...and an old one stays old", D.first_seen_date(seen, "Wire", "https://w.test/a") < today - dt.timedelta(days=7))
check("an unknown link has no first-seen date", D.first_seen_date(seen, "Wire", "https://w.test/zzz") is None)
D.prune_seen(seen, today + dt.timedelta(days=100))
check("old entries are pruned", seen["Wire"] == {})

# ---------------------------------------------------------------- yield
rows = [{"source": "A", "verdict": "IN"}, {"source": "A", "verdict": "OUT"},
        {"source": "A", "verdict": "BLOCKED"}, {"source": "B", "verdict": "UNDATED"},
        {"source": "B", "verdict": "UNDATED"}]
y = D.source_yield(rows)
check("yield counts where each source's candidates went",
      y["A"] == {"listed": 3, "blocked": 1, "skipped": 0, "unreadable": 0, "stale": 0, "undated": 0,
                 "too_short": 0, "unscored": 0, "scored": 2, "selected": 1}
      and y["B"]["undated"] == 2 and y["B"]["scored"] == 0, str(y))
hist = [{"date": "d1", "sources": D.source_yield(rows)}, {"date": "d2", "sources": D.source_yield(rows)}]
al = SH.yield_alerts(hist, ["A", "B"])
check("a source with candidates but nothing scored two runs in a row alerts, with the reasons",
      len(al) == 1 and al[0].startswith("B:") and "undated" in al[0], str(al))
check("one bad run alone does not alert", SH.yield_alerts(hist[-1:], ["A", "B"]) == [])
check("a source with nothing listed is the fetch alert's business, not the yield alert's",
      SH.yield_alerts([{"sources": {"B": {"listed": 0, "scored": 0}}}] * 2, ["B"]) == [])

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
