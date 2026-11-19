"""Injection traces are bounded and never contain the raw query."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import injection_trace
from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths


class InjectionTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-inject-")
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HIPPOMIND_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_record_and_summary(self):
        entry = injection_trace.record(agent="codex", query="secret query text",
                                       context="<MEMORY_DATA_UNTRUSTED>x</MEMORY_DATA_UNTRUSTED>",
                                       item_count=2, source="test")
        self.assertTrue(entry["injected"])
        self.assertEqual(entry["items"], 2)
        self.assertNotIn("secret query text", Path(self.tmp.name, "injection.jsonl").read_text("utf-8"))
        rows = injection_trace.recent(5)
        self.assertEqual(1, len(rows))
        self.assertEqual("codex", rows[0]["agent"])
        summary = injection_trace.summary()
        self.assertEqual(1, summary["count"])
        self.assertEqual(1, summary["injected"])


class InjectionTraceHardeningTests(unittest.TestCase):
    """WP-B：trace 文件的观测面加固（有界读取 / 0600 / 轮转）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-inject-harden-")
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HIPPOMIND_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _path(self) -> Path:
        return Path(self.tmp.name, "injection.jsonl")

    def test_trace_file_is_private(self):
        injection_trace.record(agent="codex", query="q", context="c",
                               item_count=1)
        path = self._path()
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        # 旧版本留下的过宽权限在下次 append 时被收紧。
        os.chmod(path, 0o644)
        injection_trace.record(agent="codex", query="q2", context="c",
                               item_count=1)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_recent_reads_only_a_bounded_tail(self):
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ancient = {"at": "2000-01-01T00:00:00+0000", "agent": "ancient",
                   "injected": True}
        filler = {"at": "2001-01-01T00:00:00+0000", "agent": "filler",
                  "injected": True, "pad": "y" * 128}
        filler_line = json.dumps(filler, separators=(",", ":")) + "\n"
        repeats = injection_trace.TAIL_READ_BYTES // len(filler_line) + 50
        tail_rows = [{"at": "2026-01-01T00:00:%02d+0000" % index,
                      "agent": "tail", "injected": index % 2 == 0}
                     for index in range(5)]
        with path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(ancient, ensure_ascii=False) + "\n")
            handle.write(filler_line * repeats)
            for row in tail_rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.assertGreater(path.stat().st_size, injection_trace.TAIL_READ_BYTES)

        # limit 远大于文件行数：全量读取会读到 ancient，尾部有界读取不会。
        agents = [row.get("agent") for row in injection_trace.recent(10_000)]
        self.assertNotIn("ancient", agents)
        self.assertIn("tail", agents)
        self.assertEqual([row["at"] for row in tail_rows],
                         [row["at"] for row in injection_trace.recent(5)])
        summary = injection_trace.summary()
        self.assertGreater(summary["count"], 0)
        self.assertLessEqual(summary["count"], 200)
        self.assertEqual("tail", summary["last_agent"])

    def test_rotation_at_size_bound(self):
        path = self._path()
        with mock.patch.object(injection_trace, "MAX_TRACE_BYTES", 1024):
            for index in range(30):
                injection_trace.record(agent="codex", query="q%d" % index,
                                       context="c" * 200, item_count=1)
        backup = path.with_name(path.name + ".1")
        self.assertTrue(backup.is_file(), "超过上限后必须轮转出 .1 备份")
        self.assertLess(path.stat().st_size, 2048,
                        "轮转后的当前文件必须保持有界")
        self.assertTrue(injection_trace.recent(5), "轮转后 recent 必须仍然可用")
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))


class DaemonInjectionTraceTests(unittest.TestCase):
    """WP-F：DSH/HTTP 路径统一注入 trace（含空结果对照组与窗口统计）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-inject-http-")
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HIPPOMIND_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        paths = RuntimePaths.for_ledger(Path(self.tmp.name) / "hub.sqlite",
                                       self.tmp.name)
        self.service = MemoryHubService(paths, watch=False)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        self.token = self.service.token
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, token=True):
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=raw, method=method)
        if raw is not None:
            request.add_header("Content-Type", "application/json")
        if token is True:
            request.add_header("X-HippoMind-Token", self.token)
        elif token:
            request.add_header("X-HippoMind-Token", token)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def trace_path(self):
        return Path(os.environ["HIPPOMIND_HOME"], "injection.jsonl")

    def ingest_memory(self, content="Memory Hub 注入观测", **overrides):
        record = {
            "kind": "task", "content": content, "source_agent": "codex",
            "task_id": "observability", "project": "hippomind",
            "evidence": ["task:observability"],
            "context": {"goal": ["让 DSH 注入有统一 trace"]},
        }
        record.update(overrides)
        result = self.request("POST", "/ingest", record)
        return result["results"][0]["memory_id"]

    def test_post_injection_records_injected_trace_with_memory_ids(self):
        memory_id = self.ingest_memory()
        pack = self.request("POST", "/context-pack", {"query": "Memory Hub 注入观测"})
        self.assertGreaterEqual(len(pack["items"]), 1)
        self.assertNotIn("debug", pack)
        rows = injection_trace.recent(5)
        self.assertTrue(rows)
        last = rows[-1]
        self.assertTrue(last["injected"])
        self.assertEqual("http", last["source"])
        # 管理员令牌未声明 agent 字段：按契约回落到 "http"。
        self.assertEqual("http", last["agent"])
        self.assertEqual(len(pack["items"]), last["items"])
        self.assertEqual(len(pack["text"]), last["chars"])
        self.assertIn(memory_id, last["memory_ids"])
        self.assertGreaterEqual(last["latency_ms"], 0.0)
        self.assertEqual("shared", last["scope"])
        self.assertIn("query_hash", last)
        self.assertNotIn("query", last)

    def test_empty_result_records_control_trace(self):
        pack = self.request("POST", "/context-pack",
                            {"query": "submarine volcanology abyssal"})
        self.assertEqual([], pack["items"])
        last = injection_trace.recent(5)[-1]
        self.assertFalse(last["injected"])
        self.assertEqual(0, last["items"])
        self.assertEqual(0, last["chars"])
        self.assertTrue(last["empty_reason"])
        # debug 请求里服务端 empty_reason 应优先于 candidates=0 兜底。
        self.request("POST", "/context-pack",
                     {"query": "submarine volcanology abyssal", "debug": True})
        debug_last = injection_trace.recent(5)[-1]
        self.assertFalse(debug_last["injected"])
        self.assertIn("候选", debug_last["empty_reason"])

    def test_injection_disabled_records_empty_control_trace(self):
        self.service.injection.update("off", "pi")
        disabled = self.request("POST", "/context-pack",
                                {"agent": "pi", "query": "Memory Hub"})
        self.assertEqual([], disabled["items"])
        self.assertFalse(disabled["injection_enabled"])
        last = injection_trace.recent(5)[-1]
        self.assertFalse(last["injected"])
        self.assertEqual("injection_disabled", last["empty_reason"])
        self.assertEqual("pi", last["agent"])

    def test_unauthenticated_request_does_not_write_trace(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("POST", "/context-pack", {"query": "unauthorized"},
                         token=False)
        self.assertEqual(401, caught.exception.code)
        self.assertEqual([], injection_trace.recent(10))
        self.assertFalse(self.trace_path().exists())

    def test_trace_never_contains_query_or_memory_text(self):
        secret_query = "SECRET-QUERY-MARKER-a1b2c3"
        secret_body = "SECRET-MEMORY-BODY-d4e5f6"
        self.ingest_memory(content=secret_body + " 注入观测")
        self.request("POST", "/context-pack", {"query": secret_query, "debug": True})
        raw = self.trace_path().read_text("utf-8")
        self.assertNotIn(secret_query, raw)
        self.assertNotIn(secret_body, raw)
        rows = injection_trace.recent(5)
        self.assertTrue(rows)
        self.assertEqual(16, len(rows[-1]["query_hash"]))
        self.assertNotEqual(secret_query, rows[-1]["query_hash"])

    def test_debug_response_does_not_erase_cached_trace_metadata(self):
        memory_id = self.ingest_memory()
        debug_pack = self.request("POST", "/context-pack",
                                  {"query": "Memory Hub 注入观测", "debug": True})
        self.assertIn("debug", debug_pack)
        plain_pack = self.request("POST", "/context-pack",
                                  {"query": "Memory Hub 注入观测"})
        self.assertNotIn("debug", plain_pack)
        again = self.request("POST", "/context-pack",
                             {"query": "Memory Hub 注入观测", "debug": True})
        self.assertIn("debug", again, "缓存包不能被前一次响应剥离 debug")
        rows = injection_trace.recent(5)
        self.assertTrue(all(row["injected"] for row in rows))
        self.assertTrue(all(memory_id in row["memory_ids"] for row in rows))

    def test_stats_exposes_bounded_trace_window(self):
        self.ingest_memory()
        self.request("POST", "/context-pack", {"query": "Memory Hub 注入观测"})
        self.request("POST", "/context-pack",
                     {"query": "submarine volcanology abyssal"})
        stats = self.request("GET", "/stats")
        window = stats["injection_trace"]
        self.assertEqual(2, window["count"])
        self.assertEqual(1, window["injected"])
        self.assertEqual(1, window["empty"])
        self.assertEqual(0, window["errors"])
        self.assertLessEqual(len(window["recent"]), 10)
        self.assertFalse(window["recent"][-1]["injected"])
        self.assertTrue(window["recent"][-1]["empty_reason"])
        # POST /stats 走同一个 hub.stats(policy)，窗口字段一致。
        post_stats = self.request("POST", "/stats", {})
        self.assertEqual(2, post_stats["injection_trace"]["count"])

    def test_get_context_pack_shares_the_same_trace_path(self):
        memory_id = self.ingest_memory()
        pack = self.request("GET", "/context-pack?query=" + quote("Memory Hub 注入观测"))
        self.assertGreaterEqual(len(pack["items"]), 1)
        last = injection_trace.recent(5)[-1]
        self.assertTrue(last["injected"])
        self.assertIn(memory_id, last["memory_ids"])
        empty = self.request("GET", "/context-pack?query=" + quote("submarine volcanology"))
        self.assertEqual([], empty["items"])
        self.assertFalse(injection_trace.recent(5)[-1]["injected"])

    def test_build_error_is_traced_and_counted(self):
        with mock.patch.object(self.service.contexts, "build",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("POST", "/context-pack", {"query": "Memory Hub"})
        self.assertEqual(500, caught.exception.code)
        last = injection_trace.recent(5)[-1]
        self.assertFalse(last["injected"])
        self.assertEqual("error", last["empty_reason"])
        self.assertEqual("RuntimeError", last["error"])
        stats = self.request("GET", "/stats")
        self.assertGreaterEqual(stats["injection_trace"]["errors"], 1)

    def test_scoped_stats_restricts_recent_trace_rows(self):
        self.ingest_memory()
        self.request("POST", "/context-pack", {"query": "Memory Hub 注入观测"})
        _credential, pi_token = self.service.ledger.issue_agent_credential(
            "pi", scopes=["shared", "agent:pi"], permissions=["read", "stats"])
        stats = self.request("GET", "/stats", token=pi_token)
        window = stats["injection_trace"]
        self.assertTrue(window.get("restricted"))
        self.assertTrue(all(row.get("agent") == "pi" for row in window["recent"]))


if __name__ == "__main__":
    unittest.main()
