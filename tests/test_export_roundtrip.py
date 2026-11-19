"""export_rows -> ingest_many 完整回灌回归（WP-E 验收）。

锁定「JSONL 导出→导入 claims 数一致且 procedural 可回导」：

* 导出记录必须携带每条记忆**完整**的 active claims 列表，而不只是第一组平铺字段；
* ``procedural`` 技能卡本体必须随包导出并可被 ``ingest_many`` 重建；
* ``context`` / ``tags`` / ``confidence`` / ``importance`` / ``valid_from`` /
  ``valid_until`` 等检索与双时态字段保留；
* 旧版平铺字段（subject/predicate/slot_key/value/...）继续存在，剥离新键后老
  导入器仍能工作（向后兼容）；
* 已登记的 causal parents 可查则随包导出；父 id 是源库坐标，导入端只在父记忆
  已存在时连边（同库迁移或先导父节点），跨库时静默跳过而不是报错。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hippomind.ledger import SQLiteLedger  # noqa: E402

FACT_CONTENT = "生产数据库端口 6432，已完成迁移"
PROCEDURAL_CONTENT = "部署技能：先检查备份，再执行迁移"
CLAIMS = [
    {"subject": "db.prod", "predicate": "config", "slot_key": "port",
     "value": {"port": "6432"}, "confidence": 0.9},
    {"subject": "db.prod", "predicate": "config", "slot_key": "theme",
     "value": "纸本", "confidence": 0.7},
]


class ExportRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-export-v3-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = SQLiteLedger(self.root / "src.sqlite")
        self.addCleanup(self.src.close)
        self.fact = self.src.ingest({
            "kind": "fact", "content": FACT_CONTENT, "source_agent": "tester",
            "scope": "shared", "evidence": ["evidence:fact"],
            "event_id": "fact-evt-1", "timestamp": "2026-01-01T00:00:00+00:00",
            "confidence": 0.9, "importance": 8, "weight": 0.75, "utility": 0.4,
            "tags": ["db", "migration"],
            "context": {"goal": ["迁移记忆账本"], "next_steps": ["导入后校验"]},
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_until": "2030-01-01T00:00:00+00:00",
            "claims": CLAIMS,
        })
        self.procedural = self.src.ingest({
            "kind": "procedural", "content": PROCEDURAL_CONTENT,
            "source_agent": "tester", "scope": "shared",
            "evidence": ["evidence:skill"], "timestamp": "2026-01-02T00:00:00+00:00",
            "procedural": {
                "trigger_pattern": ["部署"],
                "preconditions": ["有备份"],
                "execution_pipeline": ["先检查", "再执行"],
                "verification_assertion": ["健康检查通过"],
                "calibration_score": {"attempts": 2, "successes": 2, "rate": 1.0},
            },
        })
        # 因果父边：父节点先写，子节点带 causal_parents。导出端必须能查到。
        self.parent = self.src.ingest({
            "kind": "decision", "content": "先备份再迁移", "source_agent": "tester",
            "scope": "shared", "evidence": ["evidence:parent"],
            "timestamp": "2026-01-03T00:00:00+00:00",
            "event_id": "causal-parent-1",
        })
        self.child = self.src.ingest({
            "kind": "result", "content": "迁移完成并校验通过", "source_agent": "tester",
            "scope": "shared", "evidence": ["evidence:child"],
            "timestamp": "2026-01-04T00:00:00+00:00",
            "causal_parents": [{"memory_id": self.parent.memory_id,
                                "relation_type": "caused_by", "confidence": 0.9}],
        })

    # -- helpers ---------------------------------------------------------
    def _export(self):
        return self.src.export_rows(scope="shared")

    @staticmethod
    def _by_content(rows, content):
        matches = [row for row in rows if row.get("content") == content]
        if len(matches) != 1:
            raise AssertionError("expected exactly one row for %r, got %d"
                                 % (content, len(matches)))
        return matches[0]

    def _dst(self, name="dst.sqlite"):
        ledger = SQLiteLedger(self.root / name)
        self.addCleanup(ledger.close)
        return ledger

    # -- tests -----------------------------------------------------------
    def test_fact_exports_complete_claims_list(self):
        record = self._by_content(self._export(), FACT_CONTENT)
        self.assertIn("claims", record)
        self.assertEqual(2, len(record["claims"]), "导出必须包含每一条 active claim")
        self.assertEqual({"port", "theme"},
                         {claim["slot_key"] for claim in record["claims"]})
        values = {claim["slot_key"]: claim["value"] for claim in record["claims"]}
        self.assertEqual({"port": "6432"}, values["port"], "claim value 必须是解码后的 JSON 值")
        self.assertEqual("纸本", values["theme"])
        # 旧平铺字段保留（老导入器只认这一组）。
        self.assertEqual("db.prod", record["subject"])
        self.assertEqual("config", record["predicate"])
        self.assertIn("slot_key", record)
        self.assertIn("value", record)

    def test_export_contains_procedural_context_and_feedback_fields(self):
        rows = self._export()
        fact = self._by_content(rows, FACT_CONTENT)
        self.assertTrue(fact["context"]["goal"])
        self.assertEqual(["db", "migration"], fact["tags"])
        # 账本里存的是「来源置信度 × agent trust」的合成值（0.7*0.9 + 0.3*0.5）。
        self.assertAlmostEqual(0.78, fact["confidence"])
        self.assertEqual(8, fact["importance"])
        self.assertAlmostEqual(0.75, fact["weight"])
        self.assertAlmostEqual(0.4, fact["utility"])
        self.assertEqual("2026-01-01T00:00:00+00:00", fact["valid_from"])
        self.assertEqual("2030-01-01T00:00:00+00:00", fact["valid_until"])
        skill = self._by_content(rows, PROCEDURAL_CONTENT)
        self.assertEqual(["部署"], skill["procedural"]["trigger_pattern"])
        self.assertEqual(["先检查", "再执行"],
                         [step["action"] for step in skill["procedural"]["execution_pipeline"]])

    def test_import_restores_both_claims(self):
        rows = self._export()
        dst = self._dst()
        results = dst.ingest_many(rows)
        self.assertTrue(all(result.inserted for result in results))
        claims = dst.list_claims(scope="shared", state="active")
        self.assertEqual(2, len(claims), "导出的两条 claim 必须全部回灌")
        self.assertEqual({"port", "theme"}, {claim["slot_key"] for claim in claims})
        by_slot = {claim["slot_key"]: claim for claim in claims}
        self.assertEqual({"port": "6432"}, by_slot["port"]["value"])
        self.assertAlmostEqual(0.9, by_slot["port"]["confidence"])

    def test_import_restores_procedural(self):
        dst = self._dst()
        dst.ingest_many(self._export())
        with sqlite3.connect(str(dst.path)) as conn:
            rows = conn.execute(
                "SELECT metadata_json FROM memories WHERE kind='procedural'").fetchall()
        self.assertEqual(1, len(rows), "procedural 记忆必须可回导")
        metadata = json.loads(rows[0][0])
        procedural = metadata["procedural"]
        self.assertEqual(["部署"], procedural["trigger_pattern"])
        self.assertEqual(["有备份"], procedural["preconditions"])
        self.assertEqual(["健康检查通过"], procedural["verification_assertion"])
        self.assertEqual(1.0, procedural["calibration_score"]["rate"])
        self.assertEqual("declarative-only", procedural["execution_policy"])
        hits = dst.search("部署技能", scope="shared", limit=5)
        self.assertIn(PROCEDURAL_CONTENT, [hit["content"] for hit in hits])

    def test_import_restores_context_tags_and_bitemporal_fields(self):
        dst = self._dst()
        dst.ingest_many(self._export())
        with sqlite3.connect(str(dst.path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM memories WHERE content=?",
                               (FACT_CONTENT,)).fetchone()
        self.assertIsNotNone(row)
        metadata = json.loads(row["metadata_json"])
        self.assertEqual(["迁移记忆账本"], metadata["context"]["goal"])
        self.assertEqual(["导入后校验"], metadata["context"]["next_steps"])
        self.assertEqual(["db", "migration"], json.loads(row["tags_json"]))
        self.assertAlmostEqual(0.75, float(row["weight"]))
        self.assertAlmostEqual(0.4, float(row["utility"]))
        self.assertEqual("2030-01-01T00:00:00+00:00", row["valid_until"])

    def test_event_id_is_preserved_across_the_round_trip(self):
        dst = self._dst()
        dst.ingest_many(self._export())
        with sqlite3.connect(str(dst.path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT metadata_json FROM memories WHERE content=?",
                               (FACT_CONTENT,)).fetchone()
        self.assertEqual("fact-evt-1", json.loads(row["metadata_json"])["external_id"])
        re_exported = self._by_content(dst.export_rows(scope="shared"), FACT_CONTENT)
        self.assertEqual("fact-evt-1", re_exported.get("event_id"),
                         "再导出必须保持事件 ID，否则第二轮迁移会换身份")

    def test_causal_parents_are_exported_when_queryable(self):
        record = self._by_content(self._export(), "迁移完成并校验通过")
        self.assertIn("causal_parents", record)
        self.assertEqual(self.parent.memory_id,
                         record["causal_parents"][0]["memory_id"])
        self.assertEqual("caused_by", record["causal_parents"][0]["relation_type"])
        # 跨库导入不能因为父 id 是源库坐标而报错：父不存在时静默跳过。
        dst = self._dst()
        results = dst.ingest_many(self._export())
        self.assertTrue(all(result.inserted for result in results))
        with sqlite3.connect(str(dst.path)) as conn:
            relations = conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]
        self.assertEqual(0, int(relations), "父记忆不在目标库时边被跳过而非崩溃")

    def test_reimport_is_idempotent(self):
        rows = self._export()
        dst = self._dst()
        first = dst.ingest_many(rows)
        second = dst.ingest_many(rows)
        self.assertEqual(4, sum(1 for result in first if result.inserted))
        self.assertEqual(0, sum(1 for result in second if result.inserted),
                         "同一份 JSONL 重导入必须全部幂等")
        self.assertEqual(2, len(dst.list_claims(scope="shared", state="active")))

    def test_old_flat_record_still_imports(self):
        """向后兼容：剥离新键（claims/procedural/...）后等于旧版导出格式。"""
        old_format = []
        for record in self._export():
            if record.get("kind") == "procedural":
                # 旧导出器不带 procedural 本体；kind=procedural 但没有技能卡会被
                # 账本的校验正确拒绝（那是旧格式的已知缺陷，不是回灌格式）。
                continue
            legacy = {key: value for key, value in record.items()
                      if key not in {"claims", "procedural", "causal_parents",
                                     "event_id", "weight", "utility"}}
            old_format.append(legacy)
        dst = self._dst("legacy.sqlite")
        results = dst.ingest_many(old_format)
        self.assertTrue(all(result.inserted for result in results),
                        "旧格式记录必须仍可导入")
        claims = dst.list_claims(scope="shared", state="active")
        self.assertEqual(1, len(claims), "旧格式只带第一组平铺字段 -> 一条 claim")

    def test_export_keeps_only_active_claims(self):
        record = self._by_content(self._export(), FACT_CONTENT)
        slots = {claim["slot_key"] for claim in record["claims"]}
        self.assertNotIn("retired", slots)
        # 从账本下线一条 claim 后，导出不应把它当新断言带回去。
        with sqlite3.connect(str(self.src.path)) as conn:
            conn.execute("UPDATE memory_claims SET state='superseded' "
                         "WHERE memory_id=? AND slot_key='theme'", (self.fact.memory_id,))
        record = self._by_content(self._export(), FACT_CONTENT)
        self.assertEqual(["port"], [claim["slot_key"] for claim in record["claims"]])


if __name__ == "__main__":
    unittest.main()
