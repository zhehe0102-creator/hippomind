"""Agent integration commands: install-auto, install-agents,
agent-status and discover-agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from ..agent_install import agent_install_status, install_agents
from ..auto_context import agent_auto_rules, install_agent_rules
from ..ledger import SQLiteLedger
from ..provider_manifest import AgentDiscovery, integration_tier, provider_manifest
from . import _paths
from .common import _print, _reject_restricted_direct


def cmd_install_auto(args) -> int:
    """Install a managed explicit-marker rule for one configured Agent."""
    target = Path(args.agents_file).expanduser() if args.agents_file else Path.home() / ".codex" / "AGENTS.md"
    package_root = Path(__file__).resolve().parents[2]
    if args.print_only:
        config = Path(args.agent_config).expanduser() if args.agent_config else None
        print(agent_auto_rules(package_root, agent=args.agent, agent_config=config), end="")
        return 0
    _reject_restricted_direct(args, "install-auto")
    if not args.agents_file:
        paths = _paths(args)
        result = install_agents(SQLiteLedger(paths.ledger_path), paths, [args.agent],
                                package_root=package_root)[0]
        result["behavior"] = "仅在普通用户文本包含记忆：或记忆:时预取并注入 Context Pack"
        _print(result)
        return 0
    changed, path = install_agent_rules(
        target, package_root, agent=args.agent,
        agent_config=Path(args.agent_config).expanduser() if args.agent_config else None,
    )
    _print({"installed": True, "changed": changed, "path": str(path), "agent": args.agent,
            "behavior": "仅在普通用户文本包含记忆：或记忆:时预取并注入 Context Pack"})
    return 0


def cmd_install_agents(args) -> int:
    _reject_restricted_direct(args, "install-agents")
    paths = _paths(args)
    package_root = Path(__file__).resolve().parents[2]
    home = Path(args.home).expanduser() if args.home else None
    agents = list(args.agent or [])
    discovery_report: Optional[Dict[str, Any]] = None
    if getattr(args, "auto", False):
        # Auto mode installs only for runtimes actually detected on this
        # machine; unknown agents stay report-only and are never touched.
        discovery = AgentDiscovery(paths.state_path.parent / "agent-discovery.json")
        discovery_report = discovery.maybe_discover(home=home, force=True)
        detected = [agent_id for agent_id, item in discovery_report["known"].items()
                    if item.get("detected")]
        if not agents or agents == ["all"]:
            agents = detected or list(discovery_report["known"].keys())
        else:
            agents = [agent for agent in agents if agent in detected] or detected
    result = install_agents(SQLiteLedger(paths.ledger_path), paths, agents or ["all"],
                            package_root=package_root, home=home,
                            scopes=args.scope or None, permissions=args.permission or None)
    payload: Dict[str, Any] = {"installed": result, "count": len(result),
                               "message": "默认每轮自动预取；也可用记忆：强制指定查询"}
    if discovery_report is not None:
        payload["discovery"] = {
            "known_detected": sorted(name for name, item in discovery_report["known"].items()
                                     if item.get("detected")),
            "unknown_reported": discovery_report["unknown"],
            "unknown_policy": "report-only，不修改",
        }
    _print(payload)
    return 0


def cmd_agent_status(args) -> int:
    _reject_restricted_direct(args, "agent-status")
    paths = _paths(args)
    home = Path(args.home).expanduser() if args.home else None
    discovery = AgentDiscovery(paths.state_path.parent / "agent-discovery.json")
    discovery.maybe_discover(home=home)
    report = discovery.report()
    known = report.get("known", {})
    agents = agent_install_status(SQLiteLedger(paths.ledger_path), paths,
                                  args.agent or ["all"], home=home)
    for item in agents:
        entry = known.get(item["agent"], {})
        item["detected_on_machine"] = bool(entry.get("detected", True))
        item["integration_tier"] = integration_tier(item["agent"], report)
        if entry.get("evidence"):
            item["detection_evidence"] = entry["evidence"]
    _print({"agents": agents, "discovery": discovery.summary(),
            "unknown_agents": report.get("unknown", []),
            "unknown_policy": "report-only：未识别的 Agent 只报告，不修改"})
    return 0


def cmd_discover_agents(args) -> int:
    """Force one discovery pass and print the report without writing rules."""
    _reject_restricted_direct(args, "discover-agents")
    paths = _paths(args)
    home = Path(args.home).expanduser() if args.home else None
    discovery = AgentDiscovery(paths.state_path.parent / "agent-discovery.json")
    report = discovery.maybe_discover(home=home, force=True)
    for agent_id, item in report["known"].items():
        item["integration_tier"] = integration_tier(agent_id, report)
    _print({"scanned_at": report["scanned_at"], "known": report["known"],
            "unknown": report["unknown"],
            "unknown_policy": "report-only：未识别的 Agent 只报告，确认后可加入 provider manifest",
            "manifest": provider_manifest()})
    return 0
