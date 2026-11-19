"""commons <-> ledger 双存储对账回归（WP-I）。

锁定验收口径：

1. 分桶正确：missing_in_ledger / missing_in_commons / stale / window_mismatch
   各自计数与样例来自真实分叉夹具（窗口漂移优先归类，可修复桶必须可收敛）；
2. 默认只读：compare 与脚本默认路径不得修改 commons/ledger 的 mtime 与 sha256；
3. --repair 显式开启，只同步 superseded_by/valid_until —— 不删行、不改内容、
   不改 updated_at，ledger 文件字节不变；
4. 重复执行幂等：第二次 plan=0/write=0，文件字节不变；
5. 不存在/空 commons 与缺失 ledger 都返回结构化报告而不是抛异常；
6. commons.stats() 暴露的 last_reconcile 只来自内存，stats 本身不产生写入。

所有夹具都在 tempfile 中构造；本机真实 commons 库（980KB）只作为只读参考，
测试绝不打开它。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.commons import (  # noqa: E402
    MemoryCommons, RECONCILE_BUCKETS, compare_shared_with_ledger,
    default_commons_db_path, repair_shared_with_ledger)
from hippomind.ledger import SQLiteLedger  # noqa: E402

SCRIPT = ROOT / "scripts" / "reconcile_commons.py"


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _snapshot(path) -> tuple:
    stat = Path(path).stat()
    return (_sha256(path), stat.st_mtime_ns, stat.st_size)


def _dir_names(directory) -> tuple:
    """目录里出现了哪些数据文件（忽略 SQLite 的 -wal/-shm 侧车）。

    -shm/-wal 是锁协调与日志侧车，任何读者都会临时创建/更新它们，而且不同
    SQLite 版本在最后一个连接关闭后是否保留侧车的行为不同（实测 3.9 的
    sqlite 3.54 保留、3.13 的 3.50 删除）。只读审计的验收标准是主库与
    commons 的 sha256/mtime/size 不变、且没有新增数据文件；把侧车算进来
    只会测出解释器自带的 SQLite 版本差异，而不是代码是否写库。
    """
    names = []
    for item in Path(directory).iterdir():
        if item.name.endswith(("-wal", "-shm")):
            continue
        names.append(item.name)
    return tuple(sorted(names))


def _insert_commons(commons_db, memory_id, *, updated_at,
                    superseded_by="", valid_until="", content=None, scope="shared"):
    with sqlite3.connect(str(commons_db)) as conn:
        conn.execute(
            "INSERT INTO shared_memories(memory_id,content,kind,source_agent,"
            "source_session,source_refs,tags,scope,valence,confidence,utility,"
            "importance,created_at,updated_at,valid_until,superseded_by,"
            "success_count,failure_count,metadata) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory_id, content or ("commons content " + memory_id),
             "semantic", "tester", "", '["chat:e1"]', "[]", scope,
             0.0, 0.6, 0.0, 5, updated_at, updated_at, valid_until,
             superseded_by, 0, 0, "{}"))


def _insert_ledger_memory(ledger, memory_id, *, updated_at,
                          superseded_by="", valid_until="", scope="shared"):
    digest = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()
    event_id = "evt_" + digest[:32]

    def write(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO ledger_events(event_id,event_type,occurred_at,"
            "ingested_at,source_agent,session_id,task_id,project,role,scope,"
            "evidence_json,content,content_hash,metadata_json,asserted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "fact", updated_at, updated_at, "tester", "", "", "", "",
             scope, "[]", "ledger content " + memory_id, digest, "{}", updated_at))
        conn.execute(
            "INSERT INTO memories(memory_id,origin_event_id,kind,content,"
            "source_agent,scope,created_at,updated_at,last_weighted_at,"
            "valid_until,superseded_by,lifecycle,raw_chat,quality_state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory_id, event_id, "fact", "ledger content " + memory_id,
             "tester", scope, updated_at, updated_at, updated_at,
             valid_until, superseded_by, "active", 0, "accepted"))

    ledger._transaction(write)


class Fixture:
    """tempfile 里的 archive（chat index + commons）与 ledger 分叉夹具。"""

    def __init__(self, root: Path, *, diverged: bool = True):
        self.root = Path(root)
        self.archive = self.root / "archive"
        (self.archive / "index").mkdir(parents=True, exist_ok=True)
        self._make_chat_index()
        self.commons = MemoryCommons(str(self.archive))
        self.commons_db = self.commons.initialize()
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        self.ledger_path = self.ledger.path
        if diverged:
            self._build_diverged()
        else:
            self._build_consistent()

    def _make_chat_index(self) -> None:
        with sqlite3.connect(str(self.archive / "index" / "index.sqlite")) as conn:
            conn.execute(
                "CREATE TABLE messages (agent TEXT, session_id TEXT, event_id TEXT,"
                " ts TEXT, role TEXT, content TEXT, model TEXT, cwd TEXT,"
                " title TEXT, source_file TEXT)")

    def _build_diverged(self) -> None:
        # commons: a/b/c/d（d 只在 commons）；ledger: a/b/c/e（e 只在 ledger）
        _insert_commons(self.commons_db, "shared:a",
                        updated_at="2026-01-01T00:00:00+00:00")
        _insert_commons(self.commons_db, "shared:b",
                        updated_at="2026-01-01T00:00:00+00:00")
        _insert_commons(self.commons_db, "shared:c",
                        updated_at="2026-01-02T00:00:00+00:00")
        _insert_commons(self.commons_db, "shared:d",
                        updated_at="2026-01-03T00:00:00+00:00")
        _insert_ledger_memory(self.ledger, "shared:a",
                              updated_at="2026-01-01T00:00:00+00:00")
        _insert_ledger_memory(self.ledger, "shared:b",
                              updated_at="2026-01-05T00:00:00+00:00")
        _insert_ledger_memory(self.ledger, "shared:c",
                              updated_at="2026-01-02T00:00:00+00:00",
                              superseded_by="shared:d",
                              valid_until="2026-02-01T00:00:00+00:00")
        _insert_ledger_memory(self.ledger, "shared:e",
                              updated_at="2026-01-06T00:00:00+00:00")

    def _build_consistent(self) -> None:
        for index in ("x", "y"):
            _insert_commons(self.commons_db, "shared:" + index,
                            updated_at="2026-03-01T00:00:00+00:00")
            _insert_ledger_memory(self.ledger, "shared:" + index,
                                  updated_at="2026-03-01T00:00:00+00:00")

    def close(self) -> None:
        self.ledger.close()


class FixtureCase(unittest.TestCase):
    diverged = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-reconcile-")
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Fixture(Path(self.tmp.name), diverged=self.diverged)
        self.addCleanup(self.fixture.close)

    @property
    def commons_db(self):
        return self.fixture.commons_db

    @property
    def ledger(self):
        return self.fixture.ledger

    def compare(self, **kwargs):
        kwargs.setdefault("commons_db", self.commons_db)
        return compare_shared_with_ledger(self.ledger, **kwargs)


class CompareTests(FixtureCase):
    def test_buckets_are_exact(self):
        report = self.compare()
        self.assertFalse(report["ok"])
        self.assertTrue(report["commons_exists"])
        self.assertTrue(report["ledger_exists"])
        self.assertEqual(4, report["commons_rows"])
        self.assertEqual(4, report["ledger_rows"])
        counts = report["counts"]
        self.assertEqual({
            "in_sync": 1,
            "missing_in_ledger": 1,
            "missing_in_commons": 1,
            "stale": 1,
            "window_mismatch": 1,
            "drift_total": 4,
        }, counts)
        self.assertEqual(1, report["repairable"])

    def test_samples_are_bounded_and_show_both_sides(self):
        report = self.compare(sample_limit=1)
        for bucket in RECONCILE_BUCKETS:
            self.assertLessEqual(len(report["samples"][bucket]), 1)
        window = report["samples"]["window_mismatch"][0]
        self.assertEqual("shared:c", window["memory_id"])
        self.assertEqual("shared:d", window["ledger_superseded_by"])
        self.assertEqual("", window["commons_superseded_by"])
        self.assertEqual("2026-02-01T00:00:00+00:00", window["ledger_valid_until"])
        stale = report["samples"]["stale"][0]
        self.assertEqual("shared:b", stale["memory_id"])
        self.assertNotEqual(stale["commons_updated_at"], stale["ledger_updated_at"])

    def test_window_mismatch_outranks_stale_for_the_same_row(self):
        """窗口漂移优先归类：它是唯一可修复桶，否则 repair 前后计数不收敛。"""
        report = self.compare()
        self.assertNotIn("shared:c",
                         [item["memory_id"] for item in report["samples"]["stale"]])
        self.assertIn("shared:c",
                      [item["memory_id"] for item in report["samples"]["window_mismatch"]])

    def test_accepts_instance_and_path(self):
        from_instance = compare_shared_with_ledger(
            self.ledger, commons_db=self.commons_db)
        from_path = compare_shared_with_ledger(
            str(self.fixture.ledger_path), commons_db=str(self.commons_db))
        self.assertEqual(from_instance["counts"], from_path["counts"])
        self.assertEqual(from_instance["commons_rows"], from_path["commons_rows"])

    def test_scope_filter_is_applied_to_both_sides(self):
        report = self.compare(scope="private")
        self.assertEqual(0, report["commons_rows"])
        self.assertEqual(0, report["ledger_rows"])
        self.assertTrue(report["ok"])

    def test_missing_commons_is_structured_not_raised(self):
        report = compare_shared_with_ledger(
            self.ledger, commons_db=self.fixture.root / "nope.sqlite")
        self.assertFalse(report["ok"])
        self.assertFalse(report["commons_exists"])
        self.assertTrue(report["commons_error"])
        self.assertEqual(0, report["commons_rows"])
        self.assertEqual(report["ledger_rows"],
                         report["counts"]["missing_in_commons"])

    def test_empty_commons_is_structured_not_raised(self):
        empty = self.fixture.root / "empty-commons.sqlite"
        with sqlite3.connect(str(empty)) as conn:
            conn.execute("CREATE TABLE unrelated (x TEXT)")
        report = compare_shared_with_ledger(self.ledger, commons_db=empty)
        self.assertFalse(report["ok"])
        self.assertTrue(report["commons_exists"])
        self.assertFalse(report["commons_table"])
        self.assertEqual(0, report["commons_rows"])
        self.assertEqual(report["ledger_rows"],
                         report["counts"]["missing_in_commons"])

    def test_missing_ledger_is_structured_not_raised(self):
        report = compare_shared_with_ledger(
            self.fixture.root / "no-ledger.sqlite", commons_db=self.commons_db)
        self.assertFalse(report["ok"])
        self.assertFalse(report["ledger_exists"])
        self.assertTrue(report["ledger_error"])
        self.assertEqual(report["commons_rows"],
                         report["counts"]["missing_in_ledger"])

    def test_default_mode_does_not_touch_files(self):
        commons_before = _snapshot(self.commons_db)
        ledger_before = _snapshot(self.fixture.ledger_path)
        commons_dir_before = _dir_names(self.commons_db.parent)
        ledger_dir_before = _dir_names(self.fixture.ledger_path.parent)
        self.compare()
        self.compare(sample_limit=3)
        self.assertEqual(commons_before, _snapshot(self.commons_db))
        self.assertEqual(ledger_before, _snapshot(self.fixture.ledger_path))
        self.assertEqual(commons_dir_before, _dir_names(self.commons_db.parent))
        self.assertEqual(ledger_dir_before,
                         _dir_names(self.fixture.ledger_path.parent))


class ConsistentPairTests(FixtureCase):
    diverged = False

    def test_clean_pair_is_ok(self):
        report = self.compare()
        self.assertTrue(report["ok"])
        self.assertEqual(2, report["counts"]["in_sync"])
        self.assertEqual(0, report["counts"]["drift_total"])
        self.assertEqual(0, report["repairable"])

    def test_clean_pair_repair_is_a_noop(self):
        before = _snapshot(self.commons_db)
        report = repair_shared_with_ledger(
            self.ledger, commons_db=self.commons_db)
        self.assertEqual(0, report["planned"])
        self.assertEqual(0, report["repaired"])
        self.assertTrue(report["window_resolved"])
        self.assertEqual(before, _snapshot(self.commons_db))


class RepairTests(FixtureCase):
    def _commons_row(self, memory_id):
        with sqlite3.connect(str(self.commons_db)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM shared_memories WHERE memory_id=?",
                (memory_id,)).fetchone()

    def test_repair_syncs_only_window_fields(self):
        before_row = self._commons_row("shared:c")
        ledger_before = _snapshot(self.fixture.ledger_path)
        report = repair_shared_with_ledger(
            self.ledger, commons_db=self.commons_db)
        self.assertEqual(1, report["planned"])
        self.assertEqual(1, report["repaired"])
        self.assertEqual("repair", report["mode"])
        self.assertTrue(report["window_resolved"])
        self.assertEqual(1, report["before"]["counts"]["window_mismatch"])
        self.assertEqual(0, report["after"]["counts"]["window_mismatch"])
        after_row = self._commons_row("shared:c")
        self.assertEqual("shared:d", after_row["superseded_by"])
        self.assertEqual("2026-02-01T00:00:00+00:00", after_row["valid_until"])
        # 只同步窗口：正文 / updated_at / created_at / 行数都不变。
        self.assertEqual(before_row["content"], after_row["content"])
        self.assertEqual(before_row["updated_at"], after_row["updated_at"])
        self.assertEqual(before_row["created_at"], after_row["created_at"])
        with sqlite3.connect(str(self.commons_db)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM shared_memories").fetchone()[0]
        self.assertEqual(4, int(total), "repair 不得删除任何行")
        self.assertEqual(ledger_before, _snapshot(self.fixture.ledger_path))

    def test_repair_dry_run_writes_nothing(self):
        before = _snapshot(self.commons_db)
        report = repair_shared_with_ledger(
            self.ledger, commons_db=self.commons_db, dry_run=True)
        self.assertEqual(1, report["planned"])
        self.assertEqual(0, report["repaired"])
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["changed"])
        self.assertEqual(1, report["after"]["counts"]["window_mismatch"])
        self.assertEqual(before, _snapshot(self.commons_db))

    def test_repair_is_idempotent(self):
        first = repair_shared_with_ledger(
            self.ledger, commons_db=self.commons_db)
        self.assertEqual(1, first["repaired"])
        settled = _snapshot(self.commons_db)
        second = repair_shared_with_ledger(
            self.ledger, commons_db=self.commons_db)
        self.assertEqual(0, second["planned"])
        self.assertEqual(0, second["repaired"])
        self.assertFalse(second["changed"])
        self.assertTrue(second["window_resolved"])
        self.assertEqual(settled, _snapshot(self.commons_db),
                         "第二次 repair 计划为空，不得触碰文件")

    def test_repair_missing_commons_is_graceful(self):
        report = repair_shared_with_ledger(
            self.ledger, commons_db=self.fixture.root / "nope.sqlite")
        self.assertEqual(0, report["repaired"])
        self.assertFalse(report["before"]["commons_exists"])
        self.assertTrue(report["error"])

    def test_repair_missing_ledger_is_graceful(self):
        report = repair_shared_with_ledger(
            self.fixture.root / "nope.sqlite", commons_db=self.commons_db)
        self.assertEqual(0, report["repaired"])
        self.assertFalse(report["before"]["ledger_exists"])
        self.assertTrue(report["error"])


class StatsTests(FixtureCase):
    def test_stats_exposes_last_reconcile_without_writing(self):
        commons_before = _snapshot(self.commons_db)
        archive_before = _snapshot(self.fixture.archive / "index" / "index.sqlite")
        self.assertIsNone(self.fixture.commons.stats()["last_reconcile"])
        report = self.fixture.commons.compare_shared_with_ledger(self.ledger)
        stats = self.fixture.commons.stats()
        last = stats["last_reconcile"]
        self.assertEqual("compare", last["mode"])
        self.assertEqual(report["counts"], last["counts"])
        self.assertFalse(last["ok"])
        self.assertEqual(commons_before, _snapshot(self.commons_db))
        self.assertEqual(archive_before,
                         _snapshot(self.fixture.archive / "index" / "index.sqlite"))

    def test_stats_records_repair_summary(self):
        self.fixture.commons.repair_shared_with_ledger(self.ledger)
        last = self.fixture.commons.stats()["last_reconcile"]
        self.assertEqual("repair", last["mode"])
        self.assertEqual(1, last["repaired"])
        self.assertTrue(last["window_resolved"])
        self.assertEqual(0, last["counts"]["window_mismatch"])

    def test_default_commons_path_honours_archive_env(self):
        archive = self.fixture.root / "portable-archive"
        with mock.patch.dict(os.environ, {"HIPPOMIND_COMMONS": str(archive)}):
            resolved = default_commons_db_path()
        self.assertEqual(archive / "index" / "hippomind-commons.sqlite", resolved)


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-reconcile-script-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root / "data", diverged=True)
        self.fixture.close()
        self.home = self.root / "home"
        self.home.mkdir()

    def _run(self, *args):
        env = dict(os.environ)
        env.update({"HIPPOMIND_HOME": str(self.home),
                    "PYTHONPATH": str(ROOT),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + list(args),
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)

    def _run_json(self, *args):
        completed = self._run(*args, "--json")
        try:
            payload = json.loads(completed.stdout.strip() or "{}")
        except ValueError:
            self.fail("脚本 stdout 不是 JSON: %r / stderr=%r"
                      % (completed.stdout[-800:], completed.stderr[-800:]))
        return completed, payload

    def test_default_json_is_read_only_and_reports_drift(self):
        commons_before = _snapshot(self.fixture.commons_db)
        ledger_before = _snapshot(self.fixture.ledger_path)
        completed, payload = self._run_json(
            "--commons-db", str(self.fixture.commons_db),
            "--ledger", str(self.fixture.ledger_path))
        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual("compare", payload["mode"])
        self.assertEqual(4, payload["counts"]["drift_total"])
        self.assertEqual(1, payload["counts"]["window_mismatch"])
        self.assertEqual(commons_before, _snapshot(self.fixture.commons_db))
        self.assertEqual(ledger_before, _snapshot(self.fixture.ledger_path))

    def test_repair_is_explicit_and_idempotent(self):
        commons_before = _snapshot(self.fixture.commons_db)
        arguments = ("--commons-db", str(self.fixture.commons_db),
                     "--ledger", str(self.fixture.ledger_path), "--repair")
        completed, payload = self._run_json(*arguments)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("repair", payload["mode"])
        self.assertEqual(1, payload["repaired"])
        self.assertTrue(payload["window_resolved"])
        self.assertEqual(0, payload["after"]["counts"]["window_mismatch"])
        self.assertNotEqual(commons_before, _snapshot(self.fixture.commons_db))
        with sqlite3.connect(str(self.fixture.commons_db)) as conn:
            rows = conn.execute(
                "SELECT superseded_by,valid_until FROM shared_memories "
                "WHERE memory_id='shared:c'").fetchone()
            total = conn.execute("SELECT COUNT(*) FROM shared_memories").fetchone()[0]
        self.assertEqual(("shared:d", "2026-02-01T00:00:00+00:00"), rows)
        self.assertEqual(4, int(total))
        settled = _snapshot(self.fixture.commons_db)
        second, second_payload = self._run_json(*arguments)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(0, second_payload["repaired"])
        self.assertEqual(0, second_payload["planned"])
        self.assertEqual(settled, _snapshot(self.fixture.commons_db),
                         "第二次 --repair 不得修改文件")

    def test_human_mode_prints_counts_and_samples(self):
        completed = self._run(
            "--commons-db", str(self.fixture.commons_db),
            "--ledger", str(self.fixture.ledger_path))
        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertIn("窗口漂移 1", completed.stdout)
        self.assertIn("仅commons 1", completed.stdout)
        self.assertIn("[window_mismatch]", completed.stdout)
        self.assertIn("存在双存储分叉", completed.stdout)

    def test_human_repair_prints_before_and_after(self):
        completed = self._run(
            "--commons-db", str(self.fixture.commons_db),
            "--ledger", str(self.fixture.ledger_path), "--repair")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("修复前", completed.stdout)
        self.assertIn("修复后", completed.stdout)
        self.assertIn("窗口收敛: 是", completed.stdout)
        self.assertIn("实际写入 1 行", completed.stdout)

    def test_clean_pair_exits_zero(self):
        clean = Fixture(self.root / "clean", diverged=False)
        clean.close()
        completed, payload = self._run_json(
            "--commons-db", str(clean.commons_db),
            "--ledger", str(clean.ledger_path))
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(0, payload["counts"]["drift_total"])

    def test_missing_commons_does_not_crash(self):
        completed, payload = self._run_json(
            "--commons-db", str(self.root / "absent.sqlite"),
            "--ledger", str(self.fixture.ledger_path))
        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["commons_exists"])
        self.assertTrue(payload["commons_error"])

    def test_missing_ledger_exits_two(self):
        completed, payload = self._run_json(
            "--commons-db", str(self.fixture.commons_db),
            "--ledger", str(self.root / "absent.sqlite"))
        self.assertEqual(2, completed.returncode, completed.stderr)
        self.assertFalse(payload["ledger_exists"])


if __name__ == "__main__":
    unittest.main()
