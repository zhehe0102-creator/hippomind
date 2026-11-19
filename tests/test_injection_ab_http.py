"""HTTP /context-pack 的注入 A/B 接入回归（WP-K）。

锁定四件事：
  1. treatment 臂照常注入，trace 记 arm=treatment；
  2. control 臂返回空上下文包，但**仍然检索**并在 trace 记下 memory_ids 与
     empty_reason=ab_control，供两臂配对比较；
  3. 实验默认关闭时响应/trace 与既有行为完全一致（不新增 arm 字段）；
  4. /stats 窗口按 arm 分桶计数，report 脚本能读有界 trace 尾部产出 JSON。
"""

from __future__ import annotations

import json
import os
import subprocess
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

from hippomind import injection_ab, injection_trace  # noqa: E402
from hippomind.daemon import (  # noqa: E402
    MemoryHubHTTPServer,
    MemoryHubService,
    RuntimePaths,
)

CONTROL_DECISION = {"arm": injection_ab.CONTROL, "inject": False, "enabled": True,
                   "rate": 0.5, "day": "2026-11-13", "bucket": 0.1,
                   "reason": "ab_assign"}
TREATMENT_DECISION = {"arm": injection_ab.TREATMENT, "inject": True, "enabled": True,
                      "rate": 0.5, "day": "2026-11-13", "bucket": 0.9,
                      "reason": "ab_assign"}


class InjectionAbHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-ab-http-")
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HIPPOMIND_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(injection_ab.ENV_SWITCH, None)
        os.environ.pop("HIPPOMIND_INJECTION_AB_RATE", None)
        paths = RuntimePaths.for_ledger(Path(self.tmp.name) / "hub.sqlite",
                                       self.tmp.name)
        self.service = MemoryHubService(paths, watch=False)
        self.ledger_path = self.service.ledger.path
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
        self.service.begin_shutdown()

    def request(self, method, path, body=None, token=True):
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=raw, method=method)
        if raw is not None:
            req.add_header("Content-Type", "application/json")
        if token is True:
            req.add_header("X-HippoMind-Token", self.token)
        elif token:
            req.add_header("X-HippoMind-Token", token)
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def ingest_memory(self, content="Memory Hub A/B 注入观测"):
        result = self.request("POST", "/ingest", {
            "kind": "task", "content": content, "source_agent": "codex",
            "task_id": "ab-http", "project": "hippomind",
            "evidence": ["task:ab-http"],
            "context": {"goal": ["验证 HTTP 注入 A/B 分桶"]},
        })
        return result["results"][0]["memory_id"]

    def last_trace(self):
        rows = injection_trace.recent(5)
        self.assertTrue(rows)
        return rows[-1]

    def test_treatment_arm_injects_and_traces_arm(self):
        memory_id = self.ingest_memory()
        with mock.patch.object(injection_ab, "assign",
                               return_value=dict(TREATMENT_DECISION)):
            pack = self.request("POST", "/context-pack",
                                {"query": "Memory Hub A/B 注入观测"})
        self.assertGreaterEqual(len(pack["items"]), 1)
        self.assertTrue(pack["text"])
        self.assertEqual(injection_ab.TREATMENT, pack["injection_arm"])
        trace = self.last_trace()
        self.assertEqual(injection_ab.TREATMENT, trace["arm"])
        self.assertTrue(trace["injected"])
        self.assertIn(memory_id, trace["memory_ids"])
        window = self.request("GET", "/stats")["injection_trace"]
        self.assertEqual(1, window["arms"][injection_ab.TREATMENT])
        self.assertEqual(0, window["arms"][injection_ab.CONTROL])

    def test_control_arm_withholds_delivery_but_keeps_matched_trace(self):
        memory_id = self.ingest_memory()
        with mock.patch.object(injection_ab, "assign",
                               return_value=dict(CONTROL_DECISION)):
            pack = self.request("POST", "/context-pack",
                                {"query": "Memory Hub A/B 注入观测"})
        # 投递被扣留：空上下文包，但端点本身仍成功、开关仍为开启。
        self.assertEqual([], pack["items"])
        self.assertEqual("", pack["text"])
        self.assertTrue(pack["injection_enabled"])
        self.assertEqual(injection_ab.CONTROL, pack["injection_arm"])
        trace = self.last_trace()
        self.assertEqual(injection_ab.CONTROL, trace["arm"])
        self.assertFalse(trace["injected"])
        self.assertEqual("ab_control", trace["empty_reason"])
        self.assertEqual(0, trace["items"])
        self.assertEqual(0, trace["chars"])
        # 配对对照的关键：control 臂仍检索并记录 offered ids。
        self.assertIn(memory_id, trace["memory_ids"])
        self.assertGreaterEqual(trace["latency_ms"], 0.0)
        window = self.request("GET", "/stats")["injection_trace"]
        self.assertEqual(1, window["arms"][injection_ab.CONTROL])

    def test_get_context_pack_obeys_control_arm(self):
        self.ingest_memory()
        with mock.patch.object(injection_ab, "assign",
                               return_value=dict(CONTROL_DECISION)):
            pack = self.request(
                "GET", "/context-pack?query=" + quote("Memory Hub A/B 注入观测"))
        self.assertEqual([], pack["items"])
        self.assertEqual(injection_ab.CONTROL, self.last_trace()["arm"])

    def test_default_off_keeps_legacy_response_and_trace(self):
        memory_id = self.ingest_memory()
        pack = self.request("POST", "/context-pack",
                            {"query": "Memory Hub A/B 注入观测"})
        self.assertGreaterEqual(len(pack["items"]), 1)
        self.assertNotIn("injection_arm", pack)
        trace = self.last_trace()
        self.assertNotIn("arm", trace)
        self.assertTrue(trace["injected"])
        self.assertIn(memory_id, trace["memory_ids"])
        window = self.request("GET", "/stats")["injection_trace"]
        self.assertGreaterEqual(window["arms"]["unknown"], 1)
        self.assertEqual(0, window["arms"][injection_ab.CONTROL])

    def test_stats_arm_split_counts_both_arms(self):
        self.ingest_memory()
        decisions = [dict(CONTROL_DECISION), dict(TREATMENT_DECISION)]
        with mock.patch.object(injection_ab, "assign", side_effect=decisions):
            self.request("POST", "/context-pack",
                         {"query": "Memory Hub A/B 注入观测"})
            self.request("POST", "/context-pack",
                         {"query": "Memory Hub A/B 注入观测 2"})
        window = self.request("GET", "/stats")["injection_trace"]
        self.assertEqual(1, window["arms"][injection_ab.CONTROL])
        self.assertEqual(1, window["arms"][injection_ab.TREATMENT])

    def test_enabled_treatment_empty_pack_keeps_organic_reason(self):
        with mock.patch.object(injection_ab, "assign",
                               return_value=dict(TREATMENT_DECISION)):
            pack = self.request("POST", "/context-pack",
                                {"query": "submarine volcanology abyssal"})
        self.assertEqual([], pack["items"])
        trace = self.last_trace()
        self.assertEqual(injection_ab.TREATMENT, trace["arm"])
        self.assertFalse(trace["injected"])
        self.assertNotEqual("ab_control", trace["empty_reason"])

    def test_report_script_reads_bounded_trace_and_summarizes_arms(self):
        self.ingest_memory()
        decisions = [dict(CONTROL_DECISION), dict(TREATMENT_DECISION)]
        with mock.patch.object(injection_ab, "assign", side_effect=decisions):
            self.request("POST", "/context-pack",
                         {"query": "Memory Hub A/B 注入观测"})
            self.request("POST", "/context-pack",
                         {"query": "Memory Hub A/B 注入观测"})
        environment = dict(os.environ)
        environment["HIPPOMIND_HOME"] = self.tmp.name
        environment["PYTHONPATH"] = str(ROOT)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/injection_ab_report.py"),
             "--ledger", str(self.ledger_path), "--json", "--limit", "50"],
            capture_output=True, text=True, timeout=90, env=environment)
        self.assertEqual(0, completed.returncode, completed.stderr[-2000:])
        report = json.loads(completed.stdout)
        self.assertEqual(30, report["min_samples"])
        self.assertEqual(1, report["arms"][injection_ab.CONTROL]["traces"])
        self.assertEqual(1, report["arms"][injection_ab.TREATMENT]["traces"])
        self.assertEqual(0, report["arms"][injection_ab.CONTROL]["injected"])
        self.assertEqual(1, report["arms"][injection_ab.TREATMENT]["injected"])
        self.assertEqual("no_samples", report["conclusion"]["code"])
        self.assertEqual(30, report["needed"]["min_samples"])


if __name__ == "__main__":
    unittest.main()
