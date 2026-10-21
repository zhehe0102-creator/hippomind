"""export --since 增量导出 + 幂等重导（前景轮八，验证后关闭项）。"""
import datetime
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    """A port nothing is listening on, so the CLI takes its offline path."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ExportSinceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-since-test-")
        self.addCleanup(self.tmp.cleanup)
        self.led = Path(self.tmp.name) / "d.sqlite"
        self.rt = Path(self.tmp.name) / "rt"

    def _run(self, *args):
        # Pin a free port: without it the CLI resolves the global default 8765
        # and a daemon serving a *different* ledger (the live one) answers 401,
        # so the write path under test is never reached.
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", *args,
             "--ledger", str(self.led), "--runtime-dir", str(self.rt),
             "--port", str(_free_port())],
            capture_output=True, text=True, cwd=str(ROOT))

    def _ingest(self, content, evidence):
        r = self._run("ingest", "--content", content, "--kind", "fact",
                      "--agent", "t", "--evidence", evidence)
        self.assertEqual(r.returncode, 0, r.stderr[-200:])

    def test_since_exports_only_late_rows_and_reimport_idempotent(self):
        for i in range(3):
            self._ingest(f"早期记录 {i}", f"t:{i}")
        since = datetime.datetime.now(datetime.timezone.utc).isoformat()
        time.sleep(0.05)
        for i in range(2):
            self._ingest(f"晚期记录 {i}", f"late:{i}")

        out = Path(self.tmp.name) / "incr.jsonl"
        r = self._run("export", "--since", since, str(out))
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        rows = [json.loads(line) for line in
                out.read_text().splitlines() if line.strip()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("晚期记录" in row["content"] for row in rows))

        fresh = Path(self.tmp.name) / "fresh.sqlite"
        for _ in range(2):  # 同批数据重导两次 → 幂等，不产生重复
            r = self._run_with_ledger(fresh, "import", str(out))
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
        n = sqlite3.connect(fresh).execute(
            "SELECT COUNT(*) FROM memories WHERE lifecycle='active'"
        ).fetchone()[0]
        self.assertEqual(n, 2)

    def _run_with_ledger(self, ledger_path, *args):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", *args,
             "--ledger", str(ledger_path), "--runtime-dir", str(self.rt)],
            capture_output=True, text=True, cwd=str(ROOT))


if __name__ == "__main__":
    unittest.main()
