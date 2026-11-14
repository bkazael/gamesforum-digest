"""Every Gemini text call goes through this module.

One client, one fallback chain, one counter.

* `gemini_json` is a single schema-constrained call to one model.
* `call_model` is what the pipeline uses: it picks the first model in a chain
  that has quota and budget left, moves down the chain on a spent quota or a
  model that keeps timing out, and records every attempt.
* Every HTTP attempt is counted per model, because on the free tier a failed
  attempt spends the same daily quota as a good one (about 20 requests a day
  per model, resetting at midnight Pacific). The count is kept for the run
  (`CALLS`) and per quota day (`usage.json`, see `configure`), and a model whose
  budget for the day is spent is skipped. That is what lets the weekly episode
  be built over several days without any one day running dry.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import runtime

# ---------------------------------------------------------------- configuration

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Defaults are models a fresh API key can reach; the repo variables of the same
# names override them. The retired 2.5 text models are not defaults any more.
GEMINI_SCORING_MODEL = os.environ.get("GEMINI_SCORING_MODEL") or "gemini-3.5-flash-lite"
GEMINI_CREATIVE_MODEL = os.environ.get("GEMINI_CREATIVE_MODEL") or "gemini-3.8-flash"
GEMINI_REVIEW_MODEL = os.environ.get("GEMINI_REVIEW_MODEL") or "gemini-3.5-flash"
TTS_MODEL = os.environ.get("TTS_MODEL") or "gemini-2.5-flash-preview-tts"
TTS_FALLBACK_MODEL = os.environ.get("TTS_FALLBACK_MODEL") or "gemini-2.5-pro-preview-tts"

# Used in order when a model's quota or budget is gone or it will not answer.
QUOTA_FALLBACKS = [m.strip() for m in (os.environ.get("GEMINI_QUOTA_FALLBACKS")
                   or "gemini-3.5-flash,gemini-3.5-flash-lite").split(",") if m.strip()]

# Requests per model per quota day this pipeline allows itself. The free tier
# gives about 20; four stay unused for manual runs and for retries.
DAILY_BUDGET = int(os.environ.get("GEMINI_DAILY_BUDGET", "16"))

# These nest, innermost first: one attempt < the whole run < the Actions step < the job.
HTTP_TIMEOUT = int(os.environ.get("API_TIMEOUT_SEC", "300"))
TTS_TIMEOUT = int(os.environ.get("TTS_TIMEOUT_SEC", "270"))
RUN_DEADLINE_SEC = int(os.environ.get("RUN_DEADLINE_SEC", "2700"))

UNHEALTHY_AFTER_FAILED_CALLS = 2
FIRST_MODEL_TRIES = 2

_run_started = time.monotonic()
USAGE_LOG = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "tts_chunks": 0}


def log(*a):
    print("[pipeline]", *a, flush=True)


# ---------------------------------------------------------------- run budget

def start_run() -> None:
    """Reset the per-run state: the clock, the counters, the skip lists."""
    global _run_started
    _run_started = time.monotonic()
    USAGE_LOG.update(input_tokens=0, output_tokens=0, thinking_tokens=0, tts_chunks=0)
    CALLS.clear()
    _QUOTA_SPENT.clear()
    _UNHEALTHY.clear()
    _MODEL_FAILS.clear()


def check_deadline() -> None:
    elapsed = time.monotonic() - _run_started
    if elapsed > RUN_DEADLINE_SEC:
        # The graceful exit: distinguishable from the Actions step being killed
        # mid-call. Progress is saved after every unit of work.
        raise RuntimeError(
            f"Run exceeded its {RUN_DEADLINE_SEC}s deadline after {elapsed:.0f}s; "
            "aborting before the CI step gets killed. Progress so far is saved.")


def remaining_timeout(requested: float) -> float:
    check_deadline()
    return max(.1, min(requested, RUN_DEADLINE_SEC - (time.monotonic() - _run_started)))


def retry_sleep(seconds: float) -> None:
    if seconds >= RUN_DEADLINE_SEC - (time.monotonic() - _run_started):
        raise RuntimeError("Insufficient run budget for retry; resume from the saved work")
    time.sleep(seconds)


def retry_delay(code: int | None, attempt: int) -> float:
    """Seconds before retry number `attempt + 1`: 30s, 1, 2, 4, then 5 minutes.
    Patient on purpose; an overloaded model recovers in minutes, not seconds."""
    return min(30 * 2 ** attempt, 300)


# ---------------------------------------------------------------- call counting and daily budget

CALLS: collections.Counter = collections.Counter()      # this run: model -> attempts
_lock = threading.Lock()
_usage_path = None
_usage: dict = {"quota_day": None, "calls": {}}


def quota_day(now: dt.datetime | None = None) -> str:
    """The free-tier day a request counts against: it resets at midnight Pacific."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:                                           # noqa: BLE001
        tz = dt.timezone(dt.timedelta(hours=-8))
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz).date().isoformat()


def configure(usage_path) -> None:
    """Keep the per-quota-day counts in `usage_path` so that every run of the same
    day sees what the earlier ones spent. Without it counts live for one run."""
    global _usage_path, _usage
    _usage_path = usage_path
    _usage = {"quota_day": quota_day(), "calls": {}}
    try:
        if usage_path and usage_path.exists():
            data = json.loads(usage_path.read_text(encoding="utf-8"))
            if data.get("quota_day") == _usage["quota_day"]:
                _usage["calls"] = {str(k): int(v) for k, v in (data.get("calls") or {}).items()}
    except Exception:                                           # noqa: BLE001
        pass


def count_call(model: str) -> None:
    with _lock:
        CALLS[model] += 1
        if _usage["quota_day"] != quota_day():
            _usage.update(quota_day=quota_day(), calls={})
        _usage["calls"][model] = _usage["calls"].get(model, 0) + 1
        if _usage_path:
            try:
                runtime.atomic_write(_usage_path, json.dumps(_usage, indent=2))
            except Exception:                                   # noqa: BLE001
                pass


def used_today(model: str) -> int:
    with _lock:
        if _usage["quota_day"] != quota_day():
            return 0
        return _usage["calls"].get(model, 0)


def budget_left(model: str) -> int:
    return max(0, DAILY_BUDGET - used_today(model))


def usage_report() -> dict:
    return {"quota_day": _usage["quota_day"], "today": dict(_usage["calls"]),
            "this_run": dict(CALLS), "daily_budget": DAILY_BUDGET}


# ---------------------------------------------------------------- retired models

# Google retires models per API key: a key from a new project gets
# 404 "models/X is no longer available to new users. Please update your code
# to use models/Y". The error names the replacement, so take it for the rest of
# the run instead of failing, and say so.
_MODEL_SWAPS: dict[str, str] = {}
_SUGGESTED_MODEL_RE = re.compile(r"\buse models/([\w.\-]+)")


def live_model(model: str) -> str:
    return _MODEL_SWAPS.get(model, model)


def swap_retired_model(model: str, code: int, detail: str, variable: str) -> bool:
    """Switch `model` to Google's suggested replacement after a 404. True if switched."""
    if code != 404 or model in _MODEL_SWAPS:
        return False
    m = _SUGGESTED_MODEL_RE.search(detail)
    if not m or m.group(1) == model:
        return False
    _MODEL_SWAPS[model] = m.group(1)
    print(f"::warning::{model} is not available to this API key; using {m.group(1)}, "
          f"the replacement Google named. Set {variable} to make this explicit.", flush=True)
    return True


def explain_billing_error(code: int) -> None:
    if code == 402:
        print("::error::Gemini returned 402: the API key's project is on prepaid billing "
              "and has no credit left. Top up in AI Studio, or use a key from a project "
              "without billing (free tier).", flush=True)


# ---------------------------------------------------------------- one call to one model

class GeminiQuotaExhausted(RuntimeError):
    """A daily quota is exhausted (the provider's, or this pipeline's own budget)."""


def _error_detail(e: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(e.read().decode("utf-8", "replace"))
        error = payload.get("error", {})
        pieces = [str(error.get("status", "")), str(error.get("message", ""))]
        for item in error.get("details", []):
            if isinstance(item, dict):
                pieces.extend(str(item.get(k, "")) for k in ("reason", "domain"))
                for violation in item.get("violations", []):
                    if isinstance(violation, dict):
                        pieces.extend(str(violation.get(k, "")) for k in
                                      ("quotaMetric", "quotaId", "quotaValue"))
                metadata = item.get("metadata", {})
                if isinstance(metadata, dict):
                    pieces.extend(str(metadata.get(k, "")) for k in
                                  ("quota_metric", "quota_id", "quota_limit", "quota_limit_value"))
        return ": " + " | ".join(dict.fromkeys(p for p in pieces if p))[:700]
    except Exception:                                           # noqa: BLE001
        return ""


def gemini_json(prompt: str, schema: dict | None = None, tries: int = 4,
                timeout: int | None = None, model: str | None = None) -> dict:
    """One schema-constrained call to one model, retried on transient errors.

    Every HTTP attempt is counted against the model (see `count_call`)."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is required.")
    attempt_timeout = timeout or HTTP_TIMEOUT
    requested = model or GEMINI_CREATIVE_MODEL
    gen_config = {"responseMimeType": "application/json"}
    if schema:
        gen_config["responseSchema"] = schema
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": gen_config}).encode()
    attempt = -1
    while attempt < tries - 1:
        attempt += 1
        check_deadline()
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{live_model(requested)}:generateContent")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY})
        started = time.monotonic()
        count_call(live_model(requested))
        try:
            with urllib.request.urlopen(req, timeout=remaining_timeout(attempt_timeout)) as r:
                data = json.loads(r.read())
            cand = (data.get("candidates") or [{}])[0]
            if cand.get("finishReason") not in (None, "STOP"):
                raise ValueError(f"Incomplete text response: {cand.get('finishReason')}")
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])
                           if not p.get("thought"))
            usage = data.get("usageMetadata", {})
            USAGE_LOG["input_tokens"] += usage.get("promptTokenCount", 0)
            USAGE_LOG["output_tokens"] += usage.get("candidatesTokenCount", 0)
            USAGE_LOG["thinking_tokens"] = USAGE_LOG.get("thinking_tokens", 0) + usage.get("thoughtsTokenCount", 0)
            log(f"    [{live_model(requested)} responded in {time.monotonic() - started:.1f}s]")
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError("Expected a JSON object from Gemini")
            return result
        except urllib.error.HTTPError as e:
            detail = _error_detail(e)
            log(f"    Gemini API attempt {attempt + 1}/{tries} failed: HTTP {e.code}{detail}")
            if swap_retired_model(requested, e.code, detail, "GEMINI_*_MODEL"):
                tries += 1                      # the switch itself costs no attempt
                continue
            explain_billing_error(e.code)
            if e.code == 429 and re.search(
                    r"per.?day|daily|requestsperday|free.?tier.?requests", detail, re.I):
                raise GeminiQuotaExhausted(detail or "daily quota exhausted") from e
            if e.code not in (408, 429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
            retry_sleep(retry_delay(e.code, attempt))
        except Exception as e:                                  # noqa: BLE001
            log(f"    Gemini API attempt {attempt + 1}/{tries} failed: {e}")
            if attempt == tries - 1:
                raise
            retry_sleep(retry_delay(None, attempt))
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- the chain

_QUOTA_SPENT: set[str] = set()
_MODEL_FAILS: dict[str, int] = {}
_UNHEALTHY: set[str] = set()


def _is_transient(e: Exception) -> bool:
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (408, 429, 500, 502, 503, 504)
    # Timeouts, resets, DNS errors. A RuntimeError is our own run-budget signal.
    return isinstance(e, (OSError, ValueError)) and not isinstance(e, GeminiQuotaExhausted)


def chain_for(model: str) -> list[str]:
    """`model` first, then the fallbacks, minus every model that is spent, skipped
    for this run, or out of its daily budget."""
    return [m for m in dict.fromkeys([model, *QUOTA_FALLBACKS])
            if m not in _QUOTA_SPENT and m not in _UNHEALTHY and budget_left(live_model(m)) > 0]


def call_model(prompt: str, schema: dict | None = None, *, model: str,
               tries: int = 4, timeout: int | None = None) -> dict:
    """`gemini_json` down the chain.

    Moves on when a model's daily quota (or this pipeline's budget for it) is
    spent, or when it exhausted its few tries on timeouts or overload; after
    repeated failed calls a model is skipped for the rest of the run. The last
    model in the chain gets all `tries` and its error is raised. When no model
    has quota or budget left, `GeminiQuotaExhausted` is raised and the caller
    stops cleanly: the rest is done on the next quota day.

    The result carries "_model": the model that answered.
    """
    chain = chain_for(model)
    if not chain:
        raise GeminiQuotaExhausted("every configured model's daily quota or budget is spent")
    for i, m in enumerate(chain):
        last = i == len(chain) - 1
        try:
            result = gemini_json(prompt, schema, tries=tries if last else min(tries, FIRST_MODEL_TRIES),
                                 timeout=timeout, model=m)
            _MODEL_FAILS[m] = 0
            result["_model"] = live_model(m)
            return result
        except GeminiQuotaExhausted:
            _QUOTA_SPENT.add(m)
            if last:
                raise
            print(f"::warning::{m}: daily request quota is spent; using {chain[i + 1]} "
                  "for the rest of this run.", flush=True)
        except Exception as e:                                  # noqa: BLE001
            if last or not _is_transient(e):
                raise
            _MODEL_FAILS[m] = _MODEL_FAILS.get(m, 0) + 1
            note = f"{m} did not answer ({type(e).__name__}); trying {chain[i + 1]} for this call"
            if _MODEL_FAILS[m] >= UNHEALTHY_AFTER_FAILED_CALLS:
                _UNHEALTHY.add(m)
                note += f", and skipping {m} for the rest of this run"
            print(f"::warning::{note}.", flush=True)
    raise GeminiQuotaExhausted("every configured model's daily quota is spent")
