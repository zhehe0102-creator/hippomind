"""并发写 claim 一致性（迭代 41）：多线程同槽位 ingest 的裁决行为。

不变量：同一 (scope,subject,predicate,slot_key) 槽位组最终至多一个
active claim 且其记忆为 accepted；其余记忆进入 superseded/conflict，
不丢失事件、不崩溃、检索不返回多个互相矛盾的活跃事实。
"""
import tempfile
import threading
import unittest
from pathlib import Path

from hippomind.ledger import SQLiteLedger


class ConcurrentClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "cc.sqlite")

    def _ingest_claim(self, value: str, confidence: float, worker: int):
        return self.led.ingest({
            "kind": "fact", "scope": "shared", "source_agent": f"w{worker}",
            "content": f"生产数据库端口是 {value}",
            "evidence": [f"cc:{worker}:{value}"],
            "claims": [{"subject": "db.prod", "predicate": "config",
                        "slot_key": "port", "value": {"port": value},
                        "confidence": confidence}],
        })

    def test_parallel_same_slot_writes_converge(self):
        errors: list = []
        results: list = []
        barrier = threading.Barrier(8)

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                # 置信度分层：0.9 一条、0.5 六条、0.95 一条——预期高分者胜出
                conf = 0.9 if i == 0 else (0.95 if i == 7 else 0.5)
                results.append((i, self._ingest_claim(f"{6000 + i}", conf, i)))
            except Exception as exc:  # pragma: no cover - 诊断用
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"并发写不得抛错: {errors}")
        self.assertEqual(len(results), 8, "不得丢写")

        # 槽位组终态：至多一个 active claim，且其记忆 accepted
        claims = self.led.list_claims(scope="shared", limit=100)
        active = [c for c in claims if c["state"] == "active"
                  and c.get("subject") == "db.prod"
                  and c.get("slot_key") == "port"]
        self.assertLessEqual(len(active), 1,
                             f"同槽位组不得有多个活跃 claim: {len(active)}")
        if active:
            mem = self.led.get_memory(active[0]["memory_id"])
            self.assertIsNotNone(mem)
            self.assertEqual(mem["quality_state"], "accepted")

        # 检索端口事实：最多一条活跃结果（其余已下线）
        hits = self.led.search("生产数据库端口", limit=20)
        live = [h for h in hits if h["quality_state"] == "accepted"
                and h["lifecycle"] == "active"]
        self.assertLessEqual(len(live), 1,
                             f"检索不得同时返回矛盾活跃事实: {len(live)}")

    def test_sequential_upsert_higher_confidence_supersedes(self):
        low = self._ingest_claim("5432", 0.55, 1)
        high = self._ingest_claim("6432", 0.95, 2)
        claims = self.led.list_claims(scope="shared", limit=10)
        states = {c["memory_id"]: c["state"] for c in claims}
        self.assertEqual(states.get(high.memory_id), "active")
        self.assertNotEqual(states.get(low.memory_id), "active")
        hits = self.led.search("生产数据库端口", limit=5)
        self.assertTrue(any("6432" in h["content"] for h in hits))
        self.assertFalse(any(h["content"].endswith("5432") and
                             h["quality_state"] == "accepted" and
                             h["lifecycle"] == "active" for h in hits))


if __name__ == "__main__":
    unittest.main()
