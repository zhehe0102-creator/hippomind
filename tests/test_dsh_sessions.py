"""DSH session.jsonl.zstd ingestion: fixtures, corruption, and real files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from hippomind.dsh_sessions import (
    build_turn_records,
    decode_session_file,
    dsh_sessions_root,
    find_session_files,
    parse_session_events,
)
from hippomind.ledger import SQLiteLedger
from hippomind.watcher import ArchiveWatcher

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
NODE_BINARY = shutil.which("node")
REAL_SESSIONS = dsh_sessions_root() if dsh_sessions_root().is_dir() else None


def write_zstd_session(path: Path, events, *, append_frames=None):
    """Create a DSH-style multi-frame session file via node's native zstd."""
    lines = [json.dumps(event, ensure_ascii=False) for event in events]
    frames = [lines[i:i + 3] for i in range(0, len(lines), 3)] or [[]]
    if append_frames:
        frames.extend(append_frames)
    script = (
        "const zlib=require('node:zlib'),fs=require('node:fs');"
        "const groups=JSON.parse(process.argv[2]);"
        "const parts=[];"
        "for(const lines of groups){parts.push(zlib.zstdCompressSync(Buffer.from(lines.join('\\n')+'\\n')))}"
        "fs.writeFileSync(process.argv[1],Buffer.concat(parts));"
    )
    subprocess.run([NODE_BINARY, "-e", script, str(path), json.dumps(frames)],
                   check=True, timeout=30, capture_output=True)


def session_event(type_name, seq, data=None, **extra):
    event = {"type": type_name, "seq": seq, "time": 1786900000000 + seq}
    if data is not None:
        event["data"] = data
    event.update(extra)
    return event


def user_event(seq, text):
    return session_event("user/message", seq, {
        "content": [{"type": "text", "text": text}],
        "source": {"kind": "user"},
    })


def assistant_event(seq, text):
    return session_event("assistant/message", seq, {
        "content": [{"type": "text", "text": text}],
    })


def plugin_injection_event(seq, text):
    return session_event("user/message", seq, {
        "content": [{"type": "text", "text": text}],
        "source": {"kind": "plugin", "plugin": "hippomind"},
    })


@unittest.skipIf(NODE_BINARY is None, "node runtime not available")
class DSHSessionFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session_dir = self.root / "--work-中文项目--" / "session-abc123"
        self.session_dir.mkdir(parents=True)
        self.path = self.session_dir / "session.jsonl.zstd"
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def standard_events(self):
        return [
            {"type": "session", "id": "session-abc123", "createdAt": 1786900000000,
             "cwd": "/tmp/work/中文项目"},
            session_event("permission/preset", 1, {"preset": "default"}),
            user_event(2, "部署 Redis 集群并验证故障转移"),
            assistant_event(5, "已部署三主三从，下一步验证 sentinel 切换。"),
            session_event("session/title", 6, {"title": "Redis 部署"}),
            user_event(9, "验证故障转移"),
            assistant_event(12, "决定采用 sentinel 方案，切换验证通过。"),
        ]

    def watcher(self):
        return ArchiveWatcher(self.ledger, archive_root=self.root / "no-archive",
                              dsh_sessions_root=self.root)

    def test_multi_frame_decode_and_turn_extraction(self):
        write_zstd_session(self.path, self.standard_events())
        text = decode_session_file(self.path, package_root=PACKAGE_ROOT)
        events = parse_session_events(text)
        self.assertEqual(events[0]["type"], "session")
        records = build_turn_records(events, self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["source_agent"], "deepseek")
        self.assertEqual(records[0]["session_id"], "session-abc123")
        self.assertEqual(records[0]["project"], "中文项目")
        self.assertIn("用户：部署 Redis", records[0]["content"])
        self.assertIn("助手：已部署三主三从", records[0]["content"])
        self.assertTrue(records[0]["evidence"][0].startswith("file:"))
        self.assertEqual(records[0]["evidence"][1],
                         "dsh-session://session-abc123#seq=2-5")
        self.assertTrue(records[0]["raw_chat"])
        self.assertEqual(records[0]["metadata"]["title"], "Redis 部署")

    def test_rle_blocks_do_not_silently_drop_following_frames(self):
        # 1MB of constant data forces RLE blocks (block type 1), whose size
        # field is a repeat count with a 1-byte payload. A parser that skips
        # `size` bytes for RLE loses every later frame without any error.
        script = (
            "const zlib=require('node:zlib'),fs=require('node:fs');"
            "const rle=zlib.zstdCompressSync(Buffer.alloc(1<<20,0x63));"
            "const json=zlib.zstdCompressSync(Buffer.from(process.argv[2]));"
            "fs.writeFileSync(process.argv[1],Buffer.concat([rle,json]));"
        )
        payload = json.dumps({
            "type": "user/message", "seq": 42,
            "data": {"content": [{"type": "text", "text": "RLE 之后的事件必须保留"}],
                     "source": {"kind": "user"}},
        }, ensure_ascii=False) + "\n"
        subprocess.run([NODE_BINARY, "-e", script, str(self.path), payload],
                       check=True, timeout=60, capture_output=True)
        records = build_turn_records(parse_session_events(
            decode_session_file(self.path, package_root=PACKAGE_ROOT)), self.path)
        self.assertEqual(len(records), 1)
        self.assertIn("RLE 之后的事件必须保留", records[0]["content"])

    def test_plugin_injections_and_memory_packs_are_never_reingested(self):
        events = [
            {"type": "session", "id": "session-x", "createdAt": 1, "cwd": "/w/p"},
            plugin_injection_event(2, "<MEMORY_DATA_UNTRUSTED>\n历史记忆包\n</MEMORY_DATA_UNTRUSTED>"),
            session_event("user/message", 3, {
                "content": [{"type": "text", "text": "<MEMORY_DATA_UNTRUSTED>二次投毒的记忆包"}],
                "source": {"kind": "user"},
            }),
            user_event(4, "真实用户请求"),
            assistant_event(6, "真实回复"),
        ]
        write_zstd_session(self.path, events)
        records = build_turn_records(parse_session_events(
            decode_session_file(self.path, package_root=PACKAGE_ROOT)), self.path)
        self.assertEqual(len(records), 1)
        self.assertNotIn("MEMORY_DATA_UNTRUSTED", records[0]["content"])
        self.assertIn("真实用户请求", records[0]["content"])

    def test_watcher_incremental_idempotent_and_append_only(self):
        write_zstd_session(self.path, self.standard_events())
        watcher = self.watcher()
        first = watcher.sync_once()
        dsh = first["sources"]["dsh-sessions"]
        self.assertGreaterEqual(dsh["imported"], 2)
        # Second sync: cursor skip, no duplicates, cheap.
        started = time.monotonic()
        second = watcher.sync_once()
        elapsed = time.monotonic() - started
        self.assertEqual(second["sources"]["dsh-sessions"]["imported"], 0)
        self.assertEqual(second["sources"]["dsh-sessions"]["duplicate"], 0)
        self.assertLess(elapsed, 5.0)
        # Appending frames only adds the new turn; earlier turns keep ids.
        write_zstd_session(self.path, [], append_frames=[
            [json.dumps(user_event(20, "追加的新问题"), ensure_ascii=False),
             json.dumps(assistant_event(22, "追加的新回复"), ensure_ascii=False)],
        ])
        third = watcher.sync_once()
        self.assertEqual(third["sources"]["dsh-sessions"]["imported"], 1)
        rows = self.ledger.search("追加的新问题", scope="shared", limit=10,
                                  include_raw_chat=True)
        self.assertTrue(rows)

    def test_corrupted_and_foreign_files_fail_without_blocking(self):
        write_zstd_session(self.path, self.standard_events())
        broken = self.root / "--other--" / "session-deadbeef"
        broken.mkdir(parents=True)
        (broken / "session.jsonl.zstd").write_bytes(
            b"\x28\xb5\x2f\xfd" + os.urandom(4096))
        watcher = self.watcher()
        result = watcher.sync_once()
        dsh = result["sources"]["dsh-sessions"]
        self.assertEqual(dsh["failed"], 1)
        self.assertGreaterEqual(dsh["imported"], 2)
        # The broken file is retried (cursor error retained), good file skipped.
        again = watcher.sync_once()
        self.assertEqual(again["sources"]["dsh-sessions"]["duplicate"], 0)

    def test_missing_node_degrades_to_source_error(self):
        write_zstd_session(self.path, self.standard_events())
        original = shutil.which
        import hippomind.dsh_sessions as module
        module_node = module.node_binary
        module.node_binary = lambda: None
        try:
            watcher = self.watcher()
            result = watcher.sync_once()
            dsh = result["sources"]["dsh-sessions"]
            self.assertEqual(dsh["imported"], 0)
            self.assertEqual(dsh["failed"], 1)
            self.assertIn("node", str(dsh["errors"]))
        finally:
            module.node_binary = module_node

    def test_database_rebuild_reimports_without_duplicates(self):
        write_zstd_session(self.path, self.standard_events())
        watcher = self.watcher()
        watcher.sync_once()
        counts_before = len(self.ledger.search("Redis", scope="shared", limit=50,
                                               include_raw_chat=True))
        # Simulate a rebuilt hub: cursors lost, ledger recreated at same path.
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.root / "hub.sqlite") + suffix)
            if candidate.exists():
                os.remove(candidate)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        rebuilt = ArchiveWatcher(self.ledger, archive_root=self.root / "no-archive",
                                 dsh_sessions_root=self.root)
        rebuilt.sync_once()
        counts_after = len(self.ledger.search("Redis", scope="shared", limit=50,
                                              include_raw_chat=True))
        self.assertEqual(counts_before, counts_after)
        # And one more sync adds nothing.
        result = rebuilt.sync_once()
        self.assertEqual(result["sources"]["dsh-sessions"]["imported"], 0)


@unittest.skipIf(NODE_BINARY is None or REAL_SESSIONS is None,
                 "node or real ~/.dsh/sessions not available")
class DSHRealSessionBlackBoxTests(unittest.TestCase):
    """Read-only black-box run against the machine's real DSH sessions."""

    @staticmethod
    def _snapshot_sessions(destination: Path) -> Path:
        """Freeze the real corpus so a live DSH write cannot make this test flaky."""
        for source in find_session_files(REAL_SESSIONS):
            target = destination / source.relative_to(REAL_SESSIONS)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return destination

    def test_real_sessions_ingest_with_evidence_and_stay_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = self._snapshot_sessions(Path(tmp) / "sessions")
            ledger = SQLiteLedger(Path(tmp) / "hub.sqlite")
            watcher = ArchiveWatcher(ledger, archive_root=Path(tmp) / "none",
                                     dsh_sessions_root=sessions)
            result = watcher.sync_once()
            dsh = result["sources"]["dsh-sessions"]
            self.assertEqual(dsh["files"], len(find_session_files(sessions)))
            self.assertGreaterEqual(dsh["imported"], 3)
            self.assertEqual(dsh["failed"], 0, dsh.get("errors"))
            rows = ledger.search("", scope="shared", limit=200,
                                 source_agents=["deepseek"], include_raw_chat=True)
            self.assertTrue(rows)
            for row in rows:
                self.assertLessEqual(len(str(row["content"])), 4200)
                self.assertTrue(any(str(ref).startswith(("file:", "dsh-session://"))
                                    for ref in row["evidence"]))
            # Idempotent second pass over the same real files.
            second = watcher.sync_once()
            self.assertEqual(second["sources"]["dsh-sessions"]["imported"], 0)

    def test_real_session_text_is_retrievable_across_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = self._snapshot_sessions(Path(tmp) / "sessions")
            ledger = SQLiteLedger(Path(tmp) / "hub.sqlite")
            watcher = ArchiveWatcher(ledger, archive_root=Path(tmp) / "none",
                                     dsh_sessions_root=sessions)
            watcher.sync_once()
            from hippomind.auto_context import build_auto_context
            pack = build_auto_context(ledger, "继续 dsh 里关于 agent 预设的工作",
                                      cwd="/tmp")
            # Raw chat is not injected by the automatic path by design; the
            # evidence itself must still be searchable in the ledger.
            rows = ledger.search("agent 预设", scope="shared", limit=10,
                                 source_agents=["deepseek"], include_raw_chat=True)
            self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main()
