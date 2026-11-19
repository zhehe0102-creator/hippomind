"""T2/R3 双向 claim 不变式：claim 跟随 memory，memory 也约束 claim。

## 这条测试锁定的真实数据（2026-10-25 生产账本）

`repair_claim_states()` 原来只保证一个方向：**claim 跟随 memory**——
记忆离开 accepted+active，它的 claim 就转 superseded。缺的是反方向：
记忆**可召回**时，它没有胜者的 claim 不该留在 superseded。

实测 1,576 条 claim 的 memory 是 lifecycle='active'，看上去都违反不变式。
但精确诊断后分成三类，只有一类是 bug：

    1,535 条  quality_state='quarantine'
              记忆本来就不可召回（判据是 accepted AND active），它的 claim
              被打成 superseded 是**正确行为**。round 12 修掉的 1,535 条
              悬空 claim 事故正是这个形状——动了就重演。

       32 条  quality_state='accepted'，但**同 slot 有另一个 memory 的
              active claim**。恢复 = 同一 slot 两条 active claim，会立刻被
              判 conflict 然后两条一起掉出召回，纯负收益。

        9 条  quality_state='accepted'、无胜者、同 slot 无竞争者。
              这才是真违反不变式的，也是唯一安全可恢复的。

所以反向收敛的判据必须四条同时成立，少一条都会造成比现状更糟的状态。
下面每一类都有独立测试，防止将来有人为了「把违反数归零」放宽判据。

## 为什么「不变式能自愈」比「脚本能修」重要

脚本只是一次性收敛存量。真正的修复是 repair_claim_states() 本身：
修好之后，那 9 条**即使不手工恢复**，下一次 repair 也会自动收敛。
test_invariant_self_heals_without_the_script 钉住这一点——如果哪天有人
把反向收敛删掉只留脚本，那条测试会红。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hippomind.ledger import SQLiteLedger


def _claim_record(content, marker, confidence, *, subject="svc", slot_key="port",
                  valid_from="2026-01-01T00:00:00+00:00", valid_until=""):
    record = {
        "kind": "fact", "scope": "shared", "source_agent": "tester",
        "content": content, "evidence": ["evidence:" + marker],
        "subject": subject, "predicate": "config", "slot_key": slot_key,
        "value": {"v": marker}, "confidence": confidence,
        "valid_from": valid_from, "timestamp": valid_from,
    }
    if valid_until:
        record["valid_until"] = valid_until
    return record


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hippo-bidir-")
        self.addCleanup(self._tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self._tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _rows(self, sql, params=()):
        with self.ledger._reading() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _claim_state(self, memory_id):
        rows = self._rows("SELECT state,valid_until FROM memory_claims WHERE memory_id=?",
                          (memory_id,))
        self.assertEqual(1, len(rows), "夹具里这条 memory 应当恰好一条 claim")
        return rows[0]["state"], rows[0]["valid_until"]

    def _set_memory(self, memory_id, **columns):
        assignments = ", ".join("%s=?" % k for k in columns)
        params = tuple(columns.values()) + (memory_id,)

        def write(conn):
            conn.execute("UPDATE memories SET " + assignments + " WHERE memory_id=?", params)

        self.ledger._transaction(write)

    def _set_claim(self, memory_id, **columns):
        assignments = ", ".join("%s=?" % k for k in columns)
        params = tuple(columns.values()) + (memory_id,)

        def write(conn):
            conn.execute("UPDATE memory_claims SET " + assignments + " WHERE memory_id=?", params)

        self.ledger._transaction(write)

    def _violations(self):
        """当前违反反向不变式的 claim 数（判据与 repair 内保持一致）。"""
        return self._rows("""
            SELECT COUNT(*) n FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id
            WHERE c.state='superseded' AND c.valid_until=''
              AND m.superseded_by='' AND m.quality_state='accepted' AND m.lifecycle='active'
              AND NOT EXISTS (
                  SELECT 1 FROM memory_claims o
                  WHERE o.claim_id != c.claim_id
                    AND o.scope=c.scope AND o.subject=c.subject
                    AND o.predicate=c.predicate AND o.slot_key=c.slot_key
                    AND o.state IN ('active','conflict'))""")[0]["n"]

class ReverseInvariantTests(_Base):
    """memory 可召回时，无胜者的 superseded claim 必须被恢复。"""

    def test_orphan_claim_on_recallable_memory_is_reactivated(self):
        result = self.ledger.ingest(_claim_record("孤立事实 1111", "1111", 0.9))
        self._set_claim(result.memory_id, state="superseded")
        self.assertEqual(1, self._violations())

        report = self.ledger.repair_claim_states()
        self.assertEqual(1, report["reactivated_superseded"])
        self.assertEqual("active", self._claim_state(result.memory_id)[0])
        self.assertEqual(0, self._violations())

    def test_claim_with_a_winner_is_never_reactivated(self):
        """有胜者就是真让位，复活等于推翻裁决。"""
        loser = self.ledger.ingest(_claim_record("败者 2222", "2222", 0.5))
        winner = self.ledger.ingest(_claim_record("胜者 3333", "3333", 0.95))
        # 高分覆盖后 loser 的 memory 会整体下线；这里把它恢复成可召回，
        # 模拟「memory 恢复了但 claim 仍带 superseded_by」的形态。
        self._set_memory(loser.memory_id, lifecycle="active", quality_state="accepted",
                         superseded_by=winner.memory_id)
        report = self.ledger.repair_claim_states()
        self.assertEqual(0, report["reactivated_superseded"],
                         "有胜者的 claim 被复活了，等于推翻裁决")
        self.assertEqual("superseded", self._claim_state(loser.memory_id)[0])

    def test_quarantined_memory_claims_stay_superseded(self):
        """round 12 事故的形状：quarantine+active 的记忆不可召回，不能复活。

        生产上 1,576 条里有 1,535 条是这一种。判据里写 accepted 而不是只看
        lifecycle，就是为了挡住它们——只看 lifecycle 会把 1,535 条全放进来，
        重演「悬空 claim」事故。
        """
        result = self.ledger.ingest(_claim_record("隔离事实 4444", "4444", 0.9))
        self._set_claim(result.memory_id, state="superseded")
        self._set_memory(result.memory_id, quality_state="quarantine")
        report = self.ledger.repair_claim_states()
        self.assertEqual(0, report["reactivated_superseded"],
                         "quarantine 记忆的 claim 被复活了（悬空 claim 事故重演）")
        self.assertEqual("superseded", self._claim_state(result.memory_id)[0])

    def test_claim_with_a_live_rival_is_not_reactivated(self):
        """同 slot 有竞争者时恢复 = 造冲突，两条一起掉出召回。"""
        a = self.ledger.ingest(_claim_record("甲 5555", "5555", 0.5))
        self.ledger.ingest(_claim_record("乙 6666", "6666", 0.52))
        conflicts = self.ledger.list_claims(state="conflict", scope="shared")
        self.assertEqual(2, len(conflicts), "夹具没造出 conflict")
        # 把 a 的 claim 打成 superseded，让 b 保持 conflict（同 slot 有活竞争者）。
        self._set_claim(a.memory_id, state="superseded", valid_until="")
        self._set_memory(a.memory_id, lifecycle="active", quality_state="accepted")
        report = self.ledger.repair_claim_states()
        self.assertEqual(0, report["reactivated_superseded"],
                         "同 slot 还有 conflict 竞争者，却把另一条复活了")

    def test_closed_window_is_not_reactivated(self):
        """valid_until 已闭合是时间语义（事实确实过期），不是状态错乱。"""
        result = self.ledger.ingest(_claim_record("过期事实 7777", "7777", 0.9))
        self._set_claim(result.memory_id, state="superseded",
                        valid_until="2026-02-01T00:00:00+00:00")
        report = self.ledger.repair_claim_states()
        self.assertEqual(0, report["reactivated_superseded"],
                         "窗口已闭合的 claim 被复活了（已失效事实重新可召回）")

    def test_invariant_self_heals_without_the_script(self):
        """**核心**：真正的修复是不变式本身，脚本只是收敛存量。

        如果哪天有人把反向收敛删掉、只留一次性脚本，这条会红。
        """
        ids = []
        for i in range(3):
            r = self.ledger.ingest(_claim_record("自愈样本 %d 888%d" % (i, i), "88%d" % i, 0.9,
                                                subject="selfheal", slot_key="k%d" % i))
            ids.append(r.memory_id)
            self._set_claim(r.memory_id, state="superseded")
        self.assertEqual(3, self._violations())

        # 不调用任何脚本，只调 repair 本身。
        report = self.ledger.repair_claim_states()
        self.assertEqual(3, report["reactivated_superseded"])
        for mid in ids:
            self.assertEqual("active", self._claim_state(mid)[0])
        self.assertEqual(0, self._violations())

    def test_repair_is_idempotent(self):
        """跑两次第二次不该改动任何行。"""
        r = self.ledger.ingest(_claim_record("幂等样本 9999", "9999", 0.9))
        self._set_claim(r.memory_id, state="superseded")
        first = self.ledger.repair_claim_states()
        self.assertEqual(1, first["reactivated_superseded"])
        second = self.ledger.repair_claim_states()
        self.assertEqual(0, second["reactivated_superseded"],
                         "第二次 repair 还在改行，不是幂等")
        self.assertEqual(0, second["superseded"])
        self.assertEqual(0, second["conflict"])
        self.assertEqual(0, second["reactivated"])


class ForwardInvariantStillWorksTests(_Base):
    """反向收敛不能破坏原来的正向收敛（claim 跟随 memory）。"""

    def test_unrecallable_memory_claim_is_superseded(self):
        result = self.ledger.ingest(_claim_record("冷置事实 1234", "1234", 0.9))
        self._set_memory(result.memory_id, lifecycle="cold")
        report = self.ledger.repair_claim_states()
        self.assertGreaterEqual(report["superseded"], 1)
        state, valid_until = self._claim_state(result.memory_id)
        self.assertEqual("superseded", state)
        self.assertNotEqual("", valid_until, "正向收敛必须闭合窗口")

    def test_both_directions_hold_after_one_pass(self):
        """一次 repair 之后，两个方向同时成立。"""
        good = self.ledger.ingest(_claim_record("可召回 2222", "2222", 0.9))
        bad = self.ledger.ingest(_claim_record("不可召回 3333", "3333", 0.9,
                                               subject="other", slot_key="k2"))
        self._set_claim(good.memory_id, state="superseded")
        self._set_memory(bad.memory_id, quality_state="quarantine")

        self.ledger.repair_claim_states()
        self.assertEqual("active", self._claim_state(good.memory_id)[0])
        self.assertEqual("superseded", self._claim_state(bad.memory_id)[0])
        # 没有悬空 claim（active 的 claim 必须属于可召回记忆）
        dangling = self._rows("""
            SELECT COUNT(*) n FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id
            WHERE c.state='active' AND (m.quality_state!='accepted' OR m.lifecycle!='active')""")[0]["n"]
        self.assertEqual(0, dangling)
        self.assertEqual(0, self._violations())


class OrphanClaimsScriptTests(_Base):
    """一次性收敛脚本：默认 dry-run、apply 前备份、幂等。"""

    def _script(self):
        import sys
        root = Path(__file__).resolve().parents[1]
        if str(root / "scripts") not in sys.path:
            sys.path.insert(0, str(root / "scripts"))
        import repair_orphan_claims
        return repair_orphan_claims

    def _run_quietly(self, fn, argv):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            return fn(argv)

    def test_dry_run_is_default_and_writes_nothing(self):
        script = self._script()
        r = self.ledger.ingest(_claim_record("脚本样本 4444", "4444", 0.9))
        self._set_claim(r.memory_id, state="superseded")
        code = self._run_quietly(script.main, ["--ledger", str(self.ledger.path)])
        self.assertEqual(0, code)
        self.assertEqual("superseded", self._claim_state(r.memory_id)[0],
                         "dry-run 竟然写入了")

    def test_apply_reactivates_and_backs_up(self):
        script = self._script()
        r = self.ledger.ingest(_claim_record("脚本 apply 5555", "5555", 0.9))
        self._set_claim(r.memory_id, state="superseded")
        backup_dir = Path(self._tmp.name) / "backups"
        code = self._run_quietly(script.main, ["--ledger", str(self.ledger.path),
                                               "--apply", "--backup-dir", str(backup_dir)])
        self.assertEqual(0, code)
        self.assertEqual("active", self._claim_state(r.memory_id)[0])
        backups = list(backup_dir.glob("*.sqlite"))
        self.assertEqual(1, len(backups), "apply 前没有做备份")
        self.assertGreater(backups[0].stat().st_size, 0)

    def test_script_is_idempotent(self):
        script = self._script()
        r = self.ledger.ingest(_claim_record("脚本幂等 6666", "6666", 0.9))
        self._set_claim(r.memory_id, state="superseded")
        backup_dir = Path(self._tmp.name) / "bk"
        self._run_quietly(script.main, ["--ledger", str(self.ledger.path),
                                        "--apply", "--backup-dir", str(backup_dir)])
        snapshot = self._claim_state(r.memory_id)
        self._run_quietly(script.main, ["--ledger", str(self.ledger.path),
                                        "--apply", "--backup-dir", str(backup_dir)])
        self.assertEqual(snapshot, self._claim_state(r.memory_id))
        self.assertEqual(1, len(list(backup_dir.glob("*.sqlite"))),
                         "幂等的 apply 不该再产生备份")

    def test_script_skips_quarantine_and_rival_cases(self):
        """脚本的判据必须和 repair_claim_states 完全一致。"""
        script = self._script()
        quar = self.ledger.ingest(_claim_record("隔离 7777", "7777", 0.9,
                                                subject="q", slot_key="k1"))
        rival_a = self.ledger.ingest(_claim_record("竞争甲 8888", "8888", 0.5,
                                                   subject="r", slot_key="k2"))
        self.ledger.ingest(_claim_record("竞争乙 9999", "9999", 0.52,
                                         subject="r", slot_key="k2"))
        self._set_claim(quar.memory_id, state="superseded")
        self._set_memory(quar.memory_id, quality_state="quarantine")
        self._set_claim(rival_a.memory_id, state="superseded", valid_until="")
        self._set_memory(rival_a.memory_id, lifecycle="active", quality_state="accepted")

        code = self._run_quietly(script.main, ["--ledger", str(self.ledger.path),
                                               "--apply",
                                               "--backup-dir", str(Path(self._tmp.name) / "b2")])
        self.assertEqual(0, code)
        self.assertEqual("superseded", self._claim_state(quar.memory_id)[0],
                         "脚本动了 quarantine 记忆的 claim")
        self.assertEqual("superseded", self._claim_state(rival_a.memory_id)[0],
                         "脚本复活了有 slot 竞争者的 claim")


if __name__ == "__main__":
    unittest.main()
