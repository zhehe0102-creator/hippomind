"""Provider manifest and low-frequency local agent auto-discovery.

The manifest is the single declarative source for how each known local agent
is detected and which integration tier it prefers (native hook > native
plugin > MCP profile > managed rule). Auto-discovery additionally scans the
home directory for *unknown* agent runtimes; those are reported with evidence
only and are never modified, because writing rules into an unrecognized
runtime could corrupt a configuration HippoMind does not understand.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .ledger import utc_now


# Marker paths are relative to the agent home and grouped as any-of sets:
# a group matches when every path in it exists. One matching group detects
# the agent; detection evidence is the list of matched paths.
KNOWN_PROVIDERS: Tuple[Dict[str, Any], ...] = (
    {
        "agent_id": "codex",
        "display_name": "Codex CLI",
        "tier": "native_hook",
        "marker_groups": (
            (".codex/config.toml", ".codex"),
            (".codex/AGENTS.md",),
        ),
        "rule_path": ".codex/AGENTS.md",
        "tiers": ("native_hook", "rules"),
    },
    {
        "agent_id": "deepseek",
        "display_name": "DeepSeek Harness (DSH)",
        "tier": "native_plugin",
        "marker_groups": (
            (".dsh/settings.yaml",),
            (".dsh/profiles",),
        ),
        "home_subpath": ".dsh",
        "rule_path": ".deepseek/AGENTS.md",
        "tiers": ("native_plugin", "mcp", "rules"),
    },
    {
        # Pi's installed pi-mcp-adapter natively reads ~/.pi/agent/mcp.json.
        # If an installation removes the adapter, status reports that failure
        # and the managed rule file remains the safe fallback.
        "agent_id": "pi",
        "display_name": "Pi",
        "tier": "mcp",
        "marker_groups": (
            (".pi/agent/AGENTS.md",),
            (".pi/agent/settings.json",),
        ),
        "rule_path": ".pi/agent/AGENTS.md",
        "tiers": ("mcp", "rules"),
    },
    {
        "agent_id": "claude",
        "display_name": "Claude Code",
        "tier": "mcp",
        "marker_groups": (
            (".claude/CLAUDE.md",),
            (".claude.json",),
        ),
        "rule_path": ".claude/CLAUDE.md",
        "tiers": ("mcp", "rules"),
    },
    {
        "agent_id": "cursor",
        "display_name": "Cursor",
        "tier": "mcp",
        "marker_groups": (
            (".cursor/rules",),
            (".cursor/config.json",),
        ),
        "rule_path": ".cursor/rules/hippomind-memory-hub.mdc",
        "tiers": ("mcp", "rules"),
    },
    {
        "agent_id": "gemini",
        "display_name": "Gemini CLI",
        "tier": "mcp",
        "marker_groups": (
            (".gemini/GEMINI.md",),
            (".gemini/settings.json",),
        ),
        "rule_path": ".gemini/GEMINI.md",
        "tiers": ("mcp", "rules"),
    },
    {
        "agent_id": "local",
        "display_name": "Generic local agent",
        "tier": "mcp",
        "marker_groups": (
            (".local-agent/AGENTS.md",),
        ),
        "rule_path": ".local-agent/AGENTS.md",
        "tiers": ("mcp", "rules"),
    },
)

# Instruction-file signatures that mark an unknown directory as an agent
# runtime. Conservative on purpose: a false "unknown agent" report is cheap,
# while writing into a non-agent directory is not.
UNKNOWN_AGENT_SIGNATURES: Tuple[str, ...] = (
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", "AGENT.md", ".cursorrules",
)

# Home-level directories that are known platforms/utilities, never agents.
UNKNOWN_SCAN_IGNORE = {
    ".Trash", ".cache", ".cargo", ".config", "Library", "Applications", "Desktop",
    "Documents", "Downloads", "Movies", "Music", "Pictures", "Public",
    ".npm", ".pnpm-store", ".local", ".vscode", ".docker", ".git", ".zsh",
    ".ssh", ".gnupg", ".DS_Store", ".cups", ".Trash-0", ".cache-clang",
}

DEFAULT_DISCOVERY_INTERVAL_SECONDS = 3_600.0
MAX_UNKNOWN_AGENTS = 32
MAX_SCAN_ENTRIES = 4_096


@dataclass(frozen=True)
class ProviderSpec:
    """One known agent runtime and its preferred integration tier."""

    agent_id: str
    display_name: str
    tier: str
    marker_groups: Tuple[Tuple[str, ...], ...]
    rule_path: str
    tiers: Tuple[str, ...]
    home_subpath: str = ""

    def detect(self, home: Path) -> Tuple[bool, List[str]]:
        """Return whether the runtime is installed plus matched evidence."""
        evidence: List[str] = []
        for group in self.marker_groups:
            if all((home / relative).exists() for relative in group):
                return True, [str(home / relative) for relative in group][:8]
            for relative in group:
                if (home / relative).exists():
                    evidence.append(str(home / relative))
        # 部分标记只作为 evidence 展示：单个残缺目录（如只有 ~/.codex 而无
        # 配置文件）不得判定为已安装，否则会触发半初始化的高级接入写入。
        return False, evidence[:8]


def _known_specs() -> List[ProviderSpec]:
    specs: List[ProviderSpec] = []
    for entry in KNOWN_PROVIDERS:
        specs.append(ProviderSpec(
            agent_id=str(entry["agent_id"]), display_name=str(entry["display_name"]),
            tier=str(entry["tier"]), marker_groups=tuple(tuple(g) for g in entry["marker_groups"]),
            rule_path=str(entry["rule_path"]), tiers=tuple(str(t) for t in entry["tiers"]),
            home_subpath=str(entry.get("home_subpath", "")),
        ))
    return specs


def provider_manifest() -> List[Dict[str, Any]]:
    """Return the declarative manifest used for tiered integration."""
    return [
        {"agent_id": spec.agent_id, "display_name": spec.display_name,
         "preferred_tier": spec.tier, "tier_order": list(spec.tiers),
         "rule_path": spec.rule_path}
        for spec in _known_specs()
    ]


def discover_known_agents(home: Optional[Path] = None, *,
                          specs: Optional[Sequence[ProviderSpec]] = None) -> Dict[str, Dict[str, Any]]:
    """Detect installed runtimes for every provider in the manifest."""
    root = Path(home).expanduser() if home else Path.home()
    known: Dict[str, Dict[str, Any]] = {}
    for spec in (specs if specs is not None else _known_specs()):
        detected, evidence = spec.detect(root)
        known[spec.agent_id] = {
            "detected": detected,
            "display_name": spec.display_name,
            "preferred_tier": spec.tier,
            "tier_order": list(spec.tiers),
            "rule_path": str(root / spec.rule_path),
            "evidence": evidence,
        }
    return known


def discover_unknown_agents(home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Report agent-like home directories that the manifest does not cover.

    Report-only by construction: this function performs no writes anywhere.
    """
    root = Path(home).expanduser() if home else Path.home()
    specs = _known_specs()
    known_roots = {"." + spec.agent_id for spec in specs}
    for spec in specs:
        if spec.home_subpath:
            known_roots.add(spec.home_subpath)
        # The managed rule location (e.g. ~/.local-agent) belongs to a known
        # provider and must not be reported as an unknown runtime.
        top = spec.rule_path.split("/", 1)[0]
        if top.startswith("."):
            known_roots.add(top)
    unknown: List[Dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return unknown
    scanned = 0
    for entry in entries:
        scanned += 1
        if scanned > MAX_SCAN_ENTRIES or len(unknown) >= MAX_UNKNOWN_AGENTS:
            break
        name = entry.name
        if not name.startswith(".") or name in UNKNOWN_SCAN_IGNORE or name in known_roots:
            continue
        if not entry.is_dir():
            continue
        signatures = [signature for signature in UNKNOWN_AGENT_SIGNATURES
                      if (entry / signature).is_file()]
        if not signatures:
            continue
        unknown.append({
            "name": name[:80],
            "path": str(entry),
            "signatures": signatures,
            "report_only": True,
            "message": "未识别的 Agent 运行时：仅报告，不修改；确认后可加入 provider manifest",
        })
    return unknown


def scan_agents(home: Optional[Path] = None) -> Dict[str, Any]:
    """One full discovery pass over known providers and unknown directories."""
    return {
        "scanned_at": utc_now(),
        "known": discover_known_agents(home),
        "unknown": discover_unknown_agents(home),
        "report_only_unknown": True,
    }


def integration_tier(agent_id: str, discovery: Optional[Dict[str, Any]] = None) -> str:
    """Pick the highest verification-backed tier for an agent.

    Native tiers require their runtime to be detected; otherwise the managed
    rule file is the only universally available integration.
    """
    specs = {spec.agent_id: spec for spec in _known_specs()}
    spec = specs.get(str(agent_id or "").strip().lower())
    if spec is None:
        return "rules"
    entry = (discovery or {}).get("known", {}).get(spec.agent_id, {}) if discovery else {}
    detected = bool(entry.get("detected", True)) if entry else True
    if spec.tier == "native_hook" and not detected:
        return "rules"
    if spec.tier == "native_plugin" and not detected:
        return "rules"
    return spec.tier


class AgentDiscovery:
    """Low-frequency, cached discovery scheduler shared by daemon and CLI.

    The scan touches only directory listings and ``stat`` calls, but it walks
    the home directory, so it runs at most once per interval and its result is
    persisted for status reporting between runs.
    """

    def __init__(self, state_path: Path, *, interval: float = DEFAULT_DISCOVERY_INTERVAL_SECONDS,
                 clock=time.monotonic):
        self.state_path = Path(state_path)
        self.interval = max(60.0, float(interval))
        self._clock = clock
        self._last_scan_monotonic: float = 0.0
        self._report: Dict[str, Any] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get("report"), dict):
            self._report = data["report"]
            scanned_at = str(data.get("scanned_at_wall", ""))
            wall_epoch = self._parse_wall_time(scanned_at)
            if wall_epoch > 0:
                # 磁盘缓存记录的是墙钟，而间隔判断用单调钟。把墙钟年龄换算成
                # 等效单调基准，否则重启后差值为巨大负数，进程永远不再扫描。
                age_seconds = max(0.0, time.time() - wall_epoch)
                self._last_scan_monotonic = self._clock() - age_seconds

    def _parse_wall_time(self, value: str) -> float:
        try:
            import datetime
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp()
        except (ValueError, OSError, OverflowError):
            return 0.0

    def _persist(self, report: Dict[str, Any]) -> None:
        document = {"scanned_at_wall": report.get("scanned_at", ""),
                    "interval_seconds": self.interval, "report": report}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + self.state_path.name + ".",
                                         suffix=".tmp", dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            try:
                os.chmod(self.state_path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def maybe_discover(self, *, home: Optional[Path] = None, force: bool = False) -> Dict[str, Any]:
        """Return the cached report, scanning only when the interval expired."""
        with self._lock:
            self._load()
            if force or not self._report or self._clock() - self._last_scan_monotonic >= self.interval:
                report = scan_agents(home)
                self._report = report
                self._last_scan_monotonic = self._clock()
                try:
                    self._persist(report)
                except OSError:
                    # A read-only state directory must never break discovery.
                    pass
            return self._report

    def report(self) -> Dict[str, Any]:
        """Return the cached report without triggering a scan."""
        self._load()
        return self._report

    def summary(self, *, ensure: bool = False) -> Dict[str, Any]:
        """Summarize the cached report; ``ensure`` runs a (interval-cached) scan."""
        report = self.maybe_discover() if ensure else self.report()
        if not report:
            return {"available": False}
        known = report.get("known", {})
        return {
            "available": True,
            "scanned_at": report.get("scanned_at", ""),
            "known_detected": sorted(name for name, item in known.items() if item.get("detected")),
            "known_absent": sorted(name for name, item in known.items() if not item.get("detected")),
            "unknown_reported": [item.get("name") for item in report.get("unknown", [])],
            "unknown_report_only": True,
            "interval_seconds": self.interval,
        }
