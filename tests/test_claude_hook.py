"""Claude Code UserPromptSubmit hook: push injection + matched control arm.

Two properties are load-bearing here:

1. **Fail closed and silent.**  A memory helper may never block, alter or
   corrupt a user's prompt.  Every operational failure returns no output.
2. **Both arms are traced.**  The control arm (injected=false) is the reason
   this hook exists at all - without it the injection-quality report can only
   ever produce an observational number with no counterpart.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import claude_hook, injection_ab, injection_trace
from hippomind.auto_context import AUTO_MAX_CHARS


def _payload(prompt: str = "请继续处理。记忆：检查 SQLite WAL 的并发事务",
             cwd: str = "/tmp/hippomind-agent") -> dict:
    return {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": cwd,
            "session_id": "s-1", "transcript_path": "/tmp/never-read.jsonl"}


class ClaudeHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-claude-hook-")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HIPPOMIND_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        for key in (injection_ab.ENV_SWITCH, "HIPPOMIND_INJECTION_AB_RATE",
                    "HIPPOMIND_INJECTION_TRACE"):
            os.environ.pop(key, None)

    def _traces(self):
        return injection_trace.recent(50)

    def test_parse_accepts_only_user_prompt_submit(self):
        parsed = claude_hook.parse_hook_input(json.dumps(_payload()).encode("utf-8"))
        self.assertIsNotNone(parsed)
        self.assertEqual("请继续处理。记忆：检查 SQLite WAL 的并发事务",
                         parsed["prompt"])
        self.assertIsNone(claude_hook.parse_hook_input(b"not-json"))
        self.assertIsNone(claude_hook.parse_hook_input(b""))
        self.assertIsNone(claude_hook.parse_hook_input(json.dumps(
            {"hook_event_name": "PreToolUse", "prompt": "x", "cwd": "/tmp"}
        ).encode("utf-8")))
        self.assertIsNone(claude_hook.parse_hook_input(json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "x"}
        ).encode("utf-8")), "缺少 cwd 必须拒绝，而不是猜")
        self.assertIsNone(claude_hook.parse_hook_input(b"{" + b"x" * (70 * 1024)))

    def test_treatment_returns_bounded_untrusted_context(self):
        pack = {"items": [{"memory_id": "m-1",
                           "evidence": [{"memory_id": "m-1"}]}],
                "text": "任务目标: 验证 WAL"}
        payload = claude_hook.parse_hook_input(json.dumps(_payload()).encode("utf-8"))
        with mock.patch.object(claude_hook, "fetch_auto_context", return_value=pack) as fetch:
            response = claude_hook.build_hook_response(payload, agent_config="/tmp/claude.json")
        self.assertIsNotNone(response)
        output = response["hookSpecificOutput"]
        self.assertEqual("UserPromptSubmit", output["hookEventName"])
        self.assertIn("<MEMORY_DATA_UNTRUSTED>", output["additionalContext"])
        self.assertIn("任务目标", output["additionalContext"])
        self.assertLessEqual(len(output["additionalContext"]), AUTO_MAX_CHARS)
        # The hook must ask for claude's own credential and its own agent name.
        self.assertEqual("claude", fetch.call_args.args[0].agent)
        self.assertEqual("/tmp/claude.json", fetch.call_args.args[0].agent_config)
        traces = self._traces()
        self.assertEqual(1, len(traces))
        self.assertTrue(traces[0]["injected"])
        self.assertEqual(["m-1"], traces[0]["memory_ids"])
        self.assertEqual("treatment", traces[0]["arm"])

    def test_control_arm_is_matched_and_recorded(self):
        """The control arm must retrieve, record, and deliver nothing.

        This is the load-bearing property of the whole experiment.  A control
        that skipped retrieval would have no memory ids, so the two arms could
        only be compared across unrelated prompts - which is exactly the
        observational comparison the A/B exists to replace.
        """
        os.environ[injection_ab.ENV_SWITCH] = "1"
        os.environ["HIPPOMIND_INJECTION_AB_RATE"] = "0.5"
        payload = claude_hook.parse_hook_input(json.dumps(_payload()).encode("utf-8"))
        pack = {"items": [{"memory_id": "m-1", "evidence": [{"memory_id": "m-1"}]}],
                "text": "任务目标: 验证 WAL"}
        # Force a control decision instead of hunting for a query that hashes
        # low: the hash is deliberately opaque, so relying on a lucky string
        # would make this test flaky by construction.
        forced = {"arm": injection_ab.CONTROL, "inject": False, "enabled": True,
                  "rate": 0.5, "day": "2026-10-25", "bucket": 0.01,
                  "reason": "ab_assign"}
        with mock.patch.object(injection_ab, "assign", return_value=forced), \
                mock.patch.object(claude_hook, "fetch_auto_context",
                                  return_value=pack) as fetch:
            response = claude_hook.build_hook_response(payload, agent_config="/tmp/claude.json")
        self.assertIsNone(response, "对照组不得把上下文交给 agent")
        fetch.assert_called_once()  # 对照组照样检索：否则没有可比对象
        traces = self._traces()
        self.assertEqual(1, len(traces))
        self.assertFalse(traces[0]["injected"])
        self.assertEqual("control", traces[0]["arm"])
        # 关键：对照组记录了"本会注入什么"，这才让它成为**匹配**对照。
        self.assertEqual(["m-1"], traces[0]["memory_ids"])
        self.assertEqual(0, traces[0]["items"])
        self.assertEqual(0, traces[0]["chars"])  # 正文没有交给 agent

    def test_trigger_gate_runs_before_the_experiment(self):
        """A prompt that would not be injected must not consume a control slot."""
        os.environ[injection_ab.ENV_SWITCH] = "1"
        payload = claude_hook.parse_hook_input(
            json.dumps(_payload(prompt="没有标记的普通要求")).encode("utf-8"))
        with mock.patch.object(injection_ab, "assign") as assign, \
                mock.patch.object(claude_hook, "fetch_auto_context") as fetch:
            response = claude_hook.build_hook_response(
                payload, agent_config="/tmp/claude.json", trigger_mode="anywhere")
        self.assertIsNone(response)
        assign.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual([], self._traces())

    def test_empty_pack_is_traced_as_a_treatment_observation(self):
        """Retrieval finding nothing is evidence, not a non-event.

        Filtering these out would silently bias the treatment arm toward
        successful lookups and make the refusal work (T3) invisible here.
        """
        payload = claude_hook.parse_hook_input(json.dumps(_payload()).encode("utf-8"))
        with mock.patch.object(claude_hook, "fetch_auto_context",
                               return_value={"items": [], "text": ""}):
            response = claude_hook.build_hook_response(payload, agent_config="/tmp/claude.json")
        self.assertIsNone(response)
        traces = self._traces()
        self.assertEqual(1, len(traces))
        self.assertTrue(traces[0]["injected"])
        self.assertEqual(0, traces[0]["items"])
        self.assertEqual("empty_pack", traces[0]["arm_reason"])

    def test_failures_are_silent(self):
        payload = claude_hook.parse_hook_input(json.dumps(_payload()).encode("utf-8"))
        with mock.patch.object(claude_hook, "fetch_auto_context",
                               side_effect=RuntimeError("offline")):
            self.assertIsNone(claude_hook.build_hook_response(
                payload, agent_config="/tmp/claude.json"))
        # A missing credential is *not* detected here: fetch_auto_context owns
        # that decision and fails closed on its own.  What this hook must
        # guarantee is that a failed lookup produces no output and no trace -
        # a fabricated "injected" row would poison the quality report.
        with mock.patch.object(claude_hook, "fetch_auto_context",
                               return_value={"items": [], "text": ""}):
            self.assertIsNone(claude_hook.build_hook_response(
                payload, agent_config="/tmp/missing.json"))
        self.assertIsNone(claude_hook.build_hook_response(
            {"hook_event_name": "Stop", "prompt": "x", "cwd": "/tmp"},
            agent_config="/tmp/claude.json"))

    def test_trace_never_contains_the_raw_query(self):
        pack = {"items": [{"memory_id": "m-1", "evidence": []}], "text": "t"}
        payload = claude_hook.parse_hook_input(json.dumps(
            _payload(prompt="记忆：秘密查询串 secret-token-xyz")).encode("utf-8"))
        with mock.patch.object(claude_hook, "fetch_auto_context", return_value=pack):
            claude_hook.build_hook_response(payload, agent_config="/tmp/claude.json")
        raw = Path(self.tmp.name, "injection.jsonl").read_text("utf-8")
        self.assertNotIn("secret-token-xyz", raw)
        self.assertIn("query_hash", raw)

    def test_main_writes_nothing_when_input_is_rejected(self):
        with mock.patch.object(sys, "stdin", mock.Mock(
                buffer=mock.Mock(read=lambda _n: b"garbage"))), \
                mock.patch.object(sys, "stdout", mock.Mock()) as out:
            self.assertEqual(0, claude_hook.main(["--agent-config", "/tmp/claude.json"]))
        out.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
