"""import 来源标记（迭代 56）：批次戳 + legacy 格式兼容 + 幂等。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ImportBatchStampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-impmark-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.SQLiteLedger = SQLiteLedger
        src = SQLiteLedger(self.tmp / "src.sqlite")
        for i in range(5):
            src.ingest({"content": f"导入标记测试 {i}", "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{i}"]})
        subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "export",
             "--ledger", str(self.tmp / "src.sqlite"), str(self.tmp / "dump.jsonl")],
            capture_output=True, text=True, cwd=str(ROOT))

    def _import(self, filename, ledger_name="dst.sqlite"):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "import",
             "--ledger", str(self.tmp / ledger_name),
             str(self.tmp / filename)],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_jsonl_import_stamps_batch_metadata(self):
        r = self._import("dump.jsonl")
        out = json.loads(r.stdout)
        self.assertEqual(out["imported"], 5)
        self.assertTrue(out["batch"].startswith("imp-"))
        dst = self.SQLiteLedger(self.tmp / "dst.sqlite")
        hits = dst.search("导入标记测试", limit=10)
        self.assertTrue(hits)
        for hit in hits:
            self.assertEqual(hit["metadata"].get("import_batch"), out["batch"])
            self.assertIn("import_source", hit["metadata"])

    def test_reimport_same_file_is_idempotent_with_same_batch(self):
        o1 = json.loads(self._import("dump.jsonl").stdout)
        o2 = json.loads(self._import("dump.jsonl").stdout)
        self.assertEqual(o2["duplicate"], 5)
        self.assertEqual(o2["batch"], o1["batch"])

    def test_legacy_hub_export_format_still_supported(self):
        legacy = {"hippomind_memory_hub_export": 2, "events": [
            {"event_type": "fact", "content": f"旧格式条目 {i}",
             "source_agent": "old", "occurred_at": "2026-01-01T00:00:00+00:00",
             "evidence_json": json.dumps([f"old:{i}"]), "metadata_json": "{}"}
            for i in range(3)]}
        (self.tmp / "legacy.json").write_text(json.dumps(legacy, ensure_ascii=False))
        r = self._import("legacy.json", "dst2.sqlite")
        out = json.loads(r.stdout)
        self.assertEqual(out["imported"], 3)
        self.assertIn("-legacy", out["batch"])


if __name__ == "__main__":
    unittest.main()
