"""Data hygiene: detection, safe degradation, and reversibility.

Every test builds a temporary ledger.  The production ledger is never opened,
because the thing under test writes.

The invariants that matter here are not "the counts are right" but:

* a dry-run must not modify anything;
* --apply must degrade, never delete, and must write its rollback list first;
* the two categories must not double-count a row;
* running twice must be a no-op (idempotent);
* the scan must never look at archived (cold) rows, because doing so would
  classify a quarter of a million legitimate archive rows as duplicates.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import data_hygiene  # noqa: E402
from hippomind.distill import classify_unusable  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402


class GateClassificationTests(unittest.TestCase):
    """The shared classifier that both the gate and the scanner use."""

    def test_empty_payload_is_rejected(self):
        for text in ('{"claims":[]}', '[ ]',
                     '只输出 JSON：{"claims":[]}',
                     '提炼（raw_chat）：{"claims":[]}'):
            self.assertEqual("empty_payload", classify_unusable(text), text)

    def test_empty_distill_summary_is_rejected(self):
        self.assertEqual("empty_distill", classify_unusable("提炼（raw_chat）："))

    def test_self_prompt_is_rejected(self):
        prompt = ("你是 HippoMind 的记忆提炼器。只输出 JSON，不要解释。"
                  "从下面的会话片段中提取稳定、可复核的事实 claims。")
        self.assertEqual("self_prompt", classify_unusable(prompt))

    def test_lowercased_prompt_variant_is_rejected(self):
        prompt = "你是 hippomind 的记忆提炼器。只输出 json，不要解释。"
        self.assertEqual("self_prompt", classify_unusable(prompt))

    def test_bare_fragment_is_rejected(self):
        self.assertEqual("noise", classify_unusable("Run:"))

    def test_blank_is_rejected(self):
        self.assertEqual("empty", classify_unusable("   \n\t "))

    def test_real_memories_are_not_rejected(self):
        """The false-positive side: a gate that eats real lessons is worse
        than the pollution it removes."""
        keep = [
            "决定采用 SQLite 作为存储，因为需要 WAL 并发控制。",
            "我使用临时 root 之后，使用 AI 帮我优化三星手机系统。",
            "这个 JSON 配置需要 slot_key 字段才能通过校验。",
            "记忆系统必须支持按时间回溯的查询。",
            "Run: make test 之后所有用例都通过了。",
            "subject/predicate/value 三元组是 claims 的基本结构。",
        ]
        for text in keep:
            self.assertEqual("", classify_unusable(text), "误杀: %s" % text)


_SEQ = [0]


def _ingest(ledger, content, **extra):
    """Insert one row, forcing a distinct identity.

    The ledger de-duplicates on a hash of (class, kind, agent, session, task,
    project, scope, content).  Identical fixtures would therefore collapse to a
    single row and silently defeat every duplicate test, so each insert gets its
    own task_id -- which is also realistic: the live duplicates came from
    different sessions/tasks re-observing the same sentence.
    """
    _SEQ[0] += 1
    record = {
        "content": content,
        "kind": extra.pop("kind", "semantic"),
        "source_agent": extra.pop("source_agent", "claude"),
        "scope": "shared",
        "task_id": extra.pop("task_id", "task-%d" % _SEQ[0]),
        "evidence": [extra.pop("evidence", "ev:%d" % _SEQ[0])],
    }
    record.update(extra)
    return ledger.ingest(record)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-hygiene-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "hub.sqlite"
        self.ledger = SQLiteLedger(self.path)
        self.addCleanup(self.ledger.close)

    def _set_lifecycle(self, memory_id, lifecycle):
        def write(conn):
            conn.execute("UPDATE memories SET lifecycle=? WHERE memory_id=?",
                         (lifecycle, memory_id))
        self.ledger._transaction(write)

    def _scan(self, **kwargs):
        with data_hygiene.HygieneScanner(self.path) as scanner:
            return scanner.scan(**kwargs)

    def test_noise_detected(self):
        _ingest(self.ledger, "你是 HippoMind 的记忆提炼器。只输出 JSON，不要解释。",
                evidence="ev:1")
        _ingest(self.ledger, '{"claims":[]}', evidence="ev:2")
        result = self._scan()
        self.assertEqual(2, result["noise"])
        self.assertEqual(0, result["duplicates"])

    def test_duplicates_detected_and_newest_kept(self):
        body = "决定采用 SQLite 作为存储，因为需要 WAL 并发控制。"
        first = _ingest(self.ledger, body, evidence="ev:1",
                        timestamp="2026-10-01T00:00:00Z")
        second = _ingest(self.ledger, body, evidence="ev:2",
                         timestamp="2026-10-20T00:00:00Z")
        result = self._scan()
        self.assertEqual(1, result["duplicates"])
        target = result["target_rows"][0]
        self.assertEqual(first.memory_id, target["memory_id"],
                         "应保留最新的一条")
        self.assertEqual(second.memory_id, target["duplicate_of"])

    def test_categories_do_not_double_count(self):
        """A row must belong to exactly one category, or the totals lie."""
        _ingest(self.ledger, '{"claims":[]}', evidence="ev:1")
        _ingest(self.ledger, '{"claims":[]}', evidence="ev:2")
        result = self._scan()
        self.assertEqual(2, result["noise"])
        self.assertEqual(0, result["duplicates"])
        self.assertEqual(2, result["targets"])

    def test_cold_rows_are_never_scanned(self):
        """The data-loss guard: archived rows are repetitive by nature."""
        body = "决定采用 SQLite 作为存储，因为需要 WAL 并发控制。"
        hot = _ingest(self.ledger, body, evidence="ev:1")
        cold = _ingest(self.ledger, body, evidence="ev:2")
        self._set_lifecycle(cold.memory_id, "cold")
        result = self._scan()
        self.assertEqual(1, result["live_rows"])
        self.assertEqual(0, result["duplicates"], "归档行不得被算作重复")
        self.assertTrue(hot.memory_id)

    def test_quarantined_rows_are_not_rescanned(self):
        body = "决定采用 SQLite 作为存储，因为需要 WAL 并发控制。"
        _ingest(self.ledger, body, evidence="ev:1")
        second = _ingest(self.ledger, body, evidence="ev:2")

        def write(conn):
            conn.execute("UPDATE memories SET quality_state='quarantine' "
                         "WHERE memory_id=?", (second.memory_id,))
        self.ledger._transaction(write)
        result = self._scan()
        self.assertEqual(0, result["duplicates"], "已隔离的行不再重复处理")

    def test_category_filter(self):
        _ingest(self.ledger, '{"claims":[]}', evidence="ev:1")
        body = "决定采用 SQLite 作为存储，因为需要 WAL 并发控制。"
        _ingest(self.ledger, body, evidence="ev:2")
        _ingest(self.ledger, body, evidence="ev:3")
        only_noise = self._scan(categories=("noise",))
        self.assertEqual(1, only_noise["noise"])
        self.assertEqual(0, only_noise["duplicates"])
        only_dup = self._scan(categories=("duplicates",))
        self.assertEqual(0, only_dup["noise"])
        self.assertEqual(1, only_dup["duplicates"])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-hygiene-apply-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "hub.sqlite"
        self.ledger = SQLiteLedger(self.path)
        self.addCleanup(self.ledger.close)
        self.rollback = Path(self.tmp.name) / "rollback.json"

    def _seed(self):
        _ingest(self.ledger, '{"claims":[]}', evidence="ev:1")
        body = "决定采用 SQLite 作为存储，因为需要 WAL 并发控制。"
        _ingest(self.ledger, body, evidence="ev:2",
                timestamp="2026-10-01T00:00:00Z")
        _ingest(self.ledger, body, evidence="ev:3",
                timestamp="2026-10-20T00:00:00Z")

    def _run(self, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = data_hygiene.main(["--ledger", str(self.path)] + list(args))
        return code, buffer.getvalue()

    def _states(self):
        with self.ledger._reading() as conn:
            return {str(row["memory_id"]): str(row["quality_state"])
                    for row in conn.execute("SELECT memory_id, quality_state FROM memories")}

    def test_dry_run_changes_nothing(self):
        self._seed()
        before = self._states()
        code, output = self._run()
        self.assertEqual(0, code)
        self.assertEqual(before, self._states(), "dry-run 不得修改账本")
        self.assertIn("DRY-RUN", output)
        self.assertFalse(self.rollback.exists())

    def test_apply_degrades_but_never_deletes(self):
        self._seed()
        before = self._states()
        code, _ = self._run("--apply", "--rollback-file", str(self.rollback))
        self.assertEqual(0, code)
        after = self._states()
        self.assertEqual(set(before), set(after), "不得物理删除任何行")
        self.assertIn("quarantine", after.values())
        # The surviving representative of the duplicated body must stay live:
        # retiring every copy would remove the fact entirely.
        self.assertEqual("accepted", after[self._keeper_id()])

    def _keeper_id(self):
        """The newest row holding the duplicated (non-noise) body."""
        with self.ledger._reading() as conn:
            return str(conn.execute(
                "SELECT memory_id FROM memories WHERE content LIKE '决定采用 SQLite%' "
                "ORDER BY created_at DESC LIMIT 1").fetchone()[0])

    def test_rollback_file_written_before_apply(self):
        self._seed()
        code, output = self._run("--apply", "--rollback-file", str(self.rollback))
        self.assertEqual(0, code)
        self.assertTrue(self.rollback.is_file())
        payload = json.loads(self.rollback.read_text(encoding="utf-8"))
        self.assertEqual("quarantine", payload["degraded_state"])
        self.assertGreater(payload["count"], 0)
        self.assertIn("rollback", output)
        sql = self.rollback.with_suffix(".rollback.sql")
        self.assertTrue(sql.is_file(), "必须同时给出可直接执行的回滚 SQL")
        self.assertIn("UPDATE memories", sql.read_text(encoding="utf-8"))

    def test_rollback_restores_every_row(self):
        self._seed()
        before = self._states()
        self._run("--apply", "--rollback-file", str(self.rollback))
        self.assertNotEqual(before, self._states())
        sql = self.rollback.with_suffix(".rollback.sql").read_text(encoding="utf-8")
        statements = [line for line in sql.splitlines()
                      if line.startswith("UPDATE")]
        def write(conn):
            for statement in statements:
                conn.execute(statement)
        self.ledger._transaction(write)
        self.assertEqual(before, self._states(), "回滚必须完整还原")

    def test_apply_is_idempotent(self):
        self._seed()
        self._run("--apply", "--rollback-file", str(self.rollback))
        first = self._states()
        code, output = self._run("--apply",
                                 "--rollback-file", str(self.rollback) + ".2")
        self.assertEqual(0, code)
        self.assertEqual(first, self._states(), "第二次 apply 不得再改任何行")
        self.assertIn("0", output)

    def test_json_output(self):
        self._seed()
        code, output = self._run("--json")
        self.assertEqual(0, code)
        payload = json.loads(output)
        for key in ("live_rows", "distinct_contents", "noise", "duplicates",
                    "targets", "by_reason", "degraded_state", "dry_run"):
            self.assertIn(key, payload)
        self.assertTrue(payload["dry_run"])
        self.assertEqual("quarantine", payload["degraded_state"])

    def test_missing_ledger_returns_2(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = data_hygiene.main(["--ledger", str(Path(self.tmp.name) / "nope.sqlite")])
        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
