"""Shared paths, validated configuration and crash-safe artifact writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

CODE_ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(os.environ.get("PODCAST_OUTPUT_DIR", CODE_ROOT)).resolve()
PROFILE_PATH = Path(os.environ.get("PODCAST_PROFILE", CODE_ROOT / "profile.toml"))


def atomic_write(path: Path, content: str | bytes) -> None:
    """Replace a file only after its complete contents reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content.encode("utf-8") if isinstance(content, str) else content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def canonical_url(url: str) -> str:
    """Drop tracking only; preserve meaningful query values and path case."""
    p = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")
             and k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"),
                       urlencode(sorted(query)), ""))


def load_profile(path: Path = PROFILE_PATH) -> dict:
    with path.open("rb") as f:
        profile = tomllib.load(f)
    names = set()
    for source in profile.get("sources", []):
        name = source.get("name", "")
        if not name or name in names:
            raise ValueError(f"Source names must be present and unique: {name!r}")
        names.add(name)
        if source.get("kind", "rss") not in {"rss", "html", "email"}:
            raise ValueError(f"{name}: kind must be rss, html or email")
        if not source.get("urls") or any(urlsplit(u).scheme not in {"http", "https"}
                                         for u in source["urls"]):
            raise ValueError(f"{name}: urls must contain http(s) URLs")
        if source.get("kind") == "html":
            import re
            re.compile(source["link_pattern"])
        if source.get("max_per_episode", 1) < 1 or source.get("weight", 1) <= 0:
            raise ValueError(f"{name}: weight and max_per_episode must be positive")
    if not names:
        raise ValueError("At least one source is required")
    thr = profile.get("thresholds", {})
    for key in ("max_articles", "min_words", "max_age_days", "max_candidates_to_fetch"):
        if int(thr.get(key, 1)) < 1:
            raise ValueError(f"thresholds.{key} must be positive")
    if not 0 <= thr.get("min_score", 6) <= 10:
        raise ValueError("thresholds.min_score must be between 0 and 10")
    if not 1 <= thr.get("fetch_workers", 4) <= 8:
        raise ValueError("thresholds.fetch_workers must be between 1 and 8")
    voice = profile.get("voice", {})
    if voice.get("speaker_a", "Dana") == voice.get("speaker_b", "Yoni"):
        raise ValueError("The two speaker names must differ")
    air = profile.get("airtime", {})
    shares = [air.get(k, d) for k, d in (("lead_share", .30), ("second_share", .20),
                                        ("remainder_share", .50))]
    if any(x <= 0 for x in shares) or abs(sum(shares) - 1) > .001:
        raise ValueError("airtime shares must be positive and sum to 1")
    return profile
