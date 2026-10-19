"""MCP hippomind_graph 工具（前景轮八）。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.mcp import MCPAdapter


class McpGraphToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-mcp-graph-")
        self.addCleanup(self.tmp.cleanup)
        self.adapter = MCPAdapter(str(Path(self.tmp.name) / "m.sqlite"),
                                  unsafe_admin=True)
        for i in range(2):
            self.adapter.call("hippomind_ingest", {
                "content": f"service-a 状态 v{i}", "kind": "fact",
                "source_agent": "tester", "evidence": [f"t:{i}"],
                "claims": [{"subject": "service-a", "predicate": "status",
                            "slot_key": "state", "value": {"v": str(i)},
                            "confidence": 0.8}]})

    def test_graph_found_shape(self):
        r = self.adapter.call("hippomind_graph", {"subject": "service-a"})
        self.assertTrue(r["found"])
        self.assertTrue(r["nodes"])
        node = r["nodes"][0]
        self.assertEqual(node["subject"], "service-a")
        self.assertIn("claims", node)
        self.assertIn("conflicts", node)

    def test_graph_not_found(self):
        r = self.adapter.call("hippomind_graph", {"subject": "不存在的主语"})
        self.assertFalse(r["found"])
        self.assertEqual(r["nodes"], [])

    def test_graph_requires_subject(self):
        with self.assertRaises(Exception):
            self.adapter.call("hippomind_graph", {})

    def test_graph_hops_clamped(self):
        r = self.adapter.call("hippomind_graph",
                              {"subject": "service-a", "hops": 99})
        self.assertEqual(r["hops"], 2)

    def test_tools_listing_includes_graph(self):
        from hippomind.mcp import TOOLS
        names = [t["name"] for t in TOOLS]
        self.assertIn("hippomind_graph", names)


if __name__ == "__main__":
    unittest.main()
