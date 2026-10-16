#!/usr/bin/env python3
"""
Can GitHub Actions reach a source's full article text, and by which route?

Run by .github/workflows/probe_source.yaml (manual only, no Gemini quota).
Written for Gamigion: its Substack RSS returns 403 to GitHub Actions
(Cloudflare), and its newsletter emails carry only a teaser and a link, so
the email adapter alone can't supply article text. This tries every other
route from the runner itself -- the only network that matters, since the
pipeline runs there -- and prints, per URL, the status and how much readable
text came back. Nothing is written to the repo.

URLS (env, comma-separated) overrides the default list.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

DEFAULT = [
    "https://gamigion.com/",
    "https://www.gamigion.com/journal",
    "https://gamigion.com/feed",
    "https://gamigion.substack.com/feed",
    "https://gamigion.substack.com/archive",
    "https://gamigion.substack.com/api/v1/archive?sort=new&limit=5",
    "https://gamigion.substack.com/api/v1/posts?limit=3",
    "https://substack.com/api/v1/profile/posts?profile_handle=gamigion&limit=3",
]


def get(url: str) -> tuple[int | str, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html,application/json,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode("utf-8", "replace")[:300]
    except Exception as e:                                  # noqa: BLE001
        return type(e).__name__, "", str(e)[:300]


def words(html: str) -> int:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    return len(re.sub(r"<[^>]+>", " ", text).split())


def main() -> int:
    urls = [u.strip() for u in os.environ.get("URLS", "").split(",") if u.strip()] or DEFAULT
    post_links: list[str] = []
    for url in urls:
        status, ctype, body = get(url)
        print(f"\n== {url}\n   status {status} | {ctype or '-'} | {len(body):,} chars | ~{words(body):,} words")
        if "json" in ctype:
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            posts = data if isinstance(data, list) else (data or {}).get("posts", []) if isinstance(data, dict) else []
            for p in posts[:5]:
                if not isinstance(p, dict):
                    continue
                if isinstance(p.get("content"), dict):        # WordPress REST
                    wp_title = (p.get("title") or {}).get("rendered") if isinstance(p.get("title"), dict) else p.get("title")
                    print(f"   wp post: {wp_title!r} | {p.get('date')} | "
                          f"content ~{words(p['content'].get('rendered') or ''):,} words | {p.get('link')}")
                    if p.get("link"):
                        post_links.append(p["link"])
                    continue
                body_html = p.get("body_html") or ""
                print(f"   post: {p.get('title')!r} | audience={p.get('audience')} | "
                      f"body_html ~{words(body_html):,} words | {p.get('canonical_url')}")
                if p.get("canonical_url"):
                    post_links.append(p["canonical_url"])
        elif "xml" in ctype or "rss" in ctype:
            items = re.findall(r"<item>.*?</item>", body, re.S)
            print(f"   RSS items: {len(items)}")
            for item in items[:3]:
                link = re.search(r"<link>(.*?)</link>", item, re.S)
                title = re.search(r"<title>(.*?)</title>", item, re.S)
                full = re.search(r"<content:encoded>(.*?)</content:encoded>", item, re.S)
                print(f"   item: {(title.group(1) if title else '?').strip()[:90]!r} | "
                      f"content:encoded ~{words(full.group(1)) if full else 0:,} words")
                if link:
                    post_links.append(link.group(1).strip())
            # What the pipeline's own feed reader gets with full_text_in_feed.
            import sources
            got = sources.from_rss({"name": "probe", "urls": [url], "full_text_in_feed": True})
            for it in got[:5]:
                text = it.get("text") or ""
                print(f"   from_rss: {len(text.split()):>4} words | {it['published']} | {it['title'][:70]!r}")
            if got and got[0].get("text"):
                print(f"   first text starts: {got[0]['text'][:300]!r}")
        else:
            post_links += re.findall(r"https://gamigion\.(?:substack\.)?com/p/[a-z0-9-]+", body)[:3]
            title = re.search(r"<title>(.*?)</title>", body, re.S | re.I)
            if title:
                print(f"   <title>: {title.group(1).strip()[:120]}")
    seen = set()
    for link in post_links:
        if link in seen or len(seen) >= 3:
            continue
        seen.add(link)
        status, ctype, body = get(link)
        print(f"\n== post page {link}\n   status {status} | ~{words(body):,} words on the page")
        # What the pipeline itself would extract -- the number that matters.
        try:
            import gamesforum_pipeline as P
            art = P.fetch_article(link)
        except Exception as e:                              # noqa: BLE001
            art = None
            print(f"   fetch_article raised {type(e).__name__}: {e}")
        if art:
            text = art.get("text") or ""
            print(f"   fetch_article: {len(text.split()):,} words | title {art.get('title')!r}")
            print(f"   starts: {text[:300]!r}")
            print(f"   ends:   {text[-200:]!r}")
        else:
            print("   fetch_article: nothing usable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
