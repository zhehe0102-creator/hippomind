"""Real MCP registration + controlled daemon auto-install tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from hippomind.daemon import MemoryHubService, RuntimePaths
from hippomind.ledger import SQLiteLedger
from hippomind.mcp_register import (
    MCP_SERVER_NAME, mcp_registration_path, mcp_registration_status,
    register_mcp_server, server_entry, unregister_mcp_server,
)
from hippomind.provider_manifest import integration_tier, provider_manifest


class MCPRegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.package_root = Path(__file__).resolve().parents[1]
        self.ledger_path = self.home / "hub.sqlite"
        self.agent_config = self.home / "agents" / "claude.json"
        self.agent_config.parent.mkdir(parents=True)
        self.agent_config.write_text(
            '{"agent_id": "claude", "token": "agt-test-token-0123456789abcdef"}\n',
            encoding="utf-8")
        # MCP 服务器启动即校验凭据文件 0600，测试夹具必须遵守同一约束。
        os.chmod(self.agent_config, 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_registration_paths_cover_standard_hosts_and_portable_fallback(self):
        self.assertEqual(mcp_registration_path("pi", home=self.home),
                         self.home / ".pi" / "agent" / "mcp.json")
        self.assertEqual(mcp_registration_path("claude", home=self.home),
                         self.home / ".claude.json")
        self.assertEqual(mcp_registration_path("cursor", home=self.home),
                         self.home / ".cursor" / "mcp.json")
        self.assertEqual(mcp_registration_path("gemini", home=self.home),
                         self.home / ".gemini" / "settings.json")
        self.assertEqual(mcp_registration_path("local", home=self.home),
                         self.home / ".agents" / "mcp.json")
        for agent in ("codex", "deepseek", "unknown"):
            self.assertIsNone(mcp_registration_path(agent, home=self.home), agent)

    def test_pi_registration_requires_real_adapter_for_runtime_ready(self):
        (self.home / ".pi" / "agent").mkdir(parents=True)
        (self.home / ".pi" / "agent" / "settings.json").write_text(
            json.dumps({"packages": ["npm:pi-mcp-adapter"]}), encoding="utf-8")
        registered = register_mcp_server("pi", package_root=self.package_root,
                                         ledger_path=self.ledger_path, agent_config=self.agent_config,
                                         home=self.home)
        self.assertTrue(registered["registered"])
        status = mcp_registration_status("pi", home=self.home, runtime_check=True)
        self.assertTrue(status["pi_adapter"]["available"])
        self.assertTrue(status["runtime"]["host_loader_ready"])
        self.assertTrue(status["runtime"]["ready"])

    def test_register_preserves_foreign_keys_and_is_idempotent(self):
        claude = self.home / ".claude.json"
        claude.write_text(json.dumps({"userID": "u-1", "projects": {"a": {}}}), encoding="utf-8")
        first = register_mcp_server("claude", package_root=self.package_root,
                                    ledger_path=self.ledger_path, agent_config=self.agent_config,
                                    home=self.home)
        self.assertTrue(first["registered"])
        self.assertTrue(first["changed"])
        document = json.loads(claude.read_text(encoding="utf-8"))
        self.assertEqual(document["userID"], "u-1")
        self.assertEqual(document["projects"], {"a": {}})
        self.assertEqual(document["mcpServers"][MCP_SERVER_NAME],
                         server_entry(self.package_root, self.ledger_path, self.agent_config))
        second = register_mcp_server("claude", package_root=self.package_root,
                                     ledger_path=self.ledger_path, agent_config=self.agent_config,
                                     home=self.home)
        self.assertTrue(second["registered"])
        self.assertFalse(second["changed"])
        entry = document["mcpServers"][MCP_SERVER_NAME]
        self.assertEqual(set(entry.keys()), {"command", "args", "cwd"})
        self.assertFalse(any(str(value).startswith("agt_") for value in
                             [entry["command"]] + entry["args"]))

    def test_cursor_registration_creates_exclusive_file_with_0600(self):
        result = register_mcp_server("cursor", package_root=self.package_root,
                                     ledger_path=self.ledger_path, agent_config=self.agent_config,
                                     home=self.home)
        self.assertTrue(result["registered"])
        path = self.home / ".cursor" / "mcp.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(list(document.keys()), ["mcpServers"])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_unparseable_config_is_never_rewritten(self):
        claude = self.home / ".claude.json"
        claude.write_text("{broken json", encoding="utf-8")
        result = register_mcp_server("claude", package_root=self.package_root,
                                     ledger_path=self.ledger_path, agent_config=self.agent_config,
                                     home=self.home)
        self.assertFalse(result["registered"])
        self.assertIn("拒绝改写", result["error"])
        self.assertEqual(claude.read_text(encoding="utf-8"), "{broken json")

    def test_unregister_removes_only_hippomind_entry(self):
        claude = self.home / ".claude.json"
        claude.write_text(json.dumps({"userID": "u-2", "mcpServers": {
            "foreign": {"command": "x"}, MCP_SERVER_NAME: {"command": "y"}}}), encoding="utf-8")
        result = unregister_mcp_server("claude", home=self.home)
        self.assertTrue(result["removed"])
        document = json.loads(claude.read_text(encoding="utf-8"))
        self.assertEqual(document["userID"], "u-2")
        self.assertEqual(list(document["mcpServers"]), ["foreign"])

    def test_status_reports_registration_and_live_runtime_handshake(self):
        self.assertFalse(mcp_registration_status("claude", home=self.home)["registered"])
        register_mcp_server("claude", package_root=self.package_root,
                            ledger_path=self.ledger_path, agent_config=self.agent_config,
                            home=self.home)
        status = mcp_registration_status("claude", home=self.home, runtime_check=True)
        self.assertTrue(status["registered"])
        self.assertTrue(status["uses_hippomind_mcp"])
        runtime = status["runtime"]
        self.assertTrue(runtime["checked"])
        self.assertTrue(runtime["ready"], runtime.get("error", ""))
        self.assertGreater(runtime["tools"], 0)

    def test_broken_registered_command_fails_runtime_check(self):
        claude = self.home / ".claude.json"
        claude.write_text(json.dumps({"mcpServers": {MCP_SERVER_NAME: {
            "command": "/nonexistent-hippomind-check", "args": []}}}), encoding="utf-8")
        status = mcp_registration_status("claude", home=self.home, runtime_check=True)
        self.assertTrue(status["registered"])
        self.assertFalse(status["uses_hippomind_mcp"])
        self.assertFalse(status["runtime"]["ready"])

    def test_pi_manifest_tier_is_mcp_with_rule_fallback(self):
        manifest = {entry["agent_id"]: entry for entry in provider_manifest()}
        self.assertEqual(manifest["pi"]["preferred_tier"], "mcp")
        self.assertEqual(manifest["pi"]["tier_order"], ["mcp", "rules"])
        self.assertEqual(integration_tier("pi"), "mcp")


class DaemonAutoInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.paths = RuntimePaths.for_ledger(self.root / "hub.sqlite", str(self.root))
        self.service = MemoryHubService(self.paths, watch=False, require_auth=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_report(self, *detected):
        return {"known": {agent: {"detected": True} for agent in detected},
                "unknown": [], "scanned_at": "2026-08-16T00:00:00Z"}

    def test_install_agent_registers_real_mcp_and_rules(self):
        outcome = self.service._auto_install_detected(self._fake_report("claude"), home=self.home)
        self.assertEqual(outcome["agents"]["claude"]["state"], "installed")
        self.assertTrue(outcome["agents"]["claude"]["verified"])
        status = outcome["agents"]["claude"]["status"]
        self.assertTrue(status["credential_valid"])
        self.assertTrue(status["rules_installed"])
        self.assertTrue(status["mcp_registration"]["registered"])
        self.assertTrue(status["mcp_registration"]["runtime"]["ready"])
        document = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertIn(MCP_SERVER_NAME, document["mcpServers"])
        # 第二轮必须收敛为 already_installed，不能重复安装。
        second = self.service._auto_install_detected(self._fake_report("claude"), home=self.home)
        self.assertEqual(second["agents"]["claude"]["state"], "already_installed")

    def test_undetected_and_unknown_agents_are_never_touched(self):
        outcome = self.service._auto_install_detected(
            {"known": {"claude": {"detected": False}},
             "unknown": [{"name": ".mystery-agent", "report_only": True}]},
            home=self.home)
        self.assertEqual(outcome["agents"], {})
        self.assertFalse((self.home / ".claude.json").exists())

    def test_repair_recovers_from_missing_rules(self):
        self.service._auto_install_detected(self._fake_report("claude"), home=self.home)
        # 模拟规则文件被外部删除 → 下一轮必须自动修复。
        (self.home / ".claude" / "CLAUDE.md").unlink()
        outcome = self.service._auto_install_detected(self._fake_report("claude"), home=self.home)
        self.assertEqual(outcome["agents"]["claude"]["state"], "installed")
        self.assertTrue((self.home / ".claude" / "CLAUDE.md").exists())

    def test_integration_complete_requires_real_integrations(self):
        base = {"credential_valid": True, "rules_installed": True}
        self.assertTrue(MemoryHubService._integration_complete(base))
        self.assertFalse(MemoryHubService._integration_complete({**base, "rules_installed": False}))
        self.assertFalse(MemoryHubService._integration_complete(
            {**base, "mcp_registration": {"supported": True, "registered": False}}))
        self.assertTrue(MemoryHubService._integration_complete(
            {**base, "mcp_registration": {"supported": False, "reason": "Pi 走规则"}}))
        self.assertFalse(MemoryHubService._integration_complete(
            {**base, "native_context_hook": {"installed": False}}))
        self.assertFalse(MemoryHubService._integration_complete(
            {**base, "native_dsh_plugin": {"detected": True, "installed": False}}))


if __name__ == "__main__":
    unittest.main()
