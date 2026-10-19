"""Native DeepSeek Harness (DSH / Cordis) integration installer.

Deploys the HippoMind Cordis plugin into ``~/.dsh/plugins/dsh-hippomind`` and
wires the profile bundle list exactly like any other local ``link:`` plugin.
The scoped credential itself never leaves its 0600 agent config file; the
plugin's patch row only carries a path reference. Every failure degrades to
the plain rule-file integration instead of raising.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .agent_config import load_agent_config
from .ledger import LedgerError


DSH_PLUGIN_ID = "hippomind"
DSH_PLUGIN_PACKAGE = "@dsh-external/dsh-hippomind"
DSH_PLUGIN_SOURCE_DIR = "integrations" / Path("dsh-plugin")
PROFILE_NAME = "web"
AGENT_CONFIG_PLACEHOLDER = "__HIPPOMIND_AGENT_CONFIG__"
PLUGIN_FILES = ("package.json", "lib/index.js")


def dsh_home(home: Optional[Path] = None) -> Path:
    # Explicit homes are used by isolated tests and installs.  They must win
    # over process-wide DSH_HOME or tests can rewrite the real ~/.dsh.
    if home is not None:
        return Path(home).expanduser() / ".dsh"
    configured = os.environ.get("DSH_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    root = Path(home).expanduser() if home else Path.home()
    return root / ".dsh"


def dsh_plugin_target(home: Optional[Path] = None) -> Path:
    return dsh_home(home) / "plugins" / "dsh-hippomind"


def dsh_profile_dir(home: Optional[Path] = None) -> Path:
    return dsh_home(home) / "profiles" / PROFILE_NAME


def _plugin_source(package_root: Path) -> Path:
    return Path(package_root).expanduser() / DSH_PLUGIN_SOURCE_DIR


def _yaml_single_quoted(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _atomic_text(path: Path, text: str, *, mode: Optional[int] = None) -> None:
    """Write through a same-directory temporary file, preserving owner mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = path.stat().st_mode & 0o777
    except OSError:
        previous = None
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        target_mode = mode if mode is not None else previous
        if target_mode is not None:
            try:
                os.chmod(path, int(target_mode))
            except OSError:
                pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.stat().st_size > 256_000:
            raise LedgerError(f"DSH 配置超过 256 KiB 安全上限: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise LedgerError(f"无法解析 DSH 配置 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LedgerError(f"DSH 配置根节点必须是对象: {path}")
    return data


def _same_content(path: Path, text: str) -> bool:
    try:
        return path.read_text(encoding="utf-8") == text
    except OSError:
        return False


def deploy_dsh_plugin(package_root: Path, agent_config: Path, *,
                      home: Optional[Path] = None) -> Dict[str, Any]:
    """Copy plugin files and render the patch row that references the credential."""
    source = _plugin_source(package_root)
    target = dsh_plugin_target(home)
    for relative in PLUGIN_FILES:
        if not (source / relative).is_file():
            raise LedgerError(f"DSH 插件源文件缺失: {source / relative}")
    agent_config_path = str(Path(agent_config).expanduser().resolve())
    # The token stays in the 0600 file; the patch row carries a path only.
    patch = (source / "cordis.patch.yml").read_text(encoding="utf-8").replace(
        AGENT_CONFIG_PLACEHOLDER, _yaml_single_quoted(agent_config_path))
    changed = []
    for relative in PLUGIN_FILES:
        destination = target / relative
        content = (source / relative).read_text(encoding="utf-8")
        if not _same_content(destination, content):
            _atomic_text(destination, content)
            changed.append(relative)
    if not _same_content(target / "cordis.patch.yml", patch):
        _atomic_text(target / "cordis.patch.yml", patch)
        changed.append("cordis.patch.yml")
    return {"path": str(target), "changed": changed or None,
            "agent_config": agent_config_path}


def _wire_profile_document(document: Dict[str, Any], plugin_target: Path) -> bool:
    """Insert HippoMind's link dependency and bundle row; touch nothing else."""
    dependencies = document.setdefault("dependencies", {})
    if not isinstance(dependencies, dict):
        raise LedgerError("DSH profile package.json.dependencies 必须是对象")
    profile = document.get("dsh")
    if not isinstance(profile, dict):
        profile = {}
        document["dsh"] = profile
    profile_profile = profile.setdefault("profile", {})
    if not isinstance(profile_profile, dict):
        raise LedgerError("DSH profile package.json.dsh.profile 必须是对象")
    bundles = profile_profile.setdefault("bundles", [])
    if not isinstance(bundles, list):
        raise LedgerError("DSH profile package.json.dsh.profile.bundles 必须是列表")
    changed = False
    expected = f"link:{plugin_target}"
    if dependencies.get(DSH_PLUGIN_PACKAGE) != expected:
        dependencies[DSH_PLUGIN_PACKAGE] = expected
        changed = True
    if DSH_PLUGIN_PACKAGE not in bundles:
        bundles.append(DSH_PLUGIN_PACKAGE)
        changed = True
    return changed


def wire_dsh_profile(*, home: Optional[Path] = None) -> Dict[str, Any]:
    """Register the deployed plugin in the active DSH profile, idempotently."""
    plugin_target = dsh_plugin_target(home)
    profile_dir = dsh_profile_dir(home)
    package_path = profile_dir / "package.json"
    document = _read_json(package_path)
    if not document:
        return {"profile": str(package_path), "wired": False,
                "reason": "DSH profile package.json 不存在或为空"}
    changed = _wire_profile_document(document, plugin_target)
    backup = ""
    if changed:
        backup_path = profile_dir / "package.json.bak.hippomind"
        if package_path.exists() and not backup_path.exists():
            shutil.copy2(package_path, backup_path)
            try:
                os.chmod(backup_path, 0o600)
            except OSError:
                pass
            backup = str(backup_path)
        _atomic_text(package_path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    link_root = profile_dir / "node_modules" / "@dsh-external"
    link_path = link_root / "dsh-hippomind"
    link_created = False
    if not link_path.exists():
        link_root.mkdir(parents=True, exist_ok=True)
        temporary = link_root / (".dsh-hippomind." + str(int(time.time() * 1000)))
        temporary.symlink_to(plugin_target, target_is_directory=True)
        os.replace(temporary, link_path)
        link_created = True
    return {"profile": str(package_path), "wired": True, "changed": changed,
            "backup": backup or None, "link": str(link_path),
            "link_created": link_created}


def install_dsh_integration(package_root: Path, agent_config: Path, *,
                            home: Optional[Path] = None) -> Dict[str, Any]:
    """Deploy + wire the native plugin; degrade to rules on any failure."""
    root = dsh_home(home)
    result: Dict[str, Any] = {"installed": False, "dsh_home": str(root)}
    if not root.is_dir():
        result["reason"] = "未检测到 ~/.dsh（DeepSeek Harness 未安装），保留规则文件回退"
        result["degraded_to"] = "rules"
        return result
    try:
        # Fail fast on an unusable credential reference before touching DSH.
        load_agent_config(agent_config)
    except LedgerError as exc:
        result["reason"] = f"agent config 不可用，保留规则文件回退: {str(exc)[:200]}"
        result["degraded_to"] = "rules"
        return result
    try:
        deployed = deploy_dsh_plugin(package_root, agent_config, home=home)
        wired = wire_dsh_profile(home=home)
    except (LedgerError, OSError) as exc:
        result["reason"] = f"DSH 原生插件接入失败，保留规则文件回退: {str(exc)[:200]}"
        result["degraded_to"] = "rules"
        return result
    if not wired.get("wired"):
        result["reason"] = str(wired.get("reason") or "DSH profile 未装配")
        result["degraded_to"] = "rules"
        return result
    result.update({"installed": True, "deploy": deployed, "profile": wired})
    # Post-install runtime verification with one automatic repair attempt:
    # a lost symlink or a hand-edited profile row is fixed in place instead
    # of leaving a half-installed native integration behind.
    status = dsh_integration_status(home=home, package_root=package_root)
    if not status.get("installed"):
        try:
            deploy_dsh_plugin(package_root, agent_config, home=home)
            wire_dsh_profile(home=home)
        except (LedgerError, OSError) as exc:
            result["repair"] = {"attempted": True, "error": str(exc)[:200]}
        status = dsh_integration_status(home=home, package_root=package_root)
        result["repair"] = {"attempted": True, "installed_after": bool(status.get("installed"))}
        if not status.get("installed"):
            result["installed"] = False
            result["degraded_to"] = "rules"
            result["reason"] = "安装后验证未通过且自动修复失败，保留规则文件回退"
    result["verified"] = bool(status.get("installed"))
    return result


def dsh_integration_status(*, home: Optional[Path] = None,
                           package_root: Optional[Path] = None) -> Dict[str, Any]:
    """Verify only HippoMind-owned wiring; never read or reveal credentials."""
    root = dsh_home(home)
    plugin = dsh_plugin_target(home)
    profile_package = dsh_profile_dir(home) / "package.json"
    status: Dict[str, Any] = {"dsh_home": str(root), "installed": False}
    if not root.is_dir():
        status["detected"] = False
        status["reason"] = "未检测到 ~/.dsh"
        return status
    status["detected"] = True
    status["plugin_files_present"] = all((plugin / name).is_file() for name in PLUGIN_FILES)
    patch_path = plugin / "cordis.patch.yml"
    references_config = False
    try:
        references_config = "agentConfig" in patch_path.read_text(encoding="utf-8")
    except OSError:
        pass
    status["patch_references_credential_path"] = references_config
    document = _read_json(profile_package)
    dependencies = document.get("dependencies", {})
    bundles = document.get("dsh", {}).get("profile", {}).get("bundles", [])
    status["profile_dependency"] = DSH_PLUGIN_PACKAGE in dependencies if isinstance(dependencies, dict) else False
    status["profile_bundle"] = DSH_PLUGIN_PACKAGE in bundles if isinstance(bundles, list) else False
    link = dsh_profile_dir(home) / "node_modules" / "@dsh-external" / "dsh-hippomind"
    status["node_link_resolves"] = link.exists() and plugin.is_dir()
    status["installed"] = bool(
        status["plugin_files_present"] and references_config
        and status["profile_dependency"] and status["profile_bundle"]
        and status["node_link_resolves"]
    )
    if package_root is not None:
        source = _plugin_source(Path(package_root))
        status["plugin_source_current"] = all(
            _same_content(plugin / name, (source / name).read_text(encoding="utf-8"))
            for name in PLUGIN_FILES if (source / name).is_file()
        ) if status["plugin_files_present"] else False
    return status


def uninstall_dsh_integration(*, home: Optional[Path] = None) -> Dict[str, Any]:
    """Remove only HippoMind-owned wiring; other plugins stay untouched."""
    root = dsh_home(home)
    removed: Dict[str, Any] = {"removed": False, "dsh_home": str(root)}
    if not root.is_dir():
        removed["reason"] = "未检测到 ~/.dsh"
        return removed
    plugin = dsh_plugin_target(home)
    profile_dir = dsh_profile_dir(home)
    package_path = profile_dir / "package.json"
    touched = []
    document = _read_json(package_path)
    if document:
        dependencies = document.get("dependencies")
        bundles = document.get("dsh", {}).get("profile", {}).get("bundles")
        changed = False
        if isinstance(dependencies, dict) and DSH_PLUGIN_PACKAGE in dependencies:
            dependencies.pop(DSH_PLUGIN_PACKAGE)
            changed = True
        if isinstance(bundles, list) and DSH_PLUGIN_PACKAGE in bundles:
            bundles.remove(DSH_PLUGIN_PACKAGE)
            changed = True
        if changed:
            _atomic_text(package_path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            touched.append(str(package_path))
    link = profile_dir / "node_modules" / "@dsh-external" / "dsh-hippomind"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
            touched.append(str(link))
    except OSError:
        pass
    try:
        if plugin.is_dir():
            shutil.rmtree(plugin)
            touched.append(str(plugin))
    except OSError:
        pass
    removed["removed"] = bool(touched)
    removed["touched"] = touched
    return removed
