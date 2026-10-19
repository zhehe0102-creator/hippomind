"""MCP 新增工具：recall/pin/forget/conflicts（前景轮）。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.mcp import MCPAdapter


class McpNewToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-mcp-test-")
        self.addCleanup(self.tmp.cleanup)
        self.adapter = MCPAdapter(str(Path(self.tmp.name) / "m.sqlite"),
                                  unsafe_admin=True)
        r = self.adapter.call("hippomind_ingest", {
            "content": "MCP 工具测试记忆", "kind": "fact",
            "source_agent": "tester", "evidence": ["t:1"]})
        self.mid = r["memory_id"]

    def test_recall_returns_hits_without_raw(self):
        r = self.adapter.call("hippomind_recall", {"query": "MCP 工具测试"})
        self.assertTrue(r["results"])
        self.assertEqual(r["results"][0]["memory_id"], self.mid)
        self.assertNotIn("raw_chat", r["results"][0])

    def test_pin_unpin_roundtrip(self):
        r = self.adapter.call("hippomind_pin", {"memory_id": self.mid})
        self.assertTrue(r["pinned"])
        r = self.adapter.call("hippomind_pin",
                              {"memory_id": self.mid, "unpin": True})
        self.assertFalse(r["pinned"])

    def test_forget_reports_impact(self):
        r = self.adapter.call("hippomind_forget",
                              {"memory_ids": [self.mid]})
        self.assertEqual(r["forgotten"], 1)
        self.assertIn("impact", r)

    def test_conflicts_shape(self):
        r = self.adapter.call("hippomind_conflicts", {})
        self.assertIn("items", r)
        self.assertIn("total", r)

    def test_read_only_policy_cannot_write(self):
        ro = MCPAdapter(str(Path(self.tmp.name) / "m.sqlite"))
        with self.assertRaises(Exception):
            ro.call("hippomind_forget", {"memory_ids": ["mem_x"]})


if __name__ == "__main__":
    unittest.main()
