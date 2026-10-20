"""The hub must never depend on the demonstration package.

HippoMind ships two products in one distribution:

* the **hub** (storage/ledger, retrieval, daemon, MCP, CLI, integrations);
* the **demo** (grid-world hippocampus: ``hippocampus``, ``memory``, ``drives``,
  ``mind``, ``grid_world``, ``agent``, ``llm_agent``, the matplotlib
  visualizers and ``main``).

Before 2026-10 the hub imported ``token_sim`` from ``hippocampus.py``, so every
``import hippomind.ledger`` dragged 1.4k lines of demo code (and, transitively,
the visualization stack) into the daemon, and ``__init__`` eagerly imported both
halves.  The text primitives moved to ``hippomind.textutil`` and ``__init__``
became lazy.  These tests pin the boundary so the dependency can never reappear
while the package is being reorganised (ledger.py / cli.py are being split into
packages right now, so the hub set is discovered dynamically, never hardcoded).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from typing import Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "hippomind")

#: The demo modules.  Only this set may import each other freely.
DEMO_MODULES = {
    "hippocampus", "memory", "drives", "mind", "grid_world", "agent",
    "llm_agent", "visualizer", "network_visualizer", "main",
}


def _module_files() -> Set[str]:
    """Top-level module names in hippomind/ (excluding packages and demos)."""
    names = set()
    for entry in os.listdir(PKG):
        full = os.path.join(PKG, entry)
        if os.path.isdir(full):
            if entry.startswith("_") or entry == "__pycache__":
                continue
            # a package's public identity is its directory name
            names.add(entry)
        elif entry.endswith(".py") and not entry.startswith("_"):
            names.add(entry[:-3])
    return names


def _imported_from(source: str, file: str) -> Set[str]:
    """Relative-import targets in one source file (name, package-qualified)."""
    found = set()
    tree = ast.parse(source, filename=file)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                found.add(node.module.split(".")[0])
            elif node.names:  # ``from . import x`` -> x may be a submodule
                found.update(alias.name for alias in node.names)
    return found


class HubDemoBoundaryTests(unittest.TestCase):
    def test_hub_modules_never_import_demo_modules(self):
        offenders = []
        for name in sorted(_module_files() - DEMO_MODULES):
            target = os.path.join(PKG, name + ".py")
            paths = []
            if os.path.isfile(target):
                paths = [target]
            elif os.path.isdir(os.path.join(PKG, name)):
                for dirpath, _dirs, files in os.walk(os.path.join(PKG, name)):
                    paths = [os.path.join(dirpath, f) for f in files if f.endswith(".py")]
            for path in paths:
                try:
                    with open(path, encoding="utf-8") as handle:
                        imports = _imported_from(handle.read(), path)
                except (SyntaxError, UnicodeDecodeError):  # caught by compileall
                    continue
                hit = imports & DEMO_MODULES
                if hit:
                    offenders.append(f"{os.path.relpath(path, ROOT)} -> {sorted(hit)}")
        self.assertEqual([], offenders,
                         "hub 模块禁止 import 演示包（用 hippomind.textutil 共享文本原语）")

    def test_demo_still_functions(self):
        """The boundary must not break the demo half."""
        from hippomind import Hippocampus, Mind
        h = Hippocampus()
        mid = h.store("看到了苹果", action="伸手", valence=1.0)
        self.assertTrue(mid)
        self.assertIsInstance(Mind(h), Mind)

    def test_init_exports_resolve_lazily(self):
        import hippomind
        self.assertEqual("1.0.0", hippomind.__version__)
        # SCHEMA_VERSION must agree with the storage layer, not a copy.
        from hippomind.ledger import SCHEMA_VERSION
        self.assertEqual(hippomind.SCHEMA_VERSION, SCHEMA_VERSION)

    def test_hub_import_does_not_load_demo_or_matplotlib(self):
        code = (
            "import sys; sys.path.insert(0, %r);\n"
            "import hippomind.ledger, hippomind.context_pack, hippomind.watcher;\n"
            "demo=[m for m in ('hippomind.hippocampus','hippomind.drives',"
            "'hippomind.grid_world','hippomind.mind','matplotlib') if m in sys.modules];\n"
            "print(','.join(demo))\n" % ROOT
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=ROOT)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual("", out.stdout.strip(),
                         f"hub 导入不应加载演示/绘图模块: {out.stdout!r}")


if __name__ == "__main__":
    unittest.main()
