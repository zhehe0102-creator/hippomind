"""文档-代码一致性防漂移（前景轮九审查修复 #3 的回归锁定）。"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.mcp import TOOLS


class DocsConsistencyTests(unittest.TestCase):
    def test_readme_mcp_tool_count_matches_implementation(self):
        """README 声称的工具数必须等于 TOOLS 实际长度。"""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        matches = re.findall(r"等 (\d+) 个工具", readme)
        self.assertTrue(matches, "README 应包含「等 N 个工具」表述")
        claimed = int(matches[0])
        self.assertEqual(
            claimed, len(TOOLS),
            f"README 写 {claimed} 个工具，实际 TOOLS={len(TOOLS)}——"
            "新增 MCP 工具时请同步更新 README")

    def test_features_doc_lists_every_tool(self):
        """FEATURES.md 必须逐一列出全部 MCP 工具名。"""
        features = (ROOT / "docs" / "FEATURES.md").read_text(encoding="utf-8")
        for tool in TOOLS:
            self.assertIn(tool["name"], features,
                          f"FEATURES.md 缺少工具 {tool['name']}")


if __name__ == "__main__":
    unittest.main()
