"""WP-M：会话感知 follow-up 查询消歧（followup anchoring）回归测试。

覆盖四层：

1. 单元：延续词/短句/普通查询判定；content_terms 口径；全局开关；
2. 锚点查询：时间窗、纯延续跳过、session 隔离、跨 scope 隔离、文本上限；
3. 端到端（ContextPackBuilder）：开启锚定后「继续」能召回上一任务；关闭时
   为空；普通查询开/关逐字节相等；有内容词的短查询守卫生效；per-request
   关闭；缓存按会话/锚点分键；
4. 透传：daemon options、CLI 参数、hook payload session_id、DSH 插件契约。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from hippomind import query_context as qc  # noqa: E402
from hippomind.context_pack import ContextPackBuilder, ContextPackOptions  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402


def _canonical(pack):
    """比较 Context Pack 时忽略 dict 顺序，但保留每个字段。"""
    return json.dumps(pack, sort_keys=True, ensure_ascii=False)


def _options(**overrides):
    base = {"max_items": 4, "min_score": 0.0, "debug": True}
    base.update(overrides)
    return ContextPackOptions(**base)


class FollowupDetectionTests(unittest.TestCase):
    """判定层：强延续模式 / 短句安全阈值 / content_terms。"""

    def test_continuation_patterns_are_recognized(self):
        for text in ("继续", "接着", "同上", "刚才", "这个", "往下", "下一步",
                     "继续优化", "再优化", "好的", "嗯嗯", "go on", "continue",
                     "keep going", "same as above", "再优化 GUI", "继续跑测试"):
            self.assertTrue(qc.is_followup(text), text)

    def test_normal_requests_are_not_followups(self):
        # Lead 明确的安全线：有 1 个内容词的短请求是新的独立请求，不得锚定。
        for text in ("跑测试", "部署", "回滚", "再次部署", "优化 GUI 的 recall 视图",
                     "数据库迁移的步骤是什么", "今天天气怎么样"):
            self.assertFalse(qc.is_followup(text), text)

    def test_empty_and_punctuation_queries_do_not_anchor(self):
        for text in ("", "   ", "？", "..."):
            self.assertFalse(qc.is_followup(text), repr(text))
        self.assertTrue(qc.is_pure_continuation("继续"))

    def test_long_acknowledgement_is_not_a_followup(self):
        # 纯符号/纯 ack 但超过短句阈值：宁可不救，也不误伤。
        self.assertFalse(qc.is_followup("好的" * 9))
        self.assertFalse(qc.is_followup("ok " * 12))

    def test_content_terms_keep_the_verb_under_continuation(self):
        # 「再优化 GUI」必须保留 {优化, gui}（>=2 才触发 original-overlap 守卫）
        self.assertEqual(["gui", "优化"], qc.content_terms("再优化 GUI"))
        self.assertEqual([], qc.content_terms("继续"))
        self.assertEqual([], qc.content_terms("同上"))
        self.assertIn("测试", qc.content_terms("跑测试"))
        self.assertIn("部署", qc.content_terms("部署"))

    def test_global_switch_off_values(self):
        for value in ("0", "off", "false", "no", "disabled", " OFF "):
            with mock.patch.dict(os.environ, {"HIPPOMIND_QUERY_CONTEXT": value}):
                self.assertFalse(qc.enabled(), value)
        with mock.patch.dict(os.environ, {"HIPPOMIND_QUERY_CONTEXT": "1"}):
            self.assertTrue(qc.enabled())
        with mock.patch.dict(os.environ, {"HIPPOMIND_QUERY_CONTEXT": "garbage"}):
            self.assertTrue(qc.enabled(), "非法值必须按开启处理，关闭要显式")


class _LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-qctx-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.env = mock.patch.dict(os.environ, {"HIPPOMIND_QUERY_CONTEXT": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.builder = ContextPackBuilder(self.ledger)

    # -- fixtures ---------------------------------------------------------
    def raw(self, content, *, session_id="s-1", scope="shared",
            agent="claude", timestamp="", event_id=""):
        record = {
            "kind": "raw_chat", "raw_chat": True, "role": "user",
            "scope": scope, "source_agent": agent, "content": content,
            "evidence": ["raw:" + content[:16]], "session_id": session_id,
        }
        if timestamp:
            record["timestamp"] = timestamp
        if event_id:
            record["event_id"] = event_id
        return self.ledger.ingest(record)

    def memory(self, content, *, kind="task", task_id="", metadata=None,
               event_id=""):
        record = {
            "kind": kind, "scope": "shared", "source_agent": "tester",
            "content": content, "evidence": ["mem:" + content[:16]],
        }
        if task_id:
            record["task_id"] = task_id
        if metadata:
            record["metadata"] = metadata
        if event_id:
            record["event_id"] = event_id
        return self.ledger.ingest(record)

    def anchor(self, query="继续", **kwargs):
        kwargs.setdefault("scope", "shared")
        return qc.anchor_context(self.ledger, query, **kwargs)


class AnchorContextTests(_LedgerCase):
    """锚点查询层（只读、有界、可隔离）。"""

    def test_anchor_comes_from_recent_real_user_messages(self):
        source = self.raw("优化 GUI 的 recall 视图")
        self.raw("继续", event_id="raw-cont")  # 纯延续不得成为锚点文本
        anchor = self.anchor()
        self.assertIsNotNone(anchor)
        self.assertIn("优化 GUI 的 recall 视图", anchor["anchor_text"])
        self.assertNotEqual("", anchor["query"])
        self.assertTrue(anchor["query"].endswith("继续"))
        self.assertEqual([source.memory_id], anchor["source_memory_ids"])
        self.assertEqual("s-1", anchor["source_session_id"])
        self.assertGreaterEqual(anchor["age_minutes"], 0.0)

    def test_only_continuation_messages_yield_no_anchor(self):
        self.raw("继续")
        self.raw("好的", event_id="raw-good")
        self.assertIsNone(self.anchor())

    def test_time_window_excludes_old_messages(self):
        old_stamp = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        self.raw("优化 GUI 的 recall 视图", timestamp=old_stamp)
        self.assertIsNone(self.anchor(), "4 小时前的消息超出 180 分钟窗口")
        old_time = datetime.fromisoformat(old_stamp)
        inside = self.anchor(now=old_time + timedelta(minutes=30))
        self.assertIsNotNone(inside, "窗口内的同一消息必须可用")

    def test_session_isolation_picks_only_that_session(self):
        self.raw("优化 GUI 的 recall 视图", session_id="s-gui")
        db = self.raw("数据库端口改成 5432", session_id="s-db")
        anchor = self.anchor(session_id="s-db")
        self.assertIsNotNone(anchor)
        self.assertIn("数据库端口改成 5432", anchor["anchor_text"])
        self.assertNotIn("GUI", anchor["anchor_text"])
        self.assertEqual("s-db", anchor["source_session_id"])
        self.assertEqual([db.memory_id], anchor["source_memory_ids"])
        gui_anchor = self.anchor(session_id="s-gui")
        self.assertIn("GUI", gui_anchor["anchor_text"])

    def test_scope_isolates_agents_without_session(self):
        self.raw("优化 GUI 的 recall 视图", scope="agent:claude", session_id="s-a")
        self.assertIsNone(self.anchor(scope="shared"),
                          "跨 scope 的会话消息不得跨 agent 混入")
        shared = self.raw("数据库端口改成 5432", scope="shared", agent="codex")
        anchor = self.anchor(scope="shared")
        self.assertEqual([shared.memory_id], anchor["source_memory_ids"])

    def test_anchor_uses_at_most_two_messages_and_caps_text(self):
        long_text = "优化 GUI 的 recall 视图 " + ("细节" * 60)
        first = self.raw(long_text, event_id="raw-long")
        second = self.raw("分组预算改到 64 条", event_id="raw-second")
        third = self.raw("继续", event_id="raw-third")
        anchor = self.anchor()
        self.assertLessEqual(len(anchor["anchor_text"]), qc.ANCHOR_TEXT_MAX_CHARS)
        self.assertEqual([first.memory_id, second.memory_id],
                         anchor["source_memory_ids"],
                         "最多两条且必须时间正序")
        self.assertIn("优化 GUI", anchor["anchor_text"])
        self.assertIn("分组预算改到 64 条", anchor["anchor_text"])
        self.assertNotIn(third.memory_id, anchor["source_memory_ids"])

    def test_anchor_text_never_exceeds_the_cap(self):
        self.raw("优化 GUI 的 recall 视图 " + ("细节" * 500), event_id="raw-cap")
        anchor = self.anchor()
        self.assertEqual(qc.ANCHOR_TEXT_MAX_CHARS,
                         len(anchor["anchor_text"]),
                         "超长锚点文本必须被截到硬上限")

    def test_continuation_between_real_messages_is_skipped(self):
        first = self.raw("优化 GUI 的 recall 视图", event_id="raw-1")
        self.raw("继续", event_id="raw-2")
        second = self.raw("分组预算改到 64 条", event_id="raw-3")
        anchor = self.anchor()
        self.assertEqual([first.memory_id, second.memory_id],
                         anchor["source_memory_ids"])
        self.assertNotIn("继续", anchor["anchor_text"])


class ContextPackFollowupTests(_LedgerCase):
    """ContextPackBuilder 端到端行为与守卫。"""

    def setUp(self):
        super().setUp()
        self.gui = self.memory(
            "GUI recall 视图优化任务：重排分组预算", task_id="gui-recall",
            metadata={"context": {"goal": ["优化 GUI 的 recall 视图"]}})
        self.gui_progress = self.memory(
            "recall 视图优化进度：分组预算已调整", kind="progress",
            task_id="gui-recall")
        self.db = self.memory(
            "数据库端口 5432 迁移任务：回滚预案就绪", task_id="db-5432",
            metadata={"context": {"goal": ["数据库端口改成 5432"]}})
        self.raw_gui = self.raw("优化 GUI 的 recall 视图", session_id="s-gui")
        self.raw_db = self.raw("数据库端口改成 5432", session_id="s-db",
                               event_id="raw-db")

    def test_followup_recalls_prior_task_and_off_is_empty(self):
        anchored = self.builder.build("继续", _options(session_id="s-gui"))
        self.assertTrue(anchored["items"], "开启锚定后「继续」必须召回上一任务")
        self.assertIn("GUI", anchored["text"])
        debug = anchored["debug"]["query_context"]
        self.assertTrue(debug["applied"])
        self.assertEqual("followup", debug["reason"])
        self.assertEqual([self.raw_gui.memory_id], debug["source_memory_ids"])
        self.assertGreater(debug["anchor_chars"], 0)

        off = self.builder.build("继续", _options(query_context=False))
        self.assertEqual([], off["items"], "关闭锚定后「继续」没有可检索内容")
        self.assertNotIn("query_context", off.get("debug", {}))

    def test_normal_query_pack_is_identical_with_switch_on_or_off(self):
        on = self.builder.build("数据库端口配置", _options())
        off = self.builder.build("数据库端口配置", _options(query_context=False))
        self.assertEqual(_canonical(on), _canonical(off),
                         "普通查询在锚定开关开/关下 JSON（含 debug）必须完全相等")
        self.assertNotIn("query_context", on["debug"])
        selected_ids = [memory_id for entry in on["debug"]["selected"]
                        for memory_id in entry["memory_ids"]]
        self.assertIn(self.db.memory_id, selected_ids)

    def test_short_request_with_content_terms_is_not_anchored(self):
        # Lead 安全测试 1：「跑测试」+ GUI 前文 -> 不触发锚定，与关闭时一致。
        on = self.builder.build("跑测试", _options())
        off = self.builder.build("跑测试", _options(query_context=False))
        self.assertEqual(_canonical(on), _canonical(off),
                         "有内容词的短请求被误判成 follow-up")
        self.assertNotIn("query_context", on["debug"])

    def test_session_isolation_does_not_mix_sessions(self):
        # Lead 安全测试 2：A 会话 GUI、B 会话数据库，当前 session_id=B 只锚 B。
        from_b = self.builder.build("继续", _options(session_id="s-db"))
        self.assertIn("数据库端口", from_b["text"])
        self.assertNotIn("GUI", from_b["text"],
                         "B 会话的锚点混入了 A 会话记忆")
        from_a = self.builder.build("继续", _options(session_id="s-gui"))
        self.assertIn("GUI", from_a["text"])
        self.assertNotIn("数据库端口", from_a["text"])

    def test_guard_drops_unrelated_anchor_and_falls_back(self):
        self.ledger2 = SQLiteLedger(Path(self.tmp.name) / "guard.sqlite")
        self.addCleanup(self.ledger2.close)
        self.ledger2.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "数据库端口 5432 已配置", "evidence": ["mem:db-guard"]})
        self.ledger2.ingest({
            "kind": "raw_chat", "raw_chat": True, "role": "user",
            "scope": "shared", "source_agent": "claude", "session_id": "s-g",
            "content": "数据库端口改成 5432", "evidence": ["raw:g"]})
        builder = ContextPackBuilder(self.ledger2)
        guarded = builder.build("再优化 GUI", _options())
        self.assertNotIn("数据库端口", guarded["text"],
                         "无关锚点把数据库记忆注入了 GUI 请求")
        debug = guarded["debug"]["query_context"]
        self.assertFalse(debug["applied"])
        self.assertEqual("guard_empty_fallback", debug["reason"])
        self.assertGreaterEqual(debug["guard_dropped"], 1)

    def test_guard_keeps_overlapping_candidates(self):
        guarded = self.builder.build("再优化 GUI", _options())
        self.assertIn("GUI", guarded["text"],
                      "命中原查询词的相关候选必须保留")
        self.assertNotIn("数据库端口", guarded["text"])
        debug = guarded["debug"]["query_context"]
        self.assertTrue(debug["applied"])
        self.assertEqual("followup", debug["reason"])
        self.assertGreaterEqual(debug["guard_dropped"], 1,
                                "无关的数据库候选应被守卫剔除")

    def test_no_anchor_reports_reason(self):
        self.ledger3 = SQLiteLedger(Path(self.tmp.name) / "empty.sqlite")
        self.addCleanup(self.ledger3.close)
        builder = ContextPackBuilder(self.ledger3)
        pack = builder.build("继续", _options())
        self.assertEqual([], pack["items"])
        debug = pack["debug"]["query_context"]
        self.assertFalse(debug["applied"])
        self.assertEqual("no_anchor", debug["reason"])

    def test_anchor_skipped_for_historical_slice(self):
        historical_on = self.builder.build(
            "继续", _options(as_of=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat()))
        historical_off = self.builder.build(
            "继续", _options(as_of=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
                             query_context=False))
        self.assertEqual(_canonical(historical_on), _canonical(historical_off),
                         "as_of 历史切片不得用当前会话锚定")
        self.assertNotIn("query_context", historical_on["debug"])

    def test_normal_query_does_not_touch_anchor_query(self):
        with mock.patch("hippomind.query_context.anchor_context",
                        wraps=qc.anchor_context) as spy:
            self.builder.build("数据库端口配置", _options())
            self.assertEqual(0, spy.call_count,
                             "非 follow-up 查询不得产生任何锚点查询")
        with mock.patch("hippomind.query_context.anchor_context",
                        wraps=qc.anchor_context) as spy:
            self.builder.build("继续", _options())
            self.assertEqual(1, spy.call_count)

    def test_cache_is_separated_by_session_and_anchor(self):
        from_a = self.builder.build("继续", _options(session_id="s-gui"))
        from_b = self.builder.build("继续", _options(session_id="s-db"))
        self.assertNotEqual(from_a["text"], from_b["text"])
        again_a = self.builder.build("继续", _options(session_id="s-gui"))
        self.assertEqual(_canonical(from_a), _canonical(again_a),
                         "同会话同锚点应命中同一缓存结果")
        self.assertNotEqual(from_a["text"], again_a is None and "")

    def test_debug_query_context_shape(self):
        pack = self.builder.build("继续", _options(session_id="s-gui"))
        debug = pack["debug"]["query_context"]
        self.assertEqual({"applied", "reason", "source_memory_ids",
                          "anchor_chars", "guard_dropped"}, set(debug))
        self.assertIsInstance(debug["source_memory_ids"], list)
        self.assertIsInstance(debug["anchor_chars"], int)
        self.assertIsInstance(debug["guard_dropped"], int)


class FixtureTests(unittest.TestCase):
    """WP-N 会复用的检索夹具：格式稳定、覆盖度足够。"""

    REQUIRED_CATEGORIES = {
        "zh_pure_followup", "zh_prefix_followup", "zh_ack_followup",
        "en_pure_followup", "en_prefix_followup", "safety_short_request",
        "session_isolation", "guard_no_overlap", "window_expiry", "normal_query",
    }

    def setUp(self):
        self.path = ROOT / "tests" / "fixtures" / "retrieval" / "followup_zh.json"
        self.payload = json.loads(self.path.read_text(encoding="utf-8"))

    def test_fixture_schema_is_stable(self):
        self.assertIn("_schema", self.payload)
        self.assertEqual(1, self.payload["_version"])
        self.assertIn("cases", self.payload)
        self.assertGreaterEqual(len(self.payload["cases"]), 20)
        ids = set()
        categories = set()
        for case in self.payload["cases"]:
            self.assertEqual({"id", "session_id", "prior_messages",
                              "followup_query", "expect", "category"},
                             set(case), case.get("id"))
            self.assertIsInstance(case["id"], str)
            self.assertNotIn(case["id"], ids, "夹具 id 重复")
            ids.add(case["id"])
            self.assertIsInstance(case["session_id"], str)
            self.assertIsInstance(case["prior_messages"], list)
            self.assertTrue(all(isinstance(m, str) for m in case["prior_messages"]))
            self.assertIsInstance(case["followup_query"], str)
            self.assertTrue(case["followup_query"].strip())
            self.assertIsInstance(case["expect"], list)
            self.assertIn(case["category"], self.REQUIRED_CATEGORIES)
            categories.add(case["category"])
        self.assertEqual(self.REQUIRED_CATEGORIES, categories,
                         "夹具类别覆盖不完整，WP-N 的 benchmark 会缺样本")

    def test_followup_cases_are_classified_as_followups(self):
        with mock.patch.dict(os.environ, {"HIPPOMIND_QUERY_CONTEXT": "1"}):
            for case in self.payload["cases"]:
                if case["category"].endswith("_followup"):
                    self.assertTrue(qc.is_followup(case["followup_query"]),
                                    case["id"])
                if case["category"] in {"safety_short_request", "normal_query"}:
                    self.assertFalse(qc.is_followup(case["followup_query"]),
                                     case["id"])


class PlumbingTests(unittest.TestCase):
    """session_id 从 HTTP/CLI/hook/DSH 透传到 ContextPackOptions。"""

    def test_daemon_options_accept_session_and_switch(self):
        from hippomind.daemon import MemoryHubRequestHandler
        options = MemoryHubRequestHandler._options({
            "options": {"session_id": "s-http", "query_context": False}})
        self.assertEqual("s-http", options.session_id)
        self.assertFalse(options.query_context)
        defaults = MemoryHubRequestHandler._options({})
        self.assertEqual("", defaults.session_id)
        self.assertTrue(defaults.query_context, "默认必须开启")

    def test_daemon_get_query_parses_session_and_switch(self):
        from hippomind.daemon import MemoryHubRequestHandler
        data = MemoryHubRequestHandler._query_data(
            "query=%E7%BB%A7%E7%BB%AD&session_id=s-get&query_context=0")
        self.assertEqual("s-get", data["session_id"])
        self.assertIs(False, data["query_context"])
        options = MemoryHubRequestHandler._options(data)
        self.assertEqual("s-get", options.session_id)
        self.assertFalse(options.query_context)

    def test_cli_parser_session_id_flags(self):
        from hippomind.cli import build_parser
        parser = build_parser()
        context_args = parser.parse_args(["context", "查询", "--session-id", "s-cli"])
        self.assertEqual("s-cli", context_args.session_id)
        auto_args = parser.parse_args(["auto-context", "继续", "--session-id", "s-auto"])
        self.assertEqual("s-auto", auto_args.session_id)
        default_args = parser.parse_args(["context", "查询"])
        self.assertEqual("", default_args.session_id)

    def test_cli_context_body_carries_session_id(self):
        from hippomind.cli.cmd_read import _pack_body
        args = SimpleNamespace(
            query="继续", max_items=8, max_chars=8000, max_tokens=2000,
            raw_chat=False, allow_no_evidence=False, scope="shared",
            project="", task="", source_agent=[], from_time="", to_time="",
            as_of="", debug=False, session_id="s-body")
        body = _pack_body(args)
        self.assertEqual("s-body", body["options"]["session_id"])

    def test_auto_context_body_and_offline_build_receive_session(self):
        from hippomind import cli
        args = SimpleNamespace(
            max_chars=8000, max_items=8, scope="shared", project="", task="",
            debug=False, session_id="s-auto")
        body = cli._auto_context_body(args, "继续")
        self.assertEqual("s-auto", body["options"]["session_id"])
        captured = {}

        def fake_build(ledger, request, **kwargs):
            captured.update(kwargs)
            return {"items": [], "text": ""}

        with tempfile.TemporaryDirectory(prefix="hippo-qctx-cli-") as tmp:
            args.ledger = str(Path(tmp) / "hub.sqlite")
            args.runtime_dir = str(Path(tmp) / "runtime")
            args.port = None
            args.agent_config = None
            args.offline = True
            args.no_start = True
            args.archive = None
            args.watch_interval = 30.0
            args.no_watch = True
            args.agent = "codex"
            args.cwd = ""
            args.trigger_mode = "always"
            args.format = "json"
            with mock.patch.dict(os.environ,
                                 {"HIPPOMIND_HOME": tmp,
                                  "HIPPOMIND_QUERY_CONTEXT": "1"}):
                with mock.patch.object(cli, "build_auto_context", fake_build):
                    cli.fetch_auto_context(args, "继续", session_id="s-auto")
        self.assertEqual("s-auto", captured.get("session_id"),
                         "离线 build_auto_context 未收到 session_id")

    def test_codex_hook_payload_session_id_reaches_fetch(self):
        from hippomind import codex_hook
        pack = {"items": [{"content": "x", "evidence": []}], "text": "回忆内容"}
        payload = {"hook_event_name": "UserPromptSubmit", "prompt": "继续",
                   "cwd": "/tmp", "session_id": "s-codex"}
        captured = {}

        def fake_fetch(args, request, session_id=""):
            captured["session_id"] = session_id
            captured["args_session"] = getattr(args, "session_id", "")
            return pack

        with mock.patch.object(codex_hook, "fetch_auto_context", fake_fetch):
            response = codex_hook.build_hook_response(
                payload, agent_config="", trigger_mode="always")
        self.assertIsNotNone(response)
        self.assertEqual("s-codex", captured["session_id"])
        self.assertEqual("s-codex", captured["args_session"])

    def test_claude_hook_payload_session_id_reaches_fetch(self):
        from hippomind import claude_hook
        pack = {"items": [{"content": "x", "evidence": []}], "text": "回忆内容"}
        payload = {"hook_event_name": "UserPromptSubmit", "prompt": "继续",
                   "cwd": "/tmp", "sessionId": "s-claude"}  # camelCase 变体
        captured = {}

        def fake_fetch(args, request, session_id=""):
            captured["session_id"] = session_id
            return pack

        with mock.patch.object(claude_hook, "fetch_auto_context", fake_fetch):
            with mock.patch.object(claude_hook, "_trace"):
                with mock.patch.object(
                        claude_hook.injection_ab, "assign",
                        return_value={"inject": True, "arm": "treatment",
                                      "reason": "test", "enabled": False}):
                    response = claude_hook.build_hook_response(
                        payload, agent_config="", trigger_mode="always")
        self.assertIsNotNone(response)
        self.assertEqual("s-claude", captured["session_id"])

    def test_dsh_plugin_contract_passes_session_id(self):
        source = (ROOT / "integrations" / "dsh-plugin" / "lib" / "index.js").read_text(
            encoding="utf-8")
        self.assertIn("async contextPack(query, sessionId", source)
        self.assertIn("session_id: clip(String(sessionId), 256)", source)
        self.assertIn("client.contextPack(query, sessionId)", source)


if __name__ == "__main__":
    unittest.main()

