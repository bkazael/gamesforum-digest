"""Regression tests for editorial, ingestion, recovery and media contracts."""
import copy
import datetime as dt
import io
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error
import wave

import article_parser
import audio_tools
import checkpoint
import discovery
import editorial
import gamesforum_pipeline as p
import memory
import runtime
import sources
import speech


class ProductionTests(unittest.TestCase):
    def test_urls_preserve_article_identifiers(self):
        self.assertEqual(runtime.canonical_url("https://EXAMPLE.com/Article/?id=12&utm_source=x#top"),
                         "https://example.com/Article?id=12")
        self.assertNotEqual(runtime.canonical_url("https://e.test/?id=1"),
                            runtime.canonical_url("https://e.test/?id=2"))

    def test_article_metadata_beats_navigation_and_modified_date(self):
        body = "Actual mobile business reporting. " * 30
        page = '<meta property="article:section" content="Sponsored"><nav>Noise</nav><script type="application/ld+json">' + json.dumps({
            "@graph": [{"@type": "NewsArticle", "headline": "A real headline",
                        "author": {"@type": "Person", "name": "A. Reporter"},
                        "datePublished": "2026-10-01", "dateModified": "2026-10-13",
                        "articleBody": body}]}) + '</script><footer>Other stories</footer>'
        result = article_parser.extract(page, "https://e.test/article", p.strip_tags)
        self.assertEqual(result["published"], dt.date(2026, 10, 1))
        self.assertEqual(result["title"], "A real headline")
        self.assertEqual(result["text"], body.strip())
        self.assertEqual(result["author"], "A. Reporter")
        self.assertEqual(result["content_label"], "Sponsored")

    def test_meta_attributes_and_article_fallback(self):
        page = "<meta content='2026-10-12T03:00:00Z' property='article:published_time'>"
        page += "<meta content='Headline' property='og:title'><nav>Noise</nav>"
        page += "<article><h1>Headline</h1>" + "Evidence. " * 60 + "</article><footer>Noise</footer>"
        result = article_parser.extract(page, "https://e.test", p.strip_tags)
        self.assertEqual(result["published"], dt.date(2026, 10, 12))
        self.assertNotIn("Noise", result["text"])

    def test_atom_prefers_alternate_html_link(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Title</title>
          <link rel="self" href="https://e.test/api/1" type="application/atom+xml"/>
          <link rel="alternate" href="https://e.test/story?id=1&amp;utm_source=feed"/>
          <published>2026-10-12T00:00:00Z</published></entry></feed>'''
        with patch.object(sources, "http_get", return_value=feed):
            items = sources.from_rss({"name": "Atom", "urls": ["https://e.test/feed"]})
        self.assertEqual(items[0]["url"], "https://e.test/story?id=1")

    def test_full_text_in_feed_supplies_article_text(self):
        # gamigion.com: the page extractor can't read its short WordPress
        # posts, but content:encoded in the feed is the whole post.
        feed = """<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
          <title>Studio raises $49M</title><link>https://www.gamigion.com/studio-raises/</link>
          <pubDate>Fri, 16 Oct 2026 08:00:00 +0000</pubDate>
          <description>Short teaser.</description>
          <content:encoded><![CDATA[<p>The studio raised <b>$49M</b> to expand.</p><p>Second paragraph.</p>]]></content:encoded>
        </item></channel></rss>"""
        with patch.object(sources, "http_get", return_value=feed):
            plain = sources.from_rss({"name": "G", "urls": ["https://gamigion.com/feed"]})
            full = sources.from_rss({"name": "G", "urls": ["https://gamigion.com/feed"],
                                     "full_text_in_feed": True})
        self.assertNotIn("text", plain[0])
        self.assertIn("raised $49M to expand", full[0]["text"])
        self.assertIn("Second paragraph", full[0]["text"])
        self.assertNotIn("<p>", full[0]["text"])

    def test_chunking_preserves_text_and_bounds_long_turns(self):
        text = "First sentence is spoken. " * 180
        turns = [{"speaker": "Dana", "text": text}, {"speaker": "Yoni", "text": "A short reply."}]
        chunks = audio_tools.chunks_for_turns(turns, 350)
        self.assertTrue(all(0 < len(c) <= 350 for c in chunks))
        restored = " ".join(line.split(": ", 1)[1] for c in chunks for line in c.splitlines())
        self.assertEqual(restored.split(), (text + "A short reply.").split())

    def test_audio_decoding_rejects_empty_wrong_rate_and_container(self):
        for pcm, mime in [(b"", "audio/L16"), (b"abc", "audio/L16"),
                          (b"abcd", "audio/L16;rate=44100"), (b"abcd", "audio/mpeg")]:
            with self.assertRaises(ValueError):
                audio_tools.decode_pcm(pcm, mime)
        buff = io.BytesIO()
        with wave.open(buff, "wb") as f:
            f.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            f.writeframes(b"\x00\x01" * 100)
        self.assertEqual(audio_tools.decode_pcm(buff.getvalue(), "audio/wav"), b"\x00\x01" * 100)

    def test_hebrew_speech_glossary_is_case_insensitive_and_does_not_mutate_script(self):
        original = [{"speaker": "Dana", "text": "Tamatem works across MENA with IAP."}]
        spoken = speech.prepare_turns(original, {"Tamatem": "טאמאטם", "MENA": "המזרח התיכון"})
        self.assertEqual(spoken[0]["text"], "טאמאטם works across המזרח התיכון with IAP.")
        self.assertEqual(original[0]["text"], "Tamatem works across MENA with IAP.")
        self.assertEqual(speech.untranslated_hebrew_terms(
            [{"text": "ה-IAP ירד, אבל MENA נשאר."}], "he"), ["MENA", "IAP"])

    def test_audio_cache_requires_content_voice_and_integrity(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(checkpoint, "CHECKPOINT_DIR", pathlib.Path(tmp)):
            first = p.audio_identity("Dana: hello")
            checkpoint.save_chunk("2026-10-13", 1, b"abcd", first)
            self.assertEqual(checkpoint.load_chunk("2026-10-13", 1, first), b"abcd")
            self.assertIsNone(checkpoint.load_chunk("2026-10-13", 1, p.audio_identity("Dana: changed")))
            with patch.object(p, "VOICE_A", "different"):
                self.assertIsNone(checkpoint.load_chunk("2026-10-13", 1, p.audio_identity("Dana: hello")))
            checkpoint._chunk_path("2026-10-13", 1).write_bytes(b"abce")
            self.assertIsNone(checkpoint.load_chunk("2026-10-13", 1, first))

    def test_api_ignores_thought_parts_and_accounts_tokens(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"thought": True, "text": "not JSON"}, {"text": '{"ok": true}'}]}}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}}).encode()
        with patch.object(p, "GEMINI_API_KEY", "secret-test"), patch.object(p, "USAGE_LOG", {
                "input_tokens": 0, "output_tokens": 0}), patch("urllib.request.urlopen", return_value=response) as call:
            self.assertEqual(p.gemini_json("x"), {"ok": True})
            self.assertEqual(p.USAGE_LOG["input_tokens"], 5)
            self.assertNotIn("secret-test", call.call_args.args[0].full_url)

    def test_permanent_api_errors_do_not_retry(self):
        with patch.object(p, "GEMINI_API_KEY", "test"), patch("urllib.request.urlopen",
                side_effect=urllib.error.HTTPError("https://api.test", 400, "invalid", {}, io.BytesIO())) as call:
            with self.assertRaises(urllib.error.HTTPError):
                p.gemini_json("x")
            self.assertEqual(call.call_count, 1)

    def test_daily_quota_429_is_diagnosed_and_not_retried(self):
        body = json.dumps({"error": {"status": "RESOURCE_EXHAUSTED",
            "message": "Quota exceeded",
            "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaMetric": "generate_content_free_tier_requests_per_day",
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}).encode()
        error = urllib.error.HTTPError("https://api.test", 429, "quota", {}, io.BytesIO(body))
        with patch.object(p, "GEMINI_API_KEY", "test"), patch("urllib.request.urlopen",
                side_effect=error) as call, patch.object(p, "_retry_sleep") as sleep:
            with self.assertRaises(p.GeminiQuotaExhausted) as caught:
                p.gemini_json("x")
            self.assertIn("requests_per_day", str(caught.exception))
            self.assertEqual(call.call_count, 1)
            sleep.assert_not_called()

    def test_free_tier_request_metric_without_daily_suffix_is_still_terminal(self):
        body = json.dumps({"error": {"status": "RESOURCE_EXHAUSTED",
            "message": "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20",
            "details": []}}).encode()
        error = urllib.error.HTTPError("https://api.test", 429, "quota", {}, io.BytesIO(body))
        with patch.object(p, "GEMINI_API_KEY", "test"), patch("urllib.request.urlopen",
                side_effect=error) as call, patch.object(p, "_retry_sleep") as sleep:
            with self.assertRaises(p.GeminiQuotaExhausted):
                p.gemini_json("x")
            self.assertEqual(call.call_count, 1)
            sleep.assert_not_called()

    def test_retired_model_switches_to_googles_replacement(self):
        # The exact 404 a new project's key got on 2026-10-27.
        body = json.dumps({"error": {"status": "NOT_FOUND", "message":
            "This model models/gemini-2.5-flash is no longer available to new users. "
            "Please update your code to use models/gemini-3.8-flash for the latest features."}}).encode()
        retired = urllib.error.HTTPError("https://api.test", 404, "gone", {}, io.BytesIO(body))
        ok = Mock()
        ok.__enter__ = Mock(return_value=io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP",
            "content": {"parts": [{"text": "{\"a\": 1}"}]}}]}).encode()))
        ok.__exit__ = Mock(return_value=False)
        with patch.object(p, "GEMINI_API_KEY", "test"), patch.dict(p._MODEL_SWAPS, clear=True), \
                patch("urllib.request.urlopen", side_effect=[retired, ok]) as call, \
                patch.object(p, "_retry_sleep") as sleep:
            self.assertEqual(p.gemini_json("x", tries=1, model="gemini-2.5-flash"), {"a": 1})
            self.assertIn("models/gemini-3.8-flash:", call.call_args_list[1].args[0].full_url)
            self.assertEqual(p.live_model("gemini-2.5-flash"), "gemini-3.8-flash")
            sleep.assert_not_called()

    def test_404_without_a_named_replacement_still_fails(self):
        error = urllib.error.HTTPError("https://api.test", 404, "gone", {},
                                       io.BytesIO(b'{"error": {"message": "not found"}}'))
        with patch.object(p, "GEMINI_API_KEY", "test"), patch.dict(p._MODEL_SWAPS, clear=True), \
                patch("urllib.request.urlopen", side_effect=error) as call:
            with self.assertRaises(urllib.error.HTTPError):
                p.gemini_json("x", model="gemini-x")
            self.assertEqual(call.call_count, 1)

    def test_overloaded_model_waits_longer_than_other_errors(self):
        self.assertGreaterEqual(p.retry_delay(503, 0), 45)
        self.assertLessEqual(p.retry_delay(503, 9), 120)
        self.assertEqual(p.retry_delay(500, 0), 10)

    def test_speech_tempo_is_applied_and_bounded(self):
        captured = {}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(p, "gemini_tts_chunk", return_value=b"\0\0" * 2400), \
                patch.object(p.runtime, "load_profile", return_value={"speech": {"tempo": 0.94}}), \
                patch.object(p, "ASSETS_DIR", pathlib.Path(tmp)), \
                patch.object(p.subprocess, "run", side_effect=lambda cmd, **k: (
                    captured.setdefault("cmd", cmd), pathlib.Path(cmd[-1]).write_bytes(b"mp3"))):
            p.synthesize_audio([{"speaker": "Dana", "text": "שלום"}],
                               pathlib.Path(tmp) / "e.wav", pathlib.Path(tmp) / "e.mp3")
        self.assertIn("atempo=0.94,loudnorm", " ".join(captured["cmd"]))
        with patch.object(p.runtime, "load_profile", return_value={"speech": {"tempo": 0.5}}), \
                patch.object(p, "gemini_tts_chunk", return_value=b"\0\0"), \
                tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                p.synthesize_audio([{"speaker": "Dana", "text": "x"}],
                                   pathlib.Path(tmp) / "e.wav", pathlib.Path(tmp) / "e.mp3")

    def test_request_timeout_cannot_outlive_run_budget(self):
        with patch.object(p, "_run_started", 100), patch.object(p.time, "monotonic", return_value=155), \
                patch.object(p, "RUN_DEADLINE_SEC", 60):
            self.assertEqual(p._remaining_timeout(300), 5)
            with self.assertRaises(RuntimeError):
                p._retry_sleep(10)

    def draft(self):
        return {"episode_title": "Weekly news", "digest_summary": [
            {"title": "A", "key_takeaway": "A result", "metrics_mentioned": []}],
            "script": [{"speaker": "Dana", "text": "The studio reported growth."},
                       {"speaker": "Yoni", "text": "Their report does not explain the cause."}]}

    def review(self, **updates):
        result = {"naturalness": 4, "focus": 4, "covered_article_ids": [1],
                  "source_assessments": [{"article_id": 1, "type": "REPORTING",
                    "basis": "Bylined report", "independent_context": True,
                    "listener_value": True, "treatment": "DEEP"}],
                  "summary": "Ready", "issues": []}
        result.update(updates)
        return result

    def run_review(self, data, call, path, strict=False):
        return editorial.review_and_revise(data, [{"title": "A", "text": "The studio reported growth."}],
            path, call, p.PODCAST_SCHEMA, "test-model", ("Dana", "Yoni"), "en", strict=strict)

    def test_review_revises_unsupported_claim_then_rechecks(self):
        issue = {"turn": 1, "kind": "FACT", "blocking": True, "problem": "Unsupported number", "fix": "Remove it"}
        call = Mock(side_effect=[self.review(issues=[issue]), self.draft(), self.review()])
        with tempfile.TemporaryDirectory() as tmp:
            data = self.run_review(self.draft(), call, pathlib.Path(tmp) / "review.json")
            self.assertTrue(data["editorial_review"]["passed"])
            self.assertEqual(call.call_count, 3)
            cached = Mock(side_effect=AssertionError("Paid twice"))
            self.run_review(data, cached, pathlib.Path(tmp) / "review.json")
            cached.assert_not_called()

    def test_persistent_failed_review_blocks_and_keeps_report(self):
        call = Mock(side_effect=[self.review(naturalness=2), self.draft(), self.review(naturalness=2)])
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "report.json"
            with self.assertRaisesRegex(RuntimeError, "quality gate failed"):
                self.run_review(self.draft(), call, path, strict=True)
            self.assertFalse(json.loads(path.read_text())["passed"])
            self.assertEqual(call.call_count, 3)

    def test_review_cannot_pass_with_missing_story_or_unknown_speaker(self):
        draft = self.draft()
        draft["script"][0]["speaker"] = "Stranger"
        self.assertTrue(editorial.structural_problems(draft, ("Dana", "Yoni"), "en"))
        call = Mock(side_effect=[self.review(covered_article_ids=[]), self.draft(),
                                 self.review(covered_article_ids=[])])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, "absent"):
            self.run_review(self.draft(), call, pathlib.Path(tmp) / "r.json", strict=True)

    def test_promotional_event_without_listener_value_fails_editorial_gate(self):
        promotional = {"article_id": 1, "type": "EVENT_PROMOTION", "basis": "Speaker promo",
                       "independent_context": False, "listener_value": False, "treatment": "EXCLUDE"}
        reporting = {"article_id": 2, "type": "REPORTING", "basis": "Independent report",
                     "independent_context": True, "listener_value": True, "treatment": "DEEP"}
        call = Mock(return_value=self.review(covered_article_ids=[2],
                                             source_assessments=[promotional, reporting]))
        with tempfile.TemporaryDirectory() as tmp:
            data = editorial.review_and_revise(self.draft(), [
                {"title": "Event", "text": "Promo details."},
                {"title": "Reporting", "text": "The studio reported growth."}],
                pathlib.Path(tmp) / "r.json", call, p.PODCAST_SCHEMA, "test-model",
                ("Dana", "Yoni"), "en")
            self.assertTrue(data["editorial_review"]["passed"])

    def test_announcement_without_independent_context_cannot_be_deep(self):
        announcement = {"article_id": 1, "type": "COMPANY_ANNOUNCEMENT", "basis": "Partnership",
                        "independent_context": False, "listener_value": True, "treatment": "DEEP"}
        call = Mock(side_effect=[self.review(source_assessments=[announcement]), self.draft(),
                                 self.review(source_assessments=[announcement])])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, "must be brief"):
            self.run_review(self.draft(), call, pathlib.Path(tmp) / "r.json", strict=True)

    def test_non_factual_problems_publish_with_warnings_by_default(self):
        # 2026-10-16 preview: a correct episode was rejected over wording and
        # coverage; without strict, that publishes and says why, loudly.
        call = Mock(side_effect=[self.review(naturalness=2), self.draft(), self.review(naturalness=2)])
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "report.json"
            data = self.run_review(self.draft(), call, path)
            self.assertTrue(data["editorial_review"]["published_with_warnings"])
            self.assertFalse(data["editorial_review"]["passed"])
            self.assertFalse(json.loads(path.read_text())["passed"])

    def test_persistent_factual_error_still_blocks_by_default(self):
        issue = {"turn": 1, "kind": "FACT", "blocking": True, "problem": "Invented number", "fix": "Remove it"}
        call = Mock(side_effect=[self.review(issues=[issue]), self.draft(), self.review(issues=[issue])])
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(RuntimeError, "Invented number"):
            self.run_review(self.draft(), call, pathlib.Path(tmp) / "r.json")

    def test_hebrew_acronyms_are_translated_before_review(self):
        turns = [{"speaker": "Dana", "text": "ה-D2C עולה ב-MENA, ו-IAP יורד."}]
        fixed, n = speech.translate_hebrew_terms(turns, "he")
        self.assertEqual(n, 3)
        self.assertEqual(speech.untranslated_hebrew_terms(fixed, "he"), [])
        self.assertIn("במזרח התיכון", fixed[0]["text"])
        self.assertEqual(speech.translate_hebrew_terms(turns, "en"), (turns, 0))

    def test_memory_retry_replaces_same_date(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(memory, "MEMORY_FILE", pathlib.Path(tmp) / "memory.json"):
            memory.append_entry({"date": "2026-10-13", "title": "first"})
            memory.append_entry({"date": "2026-10-13", "title": "updated"})
            self.assertEqual(json.loads(memory.MEMORY_FILE.read_text()), [{"date": "2026-10-13", "title": "updated"}])

    def test_orphan_audio_is_not_publication(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, "EPISODES", pathlib.Path(tmp)):
            (pathlib.Path(tmp) / "2026-10-13.mp3").write_bytes(b"partial")
            self.assertFalse(p.publication_complete("2026-10-13"))

    def test_profile_rejects_unknown_adapter_and_duplicate_sources(self):
        text = runtime.PROFILE_PATH.read_text()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "profile.toml"
            path.write_text(text.replace('kind = "html"', 'kind = "typo"', 1))
            with self.assertRaisesRegex(ValueError, "kind"):
                runtime.load_profile(path)
            path.write_text(text.replace('name = "PocketGamer.biz"', 'name = "Gamesforum"', 1))
            with self.assertRaisesRegex(ValueError, "unique"):
                runtime.load_profile(path)

    def test_failed_atomic_write_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.json"
            runtime.atomic_write(path, "old")
            with patch.object(runtime.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    runtime.atomic_write(path, "new")
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_publication_marker_detects_modified_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            paths = [root / "episodes" / "2026-10-13.mp3",
                     root / "episodes" / "2026-10-13.json", root / "digests" / "2026-10-13.md",
                     root / "digests" / "2026-10-13-script.md", root / "feed.xml"]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("bens-digest-2026-10-13</guid>" if path.name == "feed.xml" else "complete")
            (root / "state.json").write_text('{"last_run":"2026-10-13"}')
            (root / "reports").mkdir()
            (root / "reports/2026-10-13-run.json").write_text(json.dumps({
                "status": "complete", "artifacts": {
                    str(path.relative_to(root)): __import__("hashlib").sha256(path.read_bytes()).hexdigest()
                    for path in paths}}))
            with patch.object(p, "ROOT", root), patch.object(p, "EPISODES", root / "episodes"), \
                    patch.object(p, "DIGESTS", root / "digests"), patch.object(p, "STATE_FILE", root / "state.json"):
                self.assertTrue(p.publication_complete("2026-10-13"))
                paths[0].write_text("corrupt")
                self.assertFalse(p.publication_complete("2026-10-13"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg tools unavailable")
    def test_real_audio_render_is_playable_mono_with_jingle(self):
        import math
        import struct
        pcm = b"".join(struct.pack("<h", int(2500 * math.sin(2 * math.pi * 200 * i / 24000)))
                       for i in range(48000))
        turns = self.draft()["script"]
        turns[0]["text"] = "Tamatem reported growth across MENA."
        tts = Mock(return_value=pcm)
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, "gemini_tts_chunk", tts):
            root = pathlib.Path(tmp)
            p.synthesize_audio(turns, root / "a.wav", root / "a.mp3")
            spoken_payload = "\n".join(call.args[0] for call in tts.call_args_list)
            self.assertIn("טאמאטם", spoken_payload)
            self.assertIn("המזרח התיכון וצפון אפריקה", spoken_payload)
            self.assertGreater(p.get_duration(root / "a.mp3"), 5)
            self.assertFalse((root / "a.part.mp3").exists())
            self.assertFalse((root / "a.wav").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
