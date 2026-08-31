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
  4. SCORE    one batched Gemini call rates relevance to YOU    (one API call per ~20 articles)

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
    fetch_article, gemini_json, log,
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
    wants = profile["identity"].get("wants", "").strip()
    core = "\n".join(f"- {x}" for x in profile["interests"]["core"])
    deprio = "\n".join(f"- {x}" for x in profile["interests"]["deprioritize"])

    items = []
    for c in candidates:
        excerpt = " ".join(c["text"].split()[:180])
        # "ID n" rather than "[n]": bracket notation in the prompt invites the
        # model to answer in that same notation instead of JSON.
        items.append(
            f"ID {c['_idx']}\n"
            f"TITLE: {c['title']}\n"
            f"SIGNALS: {substance_note(c['signals'])}\n"
            f"EXCERPT: {excerpt}"
        )
    blob = "\n\n---\n\n".join(items)

    return f"""You are this person's research analyst. Decide what earns a
place in his weekly briefing.

THE READER:
{ident}

WHAT HE WANTS OUT OF AN EPISODE:
{wants}

SUBJECTS THAT CONCERN HIM:
{core}

RARELY WORTH HIS TIME:
{deprio}

HOW TO THINK ABOUT EACH ARTICLE

Do not pattern-match on topic. "Mentions monetization" is not relevance.
Reason about this specific operator, running these specific genres, and ask
which of these four things the article actually delivers:

  DECISION    could plausibly change something he does: pricing, channel mix,
              a LiveOps choice, a build-vs-buy call, a compliance exposure
  TACTIC      a concrete method another studio used, described in enough
              detail that he could try a version of it
  COMPETITIVE what studios in casual, puzzle, hybrid-casual or real-money
              skill gaming are actually doing, and what happened as a result
  MARKET      worth knowing because the industry is talking about it, even
              with no immediate action. He values this on its own.

An article needs to be strong on ONE of these, not all four.

WEIGH THESE HEAVILY
- Real-money skill gaming is his most exposed and least covered genre.
  Anything on skill-versus-chance rulings, state or territory legality,
  payments, withdrawals, KYC, collusion, bots, or Apple and Google policy on
  real-money apps matters to him far more than the headline suggests.
  Score these high even when the article is thin, because the alternative is
  he does not hear about it at all.
- Named studios in his genres, with outcomes attached.
- Actual figures. A benchmark he can measure himself against.

BE SCEPTICAL OF
- Confident headlines with no evidence underneath. "The future of X" with no
  data is a 3, not an 8.
- Vendor bylines that are really product marketing. If a named company's
  employee wrote it and the conclusion is "use a tool like ours", drop it a
  few points, unless the data in it stands on its own.
- Funding announcements with no operational lesson.

SCALE
  9-10  he would be worse off not knowing this
  7-8   solid, clearly earns its airtime
  5-6   worth a mention, thin or partly familiar
  3-4   tangential, or assertion with no evidence
  0-2   promotion or PR with nothing underneath

Judge the excerpt you were given, not the headline's confidence.

For each article, first write your reasoning, then the axis it delivers on,
then the score. Reason before you score, not after.

"why" must be concrete and specific to the article. "Relevant to mobile
gaming" is a useless answer. "ZBD survey, 195 execs, retention budget data"
is a useful one.

Score every article listed, using the ID given for each.

ARTICLES:
{blob}
"""


# Property definition order matters: the model fills fields in this order,
# so putting reasoning before score means it actually thinks first and
# commits to a number second. Reversed, the number comes out of nowhere and
# the reasoning becomes a justification written after the fact.
#
# The batch of per-article rows is wrapped under "articles" rather than
# being a bare top-level array, so a partially-valid response still parses
# as a dict with whatever survived, instead of failing to parse at all.
#
# Type names are UPPERCASE ("OBJECT", "STRING", ...): that's what Gemini's
# responseSchema expects, matching PODCAST_SCHEMA above. This schema used to
# be sent to a different API (see CHANGELOG) whose schema convention is
# lowercase JSON Schema types, and the casing was never updated when the
# call underneath switched -- Gemini likely rejected every scoring batch
# outright, silently pushing every article onto the substance-signal
# fallback below instead of a real relevance judgment.
SCORE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "articles": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "reasoning": {
                        "type": "STRING",
                        "description": "What this article actually contains "
                                       "and what it would mean for this "
                                       "specific operator. Two or three "
                                       "sentences.",
                    },
                    "axis": {
                        "type": "STRING",
                        "enum": ["DECISION", "TACTIC", "COMPETITIVE",
                                 "MARKET", "NONE"],
                    },
                    "score": {"type": "INTEGER"},
                    "why": {
                        "type": "STRING",
                        "description": "One concrete line naming what is "
                                       "in it.",
                    },
                    "topic": {"type": "STRING"},
                },
                "required": ["id", "reasoning", "axis", "score", "why"],
            },
        },
    },
    "required": ["articles"],
}

# Batch size for scoring. Seventy-plus articles in one call is where the
# first run fell over: the response drifted out of JSON entirely.
SCORE_BATCH = 20


def score_all(profile: dict, candidates: list[dict]) -> dict[int, dict]:
    """Score in batches, with the response shape enforced by the API."""
    out: dict[int, dict] = {}
    for start in range(0, len(candidates), SCORE_BATCH):
        batch = candidates[start:start + SCORE_BATCH]
        try:
            result = gemini_json(build_scoring_prompt(profile, batch),
                                 SCORE_SCHEMA)
            rows = result.get("articles", []) if isinstance(result, dict) else []
        except Exception as e:                          # noqa: BLE001
            log(f"  batch {start // SCORE_BATCH + 1} failed ({e})")
            continue
        valid = {c["_idx"] for c in batch}
        for row in rows if isinstance(rows, list) else []:
            try:
                i = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if i not in valid:
                continue
            out[i] = {
                "score": max(0, min(10, int(row.get("score", 0)))),
                "why": str(row.get("why", "")).strip(),
                "topic": str(row.get("topic", "")).strip(),
                "axis": str(row.get("axis", "")).strip(),
                "reasoning": str(row.get("reasoning", "")).strip(),
            }
        log(f"  batch {start // SCORE_BATCH + 1}: "
            f"{len(batch)} sent, {len(out)} scored so far")
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


STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "as", "by", "from", "is", "are", "was", "were", "be",
    "its", "it", "this", "that", "how", "why", "what", "new", "more",
    "says", "said", "after", "over", "into", "out", "up", "down", "his",
    "her", "their", "not", "can", "will", "has", "have", "had",
}


# Words that say "mobile games article" and nothing about WHICH story.
# Without this list every pair looks related.
GENERIC = {
    "gaming", "games", "game", "mobile", "studio", "studios", "player",
    "players", "market", "revenue", "app", "apps", "store", "industry",
    "company", "companies", "data", "report", "million", "billion",
    "growth", "launch", "users", "user", "based", "case", "against",
    "showing", "shows", "using", "expand", "target", "results", "quarter",
    "business", "platform", "advertising", "spend", "acquisition",
    "acquires", "financial", "detailed", "insights", "strategy",
}


def _is_name(tok: str) -> bool:
    """Would this token identify WHICH story, if shared?

    Bare integers and years fail: two unrelated pieces both mentioning 2026
    are not the same story. A figure like 719m does identify one.
    """
    if tok in GENERIC:
        return False
    if tok.isdigit():
        return False                       # includes years
    if tok[0].isdigit() and len(tok) < 3:
        return False
    return len(tok) >= 3


def _fingerprint(art: dict) -> set[str]:
    """Distinctive tokens from title plus the model's one-line summary.

    Two outlets covering one event rarely share a headline, but they always
    share the names: Papaya, Skillz, 719m.
    """
    text = f"{art.get('title', '')} {art.get('_why', '')}".lower()
    tokens = re.findall(r"[a-z][a-z0-9'.-]{2,}|\d[\d.,]*[a-z]*", text)
    return {t.strip(".,'") for t in tokens if t not in STOPWORDS} - {""}


def _heuristic_clash(art: dict, kept: list[dict], min_shared: int = 3,
                     min_overlap: float = 0.18) -> dict | None:
    """Cheap first pass: do two articles share enough distinctive names to be
    worth CHECKING for duplication? This does not itself decide they are
    duplicates -- shared company names and the model's own formulaic
    connector words ("impacting", "reveals") in its "why" line can cross
    this bar for two genuinely different stories. Seen in production on
    2026-08-31: "Google settles a $353m lawsuit" and "Google Play's new
    performance requirements" matched on {google, play, impacting} alone --
    unrelated announcements, same company.

    Two conditions, both required, to keep this pre-filter tight enough that
    confirm_same_story() below isn't called on every pair that merely
    mentions the same brand:
      * at least `min_shared` distinctive names in common
      * overlap of at least `min_overlap` against the smaller item
    """
    fp = _fingerprint(art)
    if not fp:
        return None
    for other in kept:
        ofp = _fingerprint(other)
        if not ofp:
            continue
        shared = fp & ofp
        distinctive = {t for t in shared if _is_name(t)}
        overlap = len(shared) / min(len(fp), len(ofp))
        if len(distinctive) >= min_shared and overlap >= min_overlap:
            return other
    return None


DEDUPE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "same_story": {
            "type": "BOOLEAN",
            "description": "True ONLY if both articles report the same "
                           "specific underlying news event -- the same "
                           "announcement, ruling, launch or figures. Being "
                           "about the same company or general topic is not "
                           "enough.",
        },
        "reasoning": {"type": "STRING"},
    },
    "required": ["same_story", "reasoning"],
}


def confirm_same_story(a: dict, b: dict) -> bool:
    """Ask the model to actually judge a heuristic clash, instead of trusting
    shared keywords alone. Rare in practice (0-3 times a week, only when
    _heuristic_clash already flagged a pair), so the extra call costs
    nothing meaningful next to one scoring batch.

    Fails closed toward keeping both articles: an API error here should
    cost the episode a little redundancy, never a whole topic silently
    dropped because a confirmation call happened to time out.
    """
    prompt = f"""Two article summaries from a mobile-gaming industry news
feed matched on a keyword heuristic and might report the same underlying
story, or might simply be two different stories about the same company or
general subject.

ARTICLE A: {a.get('title', '')}
{a.get('_why', '')}

ARTICLE B: {b.get('title', '')}
{b.get('_why', '')}

Are these genuinely the SAME underlying news event -- the same announcement,
ruling, launch, or figures -- not merely related or about the same company?"""
    try:
        result = gemini_json(prompt, DEDUPE_SCHEMA)
        same = bool(result.get("same_story"))
        log(f"    dedupe check: {a['title'][:40]!r} vs {b['title'][:40]!r} "
            f"-> {'SAME' if same else 'distinct'} "
            f"({result.get('reasoning', '')[:80]})")
        return same
    except Exception as e:                              # noqa: BLE001
        log(f"    dedupe confirmation failed ({e}); treating as distinct stories")
        return False


def select_stories(scored: list[dict], sources: list[dict], thr: dict) -> list[dict]:
    """Rank once; take each candidate if it clears the per-source cap AND is
    not a CONFIRMED duplicate of something already kept -- both checked
    together, in one pass, so a cap slot a duplicate would have used stays
    available for the next candidate from that source instead of being lost.

    This replaces a two-pass design (cap on a widened limit, dedupe after)
    that could fill a source's cap with a pick dedupe then removed, with
    nothing behind it to backfill the freed slot. On 2026-08-31 that alone
    cut what should have been a 7-article episode to 5: Gamesforum's 2-slot
    cap and PocketGamer.biz's 3-slot cap each lost one pick to a dedupe hit,
    and the next-best candidate from that same source -- sitting right
    there, unrelated to the dropped story -- was never reconsidered.
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

        clash = _heuristic_clash(art, chosen)
        if clash and confirm_same_story(art, clash):
            art["_reject"] = f"אותו סיפור כמו \"{clash['title'][:40]}\" (מאושר ע\"י Gemini)"
            log(f"  duplicate story dropped: {art['title'][:56]}")
            # Keep the link, so the show notes still credit both outlets.
            clash.setdefault("_also", []).append(
                {"title": art["title"], "url": art["url"],
                 "source": art.get("source", "")}
            )
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

    # Runtime safety valve: fetching is one sequential HTTP request per
    # candidate, with retries on each. Fine at today's 4 sources; if more
    # get added later the fetch stage is where a much longer candidate list
    # would first threaten the Action's 20-minute timeout. Cap it here,
    # loudly, rather than let a slow run fail partway through TTS having
    # already spent the text-generation budget.
    max_fetch = thr.get("max_candidates_to_fetch", 150)
    if len(to_fetch) > max_fetch:
        log(f"  {len(to_fetch)} candidates to fetch exceeds "
            f"max_candidates_to_fetch ({max_fetch}); keeping the first "
            f"{max_fetch}, dropping the rest for this run")
        for cand in to_fetch[max_fetch:]:
            rows.append({
                "title": cand["title"], "url": cand["url"],
                "source": cand["source"], "score": None,
                "why": f"מעל תקרת max_candidates_to_fetch ({max_fetch})",
                "verdict": "SKIPPED",
            })
        to_fetch = to_fetch[:max_fetch]

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
    for i, art in enumerate(survivors):
        art["_idx"] = i
    scores = score_all(profile, survivors) if survivors else {}
    log(f"  scored {len(scores)}/{len(survivors)}")
    if survivors and not scores:
        log("  WARNING: nothing scored, falling back to substance signals")

    for i, art in enumerate(survivors):
        s = scores.get(i)
        src = by_name.get(art["source"], {})
        weight = src.get("weight", 1.0)

        if s is None:
            # Scoring failed for this one. Do NOT hand out a flat pass mark:
            # that makes selection arbitrary and hides the failure behind a
            # plausible-looking number. Fall back to the deterministic
            # substance signal instead, and label it clearly in the ledger.
            art["_score"] = round(
                min(3.0 + art["signals"]["figures_per_100w"] * 1.5, 7.0), 1
            )
            art["_why"] = (
                f"לא דורג · דירוג חלופי לפי צפיפות נתונים "
                f"({art['signals']['figures_per_100w']} מספרים ל-100 מילים)"
            )
            continue

        raw = s["score"]
        art["_why"] = s["why"] or "-"
        art["_reasoning"] = s.get("reasoning", "")
        notes = []
        if s.get("axis") and s["axis"] != "NONE":
            notes.append(f"**{s['axis']}**")

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

    chosen = select_stories(survivors, sources, thr)
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
