"""Every path named in the build/analysis configuration must exist.

Two real incidents motivated this file:

* the published source stayed at schema v5 while the live ledger advanced to v8,
  so no code could open 1.5M memories;
* after ``ledger.py``/``cli.py`` were split into packages, ``[tool.mypy].files``
  still named the old module paths and silently checked nothing.

This is a cheap, dependency-free guard: it parses pyproject.toml and CI, and
asserts that each declared path resolves -- catching config drift the moment it
is introduced instead of when a tool quietly does less work than it claims.
"""

from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


class ConfigPathTests(unittest.TestCase):
    def test_mypy_files_exist(self):
        text = _read("pyproject.toml")
        block = re.search(r"\[tool\.mypy\](.*?)(?:^\[|\Z)", text, re.S | re.M)
        self.assertIsNotNone(block, "[tool.mypy] section vanished")
        files = re.search(r"^files = \[(.*?)\]", block.group(1), re.M | re.S)
        self.assertIsNotNone(files, "[tool.mypy] lost its files list")
        paths = [p.strip().strip('"').strip("'") for p in files.group(1).split(",") if p.strip()]
        self.assertTrue(paths, "mypy scope is empty")
        missing = [p for p in paths if not os.path.exists(os.path.join(ROOT, p))]
        self.assertEqual([], missing, f"[tool.mypy].files 指向不存在的路径: {missing}")

    def test_console_entry_point_resolves(self):
        """``hippomind = "hippomind.cli:main"`` must still import and bind."""
        text = _read("pyproject.toml")
        module, attribute = re.search(
            r'^hippomind = "([\w.]+):(\w+)"', text, re.M).groups()
        import importlib
        bound = getattr(importlib.import_module(module), attribute, None)
        self.assertTrue(callable(bound), f"{module}:{attribute} is not callable")

    def test_split_modules_are_not_resurrected_as_files(self):
        """If a module became a package, nothing may still import the old file."""
        for name in ("ledger", "cli"):
            package = os.path.join(ROOT, "hippomind", name)
            legacy = package + ".py"
            if os.path.isdir(package):
                self.assertFalse(os.path.exists(legacy),
                                 f"{legacy} 与 {name}/ 包并存，导入结果取决于打包顺序")

    def test_ci_steps_reference_existing_paths(self):
        ci = os.path.join(ROOT, ".github", "workflows", "ci.yml")
        if not os.path.exists(ci):
            self.skipTest("no CI workflow in this tree")
        missing = []
        for path in re.findall(r"python3? (\S+\.py)", _read(".github/workflows/ci.yml")):
            if not os.path.exists(os.path.join(ROOT, path)):
                missing.append(path)
        self.assertEqual([], missing, f"CI 调用了不存在的脚本: {missing}")


if __name__ == "__main__":
    unittest.main()
