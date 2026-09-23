#!/usr/bin/env python3
"""
Tests for sources.py's from_email() adapter (kind = "email" in profile.toml).

Nothing here talks to a real mailbox -- imaplib.IMAP4_SSL is replaced with a
small fake that serves a fixture built to look like a real Substack "new
post" email (subject, a /p/<slug> permalink, an <h1>, body paragraphs, and
footer chrome after an "Unsubscribe" marker). That fixture is a best-effort
reconstruction of Substack's publicly known email template, not a captured
real specimen -- see sources.py's _FOOTER_MARKERS comment and README for
why, and what to do once a real Gamigion email is available to calibrate
against.

What IS real and being tested for keeps regardless of the exact template:
  - IMAP is only ever asked for UNSEEN messages, via BODY.PEEK (no
    unintended read-marking).
  - A message is flagged \\Seen only after it parses successfully.
  - The URL/title/text extraction shape matches what discovery.py expects.
  - A connection/login failure raises EmailFetchError distinctly from
    "mailbox reachable, nothing new" (empty list).
  - collect() (sources.py) catches that exception and records it as -1,
    not 0 -- and source_health.check() treats -1 and email-kind-0
    completely differently (see test_source_health.py for the alert side).
"""

from __future__ import annotations

import email.utils
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import sources as S  # noqa: E402

FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS += 1


def _fixture_email_bytes(slug: str = "deconstruction-issue-12",
                          subject: str = "Deconstruction: the level-design issue") -> bytes:
    """A synthetic Substack "new post" email, built from the platform's
    publicly documented template shape (subject line, masthead, h1 title,
    body, footer boilerplate after an Unsubscribe link) -- not a captured
    real message. See module docstring."""
    html_body = f"""<html><body>
<div class="masthead">Mobile Gaming Today</div>
<a href="https://gamigion.substack.com/p/{slug}">View in browser</a>
<h1>Deconstruction: the level-design issue</h1>
<p>This week we take apart a real level-design decision with actual data
behind it: a puzzle title cut its tutorial from twelve steps to four and
saw Day-1 retention move from thirty-one percent to forty-four percent.
The team credits removing every step that only existed to explain a
mechanic the level itself already taught by being played.</p>
<p>A second study covers a hybrid-casual title's meta layer, where a
seemingly cosmetic change to the reward screen's pacing changed session
length by nearly two minutes on average, across a sample large enough
that the team is confident it was not noise.</p>
<div class="footer">
Unsubscribe from this list | Manage your subscription preferences
&copy; 2026 Gamigion
</div>
</body></html>"""
    msg = f"""From: Gamigion <newsletter@mail.gamigion.com>
To: digest-ingest@example.com
Subject: {subject}
Date: {email.utils.format_datetime(__import__("datetime").datetime.now(__import__("datetime").timezone.utc))}
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

{html_body}"""
    return msg.encode("utf-8")


class _FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL. Serves whatever _INBOX holds at
    construction time; a message is only removed from the "unseen" set once
    from_email() stores the \\Seen flag on it, mirroring a real mailbox."""

    # Populated per-test before from_email() runs.
    INBOX: dict[bytes, bytes] = {}
    UNSEEN: set[bytes] = set()
    RAISE_ON_CONNECT = False

    def __init__(self, host):
        if _FakeIMAP.RAISE_ON_CONNECT:
            raise OSError("connection refused (simulated)")
        self.host = host

    def login(self, address, app_password):
        if app_password == "wrong-password":
            raise Exception("AUTHENTICATIONFAILED")  # noqa: TRY002

    def select(self, mailbox):
        return "OK", [b"1"]

    def search(self, charset, criteria):
        assert criteria == "UNSEEN"
        ids = b" ".join(sorted(_FakeIMAP.UNSEEN))
        return "OK", [ids]

    def fetch(self, msg_id, spec):
        assert spec == "(BODY.PEEK[])"
        if msg_id not in _FakeIMAP.INBOX:
            return "NO", [None]
        return "OK", [(b"1 (BODY[] {123}", _FakeIMAP.INBOX[msg_id])]

    def store(self, msg_id, flag_cmd, flags):
        assert flag_cmd == "+FLAGS" and flags == "\\Seen"
        _FakeIMAP.UNSEEN.discard(msg_id)

    def close(self):
        pass

    def logout(self):
        pass


def _install_fake(monkeypatch_inbox: dict[bytes, bytes], monkeypatch_unseen: set[bytes],
                   raise_on_connect: bool = False):
    _FakeIMAP.INBOX = monkeypatch_inbox
    _FakeIMAP.UNSEEN = monkeypatch_unseen
    _FakeIMAP.RAISE_ON_CONNECT = raise_on_connect
    S.imaplib.IMAP4_SSL = _FakeIMAP


SOURCE = {
    "name": "Gamigion (Mobile Gaming Today)",
    "kind": "email",
    "urls": ["https://gamigion.substack.com/feed"],
}

# ---------------------------------------------------------------- setup

real_imap_ssl = S.imaplib.IMAP4_SSL
os.environ["GMAIL_ADDRESS"] = "digest-ingest@example.com"
os.environ["GMAIL_APP_PASSWORD"] = "correct-password"

# ---------------------------------------------------------------- 1. happy path: one unread message

_install_fake({b"1": _fixture_email_bytes()}, {b"1"})
items = S.from_email(SOURCE)
check("one unread message yields one item", len(items) == 1, f"got {len(items)}")
if items:
    it = items[0]
    check("url is the /p/<slug> permalink",
          it["url"] == "https://gamigion.substack.com/p/deconstruction-issue-12", it["url"])
    check("title comes from the Subject header",
          it["title"] == "Deconstruction: the level-design issue", it["title"])
    check("text carries the real body content",
          "thirty-one percent to forty-four percent" in it["text"], it["text"][:200])
    check("text excludes footer boilerplate",
          "Unsubscribe" not in it["text"] and "Manage your subscription" not in it["text"])
    check("item carries a usable published date", it["published"] is not None)
check("the message was marked \\Seen after successful parse",
      b"1" not in _FakeIMAP.UNSEEN, f"still unseen: {_FakeIMAP.UNSEEN}")

# ---------------------------------------------------------------- 2. no unread mail this week -- silence, not an error

_install_fake({}, set())
items = S.from_email(SOURCE)
check("zero unread messages returns an empty list without raising", items == [])

# ---------------------------------------------------------------- 3. a message with no discoverable permalink is skipped, not crashed

bad = b"""From: Gamigion <newsletter@mail.gamigion.com>
Subject: Some announcement
Date: Mon, 01 Sep 2025 08:00:00 +0000
Content-Type: text/html; charset="utf-8"

<html><body><h1>Announcement</h1><p>No permalink in this one.</p></body></html>"""
_install_fake({b"2": bad}, {b"2"})
items = S.from_email(SOURCE)
check("a message with no matching /p/... link is skipped, not returned", items == [])
check("an unparseable message is left UNSEEN for retry next run",
      b"2" in _FakeIMAP.UNSEEN, f"unseen: {_FakeIMAP.UNSEEN}")

# ---------------------------------------------------------------- 4. login failure raises EmailFetchError distinctly

os.environ["GMAIL_APP_PASSWORD"] = "wrong-password"
_install_fake({}, set())
try:
    S.from_email(SOURCE)
    check("a login failure raises EmailFetchError", False, "no exception raised")
except S.EmailFetchError:
    check("a login failure raises EmailFetchError", True)
except Exception as e:                                    # noqa: BLE001
    check("a login failure raises EmailFetchError", False, f"wrong exception type: {type(e)}")
os.environ["GMAIL_APP_PASSWORD"] = "correct-password"

# ---------------------------------------------------------------- 5. missing credentials raise before ever touching IMAP

del os.environ["GMAIL_ADDRESS"]
try:
    S.from_email(SOURCE)
    check("missing GMAIL_ADDRESS raises EmailFetchError", False, "no exception raised")
except S.EmailFetchError:
    check("missing GMAIL_ADDRESS raises EmailFetchError", True)
os.environ["GMAIL_ADDRESS"] = "digest-ingest@example.com"

# ---------------------------------------------------------------- 6. collect() catches a raising adapter and records -1, not a crash

_install_fake({}, set(), raise_on_connect=True)
out, highlighted, raw_counts = S.collect([SOURCE], max_age_days=30)
check("collect() survives an adapter that raises", out == [] and highlighted == set())
check("collect() records -1 for a hard adapter failure, not 0",
      raw_counts.get(SOURCE["name"]) == -1, f"raw_counts={raw_counts}")

# ---------------------------------------------------------------- teardown

S.imaplib.IMAP4_SSL = real_imap_ssl
os.environ.pop("GMAIL_ADDRESS", None)
os.environ.pop("GMAIL_APP_PASSWORD", None)

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
