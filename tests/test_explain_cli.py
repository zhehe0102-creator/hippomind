"""Tests for the offline explain CLI (hippomind/cli/cmd_explain.py).

Every ledger lives in a temporary directory and --ledger is always explicit:
these tests never read or write the production ledger.  The trace cases use a
temporary HIPPOMIND_HOME so injection.jsonl is never the real one.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import injection_trace  # noqa: E402
from hippomind.cli import main  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402


def _record(content, evidence="ev-1", **extra):
    record = {
        "content": content,
        "kind": "fact",
        "scope": "shared",
        "source_agent": "tester",
        "evidence": [evidence],
    }
    record.update(extra)
    return record


DEFAULT_RECORDS = [
    _record("Orion 网关限流阈值是每分钟 1200 次请求", "ev:gateway"),
    _record("Orion 用户服务负责人是张伟", "ev:owner",
            task_id="orion-rollout", scope="shared",
            context={"progress": ["灰度阶段 10% 流量"]}),
]


class ExplainCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-explain-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.ledger_path = self.dir / "hub.sqlite"
        self.memory_ids = self.seed(DEFAULT_RECORDS)

    def seed(self, records):
        ledger = SQLiteLedger(self.ledger_path)
        try:
            return [result.memory_id for result in ledger.ingest_many(records)]
        finally:
            ledger.close()

    def run_cli(self, argv, *, env_extra=None):
        env = {"HIPPOMIND_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        if env_extra:
            env.update(env_extra)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv, env_extra=None):
        code, out, err = self.run_cli(
            ["explain", *argv, "--ledger", str(self.ledger_path), "--json"],
            env_extra=env_extra)
        return code, json.loads(out), err

    def record_trace(self, **kwargs):
        with mock.patch.dict(os.environ, {"HIPPOMIND_HOME": str(self.home)}):
            return injection_trace.record(**kwargs)

    def file_state(self):
        stat = self.ledger_path.stat()
        digest = hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()
        return stat.st_size, stat.st_mtime_ns, digest

    # -- happy path -----------------------------------------------------
    def test_hit_json_contains_candidates_selected_and_evidence(self):
        code, payload, err = self.run_json("Orion 网关限流阈值")
        self.assertEqual(0, code, err)
        self.assertEqual("Orion 网关限流阈值", payload["query"])
        self.assertTrue(payload["explained"])
        self.assertGreaterEqual(len(payload["candidates"]), 1)
        first = payload["candidates"][0]
        for key in ("memory_id", "kind", "project", "created_at", "score", "score_parts"):
            self.assertIn(key, first)
        self.assertTrue(first["evidence"])
        self.assertGreaterEqual(len(payload["selected"]), 1)
        selected = payload["selected"][0]
        self.assertIn("key", selected)
        self.assertTrue(selected["items"])
        self.assertIn("score_parts", selected)
        self.assertIn("latest", selected)
        self.assertEqual("", payload["empty_reason"])
        self.assertEqual(0.20, payload["gate"]["min_score"])
        self.assertGreaterEqual(payload["gate"]["passed_candidates"], 1)
        self.assertGreaterEqual(payload["latency_ms"], 0.0)
        self.assertEqual({"embedder", "vector", "entity"}, set(payload["components"]))
        self.assertEqual({"total", "reasons", "samples"}, set(payload["filtered"]))

    def test_latest_state_line_is_reported_for_task_memory(self):
        code, payload, err = self.run_json("Orion 用户服务负责人")
        self.assertEqual(0, code, err)
        self.assertGreaterEqual(len(payload["selected"]), 1)
        latest = [row["latest"] for row in payload["selected"] if row["latest"]]
        self.assertTrue(latest)
        self.assertIn("progress", latest[0]["field"])
        self.assertIn("灰度阶段", latest[0]["text"])

    def test_scope_filter_limits_candidates(self):
        other = self.dir / "scoped.sqlite"
        ledger = SQLiteLedger(other)
        try:
            shared_id = ledger.ingest(_record(
                "Orion 项目方案代号是 shared-only", "ev:shared")).memory_id
            agent_id = ledger.ingest(_record(
                "Orion 项目方案代号是 agent-only", "ev:agent",
                scope="agent:scout")).memory_id
        finally:
            ledger.close()
        code, payload, err = self.run_cli([
            "explain", "Orion 项目方案代号", "--ledger", str(other),
            "--scope", "agent:scout", "--json"])
        self.assertEqual(0, code, err)
        payload = json.loads(payload)
        self.assertEqual("agent:scout", payload["scope"])
        candidate_ids = {row["memory_id"] for row in payload["candidates"]}
        self.assertIn(agent_id, candidate_ids)
        self.assertNotIn(shared_id, candidate_ids)

    def test_task_filter_is_applied(self):
        code, payload, err = self.run_json("Orion", "--task", "orion-rollout")
        self.assertEqual(0, code, err)
        self.assertEqual("orion-rollout", payload["task_id"])
        for row in payload["candidates"]:
            self.assertEqual("orion-rollout", row["task_id"])

    def test_max_items_and_min_score_are_echoed_and_respected(self):
        code, payload, err = self.run_json(
            "Orion 网关限流阈值 负责人", "--max-items", "1", "--min-score", "0.05")
        self.assertEqual(0, code, err)
        self.assertEqual(0.05, payload["gate"]["min_score"])
        self.assertLessEqual(len(payload["selected"]), 1)

    def test_scope_project_task_and_session_are_echoed(self):
        code, payload, err = self.run_json(
            "Orion 用户服务负责人", "--scope", "shared", "--project", "orion",
            "--task", "orion-rollout", "--session-id", "sess-42")
        self.assertEqual(0, code, err)
        self.assertEqual("shared", payload["scope"])
        self.assertEqual("orion", payload["project"])
        self.assertEqual("orion-rollout", payload["task_id"])
        self.assertEqual("sess-42", payload["session_id"])

    def test_query_flag_and_positional_are_equivalent(self):
        code_a, payload_a, err_a = self.run_json("Orion 网关限流阈值")
        code_b, payload_b, err_b = self.run_json("--query", "Orion 网关限流阈值")
        self.assertEqual(0, code_a, err_a)
        self.assertEqual(0, code_b, err_b)
        self.assertEqual(payload_a["query"], payload_b["query"])

    # -- empty pack -----------------------------------------------------
    def test_empty_pack_exits_zero_with_reason(self):
        code, payload, err = self.run_json("火星基地的氧气配额是多少")
        self.assertEqual(0, code, err)
        self.assertEqual([], payload["selected"])
        self.assertTrue(payload["empty_reason"])
        self.assertEqual(0, payload["gate"]["passed_candidates"])
        self.assertTrue(payload["explained"])

    # -- read-only contract --------------------------------------------
    def test_explain_does_not_write_the_ledger(self):
        before = self.file_state()
        code, payload, err = self.run_json("Orion 网关限流阈值")
        self.assertEqual(0, code, err)
        self.assertGreaterEqual(len(payload["selected"]), 1)
        self.assertEqual(before, self.file_state(),
                         "explain 绝不能写账本（size/mtime/hash 必须不变）")

    # -- errors ---------------------------------------------------------
    def test_missing_ledger_exits_two(self):
        missing = self.dir / "missing.sqlite"
        code, out, err = self.run_cli([
            "explain", "查询", "--ledger", str(missing), "--json"])
        self.assertEqual(2, code)
        self.assertIn("账本不存在", err)
        self.assertFalse(missing.exists())
        self.assertEqual("", out)

    def test_missing_query_exits_two(self):
        code, out, err = self.run_cli(["explain", "--ledger", str(self.ledger_path)])
        self.assertEqual(2, code)
        self.assertIn("QUERY", err)
        self.assertEqual("", out)

    # -- trace ----------------------------------------------------------
    def test_trace_reads_temporary_file_and_filters_agent(self):
        self.record_trace(agent="codex", query="q1", context="ctx",
                          item_count=2, injected=True)
        self.record_trace(agent="claude", query="q2", item_count=0,
                          injected=False)
        code, payload, err = self.run_json("Orion 网关限流阈值", "--trace", "10")
        self.assertEqual(0, code, err)
        trace = payload["trace"]
        self.assertEqual(10, trace["requested"])
        self.assertEqual(2, trace["returned"])
        self.assertEqual(1, trace["injected"])
        self.assertEqual(1, trace["not_injected"])
        self.assertEqual({"codex", "claude"}, {row["agent"] for row in trace["records"]})
        self.assertTrue(all("injected" in row for row in trace["records"]))

        code, payload, err = self.run_json(
            "Orion 网关限流阈值", "--trace", "10", "--agent", "codex")
        self.assertEqual(0, code, err)
        self.assertEqual(1, payload["trace"]["returned"])
        self.assertEqual("codex", payload["trace"]["records"][0]["agent"])

    def test_trace_without_records_exits_zero(self):
        code, payload, err = self.run_json("Orion 网关限流阈值", "--trace", "5")
        self.assertEqual(0, code, err)
        self.assertEqual(0, payload["trace"]["returned"])
        self.assertEqual([], payload["trace"]["records"])

        code, out, err = self.run_cli(
            ["explain", "--trace", "5", "--ledger", str(self.ledger_path)])
        self.assertEqual(0, code, err)
        self.assertIn("无记录", out)

    def test_trace_only_mode_does_not_need_a_ledger(self):
        missing = self.dir / "missing-trace.sqlite"
        code, out, err = self.run_cli([
            "explain", "--trace", "3", "--ledger", str(missing), "--json"])
        self.assertEqual(0, code, err)
        payload = json.loads(out)
        self.assertFalse(payload["explained"])
        self.assertEqual(3, payload["trace"]["requested"])
        self.assertFalse(missing.exists())

    # -- fact cards -----------------------------------------------------
    def test_fact_cards_are_reported_when_enabled_by_env(self):
        card_ledger = self.dir / "cards.sqlite"
        ledger = SQLiteLedger(card_ledger)
        try:
            ledger.ingest(_record(
                "Orion 搜索集群分片数已调整为更高容量", "ev:cards",
                claims=[{"subject": "orion-search", "predicate": "config",
                         "slot_key": "shards", "value": 16, "confidence": 0.9}]))
        finally:
            ledger.close()
        code, payload, err = self.run_cli(
            ["explain", "Orion 搜索集群分片数", "--ledger", str(card_ledger),
             "--json"], env_extra={"HIPPOMIND_FACT_CARDS": "1"})
        self.assertEqual(0, code, err)
        payload = json.loads(payload)
        self.assertIsInstance(payload["fact_cards"], dict)
        self.assertIn("count", payload["fact_cards"])
        self.assertGreaterEqual(payload["fact_cards"]["count"], 1)

    # -- output shape ---------------------------------------------------
    def test_human_output_is_numbered_and_shared_with_json(self):
        code, out, err = self.run_cli([
            "explain", "Orion 网关限流阈值", "--ledger", str(self.ledger_path)])
        self.assertEqual(0, code, err)
        for marker in ("1. 查询", "2. 组件", "3. 候选", "4. 过滤",
                       "5. 选中", "6. 空包原因", "7. 门控", "8. 事实卡",
                       "9. 延迟"):
            self.assertIn(marker, out)
        self.assertNotIn("注入轨迹", out)

    def test_subprocess_cli_json_is_parseable(self):
        env = dict(os.environ)
        env.update({
            "HIPPOMIND_HOME": str(self.home),
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        proc = subprocess.run(
            [sys.executable, "-m", "hippomind", "explain",
             "Orion 网关限流阈值", "--ledger", str(self.ledger_path), "--json"],
            capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120)
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["explained"])
        self.assertGreaterEqual(len(payload["candidates"]), 1)


if __name__ == "__main__":
    unittest.main()
