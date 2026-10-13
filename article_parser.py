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
        return None


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
    published = parse_date(node.get("datePublished") or parser.meta.get("article:published_time")
                           or parser.meta.get("datepublished"))
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
    return {"url": url, "title": strip_tags(str(title)), "text": text[:18000],
            "published": published, "author": strip_tags(str(author)) if author else None,
            "content_label": strip_tags(str(content_label)) if content_label else None,
            "extraction": method}
