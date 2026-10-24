"""Direct LLM client: transport contract, breaker, and credential hygiene.

Every test here is offline.  urlopen is replaced with a fake so no request can
leave the machine, and the configuration is a fixture file rather than the
operator's real ~/.claude/settings.json -- a test that read the real file would
both leak the environment into the suite and pass or fail depending on whose
machine it ran on.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import llm_client  # noqa: E402

#: Not a real credential: the tests assert this string never reaches a report.
FAKE_KEY = "sk-test-not-a-real-credential-0000000000"


class Response:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, body, status=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def message_body(text):
    return {"content": [{"type": "text", "text": text}], "model": "m"}


class ClientCase(unittest.TestCase):
    """Points the client at a fixture settings file and resets global state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-llm-")
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "settings.json"
        self.write_settings()
        patcher = mock.patch.dict(os.environ, {
            llm_client.SETTINGS_ENV: str(self.settings),
        }, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("HIPPOMIND_LLM_BASE_URL", "HIPPOMIND_LLM_API_KEY",
                     "HIPPOMIND_LLM_MODEL"):
            if name in os.environ:
                self.addCleanup(os.environ.pop, name, None)
                os.environ.pop(name, None)
        llm_client.reset()
        self.addCleanup(llm_client.reset)

    def write_settings(self, env=None):
        payload = {"env": env if env is not None else {
            "ANTHROPIC_BASE_URL": "https://llm.example.test",
            "ANTHROPIC_API_KEY": FAKE_KEY,
            "ANTHROPIC_MODEL": "test-model-1",
        }}
        self.settings.write_text(json.dumps(payload), encoding="utf-8")

    def fake_urlopen(self, body, status=200):
        """A urlopen replacement that records the requests it was handed."""
        calls = []

        def opener(request, timeout=None):
            calls.append((request, timeout))
            return Response(body, status)

        return opener, calls


class ConfigTests(ClientCase):
    def test_settings_file_supplies_base_key_and_model(self):
        config = llm_client.load_config()
        self.assertIsNotNone(config)
        self.assertEqual("https://llm.example.test", config.base_url)
        self.assertEqual("test-model-1", config.model)
        self.assertEqual(FAKE_KEY, config.api_key)
        self.assertEqual("https://llm.example.test/v1/messages", config.messages_url)

    def test_environment_overrides_the_settings_file(self):
        os.environ["HIPPOMIND_LLM_BASE_URL"] = "https://override.test"
        os.environ["HIPPOMIND_LLM_MODEL"] = "override-model"
        config = llm_client.load_config()
        self.assertEqual("https://override.test", config.base_url)
        self.assertEqual("override-model", config.model)
        self.assertEqual("env", config.source)

    def test_auth_token_is_accepted_when_the_api_key_is_absent(self):
        self.write_settings({"ANTHROPIC_BASE_URL": "https://llm.example.test",
                             "ANTHROPIC_AUTH_TOKEN": FAKE_KEY})
        config = llm_client.load_config()
        self.assertEqual(FAKE_KEY, config.api_key)

    def test_base_url_forms_all_resolve_to_one_endpoint(self):
        for raw in ("https://h.test", "https://h.test/", "https://h.test/v1",
                    "https://h.test/v1/"):
            self.assertEqual("https://h.test/v1/messages",
                             llm_client._normalise_base(raw) + llm_client.MESSAGES_PATH,
                             raw)
        self.assertEqual("https://h.test", llm_client._normalise_base("h.test"))

    def test_missing_settings_file_is_unavailable_not_an_error(self):
        self.settings.unlink()
        llm_client.reset()
        self.assertIsNone(llm_client.load_config())
        self.assertFalse(llm_client.available())

    def test_malformed_settings_file_is_unavailable(self):
        self.settings.write_text("{not json", encoding="utf-8")
        llm_client.reset()
        self.assertFalse(llm_client.available())

    def test_settings_without_env_block_is_unavailable(self):
        self.settings.write_text(json.dumps({"model": "x"}), encoding="utf-8")
        llm_client.reset()
        self.assertFalse(llm_client.available())

    def test_missing_key_makes_it_unavailable(self):
        self.write_settings({"ANTHROPIC_BASE_URL": "https://llm.example.test"})
        llm_client.reset()
        self.assertFalse(llm_client.available())

    def test_settings_file_is_cached_until_it_changes(self):
        first = llm_client._settings_env()
        self.assertEqual("test-model-1", first["ANTHROPIC_MODEL"])
        self.write_settings({"ANTHROPIC_BASE_URL": "https://changed.test",
                             "ANTHROPIC_API_KEY": FAKE_KEY})
        # A rewrite changes mtime/size, so the cache must not serve the old block.
        second = llm_client._settings_env()
        self.assertEqual("https://changed.test", second["ANTHROPIC_BASE_URL"])


class CredentialHygieneTests(ClientCase):
    """The key must never appear in anything a log or /stats could capture."""

    def test_describe_reports_presence_never_the_key(self):
        report = llm_client.describe()
        self.assertTrue(report["available"])
        self.assertTrue(report["has_key"])
        self.assertNotIn(FAKE_KEY, json.dumps(report, ensure_ascii=False))
        self.assertNotIn("api_key", report)

    def test_config_repr_and_str_mask_the_key(self):
        config = llm_client.load_config()
        self.assertNotIn(FAKE_KEY, repr(config))
        self.assertNotIn(FAKE_KEY, str(config))
        self.assertIn("***", repr(config))

    def test_failure_messages_are_redacted(self):
        breaker = llm_client.Breaker()
        breaker.record_failure("boom while using %s" % FAKE_KEY)
        self.assertNotIn(FAKE_KEY, breaker.last_error)
        self.assertIn("***", breaker.last_error)

    def test_breaker_status_carries_no_key(self):
        breaker = llm_client.Breaker()
        breaker.record_failure(FAKE_KEY)
        self.assertNotIn(FAKE_KEY, json.dumps(breaker.status()))

    def test_describe_is_safe_when_unconfigured(self):
        self.settings.unlink()
        llm_client.reset()
        report = llm_client.describe()
        self.assertFalse(report["available"])
        self.assertFalse(report["has_key"])
        self.assertIn("reason", report)


class CompleteTests(ClientCase):
    def test_successful_call_returns_the_text(self):
        opener, calls = self.fake_urlopen(message_body("可以"))
        with mock.patch("urllib.request.urlopen", opener):
            self.assertEqual("可以", llm_client.complete("hi"))
        self.assertEqual(1, len(calls))
        request, timeout = calls[0]
        self.assertEqual("https://llm.example.test/v1/messages", request.full_url)
        self.assertEqual(45.0, timeout)

    def test_request_carries_the_expected_headers_and_body(self):
        opener, calls = self.fake_urlopen(message_body("ok"))
        with mock.patch("urllib.request.urlopen", opener):
            llm_client.complete("问一句", max_tokens=128, model="other-model")
        request, _ = calls[0]
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(FAKE_KEY, headers["x-api-key"])
        self.assertEqual("Bearer " + FAKE_KEY, headers["authorization"])
        self.assertEqual("2023-06-01", headers["anthropic-version"])
        self.assertEqual("application/json", headers["content-type"])
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual("other-model", payload["model"])
        self.assertEqual(128, payload["max_tokens"])
        self.assertEqual([{"role": "user", "content": "问一句"}], payload["messages"])

    def test_configured_model_is_used_by_default(self):
        opener, calls = self.fake_urlopen(message_body("ok"))
        with mock.patch("urllib.request.urlopen", opener):
            llm_client.complete("hi")
        payload = json.loads(calls[0][0].data.decode("utf-8"))
        self.assertEqual("test-model-1", payload["model"])

    def test_multiple_text_blocks_are_concatenated(self):
        body = {"content": [{"type": "text", "text": "a"},
                            {"type": "tool_use", "id": "x"},
                            {"type": "text", "text": "b"}]}
        opener, _ = self.fake_urlopen(body)
        with mock.patch("urllib.request.urlopen", opener):
            self.assertEqual("ab", llm_client.complete("hi"))

    def test_empty_prompt_does_not_hit_the_network(self):
        opener, calls = self.fake_urlopen(message_body("ok"))
        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("   "))
        self.assertEqual([], calls)

    def test_unconfigured_client_returns_none_without_a_request(self):
        self.settings.unlink()
        llm_client.reset()
        opener, calls = self.fake_urlopen(message_body("ok"))
        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertEqual([], calls)

    def test_http_error_returns_none_and_does_not_raise(self):
        error = urllib.error.HTTPError("https://x", 404, "Not Found", {}, io.BytesIO(b'{"error":"no"}'))

        def opener(request, timeout=None):
            raise error

        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertIn("HTTP 404", llm_client.breaker().last_error)

    def test_url_error_returns_none(self):
        def opener(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertIn("URLError", llm_client.breaker().last_error)

    def test_timeout_returns_none(self):
        def opener(request, timeout=None):
            raise TimeoutError("timed out")

        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertIn("TimeoutError", llm_client.breaker().last_error)

    def test_broken_json_returns_none(self):
        opener, _ = self.fake_urlopen("not json at all")
        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))

    def test_empty_content_returns_none(self):
        for body in ({"content": []}, {"content": [{"type": "text", "text": "  "}]},
                     {"content": "nope"}, {}, {"content": [{"type": "thinking"}]}):
            opener, _ = self.fake_urlopen(body)
            with mock.patch("urllib.request.urlopen", opener):
                self.assertIsNone(llm_client.complete("hi"), body)

    def test_unexpected_transport_exception_is_contained(self):
        def opener(request, timeout=None):
            raise RuntimeError("socket exploded")

        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertIn("RuntimeError", llm_client.breaker().last_error)

    def test_parse_completion_accepts_bytes(self):
        raw = json.dumps(message_body("hi")).encode("utf-8")
        self.assertEqual("hi", llm_client.parse_completion(raw))


class RetryTests(ClientCase):
    """Transient transport failures are retried; real answers are not.

    Measured on this deployment: an identical prompt that answers in ~10s can
    also come back as RemoteDisconnected with no body, at roughly a 1-in-6 rate.
    Without a retry one dropped socket silently degrades an entire 8-item batch
    to the heuristic.
    """

    def setUp(self):
        super().setUp()
        # Never actually wait between attempts in a test.
        patcher = mock.patch.object(llm_client, "RETRY_BACKOFF", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_dropped_connection_is_retried_and_can_succeed(self):
        attempts = []

        def opener(request, timeout=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionResetError("Remote end closed connection")
            return Response(message_body("recovered"))

        with mock.patch("urllib.request.urlopen", opener):
            self.assertEqual("recovered", llm_client.complete("hi"))
        self.assertEqual(2, len(attempts))
        self.assertEqual(1, llm_client.breaker().successes)

    def test_a_persistent_connection_failure_stops_after_the_budget(self):
        attempts = []

        def opener(request, timeout=None):
            attempts.append(1)
            raise ConnectionResetError("down")

        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertEqual(llm_client.TRANSIENT_RETRIES + 1, len(attempts))
        self.assertEqual(1, llm_client.breaker().failures)

    def test_an_http_error_is_not_retried(self):
        """A status code is the server's answer, not a dropped socket."""
        attempts = []

        def opener(request, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError("https://x", 500, "Server Error", {},
                                         io.BytesIO(b'{"error":"boom"}'))

        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertEqual(1, len(attempts), "HTTP 错误不应重试")

    def test_a_bad_body_is_not_retried(self):
        attempts = []

        def opener(request, timeout=None):
            attempts.append(1)
            return Response("not json")

        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertEqual(1, len(attempts), "坏 JSON 不应重试")

    def test_cancellation_during_retry_stops_immediately(self):
        """Withdrawing during the retry window must not be charged to the endpoint."""
        attempts = []
        token = threading.Event()

        def opener(request, timeout=None):
            attempts.append(1)
            token.set()          # the caller withdraws while this attempt fails
            raise ConnectionResetError("down")

        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi", cancel=token.is_set))
        self.assertEqual(1, len(attempts), "取消后不应继续重试")
        self.assertEqual(0, llm_client.breaker().failures,
                         "取消不应被计为端点失败")


class BreakerTests(ClientCase):
    def test_two_failures_open_the_breaker(self):
        breaker = llm_client.Breaker(threshold=2, cooldown=600.0)
        self.assertTrue(breaker.allow())
        breaker.record_failure("boom")
        self.assertTrue(breaker.allow())
        breaker.record_failure("boom")
        self.assertFalse(breaker.allow())
        self.assertTrue(breaker.tripped)

    def test_success_closes_the_breaker(self):
        breaker = llm_client.Breaker(threshold=2, cooldown=600.0)
        breaker.record_failure("boom")
        breaker.record_failure("boom")
        self.assertTrue(breaker.tripped)
        breaker.record_success()
        self.assertFalse(breaker.tripped)

    def test_cooldown_expires_without_sleeping(self):
        now = [1000.0]
        breaker = llm_client.Breaker(threshold=1, cooldown=600.0,
                                     clock=lambda: now[0])
        breaker.record_failure("boom")
        self.assertTrue(breaker.tripped)
        now[0] += 599.0
        self.assertTrue(breaker.tripped)
        now[0] += 2.0
        self.assertFalse(breaker.tripped)

    def test_open_breaker_does_not_issue_a_request(self):
        opener, calls = self.fake_urlopen(message_body("ok"))
        llm_client.breaker().record_failure("boom")
        llm_client.breaker().record_failure("boom")
        self.assertTrue(llm_client.breaker().tripped)
        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertEqual([], calls, "冷却期内不得发起请求")

    def test_failures_then_success_resets_the_counter(self):
        breaker = llm_client.Breaker(threshold=2, cooldown=600.0)
        breaker.record_failure("boom")
        breaker.record_success()
        breaker.record_failure("boom")
        self.assertFalse(breaker.tripped, "成功一次应重置连续失败计数")

    def test_calls_are_counted(self):
        opener, _ = self.fake_urlopen(message_body("ok"))
        with mock.patch("urllib.request.urlopen", opener):
            llm_client.complete("a")
            llm_client.complete("b")
        self.assertEqual(2, llm_client.breaker().calls)
        self.assertEqual(2, llm_client.breaker().successes)


class CancellationTests(ClientCase):
    def test_pre_cancelled_token_skips_the_request(self):
        opener, calls = self.fake_urlopen(message_body("ok"))
        token = threading.Event()
        token.set()
        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi", cancel=token))
        self.assertEqual([], calls)

    def test_callable_token_is_honoured(self):
        opener, calls = self.fake_urlopen(message_body("ok"))
        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi", cancel=lambda: True))
        self.assertEqual([], calls)

    def test_cancelling_mid_flight_abandons_the_wait(self):
        """A request already in flight must be abandonable, not waited out."""
        token = threading.Event()
        started = threading.Event()
        release = threading.Event()

        def opener(request, timeout=None):
            started.set()
            # Hold the socket open long enough that the caller is genuinely
            # blocked inside the request when the token fires.
            release.wait(2.0)
            return Response(message_body("late"))

        timer = threading.Timer(0.1, token.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.addCleanup(release.set)
        try:
            with mock.patch("urllib.request.urlopen", opener):
                result = llm_client.complete("hi", cancel=token)
        finally:
            release.set()
        self.assertTrue(started.is_set(), "请求应已发出")
        self.assertIsNone(result)

    def test_a_cancellation_is_not_counted_as_an_endpoint_failure(self):
        token = threading.Event()
        token.set()
        llm_client.complete("hi", cancel=token)
        self.assertEqual(0, llm_client.breaker().failures)


class ResetTests(ClientCase):
    def test_reset_clears_cache_and_breaker(self):
        llm_client.breaker().record_failure("boom")
        llm_client.breaker().record_failure("boom")
        llm_client.reset()
        self.assertFalse(llm_client.breaker().tripped)
        self.assertEqual(0, llm_client.breaker().failures)
        self.assertTrue(llm_client.available())


if __name__ == "__main__":
    unittest.main()
