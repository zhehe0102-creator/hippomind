"""The ``hippomind.cli`` package must expose exactly what the single module did.

Why this file exists
--------------------
``hippomind/cli.py`` was split into the ``hippomind/cli/`` package without
changing behaviour.  Nothing in the old suite proves that, because the tests
exercise individual commands, not the *contract*: the entry points
``hippomind.cli:main`` / ``python -m hippomind`` / ``python -m hippomind.cli``,
the argparse surface (every sub-command, option, default and handler), and the
``hippomind.cli.<name>`` attributes the suite patches and the native Codex hook
imports.

What this pins
--------------
1. Every sub-command's option table (dest, default, nargs, choices, type,
   action, required) hashes to the value captured from the pre-split module,
   and every sub-command is still bound to the same handler name.
2. Every public name the single-file module exposed still resolves.
3. The helpers the suite patches (``_paths``, ``_request``,
   ``_start_or_reuse_daemon``, ``_restart_daemon``) are still defined in
   ``hippomind.cli`` itself, so ``mock.patch("hippomind.cli._request")`` keeps
   the same reach, and ``hippomind.codex_hook.fetch_auto_context`` is still the
   same object as ``hippomind.cli.fetch_auto_context``.
4. All documented invocation routes still work, including that the split did
   not break the repo-root path the integrations are generated from.

The digests are frozen values, deliberately: changing an option, a default or a
handler must be an explicit decision, not a side effect of a refactor.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rows(build_parser):
    """Canonical, version-stable rows for one argparse tree."""
    import argparse

    out = []

    def dump(parser, path):
        for a in parser._actions:
            opts = ",".join(a.option_strings) or a.dest
            if isinstance(a, argparse._SubParsersAction):
                choices = "sub=%s" % ",".join(sorted(getattr(a, "choices", {}) or {}))
            else:
                choices = repr(a.choices)
            out.append("|".join([
                path, opts, a.dest, repr(a.default), repr(a.nargs), choices,
                str(getattr(a.type, "__name__", a.type)), type(a).__name__,
                str(bool(a.required)),
            ]))
        sub = getattr(parser, "_subparsers", None)
        if sub is None:
            return
        for g in sub._group_actions:
            for name in (getattr(g, "choices", None) or {}):
                child = g.choices[name]
                handler = child._defaults.get("func")
                out.append("%s|SUBCOMMAND %s|func=%s"
                           % (path, name, getattr(handler, "__name__", repr(handler))))
                dump(child, path + " " + name)

    dump(build_parser(), "root")
    return out


def _grouped(rows):
    """Rows per sub-command; '(root)' holds the top-level parser's own rows."""
    per = {"(root)": []}
    for row in rows:
        path = row.split("|", 1)[0].strip().split(" ")
        if path == ["root"]:
            per["(root)"].append(row)
            continue
        head = path[1] if path[0] == "root" and len(path) > 1 else path[0]
        per.setdefault(head, []).append(row)
    return per


def _digest(rows):
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


#: Captured from the pre-split single-file ``hippomind/cli.py``: sub-command ->
#: (sha256 of its option table, row count). '(root)' is the top-level parser.
PARSER_SURFACE = {
    "(root)": ("204fed7438ee2ea4d2220bddd33e9c4effb653482b2533fb9487df6b0a8c0279", 39),
    "agent-status": ("438e0a06002fac39467c14bc09b6babe6ee6d76c9dc41839d3c466f02cbc0bd1", 7),
    "auto-context": ("7298c4afdd5e05e6d59db13549452c4e2a41bdde6a5b6dbb117da1fcf9614260", 22),
    "backup": ("be21c2a2b809dfeb6c0265eaa0614bde22ef03cd0d27ed6fb06e5186c37abd47", 6),
    "checkup": ("f9a15fc49afbaa1fdff02c2b0a102fdf0f4586b99a213d5504f79b2dc929a816", 7),
    "conflicts": ("3e1a6772ff8b1815507ce04d31290a794832cdf248d898d8e5816528e4f75cad", 8),
    "context": ("10720e7c6128c88ad80bb01973c0b641a9bc5cdb995ff37c005015dbeed98d5e", 22),
    "discover-agents": ("c88800b68f735757f8888ee46c86bb4385d1652a607dece2ec463453a8922bf9", 6),
    "doctor": ("e47583291643b78b4535e80aba4e7c3a10ace826ff44665d5b7405a288fd4035", 7),
    "export": ("97d8bcc40d5a69fa8f9f508ef339b2460f174aa399b72b6687365759ece1044d", 13),
    "facts": ("6fd7cde92b64265ee7ff9b845d1e7614c6030f6ede4b8970442cc2de5d33a904", 9),
    "feedback": ("5f0c032c1a1356d01d4ae70b6297d006603bcc4a286225ae78c0e4f3e3986fe7", 10),
    "forget": ("0ebec204a071c69fc896a053eb3b7e9210bbc38d7098b975fa34534dc7c2d2a8", 12),
    "graph": ("0376060228ee1a21294ec2b69ebc1e75f3cd6fe4256b5dd29b84dbeeaacf95b2", 10),
    "import": ("5237dee4dc9600a021235c9ddbd98c6c0ede826cda4a7647ea26aa304c95984f", 9),
    "ingest": ("864dacc3187255bb93209ca9a6737d3f26de9995695424b21d8e2e60425251cf", 16),
    "init": ("3b3d860d7c5d625e7a913677925cbb769b2fbbbe72c23eeb2121335384939c42", 7),
    "inject": ("d4aaa78949c7b98f5b9e570a584c8a44385862613affa3b6e283aab274938234", 7),
    "install-agents": ("4644ea500d1e8768ef6e3380675f2de770d0cfcd5518583c3569d66c8ab7442b", 10),
    "install-auto": ("ab7c897c7e313710b7fab11fc725b123b4526c096367a12b344aa9a5144e3445", 5),
    "login-install": ("802df563a3c2c63bdbde4efb154dff6b40480a2e531c8cd365c548dd7acdd527", 9),
    "login-remove": ("4a83c8cac43e06b745b67176fbf08a9237b29b6167de671fcae51f876af27725", 1),
    "maintain": ("c0d16821861d5afd67cf831ee3241cd146a2694222692fb9cb686dd19df6bbb6", 15),
    "monitor": ("c4a7957b78d9d0fbf0aaf1e8251d2fd4f745ef75bec2049a2ea83cca569914b9", 8),
    "pin": ("264a24dab4e195cd55882fc104da26cd0bcb5f489ebc9801f5a1922d04f86b21", 7),
    "pins": ("a00513e24af6f4306d55b26d8b13ee3a8b54568cc2a14f0fc5c3507a698b2f4f", 8),
    "recovery-check": ("a4d138fd21ed00340589b8a8678d22f01cf68afe1dbb965afa46795e18c41866", 5),
    "redact-raw": ("97258ecdbfbbc538b84c34d97d37fbdb19e1d8ff66db2243616bfdf97d70d0dc", 7),
    "resolve": ("11dc795afb3c71f14ae1aa21a5b02a1ffc4c2ff23daefe22ae8c621b7942c8f8", 8),
    "restore": ("27ca55fe9f223db71c8e9a595fa21977005f9766736748c3e43362963c2296da", 7),
    "scan-raw": ("b1b9d4565e993dc7ac230a5ab27f04cb15fdb1d5ff305677c0df293680608bb6", 6),
    "skills": ("5dc3324b0c8f0018cc0984a1a5ba49c738a81ae3605a1e50a3a2d191a4d8e74a", 8),
    "start": ("d18f69577166c6d0f533b94727f1eb0f1f7341499caa760f4b429315e9d8d21c", 11),
    "status": ("d838a3ec04725c34468bc14675f982b34a500bf43452414f7da60a7c4aa12908", 5),
    "stop": ("433fae3cc8c6e4fa1109d85b1968864d5b7beff8d9015c60e967b1f305622611", 5),
    "sync": ("76a415aa7946d43012256400a1c86b6b05d4e80d2d78bdf4ccfbeeada26930fa", 7),
    "version": ("044ee9154d69821de418f8b76c846c0ec0931749391eb38c0248f6adf620c557", 6),
    "watch": ("a20b8887569ca48b5192f789b9a785f676ea3c96147fd4db98531a76b442e9bc", 8),
}

#: Every public attribute name of the pre-split single-file module.  Names, not
#: type reprs: ``type(x).__name__`` of a few typing constructs differs between
#: the Python versions CI runs (3.9/3.11/3.12).  The package may add submodule
#: names; it may drop none of these.
CLI_FACE = frozenset([
    "API_VERSION", "AUTO_MAX_CHARS", "AUTO_MAX_ITEMS", "AUTO_TRIGGER_MODES",
    "AgentDiscovery", "Any", "ArchiveWatcher", "ContextPackBuilder",
    "ContextPackOptions", "DEFAULT_MAX_CHARS", "DEFAULT_MAX_TOKENS", "Dict",
    "HashingNGramEmbedding", "InjectionState", "LedgerError", "Mapping",
    "Optional", "PROMPT_CLOSE", "PROMPT_OPEN", "Path",
    "RuntimePaths", "SCHEMA_VERSION", "SERVER_VERSION", "SQLiteLedger",
    "_agent_settings", "_agent_token", "_auto_context_body", "_auto_context_request",
    "_backup_registry_path", "_connection_arguments", "_daemon_lifecycle_lock", "_daemon_serves_ledger",
    "_health", "_health_for_paths", "_ingest_record", "_launch_agent_path",
    "_launchd_path_with_node", "_legacy_health_is_owned", "_load_backup_registry", "_monitor_snapshot",
    "_pack_body", "_paths", "_port", "_print",
    "_reject_restricted_direct", "_render_checkup_junit", "_render_checkup_md", "_request",
    "_restart_daemon", "_save_backup_registry", "_secure_local_file", "_sha256_of",
    "_start_or_reuse_daemon", "_start_or_reuse_daemon_locked", "_verified_scoped_credential", "agent_auto_rules",
    "agent_install_status", "annotations", "argparse", "build_auto_context",
    "build_parser", "cmd_agent_status", "cmd_auto_context", "cmd_backup",
    "cmd_checkup", "cmd_conflicts", "cmd_context", "cmd_discover_agents",
    "cmd_doctor", "cmd_export", "cmd_facts", "cmd_feedback",
    "cmd_forget", "cmd_graph", "cmd_import", "cmd_ingest",
    "cmd_init", "cmd_inject", "cmd_install_agents", "cmd_install_auto",
    "cmd_login_install", "cmd_login_remove", "cmd_maintain", "cmd_monitor",
    "cmd_pin", "cmd_pins", "cmd_recovery_check", "cmd_redact_raw",
    "cmd_resolve", "cmd_restore", "cmd_scan_raw", "cmd_skills",
    "cmd_start", "cmd_status", "cmd_stop", "cmd_sync",
    "cmd_version", "cmd_watch", "contextlib", "default_dsh_sessions_root",
    "extract_memory_request", "fetch_auto_context", "getpass", "hashlib",
    "install_agent_rules", "install_agents", "integration_tier", "json",
    "ledger_identity", "load_agent_config", "load_runtime_state", "main",
    "os", "plistlib", "provider_manifest", "read_local_token",
    "render_untrusted_context", "run_daemon", "shutil", "sqlite3",
    "subprocess", "sys", "tempfile", "time",
    "urllib", "utc_now",
])


def _run(module, *args):
    return subprocess.run([sys.executable, "-m", module, *args],
                          capture_output=True, text=True, cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": ROOT})


class CliPackageSurfaceTests(unittest.TestCase):
    """The split must not have touched the CLI's contract with the outside."""

    @classmethod
    def setUpClass(cls):
        import hippomind.cli

        cls.cli = hippomind.cli
        cls.rows = _rows(cls.cli.build_parser)
        cls.grouped = _grouped(cls.rows)

    def test_every_subcommand_option_table_is_unchanged(self):
        self.assertEqual(sorted(self.grouped), sorted(PARSER_SURFACE),
                         "the set of sub-commands changed")
        for name, (digest, count) in sorted(PARSER_SURFACE.items()):
            rows = self.grouped[name]
            self.assertEqual(len(rows), count, "%s: option count changed" % name)
            self.assertEqual(_digest(rows), digest,
                             "%s: argparse surface differs from the frozen "
                             "single-module CLI" % name)

    def test_every_subcommand_binds_the_same_handler(self):
        handlers = {}
        for row in self.rows:
            if "|SUBCOMMAND " in row:
                name = row.split("|SUBCOMMAND ")[1].split("|")[0]
                handlers[name] = row.rsplit("func=", 1)[1]
        self.assertEqual(37, len(handlers))
        for name, handler in sorted(handlers.items()):
            self.assertTrue(callable(getattr(self.cli, handler)),
                            "%s -> %s is not reachable on hippomind.cli"
                            % (name, handler))

    def test_public_face_is_preserved(self):
        present = {n for n in dir(self.cli) if not n.startswith("__")}
        self.assertEqual(sorted(CLI_FACE - present), [],
                         "names the single-file module exposed are gone")

    def test_command_and_helper_face_is_callable(self):
        for name in sorted(CLI_FACE):
            if name.startswith("cmd_") or name in {"main", "build_parser",
                                                   "fetch_auto_context"}:
                self.assertTrue(callable(getattr(self.cli, name)),
                                "%s is no longer callable" % name)

    def test_patched_helpers_stay_in_the_package_module(self):
        """``mock.patch("hippomind.cli._x")`` must keep reaching its callers."""
        for name in ("_paths", "_request", "_start_or_reuse_daemon",
                     "_restart_daemon", "fetch_auto_context", "cmd_auto_context"):
            self.assertEqual("hippomind.cli", getattr(self.cli, name).__module__,
                             "%s moved out of hippomind.cli: patching it would "
                             "no longer affect the auto-context path" % name)

    def test_codex_hook_shares_the_same_fetch_helper(self):
        hook = importlib.import_module("hippomind.codex_hook")
        self.assertIs(hook.fetch_auto_context, self.cli.fetch_auto_context)

    def test_entry_point_target_resolves(self):
        """pyproject declares ``hippomind = "hippomind.cli:main"``."""
        ep_main = getattr(importlib.import_module("hippomind.cli"), "main")
        self.assertTrue(callable(ep_main))
        self.assertIs(ep_main, self.cli.main)

    def test_main_is_reachable_as_a_from_import(self):
        from hippomind.cli import main

        self.assertTrue(callable(main))


class CliPackageEntryPointTests(unittest.TestCase):
    """All documented invocation routes must still work after the split."""

    def test_module_entry_via_package_run(self):
        result = _run("hippomind.cli", "--help")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("usage: hippomind", result.stdout)

    def test_module_entry_via_hippomind_run(self):
        result = _run("hippomind", "version", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('"package": "1.0.0"', result.stdout)

    def test_subcommand_help_lists_every_command(self):
        result = _run("hippomind.cli", "--help")
        self.assertEqual(0, result.returncode, result.stderr)
        for name in PARSER_SURFACE:
            if name != "(root)":
                self.assertIn(name, result.stdout)

    def test_unknown_command_still_exits_two(self):
        result = _run("hippomind.cli", "definitely-not-a-command")
        self.assertEqual(2, result.returncode)
        self.assertIn("invalid choice", result.stderr)

    def test_repo_root_path_is_unchanged_by_the_split(self):
        """``cli.py`` became ``cli/``, so its ``__file__`` parents index had to
        shift; the generated rule must still reference the repository root and
        not the package directory itself."""
        result = _run("hippomind.cli", "install-auto", "--print-only",
                      "--agent", "codex")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(os.path.realpath(ROOT), result.stdout)
        self.assertNotIn(os.path.join(os.path.realpath(ROOT), "hippomind", "cli"),
                         result.stdout)


if __name__ == "__main__":
    unittest.main()
