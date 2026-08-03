#!/usr/bin/env python3
"""
Source adapters. Two kinds, one interface.

  rss   PocketGamer.biz and .com both serve application/rss+xml at
        /index.rss. Verified. This is strictly better than scraping:
        titles, links and dates come structured, and the format does not
        change when someone redesigns the site.

  html  Gamesforum has no feed. Its sitemap.xml is empty, /rss 404s. So the
        listing pages get scraped. This is the one fragile adapter, and it is
        fragile by necessity rather than by choice.

Every adapter returns the same shape:

  {"url", "title", "source", "published" (date|None), "summary"}
"""

from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET

from gamesforum_pipeline import http_get, log, strip_tags

# RSS dates are RFC-822; some feeds emit ISO-8601 anyway.
_RFC822 = "%a, %d %b %Y %H:%M:%S"


def _parse_date(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    raw = raw.strip()
    # Strip the trailing zone, which is inconsistently +0000 / GMT / BST.
    trimmed = re.sub(r"\s*(?:GMT|UTC|[A-Z]{2,4}|[+-]\d{4})\s*$", "", raw)
    for fmt in (_RFC822, "%d %b %Y %H:%M:%S", "%a, %d %b %Y"):
        try:
            return dt.datetime.strptime(trimmed, fmt).date()
        except ValueError:
            pass
    try:                                    # ISO-8601 fallback
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _text(node, *names: str) -> str:
    """First non-empty child among names, namespace-insensitive."""
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names and (child.text or "").strip():
            return html.unescape(child.text.strip())
    return ""


def from_rss(source: dict) -> list[dict]:
    items: list[dict] = []
    for url in source["urls"]:
        try:
            raw = http_get(url)
        except Exception as e:                          # noqa: BLE001
            log(f"  [{source['name']}] feed unreachable: {e}")
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            log(f"  [{source['name']}] malformed feed: {e}")
            continue

        # RSS 2.0 uses item, Atom uses entry. Accept either.
        nodes = [n for n in root.iter()
                 if n.tag.rsplit("}", 1)[-1].lower() in ("item", "entry")]
        for node in nodes:
            title = _text(node, "title")
            link = _text(node, "link", "guid")
            if not link:                                # Atom puts it in href
                for child in node:
                    if child.tag.rsplit("}", 1)[-1].lower() == "link":
                        link = child.attrib.get("href", "")
                        if link:
                            break
            if not (title and link.startswith("http")):
                continue
            summary = strip_tags(_text(node, "description", "summary"))
            items.append({
                "url": link.split("?")[0],
                "title": title,
                "source": source["name"],
                "published": _parse_date(
                    _text(node, "pubdate", "published", "updated", "date")
                ),
                "summary": summary[:600],
            })
    log(f"  [{source['name']}] {len(items)} items from feed")
    return items


def from_html(source: dict) -> list[dict]:
    pattern = re.compile(
        rf'href="({source["link_pattern"]})"', re.I
    )
    seen: set[str] = set()
    items: list[dict] = []
    for listing in source["urls"]:
        try:
            page = http_get(listing)
        except Exception as e:                          # noqa: BLE001
            log(f"  [{source['name']}] listing unreachable: {e}")
            continue
        base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(listing))
        for path in pattern.findall(page):
            url = base + path
            if url in seen:
                continue
            seen.add(url)
            items.append({
                "url": url,
                # No reliable date or title on the listing markup, so the slug
                # stands in until the article body is fetched.
                "title": path.rsplit("/", 1)[-1].replace("-", " "),
                "source": source["name"],
                "published": None,
                "summary": "",
            })
    log(f"  [{source['name']}] {len(items)} items from listings")
    if not items:
        log(f"  [{source['name']}] WARNING: zero links, the page markup "
            "probably changed; check link_pattern in profile.toml")
    return items


ADAPTERS = {"rss": from_rss, "html": from_html}


def harvest_roundups(items: list[dict], source: dict) -> tuple[list[dict], set[str]]:
    """Split roundups out of the stream and mine them for editorial signal.

    A publication's own weekly roundup is the newsroom telling you which of
    its stories mattered. That judgement is worth a lot, but the roundup text
    itself is not: summarising a summary strips out exactly the figures the
    digest depends on.

    So roundups are removed from the content stream, fetched once, and reduced
    to the set of article URLs they point at. Those articles then get a score
    boost in discovery.

    Returns (items_without_roundups, boosted_urls).
    """
    patterns = [p.lower() for p in source.get("roundup_patterns", [])]
    if not patterns:
        return items, set()

    keep, roundups = [], []
    for item in items:
        title = item["title"].lower()
        (roundups if any(p in title for p in patterns) else keep).append(item)

    boosted: set[str] = set()
    for r in roundups:
        try:
            page = http_get(r["url"])
        except Exception as e:                          # noqa: BLE001
            log(f"  [{source['name']}] roundup unreachable: {e}")
            continue
        host = urllib.parse.urlparse(r["url"]).netloc
        for href in re.findall(r'href="(https?://[^"]+)"', page):
            p = urllib.parse.urlparse(href)
            # Same-site article links only: skip nav, tags, social, media.
            if p.netloc != host:
                continue
            path = p.path.rstrip("/")
            if not path or path.count("/") != 1 or len(path) < 25:
                continue
            if any(seg in path for seg in
                   ("/tags", "/browse", "/videos", "/jobs", "/latest",
                    "/news", "/podcasts", "/industry-voices", "/deals")):
                continue
            boosted.add(f"{p.scheme}://{p.netloc}{path}")

    if roundups:
        log(f"  [{source['name']}] {len(roundups)} roundup(s) mined -> "
            f"{len(boosted)} editor-highlighted articles")
    return keep, boosted


def collect(sources: list[dict], max_age_days: int) -> tuple[list[dict], set[str]]:
    """Pull every source, drop stale items, de-duplicate across feeds.

    Returns (items, editor_highlighted_urls).
    """
    cutoff = dt.date.today() - dt.timedelta(days=max_age_days)
    out: list[dict] = []
    seen: set[str] = set()
    highlighted: set[str] = set()

    for source in sources:
        adapter = ADAPTERS.get(source.get("kind", "rss"))
        if not adapter:
            log(f"  [{source.get('name')}] unknown kind, skipped")
            continue
        fetched = adapter(source)
        fetched, boosted = harvest_roundups(fetched, source)
        highlighted |= boosted
        for item in fetched:
            # An unknown date is kept: html sources have none, and dropping
            # them would silently disable Gamesforum entirely.
            if item["published"] and item["published"] < cutoff:
                continue
            key = item["url"].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)

    log(f"  {len(out)} unique items across {len(sources)} sources "
        f"(last {max_age_days} days)")
    return out, highlighted
