"""Round 14 全面代码审计与 SOTA 优化回归测试套件。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions  # noqa: E402
from hippomind.distill import Candidate, DistillEngine  # noqa: E402
from hippomind.drives import DriveEngine  # noqa: E402
from hippomind.entity_index import EntityIndex  # noqa: E402
from hippomind.fact_cards import build_fact_cards, render_fact_cards  # noqa: E402
from hippomind.hippocampus import Hippocampus  # noqa: E402
from hippomind.access import AccessDenied  # noqa: E402
from hippomind.ledger import LedgerError, SQLiteLedger  # noqa: E402
from hippomind.mcp import MCPAdapter  # noqa: E402
from hippomind.query_context import content_terms, is_followup, strip_workspace_hint  # noqa: E402
from hippomind.query_expansion import Glossary, expansion_terms  # noqa: E402
from hippomind.session_digest import SessionDigester  # noqa: E402
from hippomind.textutil import normalise_for_dedup  # noqa: E402
from hippomind.time_parse import parse_temporal_expression  # noqa: E402
from hippomind.transcripts import TranscriptScanner  # noqa: E402
from hippomind.embeddings import HashingNGramEmbedding  # noqa: E402
from hippomind.vector_index import VectorIndex  # noqa: E402


class Round14AuditFixesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-r14-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "ledger.sqlite"
        self.ledger = SQLiteLedger(self.db_path)
        self.addCleanup(self.ledger.close)

    # -- P0/P1: Text, Time, Query Context & Expansion -----------------------
    def test_normalise_for_dedup_preserves_dates(self):
        a = normalise_for_dedup("2026-01-10 发布 v1.0")
        b = normalise_for_dedup("2026-02-15 发布 v1.0")
        self.assertNotEqual(a, b)

    def test_time_parse_chinese_tens_and_explicit_n_days(self):
        from datetime import datetime, timezone

        now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
        start, end = parse_temporal_expression("二十天内的变更", now=now)
        self.assertTrue(start)
        self.assertEqual(start[:10], "2026-10-29")
        false_start, _ = parse_temporal_expression("我花了3天修复这个问题", now=now)
        self.assertEqual(false_start, "")

    def test_query_context_keeps_embedded_chars_and_word_boundaries(self):
        cleaned = strip_workspace_hint("很好的方案设计\n__hm_workspace_hint__ foo")
        self.assertEqual(cleaned, "很好的方案设计")
        self.assertFalse(is_followup("很好用的网关配置"))
        self.assertIn("好用", content_terms("很好用的网关配置"))
        self.assertFalse(is_followup("debug why gateway restarted"))
        self.assertTrue(is_followup("嗯"))
        self.assertTrue(is_followup("再优化一下"))

    def test_query_expansion_short_ascii_word_boundary(self):
        glossary = Glossary(entries=(("db", ("postgres", "database")),), path="mem")
        with mock.patch.dict(os.environ, {"HIPPOMIND_QUERY_EXPANSION": "1"}):
            self.assertEqual(expansion_terms("debug timeout issue", glossary=glossary), [])
            self.assertIn("postgres", expansion_terms("check db timeout", glossary=glossary))

    # -- P0/P1: EntityIndex & VectorIndex -----------------------------------
    def test_entity_index_2char_cjk_and_df_dedup(self):
        # Repeat same entity 5 times inside 1 memory + 1 unrelated memory -> DF must be 0.5 (1/2)
        m1 = self.ledger.ingest({
            "content": "灰度发布接入微信登录",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:m1"],
            "claims": [
                {"subject": "灰度", "predicate": "p1", "slot_key": "s1", "value": "v1", "confidence": 0.9},
                {"subject": "灰度", "predicate": "p2", "slot_key": "s2", "value": "v2", "confidence": 0.9},
                {"subject": "灰度", "predicate": "p3", "slot_key": "s3", "value": "v3", "confidence": 0.9},
                {"subject": "灰度", "predicate": "p4", "slot_key": "s4", "value": "v4", "confidence": 0.9},
                {"subject": "微信", "predicate": "p5", "slot_key": "s5", "value": "v5", "confidence": 0.9},
            ],
        })
        self.ledger.ingest({
            "content": "无关任务记录",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:m2"],
            "claims": [
                {"subject": "Orion", "predicate": "p", "slot_key": "s", "value": "v", "confidence": 0.9},
            ],
        })
        idx = EntityIndex.build(self.ledger)
        self.assertIn("灰度", [e for e, _ in idx.match_entities("灰度方案")])
        self.assertIn("微信", [e for e, _ in idx.match_entities("微信回调")])
        self.assertIn(m1.memory_id, [mid for mid, _, _ in idx.search_text("微信回调查问题")])

    def test_vector_index_add_many_dedup_and_sync(self):
        emb = HashingNGramEmbedding(16)
        m1 = self.ledger.ingest({
            "content": "Orion gateway timeout issue",
            "kind": "fact", "source_agent": "codex", "scope": "shared", "evidence": ["t:v1"],
        })
        vi = VectorIndex.build(self.ledger, emb)
        vi.add_many([
            (m1.memory_id, "旧内容"),
            (m1.memory_id, "Orion gateway timeout"),
        ])
        self.assertEqual(len(vi.ids), 1)
        hits = vi.search(emb.embed("Orion gateway timeout"), topn=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], m1.memory_id)

    # -- P0/P1: Ledger Invariants, Temporal Claims, FTS, Health, Export -----
    def test_bitemporal_claim_overlap_disjoint_vs_intersecting(self):
        self.ledger.ingest({
            "content": "Q1 gateway port 8000",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:q1"],
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": "2026-02-01T00:00:00Z",
            "claims": [{
                "subject": "gw", "predicate": "port", "slot_key": "gw:port",
                "value": "8000", "confidence": 0.9,
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_until": "2026-02-01T00:00:00Z",
            }],
        })
        # Disjoint later finite interval [03-01, 04-01) -> no conflict
        self.ledger.ingest({
            "content": "Q1b gateway port 9000",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:q1b"],
            "valid_from": "2026-03-01T00:00:00Z",
            "valid_until": "2026-04-01T00:00:00Z",
            "claims": [{
                "subject": "gw", "predicate": "port", "slot_key": "gw:port",
                "value": "9000", "confidence": 0.9,
                "valid_from": "2026-03-01T00:00:00Z",
                "valid_until": "2026-04-01T00:00:00Z",
            }],
        })
        self.assertEqual(self.ledger.list_conflicts(scope="shared")["total"], 0)
        # Overlapping finite interval [01-15, 02-15) -> conflict
        self.ledger.ingest({
            "content": "Q1c gateway port 7000",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:q1c"],
            "valid_from": "2026-01-15T00:00:00Z",
            "valid_until": "2026-02-15T00:00:00Z",
            "claims": [{
                "subject": "gw", "predicate": "port", "slot_key": "gw:port",
                "value": "7000", "confidence": 0.9,
                "valid_from": "2026-01-15T00:00:00Z",
                "valid_until": "2026-02-15T00:00:00Z",
            }],
        })
        self.assertGreaterEqual(self.ledger.list_conflicts(scope="shared")["total"], 1)

    def test_tie_conflict_removes_old_fts_and_unifies_sibling_claims(self):
        m1 = self.ledger.ingest({
            "content": "ServiceA auth token JWT and cache Redis",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:tie1"],
            "claims": [
                {"subject": "ServiceA", "predicate": "auth", "slot_key": "a:auth", "value": "JWT", "confidence": 0.8},
                {"subject": "ServiceA", "predicate": "cache", "slot_key": "a:cache", "value": "Redis", "confidence": 0.8},
            ],
        })
        m2 = self.ledger.ingest({
            "content": "ServiceA auth token OAuth",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:tie2"],
            "claims": [
                {"subject": "ServiceA", "predicate": "auth", "slot_key": "a:auth", "value": "OAuth", "confidence": 0.8},
            ],
        })
        with self.ledger._reading() as conn:
            fts_ids = {r[0] for r in conn.execute("SELECT memory_id FROM memory_fts").fetchall()}
            claim_states = {r[0] for r in conn.execute(
                "SELECT state FROM memory_claims WHERE memory_id=?", (m1.memory_id,)).fetchall()}
        self.assertNotIn(m1.memory_id, fts_ids)
        self.assertNotIn(m2.memory_id, fts_ids)
        self.assertEqual(claim_states, {"conflict"})

    def test_fts_sync_report_on_read_only_ledger(self):
        self.ledger.ingest({
            "content": "只读同步健康检查条目",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:ro1"],
        })
        self.ledger.close()
        ro = SQLiteLedger(self.db_path, read_only=True)
        try:
            report = ro.fts_sync_report()
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["stale"], 0)
            self.assertEqual(report["orphan"], 0)
        finally:
            ro.close()

    def test_feedback_negative_signal_does_not_boost_confidence(self):
        res = self.ledger.ingest({
            "content": "默认超时 5 秒",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "confidence": 0.6,
            "evidence": ["t:fb1"],
        })
        self.ledger.feedback(res.memory_id, -1.0, "wrong", "codex")
        mem = self.ledger.get_memory(res.memory_id)
        self.assertLessEqual(float(mem["confidence"]), 0.6)

    def test_export_sql_includes_v4_columns_and_idempotent_fts(self):
        self.ledger.ingest({
            "content": "SQL 导出完整性验证内容",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:sql1"],
            "valid_from": "2026-05-01T00:00:00Z",
        })
        text = self.ledger.export_sql()
        self.assertIn("agent_trust,quality_score,quality_state", text)
        self.assertIn("NOT IN (SELECT memory_id FROM memory_fts)", text)

    def test_consolidate_supersedes_previous_summary(self):
        for i in range(3):
            self.ledger.ingest({
                "content": f"任务进度步骤 {i}: 完成模块配置",
                "kind": "progress",
                "source_agent": "codex",
                "scope": "shared",
                "task_id": "task-cons-1",
                "evidence": [f"t:c{i}"],
            })
        first = self.ledger.consolidate()
        self.assertEqual(len(first), 1)
        self.ledger.ingest({
            "content": "任务进度步骤 3: 灰度上线完成",
            "kind": "progress",
            "source_agent": "codex",
            "scope": "shared",
            "task_id": "task-cons-1",
            "evidence": ["t:c3"],
        })
        second = self.ledger.consolidate()
        self.assertEqual(len(second), 1)
        old_mem = self.ledger.get_memory(first[0].memory_id)
        self.assertEqual(old_mem["lifecycle"], "superseded")
        self.assertEqual(old_mem["superseded_by"], second[0].memory_id)

    # -- Phase 2: Relevance Gate, Control Touch, Associative PPR, Fact Cards --
    def test_context_pack_control_arm_does_not_increment_touch_and_staleness(self):
        res = self.ledger.ingest({
            "content": "Orion 路由配置位于 missing_module.py",
            "kind": "fact",
            "source_agent": "codex",
            "scope": "shared",
            "evidence": ["t:cp1"],
            "context": {"related_files": ["missing_module.py"]},
        })
        builder = ContextPackBuilder(self.ledger)
        pack = builder.build("Orion 路由配置", ContextPackOptions(
            scope="shared", record_touch=False, workspace_cwd=str(self.root), debug=True
        ))
        self.assertTrue(pack["items"])
        with self.ledger._reading() as conn:
            acc = int(conn.execute(
                "SELECT touch_count FROM memories WHERE memory_id=?", (res.memory_id,)
            ).fetchone()[0])
        self.assertEqual(acc, 0)
        self.assertIn("工作区路径已变更", pack["text"])

    def test_associative_2hop_ppr_and_scope_isolation(self):
        a = self.ledger.ingest({
            "content": "AnchorFact AlphaBeta 网关限流器配置",
            "kind": "fact", "source_agent": "codex", "scope": "shared",
            "evidence": ["t:a1"], "confidence": 0.9, "importance": 8,
        })
        b = self.ledger.ingest({
            "content": "HopOne 中间依赖池设定",
            "kind": "fact", "source_agent": "codex", "scope": "shared",
            "evidence": ["t:b1"], "confidence": 0.9,
            "causal_parents": [{"memory_id": a.memory_id, "relation_type": "caused_by", "confidence": 0.9}],
        })
        c = self.ledger.ingest({
            "content": "HopTwo 最终降级熔断器策略",
            "kind": "fact", "source_agent": "codex", "scope": "shared",
            "evidence": ["t:c1"], "confidence": 0.9,
            "causal_parents": [{"memory_id": b.memory_id, "relation_type": "caused_by", "confidence": 0.9}],
        })
        # Private cross-scope node attached to a (should never leak to shared)
        priv = self.ledger.ingest({
            "content": "PrivateSecret 私有日志",
            "kind": "fact", "source_agent": "claude", "scope": "agent:claude",
            "evidence": ["t:p1"],
        })
        self.ledger.add_relations(a.memory_id, [{"memory_id": priv.memory_id, "relation_type": "related_to", "confidence": 0.9}])
        builder = ContextPackBuilder(self.ledger)
        assoc = builder._associative_candidates(
            [{"memory_id": a.memory_id, "score": 1.0, "scope": "shared"}],
            ContextPackOptions(scope="shared", raw_chat=False, require_evidence=True),
        )
        assoc_ids = [row["memory_id"] for row in assoc]
        self.assertIn(b.memory_id, assoc_ids)
        self.assertIn(c.memory_id, assoc_ids)
        self.assertNotIn(priv.memory_id, assoc_ids)

    def test_fact_cards_sql_pushdown_and_max_tokens(self):
        m1 = self.ledger.ingest({
            "content": "GatewayX primary port 8443",
            "kind": "fact", "source_agent": "codex", "scope": "shared", "evidence": ["t:fc1"],
            "claims": [{"subject": "GatewayX", "predicate": "port", "slot_key": "gx:port", "value": "8443", "confidence": 0.9}],
        })
        m2 = self.ledger.ingest({
            "content": "GatewayX region us-west-1",
            "kind": "fact", "source_agent": "codex", "scope": "shared", "evidence": ["t:fc2"],
            "claims": [{"subject": "GatewayX", "predicate": "region", "slot_key": "gx:region", "value": "us-west-1", "confidence": 0.9}],
        })
        cards = build_fact_cards(self.ledger, memory_ids=[m1.memory_id, m2.memory_id], scope="shared")
        self.assertEqual(len(cards), 2)
        rendered = render_fact_cards(cards, max_chars=40)
        self.assertLessEqual(len(rendered), 40)
        pack = ContextPackBuilder(self.ledger).build(
            "GatewayX port region",
            ContextPackOptions(scope="shared", fact_cards=True, max_tokens=20, debug=True),
        )
        self.assertTrue(pack["fact_cards"]["text"] or pack["items"])
        self.assertLessEqual(pack["fact_cards"]["chars"], 80)

    # -- Phase 3 & Scope Isolation: Distill, SessionDigest, MCP -------------
    def test_session_digest_isolates_scopes_for_identical_session_id(self):
        for i in range(4):
            self.ledger.ingest({
                "content": f"Claude 私有会话消息 {i}: 决定采用方案 Alpha 进行重构",
                "kind": "raw_chat", "source_agent": "claude", "scope": "agent:claude",
                "raw_chat": True, "session_id": "shared-sess-id",
                "evidence": [f"c:{i}"], "timestamp": f"2026-11-18T10:0{i}:00Z",
            })
            self.ledger.ingest({
                "content": f"Codex 私有会话消息 {i}: 决定采用方案 Beta 完成迁移",
                "kind": "raw_chat", "source_agent": "codex", "scope": "agent:codex",
                "raw_chat": True, "session_id": "shared-sess-id",
                "evidence": [f"x:{i}"], "timestamp": f"2026-11-18T10:1{i}:00Z",
            })
        digester = SessionDigester(self.ledger, extractor=None)
        summary = digester.run(limit=10)
        self.assertEqual(summary["digested"], 2)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT scope, content FROM memories WHERE source_agent='hippomind-digest'"
            ).fetchall()
        by_scope = {r["scope"]: r["content"] for r in rows}
        self.assertEqual(set(by_scope.keys()), {"agent:claude", "agent:codex"})
        self.assertIn("Alpha", by_scope["agent:claude"])
        self.assertNotIn("Beta", by_scope["agent:claude"])
        self.assertIn("Beta", by_scope["agent:codex"])
        self.assertNotIn("Alpha", by_scope["agent:codex"])

    def test_distill_scope_isolation_repeat_mark_and_canonical_subject(self):
        # Seed canonical subject "OrionGateway"
        self.ledger.ingest({
            "content": "OrionGateway 初始配置已确认",
            "kind": "fact", "source_agent": "codex", "scope": "shared", "evidence": ["t:can0"],
            "claims": [{"subject": "OrionGateway", "predicate": "mode", "slot_key": "og:mode", "value": "strict", "confidence": 0.9}],
        })
        # Two identical-content decisions in DIFFERENT scopes must not collide or dedupe across scopes
        for sc in ("shared", "agent:claude"):
            self.ledger.ingest({
                "content": "决定将心跳阈值改为 30 秒以减少抖动",
                "kind": "decision", "source_agent": "claude" if sc != "shared" else "codex",
                "scope": sc, "evidence": [f"t:sc:{sc}"],
            })
        mock_ext = mock.MagicMock()
        mock_ext.available = True
        mock_ext.name = "mock-llm"
        mock_ext.extract.side_effect = lambda cands: [
            {"subject": "oriongateway", "predicate": "heartbeat", "slot_key": "og:hb",
             "value": "30s", "confidence": 0.9, "source": i + 1}
            for i in range(len(cands))
        ]
        engine = DistillEngine(self.ledger, extractor=mock_ext)
        res = engine.run(limit=10)
        self.assertEqual(res["distilled"], 3)
        with self.ledger._reading() as conn:
            claims = conn.execute(
                "SELECT scope, subject FROM memory_claims WHERE slot_key='og:hb'"
            ).fetchall()
        by_scope = {r["scope"]: r["subject"] for r in claims}
        self.assertEqual(by_scope.get("shared"), "OrionGateway")

    def test_mcp_pin_and_forget_enforce_scope_and_admin_purge(self):
        priv = self.ledger.ingest({
            "content": "Codex 私有备忘", "kind": "fact",
            "source_agent": "codex", "scope": "agent:codex", "evidence": ["t:p_codex"],
        })
        shared = self.ledger.ingest({
            "content": "共享配置备忘", "kind": "fact",
            "source_agent": "claude", "scope": "shared", "evidence": ["t:p_shared"],
        })
        own = self.ledger.ingest({
            "content": "Claude 私有备忘可置顶", "kind": "fact",
            "source_agent": "claude", "scope": "agent:claude", "evidence": ["t:p_claude"],
        })
        _cred, raw_token = self.ledger.issue_agent_credential(
            "claude", scopes=["shared", "agent:claude"], permissions=["read", "ingest", "shared_ingest"],
        )
        scoped = MCPAdapter(str(self.db_path), agent_token=raw_token)
        self.assertTrue(scoped.call("hippomind_pin", {"memory_id": own.memory_id})["pinned"])
        with self.assertRaises(AccessDenied):
            scoped.call("hippomind_pin", {"memory_id": priv.memory_id})
        with self.assertRaises(AccessDenied):
            scoped.call("hippomind_forget", {"memory_ids": [priv.memory_id]})
        with self.assertRaises(AccessDenied):
            scoped.call("hippomind_forget", {"memory_ids": [shared.memory_id], "purge": True})

    # -- Capture & Demo Modules ---------------------------------------------
    def test_transcript_scanner_skips_oversized_line_instead_of_wedging(self):
        tx_root = self.root / "tx" / "codex"
        tx_root.mkdir(parents=True)
        tx_file = tx_root / "sess.jsonl"
        big_line = json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": "A" * (600 * 1024)}}) + "\n"
        small_line = json.dumps({
            "type": "response_item",
            "timestamp": "2026-11-18T10:00:00Z",
            "payload": {"type": "message", "role": "user", "content": "第二行正常消息"},
        }) + "\n"
        tx_file.write_text(big_line + small_line, encoding="utf-8")
        scanner = TranscriptScanner(self.ledger, roots=[tx_root])
        first = scanner.sync(max_messages=20)
        second = scanner.sync(max_messages=20)
        self.assertGreaterEqual(first["messages"] + second["messages"], 1)

    def test_drive_resonance_never_clamps_down_above_ceil(self):
        drives = DriveEngine()
        dname = next(iter(drives.values))
        drives.values[dname] = drives.cfg["ceil"][dname] + 0.2
        before = drives.values[dname]
        # Positive resonance must never lower current value below before
        domains = [dom for dom, mp in drives.cfg["affinity"].items() if dname in mp]
        if domains:
            drives.resonance([domains[0]])
            self.assertGreaterEqual(drives.values[dname], before)

    def test_hippocampus_link_similar_skips_superseded_memories(self):
        h = Hippocampus(seed=1)
        old = h.store("Orion gateway port config v1", action="keep")
        new = h.store("Orion gateway port config v2", action="keep")
        h.supersede(old, new)
        third = h.store("Orion gateway port config v3", action="keep")
        linked = {(min(a, b), max(a, b)) for (a, b) in h.assoc}
        pair_with_old = (min(old, third), max(old, third))
        self.assertNotIn(pair_with_old, linked)

    def test_claude_hook_single_trace_and_cursor_scanner_oversized_skip(self):
        from hippomind import claude_hook, injection_ab, injection_trace
        from hippomind.cursor_capture import MAX_LINE_BYTES, CursorTranscriptScanner

        trace_file = self.root / "injection.jsonl"
        cfg = self.root / "claude.json"
        cfg.write_text('{"token":"x"}\n', encoding="utf-8")
        os.chmod(cfg, 0o600)
        with mock.patch.dict(os.environ, {
            "HIPPOMIND_HOME": str(self.root),
            "HIPPOMIND_INJECTION_TRACE": "1",
            "HIPPOMIND_AUTO_INJECT": "always",
        }):
            with mock.patch.object(injection_ab, "assign", return_value={
                "arm": "treatment", "inject": True, "enabled": True, "reason": "ab_assign",
            }), mock.patch.object(claude_hook, "fetch_auto_context", return_value={
                "items": [{"memory_id": "m1", "content": "fact 1"}],
                "text": "fact 1",
                "debug": {"selected": [{"memory_ids": ["m1"]}]},
            }):
                resp = claude_hook.build_hook_response({
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "记住网关超时 30s",
                    "cwd": str(self.root),
                }, agent_config=str(cfg), trigger_mode="always")
        self.assertIsNotNone(resp)
        lines = trace_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]).get("arm"), "treatment")

        c_root = self.root / "cursor_store"
        t_dir = c_root / "projects" / "demo" / "agent-transcripts"
        t_dir.mkdir(parents=True)
        t_file = t_dir / "chat.jsonl"
        big = json.dumps({"role": "user", "message": {"role": "user", "content": "X" * (MAX_LINE_BYTES + 1024)}}) + "\n"
        ok = json.dumps({"role": "user", "message": {"role": "user", "content": "第二条正常 Cursor 记录"}}) + "\n"
        t_file.write_text(big + ok, encoding="utf-8")
        c_scanner = CursorTranscriptScanner(self.ledger, roots=[c_root])
        s1 = c_scanner.sync(max_messages=20)
        s2 = c_scanner.sync(max_messages=20)
        self.assertGreaterEqual(s1["messages"] + s2["messages"], 1)

    def test_gui_archive_stats_codex_hook_gemini_v3_and_distill_noop(self):
        from hippomind import codex_hook, injection_ab
        from hippomind.distill import ClaudeExtractor
        from hippomind.gemini_capture import GeminiSummaryScanner
        from hippomind.gui_api import GuiApi
        from hippomind.ledger._helpers import RAW_ARCHIVE_DIR_NAME

        # 1) gui_api._archive_stats includes purged/dedupe archives
        arch_dir = self.db_path.parent / (self.db_path.stem + "." + RAW_ARCHIVE_DIR_NAME)
        arch_dir.mkdir(parents=True, exist_ok=True)
        for name in ("raw-1.jsonl.gz", "purged-1.jsonl.gz", "dedupe-1.jsonl.gz"):
            (arch_dir / name).write_bytes(b"abc")
        gui = GuiApi(hub=type("H", (), {"ledger": self.ledger, "paths": type("P", (), {})()})())
        stats = gui._archive_stats()
        self.assertEqual(stats["archive_files"], 3)
        self.assertEqual(stats["archive_bytes"], 9)

        # 2) codex_hook writes exact 1 trace with memory_ids + ab_handled
        trace_file = self.root / "injection.jsonl"
        cfg = self.root / "codex.json"
        cfg.write_text('{"token":"x"}\n', encoding="utf-8")
        os.chmod(cfg, 0o600)
        with mock.patch.dict(os.environ, {
            "HIPPOMIND_HOME": str(self.root),
            "HIPPOMIND_INJECTION_TRACE": "1",
        }):
            with mock.patch.object(injection_ab, "assign", return_value={
                "arm": "treatment", "inject": True, "enabled": True, "reason": "ab_assign",
            }), mock.patch.object(codex_hook, "fetch_auto_context", return_value={
                "items": [{"memory_id": "m1", "content": "codex fact"}],
                "text": "codex fact",
                "debug": {"selected": [{"memory_ids": ["m1"]}]},
            }) as mocked_fetch:
                resp = codex_hook.build_hook_response({
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "记住网关规则",
                    "cwd": str(self.root),
                }, agent_config=str(cfg), trigger_mode="always")
        self.assertIsNotNone(resp)
        self.assertTrue(mocked_fetch.call_args.args[0].ab_handled)
        trace_rows = trace_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(trace_rows), 1)
        parsed_trace = json.loads(trace_rows[0])
        self.assertEqual(parsed_trace["memory_ids"], ["m1"])

        # 3) gemini_capture supersedes revised conversation_id via Identity v3
        gdb = self.root / "gem.db"
        gconn = sqlite3.connect(gdb)
        gconn.execute("""CREATE TABLE conversation_summaries(
            conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT,
            step_count INTEGER, last_modified_time TEXT, workspace_uris TEXT,
            status TEXT, project_id TEXT, agent_name TEXT)""")
        gconn.execute("INSERT INTO conversation_summaries VALUES(?,?,?,?,?,?,?,?,?)",
                      ("conv-1", "Init", "First preview", 2, "2026-11-18T10:00:00Z", "", "IDLE", "p", "gemini"))
        gconn.commit()
        scanner = GeminiSummaryScanner(self.ledger, databases=[gdb])
        self.assertEqual(scanner.sync(max_rows=10)["inserted"], 1)
        gconn.execute("UPDATE conversation_summaries SET preview=?, last_modified_time=? WHERE conversation_id=?",
                      ("Updated preview v2", "2026-11-18T11:00:00Z", "conv-1"))
        gconn.commit()
        gconn.close()
        self.assertEqual(scanner.sync(max_rows=10)["inserted"], 1)
        with self.ledger._reading() as conn:
            active_gem = conn.execute(
                "SELECT content FROM memories WHERE source_agent='gemini' AND lifecycle='active'"
            ).fetchall()
        self.assertEqual(len(active_gem), 1)
        self.assertIn("Updated preview v2", active_gem[0]["content"])

        # 4) DistillEngine Phase 3: related_files heuristic + ClaudeExtractor known_slots & NOOP filter
        self.ledger.ingest({
            "content": "Orion 部署文档位于 deploy.yaml",
            "kind": "task", "source_agent": "codex", "scope": "shared", "task_id": "t-rel",
            "evidence": ["t:rel1"],
            "metadata": {"context": {"related_files": ["deploy.yaml"]}},
        })
        ext = ClaudeExtractor(complete=lambda prompt, **kw: json.dumps({
            "claims": [
                {"subject": "Orion", "predicate": "mode", "slot_key": "o:mode", "value": "strict", "op": "NOOP", "source": 1},
                {"subject": "Orion", "predicate": "port", "slot_key": "o:port", "value": "8080", "op": "ADD", "source": 1},
            ]
        }), transport="api")
        ext.use_api = True
        ext.use_cli = False
        eng = DistillEngine(self.ledger, extractor=ext)
        d_res = eng.run(limit=10)
        self.assertGreaterEqual(d_res["distilled"], 1)
        with self.ledger._reading() as conn:
            ops = [r["slot_key"] for r in conn.execute("SELECT slot_key FROM memory_claims WHERE slot_key IN ('o:mode','o:port')").fetchall()]
        self.assertEqual(ops, ["o:port"])
        # Also verify heuristic extraction emits related_files claim
        h_claims = DistillEngine._heuristic(Candidate(
            "m_rf", "task", "check deploy", "codex", "proj", "t-rf", "", "",
            {"context": {"related_files": ["src/app.py"]}}, "shared",
        ))
        self.assertTrue(any(c["predicate"] == "related_files" for c in h_claims))

    def test_calibrated_semantic_gate_paraphrase_recall_vs_noise_abstention(self):
        from hippomind.embeddings import LocalVectorEmbedding

        # Construct semantic dictionary bridging paraphrase wording ("恶意载荷防线" <-> "提示注入")
        # and verify zero lexical overlap paraphrase passes via calibrated margin while noise abstains.
        vec_path = self.root / "sem_vecs.json"
        vec_path.write_text(json.dumps({
            "dim": 4,
            "vectors": {
                "提示注入": [1.0, 0.0, 0.0, 0.0],
                "恶意指令防线": [0.95, 0.31, 0.0, 0.0],
                "数据库": [0.0, 1.0, 0.0, 0.0],
                "前端主题": [0.0, 0.0, 1.0, 0.0],
                "部署窗口": [0.0, 0.0, 0.0, 1.0],
            },
        }), encoding="utf-8")
        emb = LocalVectorEmbedding.load(vec_path)
        self.assertIsNotNone(emb)
        target = self.ledger.ingest({
            "content": "开启系统核心安全边界以隔离提示注入风险",
            "kind": "decision", "source_agent": "codex", "scope": "shared",
            "evidence": ["t:sem1"], "confidence": 0.9, "importance": 8,
        })
        self.ledger.ingest({
            "content": "默认使用数据库持久化索引",
            "kind": "fact", "source_agent": "codex", "scope": "shared",
            "evidence": ["t:sem2"],
        })
        self.ledger.ingest({
            "content": "前端主题采用高对比纸本配色",
            "kind": "fact", "source_agent": "codex", "scope": "shared",
            "evidence": ["t:sem3"],
        })
        vi = VectorIndex.build(self.ledger, emb)
        builder = ContextPackBuilder(self.ledger, embedder=emb, vector_index=vi)
        # Paraphrase query shares zero chars with "开启系统核心安全边界以隔离提示注入风险"
        paraphrase_pack = builder.build(
            "恶意指令防线",
            ContextPackOptions(scope="shared", min_score=0.20, hybrid_rerank=True, debug=True),
        )
        self.assertTrue(paraphrase_pack["items"])
        self.assertIn("提示注入", paraphrase_pack["text"])
        # Completely unrelated noise query must still abstain
        noise_pack = builder.build(
            "南极企鹅冬季迁徙路线规划",
            ContextPackOptions(scope="shared", min_score=0.20, hybrid_rerank=True, debug=True),
        )
        self.assertEqual(noise_pack["items"], [])
        self.assertEqual(
            [r["memory_id"] for r in self.ledger.search("开启系统", scope="shared")],
            [target.memory_id],
        )

    def test_entity_graph_excludes_quarantined_and_filters_by_scope(self):
        self.ledger.ingest({
            "content": "SvcMain 正常运行", "kind": "fact",
            "source_agent": "codex", "scope": "shared", "evidence": ["t:eg1"],
            "claims": [{"subject": "SvcMain", "predicate": "state", "slot_key": "svc:s", "value": "ok", "confidence": 0.9}],
        })
        q = self.ledger.ingest({
            "content": "SvcBad 待审查", "kind": "fact",
            "source_agent": "unknown-untrusted", "scope": "shared", "evidence": ["t:eg2"],
            "claims": [{"subject": "SvcBad", "predicate": "state", "slot_key": "svcb:s", "value": "bad", "confidence": 0.2}],
        })
        with self.ledger._reading() as conn:
            q_state = conn.execute("SELECT quality_state FROM memories WHERE memory_id=?", (q.memory_id,)).fetchone()[0]
        if q_state == "quarantine":
            g = self.ledger.entity_graph("SvcBad", scope="shared")
            self.assertFalse(g["found"])
        g_main = self.ledger.entity_graph("SvcMain", scope="shared")
        self.assertTrue(g_main["found"])

    def test_schema_v9_bitemporal_relations_and_entity_graph_as_of(self):
        from hippomind.ledger import SCHEMA_VERSION

        self.assertEqual(SCHEMA_VERSION, 9)
        t1 = "2026-01-01T00:00:00Z"
        t2 = "2026-06-01T00:00:00Z"
        t_mid = "2026-03-01T00:00:00Z"
        m1 = self.ledger.ingest({
            "content": "NodeA v1 depends on NodeB", "kind": "fact",
            "source_agent": "codex", "scope": "shared", "evidence": ["t:v9_1"],
            "valid_from": t1,
            "claims": [{"subject": "NodeA", "predicate": "tier", "slot_key": "na:tier", "value": "v1", "confidence": 0.6}],
        })
        m2 = self.ledger.ingest({
            "content": "NodeB active downstream", "kind": "fact",
            "source_agent": "codex", "scope": "shared", "evidence": ["t:v9_2"],
            "valid_from": t1,
            "causal_parents": [{"memory_id": m1.memory_id, "relation_type": "caused_by", "confidence": 0.8}],
            "claims": [{"subject": "NodeB", "predicate": "tier", "slot_key": "nb:tier", "value": "v1", "confidence": 0.8}],
        })
        m3 = self.ledger.ingest({
            "content": "NodeA v2 replaces v1", "kind": "fact",
            "source_agent": "codex", "scope": "shared", "evidence": ["t:v9_3"],
            "valid_from": t2,
            "claims": [{"subject": "NodeA", "predicate": "tier", "slot_key": "na:tier", "value": "v2", "confidence": 0.95}],
        })
        self.ledger.supersede(m1.memory_id, m3.memory_id, source_agent="codex")
        # Historical graph slice at t_mid sees NodeA -> NodeB edge
        g_hist = self.ledger.entity_graph("NodeA", scope="shared", hops=1, as_of=t_mid)
        self.assertTrue(g_hist["found"])
        self.assertTrue(any(e["to"] == "NodeB" for e in g_hist["edges"]))
        # Current causal_ancestors from m2 no longer surfaces closed m1 parent
        anc_now = self.ledger.causal_ancestors([m2.memory_id], scope="shared")
        self.assertNotIn(m1.memory_id, [r["memory_id"] for r in anc_now])
        # Historical causal_ancestors at t_mid still sees m1
        anc_hist = self.ledger.causal_ancestors([m2.memory_id], scope="shared", as_of=t_mid)
        self.assertIn(m1.memory_id, [r["memory_id"] for r in anc_hist])

    def test_core_playbook_include_pinned_opt_in(self):
        pinned = self.ledger.ingest({
            "content": "项目铁律：所有 SQLite 连接必须使用上下文管理器",
            "kind": "fact", "source_agent": "codex", "scope": "shared",
            "project": "atlas", "evidence": ["t:pin1"], "importance": 9,
        })
        self.ledger.pin_memory(pinned.memory_id, pinned=True, source="codex")
        builder = ContextPackBuilder(self.ledger)
        # Default (include_pinned=False): unrelated query stays empty (abstention intact)
        default_pack = builder.build("南极企鹅迁徙", ContextPackOptions(scope="shared", min_score=0.20))
        self.assertEqual(default_pack["items"], [])
        # Opt-in include_pinned=True surfaces [核心置顶] playbook
        core_pack = builder.build("南极企鹅迁徙", ContextPackOptions(
            scope="shared", project="atlas", min_score=0.20, include_pinned=True
        ))
        self.assertTrue(core_pack["items"])
        self.assertIn("[核心置顶]", core_pack["text"])
        self.assertIn("上下文管理器", core_pack["text"])

    def test_integrations_query_connections_close_cleanly(self):
        from integrations import query as iq

        missing_shared = self.root / "missing_shared.sqlite"
        with mock.patch.object(iq, "SHARED_DB", missing_shared):
            self.assertEqual(iq.search_shared("test"), [])


if __name__ == "__main__":
    unittest.main()
