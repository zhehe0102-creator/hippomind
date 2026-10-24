"""Pi native session capture (JSONL) — incremental, bounded and deletion-aware."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import pi_capture
from hippomind.ledger import SQLiteLedger
from hippomind.pi_capture import PiSessionScanner, default_pi_session_roots


def _line(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


class PiCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-pi-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.sessions = self.root / "sessions"
        self.workdir = self.sessions / "--work-atlas--"
        self.workdir.mkdir(parents=True)
        self.session_id = "01a0b02b-0a17-72bc-b137-65b9cadad13b"
        self.path = self.workdir / ("2026-10-23T10-00-00-000Z_" + self.session_id + ".jsonl")
        self.scanner = PiSessionScanner(self.ledger, roots=[self.sessions])

    # -- fixtures -------------------------------------------------------
    def _write_session(self, *, deleted=False, name="Atlas 重构"):
        lines = [
            {"type": "session", "version": 3, "id": self.session_id,
             "timestamp": "2026-10-23T10:00:00.000Z", "cwd": "/work/atlas"},
            {"type": "model_change", "id": "m1", "provider": "local", "modelId": "x"},
            {"type": "session_info", "id": "i1", "timestamp": "2026-10-23T10:00:01Z", "name": name},
            {"type": "message", "id": "u1", "timestamp": "2026-10-23T10:00:05.123456Z",
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "请记住：项目使用 SQLite"}]}},
            {"type": "message", "id": "a1", "timestamp": "2026-10-23T10:00:06Z",
             "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "内部推理不应入库"},
                 {"type": "text", "text": "决定采用 WAL 模式。"},
                 {"type": "toolCall", "name": "edit", "arguments": {"path": "db.py"}}]}},
            {"type": "message", "id": "t1", "timestamp": "2026-10-23T10:00:07Z",
             "message": {"role": "toolResult", "content": [{"type": "text", "text": "工具输出不应入库"}]}},
            {"type": "message", "id": "s1", "timestamp": "2026-10-23T10:00:08Z",
             "message": {"role": "system", "content": [{"type": "text", "text": "系统提示不应入库"}]}},
            {"type": "compaction", "id": "c1", "timestamp": "2026-10-23T10:01:00Z",
             "summary": "## Goal\n完成 Atlas 存储层重构。"},
            {"type": "custom", "id": "x1", "customType": "agent-summary", "data": {"commands": 1}},
        ]
        if deleted:
            lines.append({"type": "message", "id": "u2", "timestamp": "2026-10-23T10:02:00Z",
                          "message": {"role": "user",
                                      "content": [{"type": "text", "text": "这条消息被用户删除"}]}})
            lines.append({"type": "deleted", "id": "d1", "originalEntryId": "u2",
                          "reason": "resend-truncate"})
        self.path.write_text("".join(_line(item) for item in lines), encoding="utf-8")

    def _memories(self):
        with self.ledger._reading() as conn:
            return conn.execute(
                "SELECT source_agent,kind,content,session_id,task_id,project,role,scope,"
                "raw_chat,metadata_json FROM memories ORDER BY created_at,memory_id").fetchall()

    # -- tests ----------------------------------------------------------
    def test_captures_user_assistant_text_and_compaction_summary(self):
        self._write_session()
        summary = self.scanner.sync(max_messages=20)
        self.assertEqual(summary["files"], 1)
        self.assertEqual(summary["messages"], 2)
        self.assertEqual(summary["summaries"], 1)
        self.assertEqual(summary["inserted"], 3)
        self.assertEqual(summary["errors"], 0)

        rows = self._memories()
        self.assertEqual(3, len(rows))
        self.assertTrue(all(row["source_agent"] == "pi" for row in rows))
        raw = [row for row in rows if row["kind"] == "raw_chat"]
        self.assertEqual({"user", "assistant"}, {row["role"] for row in raw})
        self.assertTrue(all(row["raw_chat"] == 1 for row in raw))
        self.assertTrue(all(row["scope"] == "agent:pi" for row in raw))
        self.assertTrue(all(row["session_id"] == self.session_id for row in raw))
        self.assertTrue(all(row["project"] == "atlas" for row in raw))
        contents = {row["content"] for row in raw}
        self.assertIn("请记住：项目使用 SQLite", contents)
        self.assertIn("决定采用 WAL 模式。", contents)
        self.assertNotIn("内部推理不应入库", contents)
        self.assertNotIn("工具输出不应入库", contents)
        self.assertNotIn("系统提示不应入库", contents)

        metadata = json.loads(raw[0]["metadata_json"])
        self.assertEqual("Atlas 重构", metadata["session_name"])
        self.assertEqual("/work/atlas", metadata["cwd"])
        self.assertEqual("native-transcript", metadata["source"])

        summaries = [row for row in rows if row["kind"] == "task"]
        self.assertEqual(1, len(summaries))
        self.assertEqual("shared", summaries[0]["scope"])
        self.assertEqual(self.session_id, summaries[0]["task_id"])
        self.assertIn("Atlas 存储层重构", summaries[0]["content"])

    def test_deleted_message_is_not_ingested(self):
        self._write_session(deleted=True)
        self.scanner.sync(max_messages=20)
        contents = {row["content"] for row in self._memories()}
        self.assertNotIn("这条消息被用户删除", contents)
        self.assertIn("请记住：项目使用 SQLite", contents)

    def test_second_sync_is_incremental(self):
        self._write_session()
        first = self.scanner.sync(max_messages=20)
        second = self.scanner.sync(max_messages=20)
        self.assertGreater(first["messages"], 0)
        self.assertEqual(0, second["messages"])
        self.assertEqual(0, second["summaries"])
        self.assertEqual(0, second["inserted"])

    def test_appended_messages_keep_session_identity(self):
        self._write_session()
        self.scanner.sync(max_messages=20)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_line({"type": "message", "id": "u9",
                                "timestamp": "2026-10-23T10:05:00Z",
                                "message": {"role": "user",
                                            "content": [{"type": "text", "text": "继续补测试"}]}}))
        summary = self.scanner.sync(max_messages=20)
        self.assertEqual(1, summary["messages"])
        self.assertEqual(1, summary["inserted"])
        row = [item for item in self._memories() if item["content"] == "继续补测试"][0]
        # The session header was in an earlier chunk; identity must come from
        # the persisted cursor, not from re-reading the whole file.
        self.assertEqual(self.session_id, row["session_id"])
        self.assertEqual("atlas", row["project"])
        self.assertEqual("agent:pi", row["scope"])

    def test_partial_trailing_line_waits_for_completion(self):
        self._write_session()
        self.scanner.sync(max_messages=20)
        partial = json.dumps({"type": "message", "id": "u7",
                              "timestamp": "2026-10-23T10:06:00Z",
                              "message": {"role": "user",
                                          "content": [{"type": "text", "text": "半行消息"}]}},
                             ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(partial)  # no trailing newline yet
        self.assertEqual(0, self.scanner.sync(max_messages=20)["messages"])
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        summary = self.scanner.sync(max_messages=20)
        self.assertEqual(1, summary["messages"])
        self.assertIn("半行消息", {row["content"] for row in self._memories()})

    def test_malformed_line_is_skipped(self):
        self._write_session()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
            handle.write(_line({"type": "message", "id": "u8",
                                "message": {"role": "user",
                                            "content": [{"type": "text", "text": "坏行之后仍然采集"}]}}))
        summary = self.scanner.sync(max_messages=20)
        self.assertGreaterEqual(summary["messages"], 1)
        self.assertIn("坏行之后仍然采集", {row["content"] for row in self._memories()})

    def test_bounded_batch_resumes_where_it_stopped(self):
        lines = [{"type": "session", "version": 3, "id": self.session_id,
                  "timestamp": "2026-10-23T10:00:00Z", "cwd": "/work/atlas"}]
        for index in range(5):
            lines.append({"type": "message", "id": "u%d" % index,
                          "timestamp": "2026-10-23T10:0%d:00Z" % index,
                          "message": {"role": "user",
                                      "content": [{"type": "text", "text": "第 %d 条消息" % index}]}})
        self.path.write_text("".join(_line(item) for item in lines), encoding="utf-8")
        batches = [self.scanner.sync(max_messages=2, max_per_file=2)["messages"]
                   for _ in range(5)]
        self.assertTrue(all(count <= 2 for count in batches))
        self.assertEqual(5, sum(batches))
        self.assertEqual(5, len({row["content"] for row in self._memories()}))
        self.assertEqual(0, self.scanner.sync(max_messages=2, max_per_file=2)["messages"])

    def test_oversized_line_is_skipped_without_losing_following_records(self):
        self._write_session()
        self.scanner.sync(max_messages=20)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_line({"type": "custom", "customType": "huge",
                                "data": "x" * 2000}))
            handle.write(_line({"type": "message", "id": "u10",
                                "message": {"role": "user",
                                            "content": [{"type": "text", "text": "超大行之后的消息"}]}}))
        with mock.patch.object(pi_capture, "MAX_LINE_BYTES", 1024):
            summary = self.scanner.sync(max_messages=20)
        self.assertGreaterEqual(summary["oversized"], 1)
        self.assertIn("超大行之后的消息", {row["content"] for row in self._memories()})
        self.assertEqual(0, self.scanner.sync(max_messages=20)["messages"])

    def test_resync_flag_survives_a_multi_cycle_oversized_skip(self):
        self._write_session()
        self.scanner.sync(max_messages=20)
        compact = lambda payload: json.dumps(payload, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(compact({"type": "custom", "customType": "huge",
                                  "data": "y" * 500}))
            handle.write(compact({"type": "message", "id": "u11",
                                  "message": {"role": "user",
                                              "content": [{"type": "text", "text": "ok"}]}}))
        # Force several cycles of skip-then-resync on the oversized record.
        with mock.patch.object(pi_capture, "MAX_LINE_BYTES", 100),                 mock.patch.object(pi_capture, "MAX_SKIP_BYTES", 100):
            for _ in range(12):
                self.scanner.sync(max_messages=20)
                if "ok" in {row["content"] for row in self._memories()}:
                    break
        self.assertIn("ok", {row["content"] for row in self._memories()})
        cursor = self.scanner._read_cursor(self.scanner._cursor_id(self.path))
        self.assertFalse(cursor.get("resync"), "resync marker was not cleared")

    def test_head_context_recovers_identity_after_cursor_without_session(self):
        self._write_session()
        first_line_length = len(_line({"type": "session", "version": 3, "id": self.session_id,
                                       "timestamp": "2026-10-23T10:00:00.000Z",
                                       "cwd": "/work/atlas"}).encode("utf-8"))
        self.ledger.set_cursor(self.scanner._cursor_id(self.path),
                               json.dumps({"offset": first_line_length, "line": 1}),
                               metadata={"kind": "pi-session"})
        summary = self.scanner.sync(max_messages=20)
        self.assertGreater(summary["messages"], 0)
        raw = [row for row in self._memories() if row["kind"] == "raw_chat"]
        self.assertTrue(raw)
        self.assertTrue(all(row["session_id"] == self.session_id for row in raw))

    def test_newest_file_is_captured_first(self):
        old_dir = self.sessions / "--work-old--"
        new_dir = self.sessions / "--work-new--"
        old_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)
        old_path = old_dir / "2026-09-01T00-00-00-000Z_old.jsonl"
        new_path = new_dir / "2026-10-23T00-00-00-000Z_new.jsonl"
        for path, session_id, text in ((old_path, "old-session", "旧会话内容"),
                                       (new_path, "new-session", "新会话内容")):
            path.write_text(_line({"type": "session", "version": 3, "id": session_id,
                                   "timestamp": "2026-10-23T00:00:00Z", "cwd": "/work/atlas"}) +
                            _line({"type": "message", "id": "u1",
                                   "message": {"role": "user",
                                               "content": [{"type": "text", "text": text}]}}),
                            encoding="utf-8")
        now = 1_790_000_000.0
        os.utime(old_path, (now - 3600, now - 3600))
        os.utime(new_path, (now, now))
        summary = self.scanner.sync(max_messages=2, max_per_file=2)
        self.assertEqual(1, summary["messages"])
        contents = {row["content"] for row in self._memories()}
        self.assertEqual({"新会话内容"}, contents)

    def test_status_reports_files_and_cursors(self):
        self._write_session()
        self.scanner.sync(max_messages=20)
        status = self.scanner.status()
        self.assertEqual(1, status["files"])
        self.assertEqual(str(self.sessions), status["roots"][0])
        source = status["sources"][0]
        self.assertGreater(source["offset"], 0)
        self.assertEqual(self.session_id, source["session_id"])

    def test_default_roots_env_override(self):
        with mock.patch.dict(os.environ, {"HIPPOMIND_PI_SESSION_ROOTS": str(self.sessions)}):
            roots = default_pi_session_roots()
        self.assertEqual([self.sessions], roots)


if __name__ == "__main__":
    unittest.main()
