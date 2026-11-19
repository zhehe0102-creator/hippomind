"""检索通道区块（pulse 视图）与 /api/v1/injection-ab 的 A/B 进度端点。

三块内容里有两块的契约落在这个文件：

  1. 检索通道状态**不新增端点**——数据已经在 /api/v1/overview 的 daemon_health
     和 /api/v1/health 的 fts 检查项里。这里钉住的是「不新增端点」和「已关闭 ≠
     不可用」这条语义：entity_index 默认 {"state":"disabled"} 是一个可开启的功能，
     不是一个坏掉的东西，界面必须能分开。

  2. A/B 进度端点的两臂计数必须来自**快速路径**（trace 尾部计数，实测 3.8 ms），
     而不是 injection_quality.evaluate()（实测 3.4–6.6 s）。这个区别是端点存在的
     全部理由，所以测试直接钉住「进度路径不碰 evaluate」。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.gui_api import (  # noqa: E402
    ENDPOINTS, INJECTION_AB_TAIL_BYTES, GuiApi, _ratio,
)
from hippomind.injection_quality import MIN_SAMPLES  # noqa: E402

NODE = shutil.which("node")
WEB = ROOT / "hippomind" / "web"
PULSE_PATH = WEB / "js" / "views" / "pulse.js"


def _api_for(path) -> GuiApi:
    return GuiApi(SimpleNamespace(ledger=SimpleNamespace(path=Path(path),
                                                       generation=0)))


def _trace(agent="claude", injected=True, at="2026-10-25T10:00:00-0700",
           items=8, memory_ids=("mem_a",)):
    return {"at": at, "agent": agent, "source": "claude-hook",
            "query_hash": "abc", "query_chars": 10, "items": items,
            "chars": 800, "injected": injected, "memory_ids": list(memory_ids)}


class InjectionAbEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-r4-ab-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.trace = self.root / "injection.jsonl"
        self.ledger = self.root / "hub.sqlite"
        self.ledger.write_bytes(b"")  # never opened by the progress path
        self.api = _api_for(self.ledger)
        patcher = mock.patch("hippomind.injection_trace.trace_path",
                             return_value=self.trace)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, rows):
        self.trace.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8")

    # -- shape ---------------------------------------------------------

    def test_endpoint_is_registered_in_the_catalogue(self):
        paths = {item["path"] for item in ENDPOINTS}
        self.assertIn("/api/v1/injection-ab", paths)
        entry = [i for i in ENDPOINTS if i["path"] == "/api/v1/injection-ab"][0]
        self.assertEqual("GET", entry["method"])
        self.assertEqual("admin", entry["auth"])

    def test_progress_payload_shape(self):
        self._write([_trace(injected=True)] * 5 + [_trace(injected=False)] * 3)
        body = self.api.injection_ab({})
        self.assertEqual({"config", "min_samples", "traces", "progress",
                          "arms", "evaluable", "verdict", "note"}, set(body))
        self.assertEqual(5, body["traces"]["treatment"])
        self.assertEqual(3, body["traces"]["control"])
        self.assertEqual(8, body["traces"]["sampled_upper_bound"])
        self.assertEqual(MIN_SAMPLES, body["min_samples"])
        self.assertEqual(MIN_SAMPLES, body["progress"]["treatment"]["need"])

    def test_progress_does_not_compute_metrics(self):
        """The whole point of the endpoint: progress is cheap, metrics are not."""
        self._write([_trace()])
        body = self.api.injection_ab({})
        self.assertIsNone(body["arms"])
        self.assertIsNone(body["evaluable"],
                          "没有算指标却报告了可评估样本数：那是编的")
        self.assertIsNone(body["verdict"])

    def test_progress_never_calls_injection_quality(self):
        """A regression guard: evaluate() costs seconds, progress must not."""
        self._write([_trace()])
        with mock.patch("hippomind.injection_quality.evaluate",
                        side_effect=AssertionError("evaluate must not run")):
            body = self.api.injection_ab({})
        self.assertEqual(1, body["traces"]["treatment"])

    def test_missing_trace_file_is_an_empty_progress_not_an_error(self):
        body = self.api.injection_ab({})
        self.assertEqual(0, body["traces"]["read"])
        self.assertEqual(0, body["progress"]["treatment"]["have"])
        self.assertEqual(MIN_SAMPLES, body["progress"]["control"]["shortfall"])

    # -- the binding arm -----------------------------------------------

    def test_control_arm_is_the_binding_constraint_at_default_rate(self):
        """At 20% control, treatment fills first; the panel must say so."""
        self._write([_trace(injected=True)] * 40 + [_trace(injected=False)] * 4)
        progress = self.api.injection_ab({})["progress"]
        self.assertEqual("control", progress["binding_arm"])
        self.assertEqual(MIN_SAMPLES - 4, progress["binding_shortfall"])
        self.assertEqual(0, progress["treatment"]["shortfall"])

    def test_both_arms_met_reports_no_shortfall(self):
        self._write([_trace(injected=True)] * 30 + [_trace(injected=False)] * 30)
        progress = self.api.injection_ab({})["progress"]
        self.assertEqual(0, progress["binding_shortfall"])
        self.assertEqual(1.0, progress["treatment"]["ratio"])
        self.assertEqual(1.0, progress["control"]["ratio"])

    def test_ratio_is_clamped(self):
        self.assertEqual(1.0, _ratio(99, 30))
        self.assertEqual(0.0, _ratio(5, 0))
        self.assertEqual(0.0, _ratio(None, 30))
        self.assertEqual(0.5, _ratio(15, 30))

    # -- robustness ----------------------------------------------------

    def test_malformed_lines_are_counted_not_dropped_silently(self):
        self.trace.write_text(
            json.dumps(_trace(injected=True)) + "\n"
            + "{not json\n"
            + "[1,2,3]\n"
            + json.dumps(_trace(injected=False)) + "\n",
            encoding="utf-8")
        body = self.api.injection_ab({})
        self.assertEqual(2, body["traces"]["read"])
        self.assertEqual(2, body["traces"]["malformed"])
        self.assertEqual(1, body["traces"]["treatment"])
        self.assertEqual(1, body["traces"]["control"])

    def test_tail_read_is_bounded_and_flagged(self):
        """An append-only file with no rotation must not become a per-request cost."""
        filler = json.dumps(_trace(injected=True, memory_ids=[])) + "\n"
        # Build a file that is genuinely over the tail limit, then append one
        # control trace as the newest line.  The tail read must still find it:
        # a boundary that eats the newest line would hide the newest evidence.
        repeats = INJECTION_AB_TAIL_BYTES // len(filler) + 5
        self.trace.write_text(filler * repeats
                              + json.dumps(_trace(injected=False)) + "\n",
                              encoding="utf-8")
        self.assertGreater(self.trace.stat().st_size, INJECTION_AB_TAIL_BYTES)
        body = self.api.injection_ab({})
        self.assertTrue(body["traces"]["truncated"],
                        "超过 tail 上界却没标记 truncated：读了多少就报多少")
        self.assertEqual(1, body["traces"]["control"],
                         "最后一行必须被读到（tail 边界不能吃掉最新一条）")
        self.assertLess(body["traces"]["read"], repeats,
                        "标记了 truncated 却读了全量：tail 上界没生效")

    def test_agents_are_listed(self):
        self._write([_trace(agent="claude"), _trace(agent="codex"),
                     _trace(agent="claude")])
        self.assertEqual(["claude", "codex"], self.api.injection_ab({})["traces"]["agents"])

    def test_config_reflects_the_ab_switch(self):
        self._write([_trace()])
        with mock.patch("hippomind.injection_ab.enabled", return_value=False):
            body = self.api.injection_ab({})
        self.assertFalse(body["config"]["enabled"])

    # -- opt-in metrics ------------------------------------------------

    def test_metrics_flag_is_opt_in_and_failure_is_contained(self):
        """A broken metrics path must not take the progress numbers down."""
        self._write([_trace()])
        with mock.patch("hippomind.injection_quality.evaluate",
                        side_effect=RuntimeError("boom")):
            body = self.api.injection_ab({"metrics": ["1"]})
        self.assertIn("metrics_error", body)
        self.assertIn("boom", body["metrics_error"])
        self.assertEqual(1, body["traces"]["treatment"],
                         "指标失败不能把进度数字一起清空")


if __name__ == "__main__":
    unittest.main()
