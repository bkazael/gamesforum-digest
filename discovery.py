#!/usr/bin/env python3
"""
Discovery and filtering. The front of the funnel.

Runnable on its own so you can tune what gets through WITHOUT generating any
audio. That is the point: iterate here until the ledger matches your taste,
then let the rest of the pipeline run.

  python3 discovery.py --dry-run     # no LLM call, deterministic signals only
  python3 discovery.py               # full scoring, writes ledger + selection

Four stages, cheapest first, so money is only spent on survivors:

  1. LIST     scrape listing pages for candidate links          (no cost)
  2. BLOCK    kill obvious promo from the title alone           (no cost)
  3. SIGNAL   fetch, measure substance: data density, quotes    (no cost)
  4. SCORE    one batched LLM call rates relevance to YOU       (~$0.002)

Everything it decides is written to ledger/<date>.md, including what it threw
away and why. If the filter is wrong you will be able to see that it is wrong,
which matters more than it being right on any given week.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys

try:                                  # stdlib from Python 3.11
    import tomllib
except ModuleNotFoundError:           # 3.10 and older
    try:
        import tomli as tomllib       # type: ignore[no-redef]
    except ModuleNotFoundError:
        sys.exit(
            "needs Python 3.11+ for tomllib, or: pip install tomli\n"
            "(the GitHub workflow pins 3.12, so this only affects local runs)"
        )

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gamesforum_pipeline import (          # noqa: E402
    fetch_article, gemini_text, log,
)
from sources import collect                # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
LEDGER = ROOT / "ledger"
STATE_FILE = ROOT / "state.json"
PROFILE = ROOT / "profile.toml"


# ------------------------------------------------------------ profile


def load_profile() -> dict:
    if not PROFILE.exists():
        sys.exit(f"missing {PROFILE.name}; it defines what you care about")
    with PROFILE.open("rb") as f:
        return tomllib.load(f)


# ------------------------------------------------------------ stage 2: block


def blocked_by_title(title: str, url: str, patterns: list[str]) -> str | None:
    """Return the pattern that killed it, or None.

    Runs before fetching. Conference marketing is reliably identifiable from
    the headline, and there is no reason to pay to download it.
    """
    hay = f"{title} {url}".lower()
    for pat in patterns:
        try:
            if re.search(pat.lower(), hay):
                return pat
        except re.error:
            if pat.lower() in hay:
                return pat
    return None


# ------------------------------------------------------------ stage 3: signal


NUM_RE = re.compile(r"\d+(?:[.,]\d+)*\s*(?:%|percent|million|billion|[mbk]\b)?")
QUOTE_RE = re.compile(r"[\"“”'']{1}[^\"“”'']{25,}[\"“”'']{1}")
PROMO_BODY = [
    "register now", "book your ticket", "early bird", "save €", "save $",
    "join us at", "buy tickets", "limited spaces",
]


def substance_signals(text: str) -> dict:
    """Cheap proxies for 'is there anything here'.

    Numeric density is the strongest single signal: articles that move an
    operator contain figures. Pure narrative pieces rarely do.
    """
    words = max(len(text.split()), 1)
    figures = len(NUM_RE.findall(text))
    quotes = len(QUOTE_RE.findall(text))
    low = text.lower()
    promo_hits = sum(1 for p in PROMO_BODY if p in low)

    return {
        "words": words,
        "figures": figures,
        "figures_per_100w": round(100 * figures / words, 2),
        "quotes": quotes,
        "promo_markers": promo_hits,
    }


def substance_note(sig: dict) -> str:
    bits = [f"{sig['words']}w", f"{sig['figures']} figures"]
    if sig["quotes"]:
        bits.append(f"{sig['quotes']} quotes")
    if sig["promo_markers"]:
        bits.append(f"{sig['promo_markers']} promo markers")
    return ", ".join(bits)


# ------------------------------------------------------------ stage 4: score


def build_scoring_prompt(profile: dict, candidates: list[dict]) -> str:
    ident = profile["identity"]["who"].strip()
    core = "\n".join(f"- {x}" for x in profile["interests"]["core"])
    deprio = "\n".join(f"- {x}" for x in profile["interests"]["deprioritize"])

    items = []
    for i, c in enumerate(candidates):
        excerpt = " ".join(c["text"].split()[:220])
        items.append(
            f"[{i}] TITLE: {c['title']}\n"
            f"    SIGNALS: {substance_note(c['signals'])}\n"
            f"    EXCERPT: {excerpt}"
        )
    blob = "\n\n".join(items)

    return f"""Rate how much each article below is worth this person's time.

THE READER:
{ident}

MOVES THE NEEDLE FOR HIM:
{core}

USUALLY NOT WORTH HIS AIRTIME:
{deprio}

SCORING, 0 to 10:
  9-10  hard data or a market move that could change a decision he makes
  7-8   substantive, named sources or real numbers, clearly in his domain
  5-6   relevant topic but thin, or mostly restates what he already knows
  3-4   tangential, or an opinion piece with no evidence
  0-2   promotion, conference marketing, vendor PR with no data

Judge the CONTENT, not the headline's confidence. An article promising
"the future of monetization" that contains no figures is a 3, not an 8.
An unglamorous piece with a real benchmark table is an 8.

Return ONLY a JSON array, one object per article, no prose and no code fence:
[{{"i": 0, "score": 7, "why": "ten-word reason", "topic": "3-word tag"}}]

"why" must be specific. "relevant to mobile gaming" is useless. Say what is
actually in it: "ZBD survey, 195 execs, retention budget data".

ARTICLES:
{blob}
"""


def parse_scores(raw: str, n: int) -> dict[int, dict]:
    """Models wrap JSON in fences and prose no matter how firmly you ask."""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        log("  scoring returned no JSON array; treating all as unscored")
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        log(f"  malformed scoring JSON ({e}); treating all as unscored")
        return {}

    out: dict[int, dict] = {}
    for row in data:
        try:
            i = int(row["i"])
            if 0 <= i < n:
                out[i] = {
                    "score": max(0, min(10, int(row.get("score", 0)))),
                    "why": str(row.get("why", "")).strip(),
                    "topic": str(row.get("topic", "")).strip(),
                }
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------ ledger


def write_ledger(date: str, rows: list[dict], profile: dict) -> pathlib.Path:
    LEDGER.mkdir(exist_ok=True)
    thr = profile["thresholds"]

    picked = [r for r in rows if r["verdict"] == "IN"]
    dropped = [r for r in rows if r["verdict"] != "IN"]

    def table(rs: list[dict]) -> str:
        if not rs:
            return "_(none)_\n"
        out = ["| ציון | מקור | כותרת | סיבה |", "|---:|---|---|---|"]
        for r in rs:
            score = r["score"] if r["score"] is not None else "-"
            title = r["title"].replace("|", "/")[:64]
            src = r.get("source", "-")
            out.append(f"| {score} | {src} | [{title}]({r['url']}) | {r['why']} |")
        return "\n".join(out) + "\n"

    mix: dict[str, int] = {}
    for r in picked:
        mix[r.get("source", "?")] = mix.get(r.get("source", "?"), 0) + 1
    mix_line = " · ".join(f"{k}: {v}" for k, v in mix.items()) or "-"

    body = f"""# Ledger — {date}

סף כניסה: {thr['min_score']}/10 · מקסימום {thr['max_articles']} כתבות
· {len(rows)} מועמדים נבדקו
**תמהיל מקורות:** {mix_line}

## נכנסו ({len(picked)})

{table(picked)}
## נזרקו ({len(dropped)})

{table(dropped)}
---

אם משהו כאן לא תואם את הטעם שלך, ערוך את `profile.toml`:

- נכנס רעש → העלה `min_score`, או הוסף דפוס ל-`block` של אותו מקור
- מקור אחד משתלט → הורד את `max_per_episode` שלו
- מקור טוב מפסיד בעקביות → העלה את ה-`weight` שלו
- מפספס דברים → הורד `min_score` או הרחב `interests.core`
"""
    path = LEDGER / f"{date}.md"
    path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------------------ main


def apply_caps(scored: list[dict], sources: list[dict], thr: dict) -> list[dict]:
    """Rank globally, then enforce a per-source ceiling.

    This is the part that stops publication volume from becoming editorial
    influence. PocketGamer.biz posts several times a day; Gamesforum posts a
    few times a week. On raw score alone the high-volume feed would fill every
    episode, not because it matters more but because there is more of it.
    """
    caps = {s["name"]: s.get("max_per_episode", 99) for s in sources}
    used: dict[str, int] = {}
    chosen: list[dict] = []

    for art in sorted(scored, key=lambda a: a["_score"], reverse=True):
        if art["_score"] < thr["min_score"]:
            art["_reject"] = "מתחת לסף"
            continue
        src = art.get("source", "?")
        if used.get(src, 0) >= caps.get(src, 99):
            art["_reject"] = f"תקרת {src} ({caps.get(src)}) מלאה"
            continue
        if len(chosen) >= thr["max_articles"]:
            art["_reject"] = f"מחוץ ל-top {thr['max_articles']}"
            continue
        used[src] = used.get(src, 0) + 1
        chosen.append(art)

    if used:
        log("  per-source mix: " + ", ".join(f"{k} {v}" for k, v in used.items()))
    return chosen


def assign_airtime(chosen: list[dict], profile: dict) -> None:
    """Translate rank into share of the episode.

    Without this every item gets equal weight and the result sounds like a
    list. The strongest story gets a narrative; the weakest gets a sentence.
    """
    air = profile.get("airtime", {})
    lead = air.get("lead_share", 0.40)
    second = air.get("second_share", 0.25)
    rest_pool = air.get("remainder_share", 0.35)

    n = len(chosen)
    for i, art in enumerate(chosen):
        if i == 0:
            share = lead if n > 1 else 1.0
        elif i == 1:
            share = second if n > 2 else 1.0 - lead
        else:
            share = rest_pool / max(n - 2, 1)
        art["_airtime"] = round(share, 3)


def select(dry_run: bool = False) -> list[dict]:
    profile = load_profile()
    thr = profile["thresholds"]
    sources = profile["sources"]
    global_blocks = profile["hard_block"]["title_patterns"]

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    done = set(state.get("processed", []))

    log("stage 1: collect from sources")
    all_items, highlighted = collect(sources, thr["max_age_days"])
    found = [i for i in all_items if i["url"] not in done]
    log(f"  {len(found)} unseen candidates")

    rows: list[dict] = []
    survivors: list[dict] = []
    by_name = {s["name"]: s for s in sources}

    log("stage 2: title block")
    to_fetch = []
    for cand in found:
        src = by_name.get(cand["source"], {})
        patterns = global_blocks + src.get("block", [])
        hit = blocked_by_title(cand["title"], cand["url"], patterns)
        if hit:
            rows.append({
                "title": cand["title"], "url": cand["url"],
                "source": cand["source"], "score": None,
                "why": f"נחסם בכותרת: `{hit}`", "verdict": "BLOCKED",
            })
        else:
            to_fetch.append(cand)
    log(f"  {len(found) - len(to_fetch)} blocked, {len(to_fetch)} to fetch")

    log("stage 3: fetch + substance signals")
    for cand in to_fetch:
        art = fetch_article(cand["url"])
        if not art:
            rows.append({
                "title": cand["title"], "url": cand["url"],
                "source": cand["source"], "score": None,
                "why": "לא ניתן לחלץ גוף כתבה", "verdict": "UNREADABLE",
            })
            continue
        art["source"] = cand["source"]
        art["published"] = cand.get("published")
        art["signals"] = substance_signals(art["text"])
        if art["signals"]["words"] < thr["min_words"]:
            rows.append({
                "title": art["title"], "url": art["url"],
                "source": art["source"], "score": None,
                "why": f"קצר מדי ({art['signals']['words']} מילים)",
                "verdict": "THIN",
            })
            continue
        survivors.append(art)
    log(f"  {len(survivors)} readable and substantial enough to score")

    if dry_run:
        for art in survivors:
            rows.append({
                "title": art["title"], "url": art["url"],
                "source": art["source"], "score": None,
                "why": f"dry-run · {substance_note(art['signals'])}",
                "verdict": "UNSCORED",
            })
        date = dt.date.today().isoformat()
        log(f"ledger: {write_ledger(date, rows, profile)}")
        return []

    log("stage 4: relevance scoring")
    scores: dict[int, dict] = {}
    if survivors:
        raw = gemini_text(build_scoring_prompt(profile, survivors))
        scores = parse_scores(raw, len(survivors))
        log(f"  scored {len(scores)}/{len(survivors)}")

    for i, art in enumerate(survivors):
        s = scores.get(i)
        src = by_name.get(art["source"], {})
        weight = src.get("weight", 1.0)

        if s is None:
            # Unscored is the model's failure, not the article's. Let it
            # through at the threshold so a human sees it in the ledger
            # rather than losing it silently.
            art["_score"], art["_why"] = thr["min_score"], "לא דורג, הוכנס כברירת מחדל"
            continue

        raw = s["score"]
        art["_why"] = s["why"] or "-"
        notes = []

        # Editorial signal: their own newsroom picked this out of the week.
        if art["url"].rstrip("/") in highlighted:
            weight *= src.get("roundup_boost", 1.0)
            notes.append("הובלט בסיכום שלהם")

        # High-value recurring formats.
        hit = blocked_by_title(art["title"], art["url"], src.get("boost", []))
        if hit:
            weight *= 1.25
            notes.append(f"פורמט מועדף: `{hit}`")

        art["_score"] = round(min(raw * weight, 10.0), 1)
        if weight != 1.0:
            notes.append(f"{raw}×{round(weight, 2)}")
        if notes:
            art["_why"] += " · " + " · ".join(notes)

    chosen = apply_caps(survivors, sources, thr)
    assign_airtime(chosen, profile)
    chosen_urls = {a["url"] for a in chosen}

    for art in survivors:
        in_ = art["url"] in chosen_urls
        why = art["_why"]
        if in_:
            why = f"**{int(art['_airtime'] * 100)}% זמן אוויר** · {why}"
        elif art.get("_reject"):
            why += f" · {art['_reject']}"
        rows.append({
            "title": art["title"], "url": art["url"],
            "source": art["source"], "score": art["_score"],
            "why": why, "verdict": "IN" if in_ else "OUT",
        })

    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    date = dt.date.today().isoformat()
    log(f"ledger: {write_ledger(date, rows, profile)}")
    log(f"selected {len(chosen)} of {len(found)} candidates")
    for a in chosen:
        log(f"  {a['_score']}/10  {int(a['_airtime'] * 100)}%  "
            f"[{a['source']}] {a['title'][:52]}")
    return chosen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="deterministic stages only, no LLM call, no cost")
    args = ap.parse_args()
    if not args.dry_run and not os.environ.get("GEMINI_API_KEY"):
        sys.exit("set GEMINI_API_KEY, or pass --dry-run")
    select(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
