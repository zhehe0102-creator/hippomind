"""注入面审计与修复的回归测试。

锁定的都是真实事故，不是假想边界：

1. 配置存在但指向失效的临时目录 —— 7 个 agent 的接入全都"看起来装好了"，
   实际一个都读不到生产账本（指向 /var/folders/... 里一个 0 条记忆的测试库）。
2. 可移动卷上的 cwd —— launchd 与后台进程会被 macOS TCC 拒绝，
   而失败是静默的，看起来像"配置没生效"。
3. 解释器路径在可移动卷上（本机 Python 装在 Xcode 里）**不是**故障。
4. 规则文件托管段之外是用户自己的内容，不能因为用户写了别的路径就判故障。
5. 路径含空格（"Application Support"）时，按空白切会误判、按贪婪匹配会写坏。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hippomind.injection_audit import (HEALTHY, MISSING, STALE_PATH, VOLATILE_CWD,
                                       WRONG_LEDGER, Finding, inspect_codex_hook,
                                       inspect_mcp_server, inspect_rule_file,
                                       summarize)
from hippomind.injection_repair import (_runtime_package_root, apply_repairs,
                                        repair_mcp_server, stable_runtime_root)
from hippomind.rule_repair import rewrite_managed_section


class TempHomeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ledger = self.home / "hippomind.sqlite"
        self.ledger.write_bytes(b"SQLite format 3")
        self.runtime = self.home / "runtime"
        (self.runtime / "hippomind").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)


class McpAuditTests(TempHomeCase):
    def _write(self, payload) -> Path:
        path = self.home / ".agent" / "mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_file_is_reported_as_missing(self):
        finding = inspect_mcp_server("x", self.home / "nope.json", ledger_path=self.ledger)
        self.assertEqual(finding.status, MISSING)

    def test_unregistered_agent_is_missing(self):
        path = self._write({"mcpServers": {"other": {}}})
        finding = inspect_mcp_server("x", path, ledger_path=self.ledger)
        self.assertEqual(finding.status, MISSING)
        self.assertIn("未注册", finding.detail)

    def test_healthy_entry_points_at_production_ledger(self):
        path = self._write({"mcpServers": {"hippomind": {
            "command": "/usr/bin/python3",
            "args": ["-m", "hippomind.mcp", "--ledger", str(self.ledger)],
            "cwd": str(self.runtime)}}})
        finding = inspect_mcp_server("x", path, ledger_path=self.ledger)
        self.assertEqual(finding.status, HEALTHY)

    def test_temp_ledger_is_not_accepted(self):
        path = self._write({"mcpServers": {"hippomind": {
            "command": "/usr/bin/python3",
            "args": ["--ledger", "/var/folders/aa/bb/T/tmpXYZ/hub.sqlite"]}}})
        finding = inspect_mcp_server("x", path, ledger_path=self.ledger)
        self.assertIn(finding.status, (WRONG_LEDGER, STALE_PATH))

    def test_volumes_cwd_is_flagged(self):
        path = self._write({"mcpServers": {"hippomind": {
            "command": "/usr/bin/python3",
            "args": ["--ledger", str(self.ledger)],
            "cwd": "/Volumes/SomeDisk/repo"}}})
        finding = inspect_mcp_server("x", path, ledger_path=self.ledger)
        self.assertEqual(finding.status, STALE_PATH)

    def test_interpreter_on_volumes_is_not_a_fault(self):
        path = self._write({"mcpServers": {"hippomind": {
            "command": "/Volumes/XcodeDev/Xcode.app/Contents/Developer/usr/bin/python3",
            "args": ["--ledger", str(self.ledger)],
            "cwd": str(self.runtime)}}})
        finding = inspect_mcp_server("x", path, ledger_path=self.ledger)
        self.assertEqual(finding.status, HEALTHY)

    def test_unreadable_json_is_not_guessed(self):
        path = self.home / ".agent" / "mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        finding = inspect_mcp_server("x", path, ledger_path=self.ledger)
        self.assertEqual(finding.status, "unreadable")


class CodexHookAuditTests(TempHomeCase):
    def _write(self, command: str) -> Path:
        path = self.home / ".codex" / "hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": command}]}]}}), encoding="utf-8")
        return path

    def test_missing_hook(self):
        path = self._write("echo hello")
        finding = inspect_codex_hook(path, ledger_path=self.ledger)
        self.assertEqual(finding.status, MISSING)

    def test_hook_with_existing_config_is_healthy(self):
        config = self.home / "agents" / "codex.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("{}", encoding="utf-8")
        path = self._write(
            "cd %s && PYTHONPATH=%s /usr/bin/python3 -m hippomind.codex_hook "
            "--managed --agent-config %s" % (self.runtime, self.runtime, config))
        finding = inspect_codex_hook(path, ledger_path=self.ledger)
        self.assertEqual(finding.status, HEALTHY)

    def test_hook_with_vanished_config_is_stale(self):
        path = self._write(
            "cd /tmp && /usr/bin/python3 -m hippomind.codex_hook --managed "
            "--agent-config /var/folders/aa/bb/T/tmpGONE/agents/codex.json")
        finding = inspect_codex_hook(path, ledger_path=self.ledger)
        self.assertEqual(finding.status, STALE_PATH)

    def test_path_with_spaces_is_parsed_whole(self):
        # 安装器会给每个路径加引号（含空格的 "Application Support" 必须如此），
        # 审计器要能正确解析带引号的整体路径，而不是按空白切成半截。
        config = self.home / "Application Support" / "agents" / "codex.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("{}", encoding="utf-8")
        path = self._write(
            "cd '%s' && /usr/bin/python3 -m hippomind.codex_hook --managed "
            "--agent-config '%s'" % (self.runtime, config))
        finding = inspect_codex_hook(path, ledger_path=self.ledger)
        self.assertEqual(finding.status, HEALTHY)


class RuleFileAuditTests(TempHomeCase):
    BEGIN = "<!-- HIPPO-MIND-AUTO-CONTEXT:BEGIN -->"
    END = "<!-- HIPPO-MIND-AUTO-CONTEXT:END -->"

    def _write(self, body: str, user_text: str = "") -> Path:
        path = self.home / "AGENTS.md"
        path.write_text(user_text + "\n" + self.BEGIN + "\n" + body + "\n" + self.END + "\n",
                        encoding="utf-8")
        return path

    def test_missing_rule_file(self):
        finding = inspect_rule_file("x", self.home / "nope.md")
        self.assertEqual(finding.status, MISSING)

    def test_healthy_managed_section(self):
        # 注意：测试的临时目录本身位于 /var/folders 下，所以这里用**真实存在**
        # 的 runtime 路径（存在 => 不是"失效的临时路径"）。
        path = self._write("cd '%s' && python3 -m hippomind auto-context" % self.runtime)
        self.assertTrue(self.runtime.exists())
        self.assertEqual(inspect_rule_file("x", path).status, HEALTHY)

    def test_volatile_path_inside_managed_section_is_flagged(self):
        path = self._write("cd '/Volumes/SomeDisk/repo' && python3 -m hippomind")
        self.assertEqual(inspect_rule_file("x", path).status, VOLATILE_CWD)

    def test_user_content_outside_managed_section_is_ignored(self):
        path = self._write(
            "cd '%s' && python3 -m hippomind auto-context" % self.runtime,
            user_text="python3 /Volumes/OtherDisk/tool/query.py search")
        self.assertTrue(self.runtime.exists())
        self.assertEqual(inspect_rule_file("x", path).status, HEALTHY)


class RuleRepairTests(TempHomeCase):
    BEGIN = "<!-- HIPPO-MIND-AUTO-CONTEXT:BEGIN -->"
    END = "<!-- HIPPO-MIND-AUTO-CONTEXT:END -->"

    def test_rewrites_temp_paths_and_keeps_user_content(self):
        config = self.home / "Application Support" / "agents" / "dsh.json"
        text = (
            "USER LINE /Volumes/OtherDisk/x.py\n"
            + self.BEGIN + "\n"
            + "cd '/Volumes/SomeDisk/repo' && python3 -m hippomind auto-context "
              "--agent dsh --agent-config /var/folders/aa/T/tmpGONE/agents/dsh.json\n"
            + self.END + "\n")
        updated, changed = rewrite_managed_section(
            text, agent="dsh", runtime_root=self.runtime,
            agent_config=config, python="/usr/bin/python3")
        self.assertTrue(changed)
        self.assertIn("USER LINE /Volumes/OtherDisk/x.py", updated)
        self.assertNotIn("tmpGONE", updated)
        self.assertNotIn("/Volumes/SomeDisk", updated)
        self.assertIn(str(config), updated)

    def test_path_with_spaces_is_not_truncated(self):
        # 事故形态：用正则替换路径时只匹配到第一个空格，残尾留在原地，
        # 产出 ".../dsh.json Support/HippoMind/agents/dsh.json'" 这种坏命令。
        # 现在的实现是**整段重新生成**，不可能留下残尾。
        config = self.home / "Application Support" / "agents" / "dsh.json"
        text = (self.BEGIN + "\n"
                + "cd '/x' && python3 -m hippomind --agent-config "
                  "'/var/folders/a/T/t/agents/dsh.json'\n"
                + self.END + "\n")
        updated, _ = rewrite_managed_section(
            text, agent="dsh", runtime_root=self.runtime,
            agent_config=config, python="/usr/bin/python3")
        # 新路径出现且被单引号包裹（含空格必须引用）。
        self.assertIn("--agent-config '%s'" % config, updated)
        # 旧临时路径彻底消失，且不残留任何残尾。
        # 注意：测试夹具的 runtime 本身就在 /var/folders 下，所以不能断言
        # "没有 /var/folders"；要断言的是"没有残尾"这一真正的坏形态。
        self.assertNotIn("Support/HippoMind", updated)
        self.assertNotIn("tmpGONE", updated)
        self.assertNotIn("Support/HippoMind", updated)
        self.assertEqual(updated.count("dsh.json"), 1)

    def test_no_managed_section_is_untouched(self):
        text = "just user content\n"
        updated, changed = rewrite_managed_section(
            text, agent="x", runtime_root=self.runtime,
            agent_config=self.home / "a.json", python="/usr/bin/python3")
        self.assertFalse(changed)
        self.assertEqual(updated, text)


class RepairApplyTests(TempHomeCase):
    def test_dry_run_writes_nothing(self):
        path = self.home / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        repair = repair_mcp_server(path, ledger_path=self.ledger,
                                   agent_config=self.home / "a.json",
                                   package_root=self.home, python="/usr/bin/python3")
        results = apply_repairs([repair], dry_run=True)
        self.assertTrue(results[0]["would_change"])
        self.assertFalse(results[0]["applied"])
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_apply_writes_backup_and_new_content(self):
        path = self.home / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        repair = repair_mcp_server(path, ledger_path=self.ledger,
                                   agent_config=self.home / "a.json",
                                   package_root=self.home, python="/usr/bin/python3")
        results = apply_repairs([repair], dry_run=False)
        self.assertTrue(results[0]["applied"])
        self.assertTrue(Path(results[0]["backup"]).exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data["mcpServers"]["hippomind"]
        self.assertIn(str(self.ledger), entry["args"])
        self.assertEqual(entry["cwd"], str(self.runtime))

    def test_apply_keeps_other_servers(self):
        path = self.home / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                        encoding="utf-8")
        repair = repair_mcp_server(path, ledger_path=self.ledger,
                                   agent_config=self.home / "a.json",
                                   package_root=self.home, python="/usr/bin/python3")
        apply_repairs([repair], dry_run=False)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("other", data["mcpServers"])
        self.assertIn("hippomind", data["mcpServers"])


class RuntimeRootTests(TempHomeCase):
    def test_prefers_internal_runtime(self):
        fallback = self.home / "repo"
        self.assertEqual(_runtime_package_root(self.ledger, fallback), self.runtime)

    def test_falls_back_when_runtime_absent(self):
        # 另建一个**没有** runtime 子目录的 ledger 目录。
        bare = self.home / "bare"
        bare.mkdir()
        empty = bare / "hippomind.sqlite"
        empty.write_bytes(b"x")
        fallback = self.home / "repo"
        self.assertEqual(_runtime_package_root(empty, fallback), fallback)

    def test_stable_runtime_root_is_next_to_ledger(self):
        self.assertEqual(stable_runtime_root(self.ledger), self.home / "runtime")


class SummaryTests(unittest.TestCase):
    def test_summary_counts_and_lists_broken(self):
        findings = [Finding("a", "rules", "/p", HEALTHY),
                    Finding("b", "mcp", "/q", MISSING, "no")]
        summary = summarize(findings)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["healthy"], 1)
        self.assertEqual(len(summary["broken"]), 1)


if __name__ == "__main__":
    unittest.main()
