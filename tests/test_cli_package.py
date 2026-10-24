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
    """Canonical, version-stable rows for one argparse tree.

    Every field argparse renders its text from is included -- dest, default,
    nargs, choices, type, required, metavar and help -- so the frozen digests
    below also pin the visible ``--help`` wording.  ``prog`` is deliberately
    left out: argparse renames its own section headings across releases
    (3.11 turned ``optional arguments:`` into ``options:``), which is a CPython
    change and not this CLI's contract.
    """
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
                str(bool(a.required)), "metavar=%r" % (a.metavar,),
                "help=%r" % (a.help,),
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
#: One deliberate exception, documented with the fix: ``pin``'s positional was
#: rebound from the unusable dest ``memory-id`` to ``memory_id`` + metavar when
#: the always-crashing ``hippomind pin`` was repaired.  Its *rendered* text did
#: not move, and test_pin_help_text_is_unchanged pins that separately.
PARSER_SURFACE = {
    "(root)": ("ac8f5163bccc8e6dad9160c42ae383f4c1b8ffb315eefa46108877f86307d259", 44),
    "agent-status": ("c6ce3680226dad9d5019b2ce233e1580998dfc861921c6e3ca1f02ff8e4ed5bf", 7),
    "auto-context": ("bcd24f3b2a2dd0f637d61a2f1dc9744cf08d71cef0717f5dfc97203004d1af8c", 22),
    "backup": ("3ba2b08b93a835ababbf4b62a644915a0df48e7e998837500580929290eadfd5", 6),
    "checkup": ("f423e1adb4f6d6617acf985908bb9ffd532c4680269f621eaa066e566c5105c5", 7),
    "conflicts": ("aef6f57aa52644e95131795e33dbc4b0882d949530e3d9504e584982c149c28f", 8),
    "context": ("d3835f73f8eb2364e3b1e08e6281f45755352304564bd108ac8ec536f3b3b5ce", 22),
    "digest": ("88bba3b9a9c08a9acb42c5de1dd72fdca23a0185134d87b6f15ff295aae5a98e", 11),
    "semantic-build": ("28dfc614186893793d48b2df0905381a02f7dcbbd0b1b1ce0b1cbf1bb60b3651", 6),
    "discover-agents": ("2e0dbd54e137ff04e8f0e4a6659d7b161494d53088ff22f152227e8ea019bbf7", 6),
    "distill": ("366a9e30187d71e3583bfc428372802792970f8ae395588b676037ab39ecec65", 9),
    "doctor": ("b953cb2db3bf28e7d6b06c9fcc2a47ee04bd7218846ceadd934aa47702d93d7f", 7),
    "export": ("a81c0f81a8cdb1b0739bf5a7e4dc23370113a13a422515cbfff1582897cc4790", 13),
    "facts": ("e6a51afb9a8d74116eb93e92a8f52ef59f2ccc7d70bfff83018e8d32fcb324ea", 9),
    "feedback": ("49fabd11ed0bf3931e58f04c5089040529ea1b11a5e6a0101fc2c5c4c9f3b376", 10),
    "forget": ("09ae72b33f1514839b071275b26b6dcf675cbfdd0e54e219c825e7a600eecb39", 12),
    "graph": ("d0de644b24f9fb148ec1aac11e4ec6f3588463af7f005e14075cf73ea53aa91f", 10),
    "import": ("7cef9cf2068a62219b1e087179f1c9c82ddd24ca9f419902809f9ee099f89735", 9),
    "ingest": ("fcbaaa978a7eb02003d9c59c155c2ada1046bbc303f4cd3f30a2badf471a0aa6", 16),
    "init": ("fbb37d4a35997259544a616bd00864ce25d48e51c7ffbe5080cab0cf5e25a5a4", 7),
    "inject": ("715fc66c32d10ce5a93b563cb0e119a73371e776b774881b404f4c397e1bc9a1", 7),
    "install-agents": ("b66764b36408dc6961cde5d40cc84b0f8fa82cbf3d3ad9b7cf0054c17c646992", 10),
    "install-auto": ("447f0eb79f1275d01df9140c413efa05515a0e03e7dc5a4f73ff1b512d83533d", 5),
    "login-install": ("c2db242076f57925e4c0ae8615f316349b881aa5f5c5979b0d3606a2ea2bd2b2", 9),
    "login-remove": ("9c3de441f2f0bdafae81d51c097cc6010804c9f51d328961caf66e9506243515", 1),
    "maintain": ("af3e553a5c81b9756656953f61dfcd1c6036915f1e71775387c19b936c919d78", 15),
    "monitor": ("2aa796bd4c54d9f4b08b7391a1f40cd2302628f2fd8b371939cc0a0ceaf000ca", 8),
    "pin": ("013be85247f25382924bfa07631cb6e3bf105cd0ee3fc429672d714e0038466d", 7),
    "pins": ("997fe940bdb19a55d118c315f5c20b3a89a20c157b56f789dc5634420a70f3f2", 8),
    "recovery-check": ("705153b2fee3eeade6ec421a0e59a0513470f2e99a1ea2fad0daf616ec0f4ade", 5),
    "redact-raw": ("625e61fc9227b0085936b70d89ed8d34503f9122957eb410383281e257074866", 7),
    "resolve": ("f9d0a3664db5a0ea7220fbc3caefdaaa011efc491b1671746aef19ecc6e7b65d", 8),
    "restore": ("64134b047bbeebb2eea83f238187a8b18c9aaeceb0294be017c388ed12e76217", 7),
    "scan-raw": ("2bfcc94e26f159637996868cae972595c7d066b96e3fee9a864cc1cc4af4d703", 6),
    "skills": ("6cc88b4798ca14e83f2fa204aee746991ad1d199c9bde035e1da858d51be7567", 8),
    "start": ("2a5aeef5581011b717bb389df5afb58d739a33f18158b2b4c9fe4bd641c9dbcb", 11),
    "status": ("03c64382fa63105c16bd68d6df76a5839be13fef7185d9dda3c386a3e1ca550e", 5),
    "stop": ("602eebd0f3d42421f8b2eef6470d81c2c0b45f0bc02f65d99796e39a2796cf91", 5),
    "sync": ("f0a185df340f6cfecbb8a2311ddb133311cc2159638d64142bbf72c99644389d", 7),
    "sync-transcripts": ("c00eb4c214f181302080df54b05453b37baef076fb081e33e7542cea738de178", 6),
    "ui": ("80c93fc2ac46263085db6ba5ea85617b69513e3231cfe690ded5b10fc9f52098", 6),
    "version": ("676dd0d22db8422bdf37d73438e472a2e1172b201bcc8e89e1d1ca3a4f9972f0", 6),
    "watch": ("a2964f1b7ccd6b0d2d93dd5429dadba056d4ecc7a7afd6b0edeb8567627ca315", 8),
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
    "cmd_start", "cmd_status", "cmd_stop", "cmd_sync", "cmd_sync_transcripts", "cmd_distill",
    "cmd_digest", "cmd_semantic_build", "cmd_ui",
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
        self.assertEqual(42, len(handlers))
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
