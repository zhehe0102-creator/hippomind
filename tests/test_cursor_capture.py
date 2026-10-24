"""Cursor capture -- synthetic fixtures only (this machine has no Cursor).

Every SQLite database used here is built by the test itself from the schema
documented in `docs/AGENT_CAPTURE_COVERAGE.md`.  That is the whole point: the
real Cursor is not installed, so these tests prove the *adapter logic*
(schema recognition, role mapping, streaming-overlap re-reads, cursors,
corruption tolerance, the three-state contract) and make no claim about a real
installation.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import cursor_capture
from hippomind.cursor_capture import (
    STATE_DETECTED, STATE_NOT_INSTALLED, STATE_UNRECOGNIZED, CursorComposerScanner,
    CursorTranscriptScanner, collect, probe,
)
from hippomind.ledger import SQLiteLedger

COMPOSER = "11111111-1111-1111-1111-111111111111"
BUBBLE_USER = "aaaaaaaa-0000-0000-0000-000000000001"
BUBBLE_ASSIST = "aaaaaaaa-0000-0000-0000-000000000002"


def _bubble(**fields):
    payload = {"_v": 3}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


class _Fixture(unittest.TestCase):
    """Builds a fake `~/Library/Application Support/Cursor/User` + `~/.cursor`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-cursor-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.data_root = self.root / "Cursor" / "User"
        self.store_root = self.root / "dot-cursor"
        self.global_db = self.data_root / "globalStorage" / "state.vscdb"
        self.global_db.parent.mkdir(parents=True)

    # -- fixture builders ------------------------------------------------
    def _make_global_db(self, rows=(), *, tables=True):
        conn = sqlite3.connect(self.global_db)
        try:
            if tables:
                conn.execute("CREATE TABLE ItemTable(key TEXT PRIMARY KEY, value BLOB)")
                conn.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value BLOB)")
                conn.executemany("INSERT INTO cursorDiskKV(key,value) VALUES(?,?)", rows)
            else:
                # Present, but nothing we recognise: the "unknown schema" case.
                conn.execute("CREATE TABLE SomethingElse(key TEXT, value BLOB)")
            conn.commit()
        finally:
            conn.close()

    def _append_rows(self, rows):
        conn = sqlite3.connect(self.global_db)
        try:
            conn.executemany("INSERT INTO cursorDiskKV(key,value) VALUES(?,?)", rows)
            conn.commit()
        finally:
            conn.close()

    def _scanner(self, **kwargs):
        return CursorComposerScanner(self.ledger, [self.global_db], **kwargs)

    def _memories(self):
        with self.ledger._reading() as conn:
            return conn.execute(
                "SELECT source_agent,kind,content,session_id,project,role,scope,"
                "raw_chat,evidence_json,metadata_json FROM memories "
                "ORDER BY created_at,memory_id").fetchall()


class CursorProbeTests(_Fixture):
    """The three-state contract: detected / not_installed / unrecognized."""

    def test_not_installed_when_no_cursor_directory_exists(self):
        report = probe([self.root / "nope"], [self.root / "nope2"])
        self.assertEqual(STATE_NOT_INSTALLED, report["state"])
        self.assertEqual(0, report["recognized"])

    def test_dot_cursor_without_cursor_subdirs_is_not_installed(self):
        """HippoMind itself writes ~/.cursor/mcp.json -- that is not Cursor."""
        (self.store_root / "rules").mkdir(parents=True)
        (self.store_root / "mcp.json").write_text("{}", encoding="utf-8")
        report = probe([self.root / "absent"], [self.store_root])
        self.assertEqual(STATE_NOT_INSTALLED, report["state"])

    def test_detected_when_cursordiskkv_and_bubbles_exist(self):
        self._make_global_db([("bubbleId:%s:%s" % (COMPOSER, BUBBLE_USER),
                               _bubble(type=1, text="hello"))])
        report = probe([self.data_root], [self.root / "absent-store"])
        self.assertEqual(STATE_DETECTED, report["state"])
        self.assertEqual(1, report["recognized"])
        kinds = [source["kind"] for source in report["sources"]]
        self.assertIn("composer-global", kinds)
        self.assertTrue(any(source.get("has_bubbles")
                            for source in report["sources"]
                            if source["kind"] == "composer-global"))

    def test_unrecognized_when_cursor_data_has_no_known_schema(self):
        self._make_global_db(tables=False)
        report = probe([self.data_root], [self.root / "absent-store"])
        self.assertEqual(STATE_UNRECOGNIZED, report["state"])
        self.assertEqual(0, report["recognized"])
        self.assertTrue(report["present_but_unrecognized"])
        self.assertIn("no_cursorDiskKV_table",
                      {item["reason"] for item in report["present_but_unrecognized"]})
        # The message must never read like "we captured zero messages".
        self.assertTrue(any("无法识别" in note for note in report["notes"]))

    def test_unrecognized_when_cursor_disk_kv_exists_but_has_no_bubbles(self):
        self._make_global_db([("agentKv:blob:deadbeef", "{}")])
        report = probe([self.data_root], [self.root / "absent-store"])
        self.assertEqual(STATE_DETECTED, report["state"])
        self.assertFalse(any(source.get("has_bubbles")
                             for source in report["sources"]
                             if source["kind"] == "composer-global"))

    def test_locked_database_is_reported_not_crashed(self):
        self._make_global_db([])
        # A file that is not a database at all: sqlite must refuse it cleanly.
        broken = self.data_root / "globalStorage" / "broken.vscdb"
        broken.write_bytes(b"not a database")
        source = cursor_capture._probe_global_db(broken)
        self.assertFalse(source["recognized"])
        self.assertTrue(source["reason"].startswith("sqlite_error")
                        or source["reason"] == "unreadable_or_locked")


class CursorComposerCaptureTests(_Fixture):
    """Bubble -> raw_chat mapping, using the documented cursorDiskKV schema."""

    def _seed(self):
        self._make_global_db([
            ("composerData:%s" % COMPOSER,
             json.dumps({"composerId": COMPOSER, "name": "Atlas 重构",
                         "workspaceIdentifier": {"uri": {"fsPath": "/work/atlas"}}})),
            ("bubbleId:%s:%s" % (COMPOSER, BUBBLE_USER),
             _bubble(type=1, text="请记住：项目使用 SQLite", createdAt="2026-05-10T10:30:00Z")),
            ("bubbleId:%s:%s" % (COMPOSER, BUBBLE_ASSIST),
             _bubble(type=2, text="决定采用 WAL 模式。",
                     createdAt="2026-05-10T10:30:02.500Z")),
            # Streaming intermediate state: empty assistant shell, must not be
            # ingested, and must be re-read once the text arrives.
            ("bubbleId:%s:cccccccc-0000-0000-0000-000000000003" % COMPOSER,
             _bubble(type=2, text="")),
            # Tool-ish bubble (type 3 is not user/assistant) -> skipped.
            ("bubbleId:%s:cccccccc-0000-0000-0000-000000000004" % COMPOSER,
             _bubble(type=3, text="工具输出不应入库")),
            # checkpoints/agentKv must never be read as messages.
            ("checkpointId:%s:dddd" % COMPOSER, json.dumps({"files": []})),
            ("agentKv:blob:abcd", json.dumps({"big": "x" * 10})),
        ])

    def test_captures_user_and_assistant_text(self):
        self._seed()
        summary = self._scanner().sync(max_messages=50)
        self.assertEqual(STATE_DETECTED, summary["state"])
        self.assertEqual(2, summary["messages"])
        self.assertEqual(2, summary["inserted"])
        self.assertEqual(0, summary["errors"])

        rows = self._memories()
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["source_agent"] == "cursor" for row in rows))
        self.assertTrue(all(row["kind"] == "raw_chat" for row in rows))
        self.assertTrue(all(row["raw_chat"] == 1 for row in rows))
        self.assertTrue(all(row["scope"] == "agent:cursor" for row in rows))
        self.assertTrue(all(row["session_id"] == COMPOSER for row in rows))
        self.assertTrue(all(row["project"] == "atlas" for row in rows))
        self.assertEqual({"user", "assistant"}, {row["role"] for row in rows})
        contents = {row["content"] for row in rows}
        self.assertEqual({"请记住：项目使用 SQLite", "决定采用 WAL 模式。"}, contents)
        self.assertNotIn("工具输出不应入库", contents)

        metadata = json.loads(rows[0]["metadata_json"])
        self.assertEqual("cursor-composer-bubble", metadata["source"])
        self.assertEqual("cursor-state-vscdb", metadata["format"])
        self.assertEqual("Atlas 重构", metadata["session_name"])
        self.assertEqual(3, metadata["bubble_schema_version"])

    def test_timestamps_come_from_createdat_and_timing_info(self):
        self._make_global_db([
            ("bubbleId:%s:%s" % (COMPOSER, BUBBLE_USER),
             _bubble(type=1, text="新格式消息", createdAt="2026-01-29T12:58:03.575Z")),
            # Older Cursor: only timingInfo.clientRpcSendTime (Unix ms).
            ("bubbleId:%s:%s" % (COMPOSER, BUBBLE_ASSIST),
             _bubble(type=2, text="旧格式消息",
                     timingInfo={"clientRpcSendTime": 1765760433648})),
        ])
        self._scanner().sync(max_messages=10)
        with self.ledger._reading() as conn:
            stamps = dict(conn.execute(
                "SELECT m.content, e.occurred_at FROM memories m "
                "JOIN ledger_events e ON e.event_id = m.origin_event_id"))
        roles = {row["content"]: row["role"] for row in self._memories()}
        self.assertEqual("user", roles["新格式消息"])
        # createdAt (newer Cursor) and timingInfo.clientRpcSendTime (older
        # Cursor, Unix ms) must both survive as ISO-8601 UTC.
        self.assertTrue(stamps["新格式消息"].startswith("2026-01-29T12:58:03"))
        self.assertTrue(stamps["旧格式消息"].startswith("2025-12"),
                        stamps["旧格式消息"])

    def test_streaming_empty_bubble_is_captured_after_it_is_filled(self):
        self._seed()
        self._scanner().sync(max_messages=50)
        # Cursor fills the empty assistant bubble in place (same row, same key).
        conn = sqlite3.connect(self.global_db)
        try:
            conn.execute("UPDATE cursorDiskKV SET value=? WHERE key=?",
                         (_bubble(type=2, text="流式补全后的答案"),
                          "bubbleId:%s:cccccccc-0000-0000-0000-000000000003" % COMPOSER))
            conn.commit()
        finally:
            conn.close()
        summary = self._scanner().sync(max_messages=50)
        self.assertEqual(1, summary["inserted"])
        self.assertIn("流式补全后的答案", {row["content"] for row in self._memories()})

    def test_second_sync_does_not_duplicate(self):
        self._seed()
        first = self._scanner().sync(max_messages=50)
        second = self._scanner().sync(max_messages=50)
        self.assertEqual(2, first["inserted"])
        self.assertEqual(0, second["inserted"])
        self.assertEqual(0, second["messages"])
        self.assertEqual(2, len(self._memories()))

    def test_appended_bubbles_are_picked_up(self):
        self._seed()
        self._scanner().sync(max_messages=50)
        self._append_rows([("bubbleId:%s:eeeeeeee-0000-0000-0000-000000000009" % COMPOSER,
                            _bubble(type=1, text="继续补测试",
                                    createdAt="2026-05-10T11:00:00Z"))])
        summary = self._scanner().sync(max_messages=50)
        self.assertEqual(1, summary["inserted"])
        row = [item for item in self._memories() if item["content"] == "继续补测试"][0]
        self.assertEqual(COMPOSER, row["session_id"])
        self.assertEqual("agent:cursor", row["scope"])


    def test_backlog_drains_completely_across_cycles(self):
        """Regression: the rowid watermark may only pass rows actually consumed.

        An earlier version advanced it to the highest row *read*, so a backlog
        larger than the per-cycle message budget was skipped forever (the
        overlap window re-read the same tail instead of the queue draining).
        """
        self._make_global_db([
            ("bubbleId:%s:b%05d" % (COMPOSER, index),
             _bubble(type=1, text="积压消息 %d" % index))
            for index in range(120)])
        cycles = 0
        while cycles < 10:
            cycles += 1
            if self._scanner().sync(max_messages=25)["inserted"] == 0:
                break
        with self.ledger._reading() as conn:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            distinct = conn.execute("SELECT COUNT(DISTINCT content) FROM memories").fetchone()[0]
        self.assertEqual(120, total, "some bubbles were skipped by the watermark")
        self.assertEqual(120, distinct, "duplicate rows were written")

    def test_corrupt_and_null_payloads_are_counted_not_fatal(self):
        self._make_global_db([
            ("bubbleId:%s:%s" % (COMPOSER, BUBBLE_USER), _bubble(type=1, text="正常消息")),
            ("bubbleId:%s:broken" % COMPOSER, b"\x00\x01 not json"),
            ("bubbleId:%s:nullrow" % COMPOSER, None),
        ])
        summary = self._scanner().sync(max_messages=50)
        self.assertEqual(1, summary["messages"])
        self.assertEqual(2, summary["corrupt"])
        self.assertEqual(0, summary["errors"])
        self.assertEqual(["正常消息"], [row["content"] for row in self._memories()])

    def test_unrecognized_schema_reports_unrecognized_not_zero(self):
        self._make_global_db(tables=False)
        summary = self._scanner().sync(max_messages=50)
        self.assertEqual(STATE_UNRECOGNIZED, summary["state"])
        self.assertGreaterEqual(summary["errors"], 1)
        self.assertEqual(0, summary["messages"])
        self.assertEqual([], self._memories())

    def test_missing_database_reports_not_installed(self):
        summary = CursorComposerScanner(
            self.ledger, [self.root / "missing" / "state.vscdb"]).sync(max_messages=10)
        self.assertEqual(STATE_NOT_INSTALLED, summary["state"])
        self.assertEqual(0, summary["messages"])

    def test_max_messages_bound_stops_the_cycle(self):
        self._make_global_db([
            ("bubbleId:%s:%s" % (COMPOSER, "b%03d" % index),
             _bubble(type=1, text="消息 %d" % index))
            for index in range(10)])
        summary = self._scanner().sync(max_messages=3)
        self.assertEqual(3, summary["inserted"])
        # The rest is captured on the next cycle rather than lost.
        summary = self._scanner().sync(max_messages=100)
        self.assertGreaterEqual(summary["inserted"], 7)


class CursorTranscriptCaptureTests(_Fixture):
    """Store-stack agent transcripts: ~/.cursor/projects/**/agent-transcripts."""

    def _transcript(self, session="aaaaaaaa-0000-0000-0000-000000000001", lines=None):
        path = (self.store_root / "projects" / "work-atlas"
                / "agent-transcripts" / session / (session + ".jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        body = lines if lines is not None else [
            {"role": "user", "message": {"content": [
                {"type": "text", "text": "<user_query>\nRead foo.txt\n</user_query>"}]}},
            {"role": "assistant", "message": {"content": [
                {"type": "text", "text": "Reading the file."},
                {"type": "tool_use", "name": "Read", "input": {"path": "/tmp/foo.txt"}}]}},
        ]
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in body),
                        encoding="utf-8")
        return path

    def test_probe_detects_transcript_schema(self):
        path = self._transcript()
        report = probe([self.root / "absent"], [self.store_root])
        self.assertEqual(STATE_DETECTED, report["state"])
        kinds = {source["kind"] for source in report["sources"]}
        self.assertIn("store-transcript", kinds)
        self.assertTrue(path.exists())

    def test_captures_only_text_blocks(self):
        self._transcript()
        scanner = CursorTranscriptScanner(self.ledger, [self.store_root])
        summary = scanner.sync(max_messages=20)
        self.assertEqual(STATE_DETECTED, summary["state"])
        self.assertEqual(2, summary["inserted"])
        rows = self._memories()
        contents = {row["content"] for row in rows}
        self.assertIn("Reading the file.", contents)
        self.assertNotIn("Read", contents)  # tool_use name must not leak in
        self.assertTrue(all(row["project"] == "work-atlas" for row in rows))

    def test_partial_trailing_line_waits_for_completion(self):
        path = self._transcript()
        scanner = CursorTranscriptScanner(self.ledger, [self.store_root])
        scanner.sync(max_messages=20)
        partial = json.dumps({"role": "user", "message": {"content": [
            {"type": "text", "text": "半行消息"}]}}, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(partial)  # no newline yet
        self.assertEqual(0, scanner.sync(max_messages=20)["messages"])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        self.assertEqual(1, scanner.sync(max_messages=20)["messages"])
        self.assertIn("半行消息", {row["content"] for row in self._memories()})

    def test_bad_lines_are_skipped_and_counted_as_absence(self):
        self._transcript(lines=[
            {"role": "user", "message": {"content": [{"type": "text", "text": "好行"}]}},
            "not-an-object",
            {"role": "system", "message": {"content": [{"type": "text", "text": "系统提示"}]}},
            {"role": "assistant"},
        ])
        scanner = CursorTranscriptScanner(self.ledger, [self.store_root])
        summary = scanner.sync(max_messages=20)
        self.assertEqual(1, summary["inserted"])
        self.assertEqual(["好行"], [row["content"] for row in self._memories()])


class CursorCollectTests(_Fixture):
    """The single entry point the daemon calls."""

    def test_collect_reports_not_installed_without_writing(self):
        summary = collect(self.ledger, data_roots=[self.root / "nope"],
                          store_roots=[self.root / "nope2"])
        self.assertEqual(STATE_NOT_INSTALLED, summary["state"])
        self.assertEqual(0, summary["messages"])
        self.assertEqual([], self._memories())

    def test_collect_reports_unrecognized_and_never_claims_success(self):
        self._make_global_db(tables=False)
        summary = collect(self.ledger, data_roots=[self.data_root],
                          store_roots=[self.root / "nope"])
        self.assertEqual(STATE_UNRECOGNIZED, summary["state"])
        self.assertEqual(0, summary["messages"])
        self.assertGreaterEqual(summary["errors"], 1)
        self.assertTrue(summary["present_but_unrecognized"])

    def test_collect_captures_both_stacks_and_is_idempotent(self):
        self._make_global_db([
            ("bubbleId:%s:%s" % (COMPOSER, BUBBLE_USER), _bubble(type=1, text="来自 IDE 的消息"))])
        transcript = (self.store_root / "projects" / "atlas" / "agent-transcripts"
                      / "bbbbbbbb-0000-0000-0000-000000000002"
                      / "bbbbbbbb-0000-0000-0000-000000000002.jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps(
            {"role": "assistant", "message": {"content": [
                {"type": "text", "text": "来自 CLI 的消息"}]}}, ensure_ascii=False) + "\n",
            encoding="utf-8")

        first = collect(self.ledger, data_roots=[self.data_root], store_roots=[self.store_root])
        self.assertEqual(STATE_DETECTED, first["state"])
        self.assertEqual(2, first["inserted"])
        self.assertEqual(0, first["errors"])
        second = collect(self.ledger, data_roots=[self.data_root], store_roots=[self.store_root])
        self.assertEqual(0, second["inserted"])
        self.assertEqual(2, len(self._memories()))

    def test_status_reports_probe_and_cursors_without_writing(self):
        self._make_global_db([
            ("bubbleId:%s:%s" % (COMPOSER, BUBBLE_USER), _bubble(type=1, text="一条消息"))])
        collect(self.ledger, data_roots=[self.data_root], store_roots=[self.root / "nope"])
        report = cursor_capture.status(self.ledger, data_roots=[self.data_root],
                                       store_roots=[self.root / "nope"])
        self.assertEqual(STATE_DETECTED, report["state"])
        self.assertTrue(report["cursors"])
        self.assertTrue(any(item["kind"] == "composer" for item in report["cursors"]))


if __name__ == "__main__":
    unittest.main()
