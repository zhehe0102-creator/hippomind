"""scope 隔离审计矩阵（迭代 37）：跨 scope/task/project/agent 检索零泄漏。

参数化矩阵：每个过滤维度组合下，检索结果必须只含匹配行——任何跨维度
泄漏都是安全缺陷（多 Agent 共库的信任边界）。
"""
import itertools
import tempfile
import unittest
from pathlib import Path

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
from hippomind.ledger import SQLiteLedger

SCOPES = ("scope-alpha", "scope-beta")
TASKS = ("task-1", "task-2")
PROJECTS = ("proj-x", "proj-y")
AGENTS = ("agent-red", "agent-blue")


def _rec(text: str, *, scope: str, task: str, project: str, agent: str) -> dict:
    return {"kind": "fact", "scope": scope, "source_agent": agent,
            "task_id": task, "project": project,
            "content": text, "evidence": [f"iso:{text}"]}


class ScopeIsolationMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "iso.sqlite")
        # 每个维度组合一条可识别记忆（内容含全部维度词，保证查询能命中）
        for scope, task, project, agent in itertools.product(
                SCOPES, TASKS, PROJECTS, AGENTS):
            text = f"隔离标记 {scope} {task} {project} {agent} 的专属事实"
            self.led.ingest(_rec(text, scope=scope, task=task,
                                 project=project, agent=agent))

    def test_search_matrix_zero_leakage(self):
        for scope, task, project, agent in itertools.product(
                SCOPES, TASKS, PROJECTS, AGENTS):
            hits = self.led.search("隔离标记 专属事实", scope=scope, task_id=task,
                                   project=project, source_agents=[agent],
                                   limit=50)
            for hit in hits:
                self.assertEqual(hit["scope"], scope,
                                 f"scope 泄漏: {hit['content']}")
                self.assertEqual(hit["task_id"], task,
                                 f"task 泄漏: {hit['content']}")
                self.assertEqual(hit["project"], project,
                                 f"project 泄漏: {hit['content']}")
                self.assertEqual(hit["source_agent"], agent,
                                 f"agent 泄漏: {hit['content']}")

    def test_scope_boundary_hard(self):
        """scope 是硬边界：alpha 的查询绝不返回 beta 的任何行。"""
        alpha_hits = self.led.search("隔离标记", scope="scope-alpha", limit=100)
        self.assertTrue(alpha_hits)
        self.assertTrue(all(h["scope"] == "scope-alpha" for h in alpha_hits))
        beta_hits = self.led.search("隔离标记", scope="scope-beta", limit=100)
        self.assertTrue(beta_hits)
        self.assertTrue(all(h["scope"] == "scope-beta" for h in beta_hits))

    def test_context_pack_respects_source_filter(self):
        builder = ContextPackBuilder(self.led)
        opts = ContextPackOptions(scope="scope-alpha",
                                  source_agents=("agent-red",)).normalise()
        pack = builder.build("隔离标记 专属事实", options=opts)
        blob = str(pack)
        self.assertNotIn("scope-beta", blob, "context-pack 泄漏了 beta scope")
        self.assertNotIn("agent-blue", blob, "context-pack 泄漏了 blue agent")

    def test_pinned_memory_does_not_cross_scope(self):
        """置顶记忆也不得跨越 scope 边界（迭代33 加权不破坏隔离）。"""
        target = None
        for hit in self.led.search("隔离标记", scope="scope-beta", limit=100):
            if hit["source_agent"] == "agent-blue":
                target = hit["memory_id"]
                break
        self.assertIsNotNone(target)
        self.led.pin_memory(target, True)
        alpha_hits = self.led.search("隔离标记", scope="scope-alpha", limit=100)
        self.assertFalse(any(h["memory_id"] == target for h in alpha_hits),
                         "置顶记忆泄漏进了其他 scope")


if __name__ == "__main__":
    unittest.main()
