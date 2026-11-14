"""The weekly episode, built over several days.

One episode a week does not need to be built in one morning. The free tier gives
about 20 requests a day per model (resetting at midnight Pacific), a single day
has to absorb every retry, and a Google outage on Monday used to be the whole
week's problem. So the work is spread, and each daily run does only what is due:

    Fri   select    collect, score, choose, plan the order and the theme
    Fri-  write     a few stories a day (the rest of the stories divided by the
    Sun             days left), each saved the moment it is written
    Sun   edit      one review of all stories, one rewrite round, title + outro +
                    the links between stories, intro, proofreading
    Mon   publish   voice, mix, feed
    Tue,  catch-up  anything that did not finish; on the last day a missing review
    Wed             or a weak writer no longer holds the episode back

What is due is decided by the date and by what is already done, never by which
run it is, so a missed day is simply picked up by the next one. A day that runs
out of model budget stops cleanly (`GeminiQuotaExhausted`) and the next quota day
continues. Nothing here is a model call; the stages call `segments`, `discovery`
and the pipeline's voice and publishing code.

State lives in `work/` (committed): `plan.json` (the week's progress and history),
`<target>-content.json` (articles, plan, stories, script) and `usage.json` (model
calls per quota day). `target` is the Monday the episode is for.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os

import checkpoint
import llm
import memory
import runtime
import segments
from llm import log

ROOT = runtime.OUTPUT_ROOT
WORK = checkpoint.WORK_DIR
PLAN_FILE = lambda: checkpoint.WORK_DIR / "plan.json"      # noqa: E731 (tests move WORK_DIR)
FEEDBACK_FILE = runtime.CODE_ROOT / "feedback.md"

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_SCHEDULE = {"select_from": "fri", "edit_from": "sun", "publish_on": "mon", "last_chance": "wed"}
# A story the weakest model wrote is not what the owner wants to hear when a better
# model would answer tomorrow. More than this share of weak stories holds the episode.
WEAK_SHARE = 0.5
MAX_HOLDS = 2


class Paused(Exception):
    """The day's work is done as far as it can go; the next run continues."""


# ---------------------------------------------------------------- the calendar

def _weekday(name: str) -> int:
    return WEEKDAYS.index(str(name).strip().lower()[:3])


def schedule(profile: dict | None = None) -> dict:
    """Offsets in days from the target Monday for each milestone (negative = before)."""
    cfg = dict(DEFAULT_SCHEDULE)
    cfg.update((profile or runtime.load_profile()).get("schedule", {}))

    def before(name: str) -> int:           # a weekday before/at the Monday: -3 for Fri, -1 for Sun, 0 for Mon
        d = _weekday(name)
        return -((7 - d) % 7)

    return {"select": before(cfg["select_from"]), "edit": before(cfg["edit_from"]),
            "publish": before(cfg["publish_on"]),
            "last_chance": (_weekday(cfg["last_chance"]) - 0) % 7}


def week_target(today: dt.date) -> dt.date:
    """The Monday this week's episode is for: this week's Monday on Mon-Wed (a
    catch-up), next Monday from Thursday on."""
    wd = today.weekday()
    return today - dt.timedelta(days=wd) if wd <= 2 else today + dt.timedelta(days=7 - wd)


# ---------------------------------------------------------------- the plan file

def load_plan(target: dt.date) -> dict:
    """The plan for `target`; a plan of another week is archived away (the git
    history keeps it) and a fresh one started."""
    try:
        plan = json.loads(PLAN_FILE().read_text(encoding="utf-8"))
        if plan.get("target") == target.isoformat():
            return plan
    except (OSError, ValueError):
        pass
    return {"target": target.isoformat(), "published": None, "holds": 0, "held": None,
            "last_error": None, "history": []}


def save_plan(plan: dict) -> None:
    checkpoint.WORK_DIR.mkdir(exist_ok=True)
    runtime.atomic_write(PLAN_FILE(), json.dumps(plan, indent=1, ensure_ascii=False))


def load_feedback(limit_entries: int = 3, limit_chars: int = 900) -> str:
    """The owner's own notes on recent episodes (feedback.md, newest entry last,
    entries start with '## '). Taste and pacing only; they never carry facts."""
    try:
        text = FEEDBACK_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    entries = [e.strip() for e in text.split("\n## ")[1:]]
    entries = [e for e in entries if e and not e.lstrip().startswith("(")][-limit_entries:]
    out = "\n".join("- " + " ".join(e.split()) for e in entries)
    return out[-limit_chars:]


# ---------------------------------------------------------------- one run

class Run:
    def __init__(self, today: dt.date, force: bool = False):
        self.today = today
        self.force = force
        self.target = week_target(today)
        self.key = self.target.isoformat()
        self.sched = schedule()
        self.day = (today - self.target).days               # negative before the Monday
        self.plan = load_plan(self.target)
        self.did: list[str] = []

    # --- what is due ---
    def due(self, stage: str) -> bool:
        return self.force or self.day >= self.sched[stage]

    @property
    def last_chance(self) -> bool:
        return self.force or self.day >= self.sched["last_chance"]

    # --- saved work ---
    def load(self):
        return checkpoint.load_content(self.key)

    def save(self, articles: list[dict], data: dict) -> None:
        checkpoint.save_content(self.key, articles, data)

    def call(self, prompt, schema=None, *, model, tries=4, timeout=None):
        return llm.call_model(prompt, schema, model=model, tries=tries, timeout=timeout)

    def writer(self, articles: list[dict], data: dict) -> segments.Writer:
        import gamesforum_pipeline as P
        plan = data.get("plan") or {}
        return segments.Writer(
            call=self.call, models={"creative": llm.GEMINI_CREATIVE_MODEL, "review": llm.GEMINI_REVIEW_MODEL},
            speakers=(P.SPEAKER_A, P.SPEAKER_B), lang=P.LANG, total=len(articles),
            memory_context=memory.load_recent_context(), theme=plan.get("through_line", ""),
            angles=plan.get("angles"), feedback=load_feedback(), log=log)

    # --- the stages ---
    def select(self) -> bool:
        """Collect, score, choose, plan. Saves the selection. False if nothing qualifies."""
        import discovery
        import source_health
        import gamesforum_pipeline as P
        articles = discovery.select()
        if not articles:
            log("No articles cleared the relevance threshold this week.")
            self.plan["no_articles"] = True
            return False
        plan = segments.build_plan(articles, call=self.call, models={"review": llm.GEMINI_REVIEW_MODEL},
                                   lang=P.LANG, log=log)
        articles = [articles[i] for i in plan["order"]]
        self.save(articles, {"segments": {}, "plan": {"through_line": plan["through_line"],
                                                     "angles": plan["angles"]},
                             "source_alerts": source_health.pending_alerts()})
        self.did.append(f"selected {len(articles)} stories")
        return True

    def stories_per_run(self, remaining: int) -> int:
        """Remaining stories divided by the writing days left (today up to and including
        the editing day), so the week's writing is spread; on the editing day, all of it."""
        days_left = self.sched["edit"] - self.day + 1
        if self.force or days_left <= 1:
            return remaining
        return max(1, math.ceil(remaining / days_left))

    def write(self, articles: list[dict], data: dict) -> None:
        done = data.setdefault("segments", {})
        todo = [i for i in range(len(articles)) if str(i) not in done]
        if not todo:
            return
        batch = todo[:self.stories_per_run(len(todo))]
        log(f"writing stories {[i + 1 for i in batch]} of {len(articles)} today "
            f"({len(todo) - len(batch)} left for later)")
        writer = self.writer(articles, data)
        try:
            segments.write_pending(batch, articles, writer, done,
                                   lambda d: self.save(articles, {**data, "segments": d}), log)
        finally:
            self.save(articles, data)
            self.did.append(f"wrote {len([i for i in batch if str(i) in done])} stories")

    def edit(self, articles: list[dict], data: dict) -> dict:
        """Review, assemble (title, outro, bridges), intro, proofread."""
        import gamesforum_pipeline as P
        import proofread
        done = data["segments"]
        if not data.get("script"):
            writer = self.writer(articles, data)
            segments.review_segments(done, articles, writer,
                                     lambda d: self.save(articles, {**data, "segments": d}),
                                     last_chance=self.last_chance)
            built = segments.assemble_episode(
                done, articles, call=self.call,
                models={"creative": llm.GEMINI_CREATIVE_MODEL, "review": llm.GEMINI_REVIEW_MODEL},
                speakers=(P.SPEAKER_A, P.SPEAKER_B), lang=P.LANG, plan=data.get("plan"), log=log)
            data = {**data, **built, "intro_done": False, "proofread_done": False}
            self.save(articles, data)
            self.did.append(f"assembled {len(built['kept'])} stories, {built['bridges_used']} bridges")
            if self.hold_back(articles, data):
                raise Paused("held back: most stories were written by the weakest model")
        if data.get("intro_done") is False:
            data = P.add_intro(data)
            self.save(articles, data)
            self.did.append("intro")
        if data.get("proofread_done") is False:
            data["script"], data["proofread"] = proofread.proofread_episode(
                data["script"], call=self.call, model=llm.GEMINI_CREATIVE_MODEL, lang=P.LANG, log=log,
                glossary=runtime.load_profile().get("speech", {}).get("replacements", {}))
            data["proofread_done"] = True
            self.save(articles, data)
            self.did.append(f"proofread ({data['proofread'].get('applied', 0)} fixes)")
        return P.finalize_episode(data, self.key)

    def hold_back(self, articles: list[dict], data: dict) -> bool:
        """True (and the weak stories scheduled for a rewrite by a better model on the
        next quota day) when most stories were written by the last-resort model.
        Never on the last chance day, and at most MAX_HOLDS times."""
        weak_model = llm.QUOTA_FALLBACKS[-1] if llm.QUOTA_FALLBACKS else None
        if not weak_model or weak_model == llm.live_model(llm.GEMINI_CREATIVE_MODEL):
            return False
        kept = data.get("kept") or []
        weak = [i for i in kept if (data["segments"][str(i)].get("model") or "") == llm.live_model(weak_model)]
        if not kept or len(weak) <= len(kept) * WEAK_SHARE:
            return False
        if self.last_chance or self.plan.get("holds", 0) >= MAX_HOLDS:
            log(f"::warning::{len(weak)} of {len(kept)} stories were written by {weak_model}; "
                "publishing anyway (last chance or hold limit reached)")
            return False
        for i in weak:
            del data["segments"][str(i)]
        for key in ("script", "kept", "dropped", "episode_title", "digest_summary", "writers",
                    "bridges_used", "prep", "intro_done", "proofread_done", "proofread", "intro_source"):
            data.pop(key, None)
        self.save(articles, data)
        self.plan["holds"] = self.plan.get("holds", 0) + 1
        self.plan["held"] = {"date": self.today.isoformat(), "rewriting": [i + 1 for i in weak],
                             "reason": f"{len(weak)} of {len(kept)} stories were written by {weak_model}"}
        self.did.append(f"held back: rewriting {len(weak)} weak stories on the next quota day")
        log(f"::warning::{self.plan['held']['reason']}; they will be rewritten by "
            f"{llm.GEMINI_CREATIVE_MODEL} on the next quota day")
        return True

    def publish(self, articles: list[dict], data: dict) -> None:
        import gamesforum_pipeline as P
        P.preflight(audio=True)
        today = self.today.isoformat()
        # Stories that did not survive review are not part of the published episode.
        kept = data.get("kept")
        articles = [a for i, a in enumerate(articles) if kept is None or i in kept]
        P.publish_episode(articles, data, self.key, today, self.plan.get("history"))
        self.plan["published"] = today
        self.plan["held"] = None
        self.did.append("published")

    def finish(self, status: str) -> None:
        self.plan["history"].append({"date": self.today.isoformat(), "did": self.did, "status": status,
                                     "calls": llm.usage_report()})
        self.plan["history"] = self.plan["history"][-14:]
        save_plan(self.plan)
        log(f"today: {'; '.join(self.did) or 'nothing'} ({status})")

    def forget_other_weeks(self) -> None:
        """Work saved for a different target (a week that never published) is stale."""
        for path in checkpoint.WORK_DIR.glob("*-content.json"):
            if not path.name.startswith(self.key):
                path.unlink(missing_ok=True)


def run(today: dt.date) -> int:
    """One daily run. Returns the process exit code: 0 unless a failure happens on
    the last chance day (then the run turns red and the owner is told)."""
    import discovery
    llm.start_run()
    llm.configure(checkpoint.WORK_DIR / "usage.json")
    force = os.environ.get("PIPELINE_FORCE", "").lower() in {"1", "true", "yes"}
    r = Run(today, force)
    log(f"{today} ({WEEKDAYS[today.weekday()]}): episode for {r.key}, day {r.day:+d} of the week's plan"
        + (" [forced]" if force else ""))

    try:                                    # no model call: remember when undated links appeared
        discovery.observe_sources()
    except Exception as e:                  # noqa: BLE001
        log(f"::warning::observing sources failed ({type(e).__name__}: {e})")

    import gamesforum_pipeline as P
    earlier = P.episode_published_for(r.target)
    if earlier or r.plan.get("published"):
        log(f"the episode for {r.key} is already published ({earlier or r.plan['published']}); nothing to do")
        return 0
    if r.plan.get("no_articles") and not force:
        log(f"nothing qualified for {r.key}; nothing to do")
        return 0
    if not r.due("select"):
        log(f"the week's work starts on {WEEKDAYS[(r.target.weekday() + r.sched['select']) % 7]}; nothing is due")
        return 0

    status = "ok"
    r.forget_other_weeks()
    try:
        loaded = r.load()
        if loaded is None:
            if not r.select():
                status = "nothing qualified this week"
                r.finish(status)
                return 0
            loaded = r.load()
        articles, data = loaded
        source_alerts_note(data)
        r.write(articles, data)
        if r.due("edit"):
            data = r.edit(articles, data)
            if r.due("publish") and not os.environ.get("PIPELINE_SKIP_PUBLISH"):
                r.publish(articles, data)
    except Paused as e:
        status = f"paused: {e}"
        log(f"::warning::{e}")
    except llm.GeminiQuotaExhausted as e:
        status = "paused: model budget spent"
        log(f"::warning::the day's model budget is spent ({str(e)[:100]}); the next run continues")
    except Exception as e:                  # noqa: BLE001
        status = f"failed: {type(e).__name__}: {e}"
        r.plan["last_error"] = {"date": today.isoformat(), "error": status[:400]}
        log(f"::warning::{status}")
        if r.last_chance:
            r.finish(status)
            raise
    r.finish(status)
    return 0


def source_alerts_note(data: dict) -> None:
    for line in data.get("source_alerts") or []:
        log(f"::warning::source alert: {line}")
