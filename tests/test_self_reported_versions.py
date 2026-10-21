"""Every version the system reports about itself must be the same version.

Three hand-maintained literals had drifted independently of the release: the MCP
``serverInfo`` announced 0.9.1, the Codex installer handshake announced 0.7 (a
version that never shipped), and ``doctor`` printed 0.9.2 while ``version``
printed 1.0.0.  Agents and humans compare these strings when diagnosing an
installation, so an inconsistency is worse than an old number: it looks like two
different builds are running.

Like ``tests/test_doc_claims.py`` this asserts *consistency*, never a specific
version, so releasing a new number needs no edit here.
"""

from __future__ import annotations

import json
import unittest

import hippomind


class SelfReportedVersionTests(unittest.TestCase):
    def test_mcp_initialize_reports_the_package_version(self):
        from hippomind.mcp import SERVER_INFO

        self.assertEqual(SERVER_INFO["version"], hippomind.__version__)
        # A client never reads the constant; it reads the initialize handshake.
        # Drive the real stdio server so the assertion covers what an editor sees.
        import os
        import subprocess
        import sys
        import tempfile
        root = os.path.dirname(os.path.abspath(hippomind.__file__))
        with tempfile.TemporaryDirectory(prefix="hippo-mcp-") as home:
            request = "".join(json.dumps(line) + "\n" for line in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "probe", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ))
            proc = subprocess.run(
                [sys.executable, "-m", "hippomind.mcp", "--ledger",
                 os.path.join(home, "m.sqlite")],
                input=request, capture_output=True, text=True, timeout=60,
                cwd=os.path.dirname(root),
                env=dict(os.environ, PYTHONPATH=os.path.dirname(root)))
        lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        initialize = next((entry for entry in lines if entry.get("id") == 1), None)
        self.assertIsNotNone(initialize, proc.stderr[-300:])
        info = initialize["result"]["serverInfo"]
        self.assertEqual(info["version"], hippomind.__version__)
        self.assertEqual(info["name"], "hippomind-memory-hub")

    def test_installer_client_info_reports_the_package_version(self):
        from hippomind.agent_install import INSTALLER_CLIENT_INFO

        self.assertEqual(INSTALLER_CLIENT_INFO["version"], hippomind.__version__)
        self.assertEqual(INSTALLER_CLIENT_INFO["name"], "hippomind-hook-installer")

    def test_no_module_hardcodes_another_version(self):
        """Source must not contain a fourth competing self-reported number."""
        import os
        import re
        root = os.path.dirname(os.path.abspath(hippomind.__file__))
        # Any dotted numeric literal in a "version" field, not just 0.x: the
        # defect class is "a version string that is not the package version".
        pattern = re.compile(r'"version":\s*"(\d+\.\d+(?:\.\d+)?)"')
        offenders = []
        for name in sorted(os.listdir(root)):
            # exFAT/APFS resource forks (._foo.py) are binary junk, not source.
            if not name.endswith(".py") or name.startswith("._"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                for number in pattern.findall(handle.read()):
                    offenders.append(f"hippomind/{name}: {number}")
        self.assertEqual([], offenders,
                         "自述版本必须来自包版本，不要写字面量：" + "; ".join(offenders))

    def test_cli_commands_agree(self):
        """version --json and doctor must not disagree about what is installed."""
        import io
        import json
        from contextlib import redirect_stdout

        from hippomind.cli import main
        import tempfile
        with tempfile.TemporaryDirectory(prefix="hippo-ver-") as home:
            base = ["--ledger", os_path(home, "h.sqlite"),
                    "--runtime-dir", os_path(home, "rt")]
            first = io.StringIO()
            with redirect_stdout(first):
                self.assertEqual(0, main(["version", "--json"] + base))
            second = io.StringIO()
            with redirect_stdout(second):
                # ``doctor`` is a readiness gate: it exits 1 when a check fails
                # (the daemon is unreachable in this fixture).  What this test
                # pins is that the *reported version* agrees, not the exit code.
                main(["doctor", "--format", "json"] + base)
            version = json.loads(first.getvalue())["package"]
            doctor = json.loads(second.getvalue())
        self.assertEqual(version, hippomind.__version__)
        self.assertEqual(str(doctor.get("version", "")), version,
                         "doctor and version disagree: " + json.dumps(
                             doctor.get("version"), ensure_ascii=False))


def os_path(directory, name):
    import os
    return os.path.join(directory, name)


if __name__ == "__main__":
    unittest.main()
