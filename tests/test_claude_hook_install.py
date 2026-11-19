"""Claude 注入面的检测、安装与修复。

为什么这些测试必须存在：注入面事故的形态是"看起来装好了"——配置文件里有
hippomind 字样、状态命令报"已安装"，但那条路径现在根本读不到账本。所以这里
断言的都不是"文件里有没有字符串"，而是三条可机器判定的性质：

1. 命令里引用的每个绝对路径现在都能解析（且不在可移动卷/临时目录上）；
2. 重复的托管 handler 会被收敛成一条（历史正则修补每跑一次多叠一层）；
3. settings.json 里用户自己的键一个字节都不动。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from hippomind.agent_install import (claude_settings_path,
                                     claude_user_prompt_hook_status,
                                     install_claude_user_prompt_hook)
from hippomind.injection_audit import (HEALTHY, MISSING, STALE_PATH,
                                       inspect_claude_hook)
from hippomind.injection_repair import apply_repairs, repair_claude_hook


class ClaudeHookCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = self.home / "hippomind.sqlite"
        self.ledger.write_bytes(b"SQLite format 3")
        self.settings = claude_settings_path(home=self.home)
        self.settings.parent.mkdir(parents=True)
        self.config = self.home / "agents" / "claude.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("{}", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _write(self, payload) -> Path:
        self.settings.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self.settings

    def _handlers(self):
        document = json.loads(self.settings.read_text("utf-8"))
        return [handler for group in document["hooks"]["UserPromptSubmit"]
                for handler in group.get("hooks", [])]


class ClaudeHookInstallTests(ClaudeHookCase):
    def test_install_preserves_every_other_setting_and_handler(self):
        self._write({
            "model": "zcode/GLM-5.3-Flash[1m]",
            "env": {"ANTHROPIC_MODEL": "deepseek-v4.1-flash"},
            "permissions": {"allow": ["Bash"]},
            "hooks": {
                "UserPromptSubmit": [{"hooks": [
                    {"type": "command", "command": "other-recorder --stdin"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "keep-me"}]}],
            },
        })
        first = install_claude_user_prompt_hook(Path("/opt/hippomind"), self.config,
                                                home=self.home)
        second = install_claude_user_prompt_hook(Path("/opt/hippomind"), self.config,
                                                 home=self.home)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"], "安装必须幂等，否则每次 install 都改盘")

        document = json.loads(self.settings.read_text("utf-8"))
        self.assertEqual("zcode/GLM-5.3-Flash[1m]", document["model"])
        self.assertEqual({"ANTHROPIC_MODEL": "deepseek-v4.1-flash"}, document["env"])
        self.assertEqual({"allow": ["Bash"]}, document["permissions"])
        self.assertEqual("keep-me",
                         document["hooks"]["Stop"][0]["hooks"][0]["command"])
        commands = [handler.get("command", "") for handler in self._handlers()]
        self.assertEqual(1, sum("hippomind.claude_hook" in c for c in commands))
        self.assertTrue(any(c == "other-recorder --stdin" for c in commands),
                        "用户自己的 hook 不能被删")

    def test_install_is_idempotent_and_status_reports_one(self):
        self._write({})
        install_claude_user_prompt_hook(Path("/opt/hippomind"), self.config, home=self.home)
        install_claude_user_prompt_hook(Path("/opt/hippomind"), self.config, home=self.home)
        status = claude_user_prompt_hook_status(home=self.home)
        self.assertTrue(status["installed"])
        self.assertEqual(1, status["count"])

    def test_install_creates_the_file_when_absent(self):
        self.assertFalse(self.settings.exists())
        result = install_claude_user_prompt_hook(Path("/opt/hippomind"), self.config,
                                                 home=self.home)
        self.assertTrue(result["installed"])
        self.assertTrue(self.settings.exists())
        self.assertEqual(0o600, self.settings.stat().st_mode & 0o777)

    def test_install_collapses_pre_existing_duplicates(self):
        """历史上正则修补叠加出的多条坏命令必须收敛，而不是再加一条。"""
        self._write({"hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "python3 -m hippomind.claude_hook --managed --agent-config /tmp/a.json"},
            {"type": "command", "command": "python3 -m hippomind.claude_hook --managed --agent-config /tmp/b.json"},
        ]}]}})
        result = install_claude_user_prompt_hook(Path("/opt/hippomind"), self.config,
                                                 home=self.home)
        self.assertEqual(1, result["removed_duplicates"])
        self.assertEqual(1, len(self._handlers()))

    def test_install_refuses_a_non_object_root(self):
        self.settings.write_text("[]", encoding="utf-8")
        from hippomind.ledger import LedgerError
        with self.assertRaises(LedgerError):
            install_claude_user_prompt_hook(Path("/opt/hippomind"), self.config,
                                            home=self.home)


class ClaudeHookAuditTests(ClaudeHookCase):
    def test_missing_file_and_unregistered_file(self):
        self.assertEqual(MISSING, inspect_claude_hook(self.settings,
                                                      ledger_path=self.ledger).status)
        self._write({"hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "other"}]}]}})
        self.assertEqual(MISSING, inspect_claude_hook(self.settings,
                                                      ledger_path=self.ledger).status)

    def test_healthy_hook_resolves_its_paths(self):
        install_claude_user_prompt_hook(Path("/opt/hippomind"), self.config, home=self.home)
        finding = inspect_claude_hook(self.settings, ledger_path=self.ledger)
        self.assertEqual(HEALTHY, finding.status, finding.detail)

    def test_temporary_agent_config_is_stale(self):
        """安装期临时目录重启即失效——这是那次事故的确切形态。"""
        self._write({"hooks": {"UserPromptSubmit": [{"hooks": [{
            "type": "command",
            "command": ("cd /tmp && /usr/bin/python3 -m hippomind.claude_hook "
                        "--managed --agent-config /var/folders/xx/yyy/T/tmpabc/claude.json"),
        }]}]}})
        finding = inspect_claude_hook(self.settings, ledger_path=self.ledger)
        self.assertEqual(STALE_PATH, finding.status)

    def test_paths_with_spaces_are_not_cut_in_half(self):
        """'Application Support' 含空格，按空白切会切出半截路径 → 假阳性。"""
        spaced = self.home / "Library" / "Application Support" / "HippoMind"
        (spaced / "runtime" / "hippomind").mkdir(parents=True)
        spaced = spaced / "runtime"
        (spaced / "hippomind" / "claude_hook.py").write_text("", encoding="utf-8")
        config = spaced / "agents" / "claude.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}", encoding="utf-8")
        self._write({"hooks": {"UserPromptSubmit": [{"hooks": [{
            "type": "command",
            "command": ("cd '%s' && /usr/bin/python3 -m hippomind.claude_hook "
                        "--managed --agent-config '%s'" % (spaced, config)),
        }]}]}})
        finding = inspect_claude_hook(self.settings, ledger_path=self.ledger)
        self.assertEqual(HEALTHY, finding.status, finding.detail)

    def test_duplicate_managed_hooks_are_reported_not_ignored(self):
        self._write({"hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "python3 -m hippomind.claude_hook --managed --agent-config /tmp/a.json"},
            {"type": "command", "command": "python3 -m hippomind.claude_hook --managed --agent-config /tmp/b.json"},
        ]}]}})
        finding = inspect_claude_hook(self.settings, ledger_path=self.ledger)
        self.assertEqual(STALE_PATH, finding.status)
        self.assertIn("重复", finding.detail)


class ClaudeHookRepairTests(ClaudeHookCase):
    def test_repair_rewrites_only_the_managed_handler(self):
        self._write({
            "model": "keep",
            "hooks": {"UserPromptSubmit": [{"hooks": [
                {"type": "command",
                 "command": "python3 -m hippomind.claude_hook --managed --agent-config /tmp/old.json"},
                {"type": "command", "command": "other"},
            ]}]},
        })
        record = repair_claude_hook(self.settings, package_root=Path("/opt/hippomind"),
                                    agent_config=self.config, python="/usr/bin/python3",
                                    ledger_path=self.ledger)
        self.assertTrue(record["changed"])
        results = apply_repairs([record], dry_run=False)
        self.assertTrue(results[0]["applied"])
        document = json.loads(self.settings.read_text("utf-8"))
        self.assertEqual("keep", document["model"])
        commands = [handler.get("command", "") for handler in self._handlers()]
        self.assertNotIn("/tmp/old.json", " ".join(commands))
        self.assertTrue(any(c == "other" for c in commands))
        self.assertTrue(any("PYTHONPATH=" in c and "hippomind.claude_hook" in c
                            for c in commands))
        # 修复后审计必须立刻变绿——否则"修好了"又是一句无法验证的声明。
        self.assertEqual(HEALTHY, inspect_claude_hook(self.settings,
                                                      ledger_path=self.ledger).status)

    def test_repair_is_idempotent(self):
        install_claude_user_prompt_hook(Path("/opt/hippomind"), self.config, home=self.home)
        # 用同一个解释器路径，否则比较的是两件事（安装器用 sys.executable）。
        record = repair_claude_hook(self.settings, package_root=Path("/opt/hippomind"),
                                    agent_config=self.config, python=sys.executable,
                                    ledger_path=self.ledger)
        self.assertFalse(record["changed"], "已经正确的配置不该被判成需要修复")

    def test_repair_without_hooks_section_is_a_noop(self):
        self._write({"model": "keep"})
        record = repair_claude_hook(self.settings, package_root=Path("/opt/hippomind"),
                                    agent_config=self.config, python="/usr/bin/python3",
                                    ledger_path=self.ledger)
        self.assertFalse(record["changed"])
        self.assertEqual("no hooks", record["reason"])


if __name__ == "__main__":
    unittest.main()
