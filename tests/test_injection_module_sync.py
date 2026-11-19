"""审计必须验证"代码在那儿"，不只是"路径能解析"。

2026-10-25 的真实失败：claude hook 装进 settings.json 后，命令、路径、账本
全部正确，审计报 healthy；但真跑起来立刻
No module named hippomind.claude_hook —— 因为内置盘 runtime 副本是
rsync --delete 出来的，新增模块没同步进去。

"路径能解析"和"模块能导入"是两件事。只查前者的审计会把整类失败报成健康，
而失败是静默的（hook 崩了，agent 只是没有记忆，看起来像"这次没找到相关内容"）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.injection_audit import (HEALTHY, NOT_SYNCED, _module_missing,
                                       inspect_claude_hook, inspect_codex_hook)


class ModulePresenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.runtime = self.home / "runtime"
        (self.runtime / "hippomind").mkdir(parents=True)
        self.ledger = self.home / "hippomind.sqlite"
        self.ledger.write_bytes(b"SQLite format 3")
        self.config = self.home / "claude.json"
        self.config.write_text("{}", encoding="utf-8")

    def _write_settings(self, command: str) -> Path:
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": command}]}]}}), encoding="utf-8")
        return path

    def test_missing_module_is_detected(self):
        root = _module_missing(
            "cd %s && /usr/bin/python3 -m hippomind.claude_hook --managed" % self.runtime,
            module="hippomind.claude_hook")
        self.assertEqual(str(self.runtime), root)

    def test_present_module_is_not_reported(self):
        (self.runtime / "hippomind" / "claude_hook.py").write_text("", encoding="utf-8")
        root = _module_missing(
            "cd %s && PYTHONPATH=%s /usr/bin/python3 -m hippomind.claude_hook"
            % (self.runtime, self.runtime), module="hippomind.claude_hook")
        self.assertEqual("", root)

    def test_quoted_root_with_spaces_is_still_checked(self):
        """真实命令里 runtime 在 Application Support 下，含空格但加了引号。"""
        spaced = self.home / "Library" / "Application Support" / "HippoMind" / "runtime"
        (spaced / "hippomind").mkdir(parents=True)
        root = _module_missing(
            "cd '%s' && PYTHONPATH='%s' /usr/bin/python3 -m hippomind.claude_hook"
            % (spaced, spaced), module="hippomind.claude_hook")
        self.assertEqual(str(spaced), root)

    def test_no_pinned_root_is_not_a_failure(self):
        """没有 cd/PYTHONPATH 的命令无法判定，不得凭空报故障。"""
        self.assertEqual("", _module_missing("/usr/bin/python3 -m hippomind.claude_hook",
                                             module="hippomind.claude_hook"))

    def test_claude_audit_flags_unsynced_runtime(self):
        (self.runtime / "hippomind").mkdir(exist_ok=True)
        path = self._write_settings(
            "cd %s && PYTHONPATH=%s /usr/bin/python3 -m hippomind.claude_hook "
            "--managed --agent-config %s" % (self.runtime, self.runtime, self.config))
        finding = inspect_claude_hook(path, ledger_path=self.ledger)
        self.assertEqual(NOT_SYNCED, finding.status)
        self.assertIn("claude_hook.py", finding.detail)

    def test_claude_audit_is_healthy_once_the_module_is_synced(self):
        (self.runtime / "hippomind" / "claude_hook.py").write_text("", encoding="utf-8")
        path = self._write_settings(
            "cd %s && PYTHONPATH=%s /usr/bin/python3 -m hippomind.claude_hook "
            "--managed --agent-config %s" % (self.runtime, self.runtime, self.config))
        self.assertEqual(HEALTHY, inspect_claude_hook(path, ledger_path=self.ledger).status)

    def test_codex_hook_has_the_same_check(self):
        """同一类失败在 codex 上也存在，必须同样被拦下。"""
        hooks = self.home / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True, exist_ok=True)
        hooks.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{
            "type": "command",
            "command": ("cd %s && PYTHONPATH=%s /usr/bin/python3 -m hippomind.codex_hook "
                        "--managed --agent-config %s"
                        % (self.runtime, self.runtime, self.config))}]}]}}), encoding="utf-8")
        finding = inspect_codex_hook(hooks, ledger_path=self.ledger)
        self.assertEqual(NOT_SYNCED, finding.status)
        (self.runtime / "hippomind" / "codex_hook.py").write_text("", encoding="utf-8")
        self.assertEqual(HEALTHY, inspect_codex_hook(hooks, ledger_path=self.ledger).status)


if __name__ == "__main__":
    unittest.main()
