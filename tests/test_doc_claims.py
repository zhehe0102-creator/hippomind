"""Numbers the docs promise must match what the code actually provides.

Documented counts rot silently: README claimed 38 CLI subcommands (there are 37),
FEATURES.md still said schema v7 after the live ledger had moved to v8, and both
quoted a test total ("218+ 项全绿") that was already wrong on the day it was
written.  This is the same failure mode as 0e7e9c9/afb1d27 -- configuration or
prose asserting more than the tree delivers -- so it gets the same cheap guard.

Deliberately does NOT assert a test-suite count: that number changes every
session and pinning it only re-creates the rot it is meant to prevent.
"""

from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


class DocClaimTests(unittest.TestCase):
    def setUp(self):
        from hippomind.cli import build_parser
        parser = build_parser()
        action = next(a for a in parser._actions
                      if a.__class__.__name__ == "_SubParsersAction")
        self.commands = set(action.choices.keys())

    def test_documented_subcommand_count_matches(self):
        text = _read("docs/FEATURES.md")
        claimed = re.search(r"CLI 全部子命令（(\d+) 个）", text)
        self.assertIsNotNone(claimed, "FEATURES.md 的子命令计数标题被改写了")
        self.assertEqual(int(claimed.group(1)), len(self.commands),
                         f"文档说 {claimed.group(1)} 个，实际 {len(self.commands)} 个；改代码就同步改文档")

    def test_every_documented_command_exists(self):
        """The command table lists names that must all be real subcommands."""
        text = _read("docs/FEATURES.md")
        listed = set()
        for row in re.findall(r"^\| `([a-z][a-z0-9-]*)`(?:`?[^|]*?)?\|", text, re.M):
            listed.add(row.strip("`"))
        for row in re.findall(r"^\| `([a-z][a-z0-9-]*)` / `([a-z][a-z0-9-]*)` \|", text, re.M):
            listed.update(row)
        self.assertTrue(listed, "没有解析到任何被文档化的命令，守卫本身失效了")
        unknown = sorted(name for name in listed if name not in self.commands)
        self.assertEqual([], unknown, f"文档提到不存在的子命令: {unknown}")

    def test_documented_mcp_tool_count_matches(self):
        from hippomind import mcp
        text = _read("docs/FEATURES.md")
        claimed = re.search(r"MCP 工具（(\d+) 个", text)
        self.assertIsNotNone(claimed, "FEATURES.md 的 MCP 工具计数标题被改写了")
        self.assertEqual(int(claimed.group(1)), len(mcp.TOOLS))
        for tool in mcp.TOOLS:
            self.assertIn(tool["name"], text, f"MCP 工具 {tool['name']} 未被文档列出")

    def test_documented_schema_version_matches(self):
        from hippomind.ledger import SCHEMA_VERSION
        text = _read("docs/FEATURES.md")
        stale = re.findall(r"schema v(\d+)", text)
        self.assertTrue(stale, "FEATURES.md 不再声明 schema 版本，守卫失效")
        for value in stale:
            self.assertEqual(int(value), SCHEMA_VERSION,
                             f"文档写 schema v{value}，代码是 v{SCHEMA_VERSION}；"
                             "版本漂移曾让 150 万条记忆无代码可读")

    def test_docs_do_not_hardcode_test_totals(self):
        """A test count in prose rots within hours; it was removed on purpose."""
        offenders = []
        for rel in ("README.md", "docs/FEATURES.md", "USAGE_GUIDE.md"):
            if not os.path.exists(os.path.join(ROOT, rel)):
                continue
            for line in _read(rel).splitlines():
                if re.search(r"\d{2,4}\+? ?项全绿", line):
                    offenders.append(f"{rel}: {line.strip()[:70]}")
        self.assertEqual([], offenders, "请勿在文档写死测试条数：" + "; ".join(offenders))


if __name__ == "__main__":
    unittest.main()
