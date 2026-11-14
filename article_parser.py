"""Small dependency-free article extractor with publication-date evidence.

Prefer publisher Article JSON-LD, then article/main markup, then the legacy
body fallback. Publication dates are never inferred from page update dates.
"""
from __future__ import annotations

import datetime as dt
from html.parser import HTMLParser
import json
import re


def parse_date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return _text_date(str(value))


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(20\d\d)\b")
_MDY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b")
_DOTTED = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](20\d\d)\b")


def _text_date(text: str):
    """A date written in words or dotted ('12 Nov 2026', 'November 12, 2026',
    '12.11.2026'); None if the text holds none. Day-first for dotted dates."""
    if m := _DMY.search(text):
        month = _MONTHS.get(m.group(2)[:3].lower())
        if month:
            return _safe_date(int(m.group(3)), month, int(m.group(1)))
    if m := _MDY.search(text):
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month:
            return _safe_date(int(m.group(3)), month, int(m.group(2)))
    if m := _DOTTED.search(text):
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def _safe_date(y, m, d):
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


_PUBLISHED_META = ("article:published_time", "og:article:published_time", "datepublished",
                   "pubdate", "publishdate", "publish-date", "date", "dc.date", "dc.date.issued",
                   "sailthru.date", "parsely-pub-date", "article:published", "og:published_time")
_TIME_TAG = re.compile(r"(?is)<time\b[^>]*?datetime\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</time>")


def find_published(page: str, meta: dict, node: dict, body_start: str = ""):
    """The publication date, from the strongest evidence the page offers: the
    article's JSON-LD, publication meta tags, a <time datetime> element, then a
    date written out in the first lines of the article. Modification dates are
    never used. Returns None when the page shows none (the caller may fall back
    to when the link was first seen, see discovery.first_seen)."""
    for value in (node.get("datePublished"), node.get("dateCreated"), node.get("uploadDate")):
        if d := parse_date(value):
            return d
    for key in _PUBLISHED_META:
        if d := parse_date(meta.get(key)):
            return d
    for m in _TIME_TAG.finditer(page):
        before = page[max(0, m.start() - 80):m.start()]
        if "updated" in before.lower() or "modified" in before.lower():
            continue
        if d := parse_date(m.group(1)) or _text_date(m.group(2)):
            return d
    return _text_date(body_start[:600])


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.jsonld = []
        self.capture = False
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            self.meta[a.get("property", a.get("name", "")).lower()] = a.get("content", "")
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self.capture, self.buffer = True, []

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.capture:
            try:
                self.jsonld.append(json.loads("".join(self.buffer)))
            except (ValueError, TypeError):
                pass
            self.capture = False


def _articles(value):
    if isinstance(value, list):
        for v in value:
            yield from _articles(v)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(t in {"Article", "NewsArticle", "BlogPosting", "ReportageNewsArticle"} for t in types):
            yield value
        yield from _articles(value.get("@graph", []))


def _name(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("name") or ""
    if isinstance(value, list):
        return ", ".join(filter(None, (_name(v) for v in value)))
    return ""


def extract(page: str, url: str, strip_tags) -> dict | None:
    parser = Page()
    parser.feed(page)
    nodes = list(_articles(parser.jsonld))
    node = max(nodes, key=lambda n: len(str(n.get("articleBody", ""))), default={})
    title = node.get("headline") or parser.meta.get("og:title") or url.rsplit("/", 1)[-1]
    author = _name(node.get("author")) or parser.meta.get("article:author") or parser.meta.get("author")
    content_label = next((parser.meta[k] for k in
        ("article:section", "article:tag", "content:type", "sponsored") if parser.meta.get(k)), "")
    published = None
    body = node.get("articleBody")
    method = "jsonld"
    if not isinstance(body, str) or len(body) < 400:
        match = re.search(r"(?is)<article\b[^>]*>(.*?)</article>", page)
        if not match:
            match = re.search(r"(?is)<main\b[^>]*>(.*?)</main>", page)
        body = match.group(1) if match else page
        method = "article/main" if match else "body-fallback"
        h1 = re.search(r"(?is)<h1[^>]*>.*?</h1>", body)
        if h1:
            body = body[h1.end():]
        cut = re.search(r"(?i)you might also like|SIGN UP TO OUR NEWSLETTER|<aside\b", body)
        if cut:
            body = body[:cut.start()]
    text = strip_tags(body)
    if len(text) < 400:
        return None
    published = find_published(page, parser.meta, node, text)
    return {"url": url, "title": strip_tags(str(title)), "text": text[:18000],
            "published": published, "author": strip_tags(str(author)) if author else None,
            "content_label": strip_tags(str(content_label)) if content_label else None,
            "extraction": method}
