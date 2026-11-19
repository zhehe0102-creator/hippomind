"""integrations.query 任务存储的原子性与并发回归测试（WP-G P2 工程债）。

审计 P2 的失败形态：
1. save_tasks() 用 "w" 截断整文件再逐行重写 —— 写入中途崩溃会丢任务；
2. 读改写无锁 —— 并发更新互相覆盖（lost update）；
3. sync_tasks_to_db() 先 DELETE FROM tasks 再重建表 —— 崩溃窗口里派生索引为空。

本文件钉住三条契约：
* 写入走「唯一临时文件 + os.replace」，失败时旧文件逐字节不变且仍可解析；
* 进程内线程与跨进程同时写不同任务，最终全部保留；
* 重复执行同一更新幂等（不写盘、内容逐字节相同）；派生索引单事务回滚。

运行：
    HIPPOMIND_HOME=$(mktemp -d /tmp/hippo-home.XXXXXX) PYTHONPATH=$PWD \
        /usr/bin/python3 -m unittest tests.test_query_rewrite -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations import query as Q  # noqa: E402


class QueryRewriteTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.archive = Path(self._tmpdir.name)
        (self.archive / "tasks").mkdir(parents=True)
        (self.archive / "index").mkdir(parents=True)
        self.tasks_file = self.archive / "tasks" / "tasks.jsonl"
        self.index_db = self.archive / "index" / "index.sqlite"
        sqlite3.connect(self.index_db).close()
        self._patch = mock.patch.multiple(
            Q, ARCHIVE=self.archive, TASKS_FILE=self.tasks_file,
            INDEX_DB=self.index_db)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def read_raw(self):
        if not self.tasks_file.exists():
            return ""
        return self.tasks_file.read_text("utf-8")

    def read_tasks(self):
        tasks = []
        for line in self.read_raw().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                tasks.append(json.loads(stripped))
            except ValueError:
                continue
        return tasks

    def temp_files(self):
        return [path.name for path in self.tasks_file.parent.glob("*.tmp")]

    def index_rows(self):
        conn = sqlite3.connect(self.index_db)
        try:
            return dict(conn.execute("SELECT task_id, title FROM tasks").fetchall())
        finally:
            conn.close()


class AtomicWriteTests(QueryRewriteTestBase):
    def test_replace_happens_after_full_temp_file_is_written(self):
        Q.create_task("第一个任务", agent="t1")
        first_raw = self.read_raw()
        observed = {}
        real_replace = os.replace

        def spy(source, destination):
            observed["old_content"] = Path(destination).read_text("utf-8")
            observed["new_content"] = Path(source).read_text("utf-8")
            real_replace(source, destination)

        with mock.patch.object(Q.os, "replace", side_effect=spy):
            Q.create_task("第二个任务", agent="t2")
        # os.replace 之前目标文件仍是完整旧内容：不存在清空窗口。
        self.assertEqual(observed["old_content"], first_raw)
        self.assertIn("第一个任务", observed["new_content"])
        self.assertIn("第二个任务", observed["new_content"])
        self.assertEqual([task["title"] for task in self.read_tasks()],
                         ["第一个任务", "第二个任务"])
        self.assertEqual(self.temp_files(), [])

    def test_failed_replace_keeps_original_bytes_and_is_parseable(self):
        Q.create_task("原始任务", agent="a")
        before = self.read_raw()
        with mock.patch.object(Q.os, "replace", side_effect=OSError("模拟磁盘满")):
            with self.assertRaises(OSError):
                Q.create_task("崩溃任务", agent="b")
        self.assertEqual(self.read_raw(), before)
        self.assertEqual([task["title"] for task in self.read_tasks()], ["原始任务"])
        self.assertEqual(self.temp_files(), [])
        # 故障恢复后仍可继续原子写入。
        Q.create_task("恢复任务", agent="c")
        self.assertEqual([task["title"] for task in self.read_tasks()],
                         ["原始任务", "恢复任务"])

    def test_failed_serialise_keeps_original_bytes_and_is_parseable(self):
        Q.create_task("原始任务", agent="a")
        before = self.read_raw()
        with mock.patch.object(Q.json, "dumps", side_effect=TypeError("模拟序列化崩溃")):
            with self.assertRaises(TypeError):
                Q.create_task("坏任务", agent="b")
        self.assertEqual(self.read_raw(), before)
        self.assertEqual([task["title"] for task in self.read_tasks()], ["原始任务"])
        self.assertEqual(self.temp_files(), [])
        Q.create_task("恢复任务", agent="c")
        self.assertEqual(len(self.read_tasks()), 2)

    @unittest.skipUnless(os.name == "posix", "POSIX 权限位检查")
    def test_lock_file_is_created_with_restricted_mode(self):
        Q.create_task("锁任务", agent="a")
        lock_path = self.tasks_file.with_name(self.tasks_file.name + ".lock")
        self.assertTrue(lock_path.exists())
        self.assertEqual(oct(lock_path.stat().st_mode & 0o777), oct(0o600))

    def test_malformed_lines_survive_rewrite_and_duplicate_ids_are_normalised(self):
        legacy = [
            {"task_id": "keep-me", "title": "旧任务", "status": "open"},
            {"task_id": "keep-me", "title": "重复旧任务", "status": "done"},
        ]
        self.tasks_file.write_text(
            json.dumps(legacy[0], ensure_ascii=False) + "\n"
            + "this line is not json" + "\n"
            + json.dumps(legacy[1], ensure_ascii=False) + "\n",
            encoding="utf-8")
        Q.create_task("新任务", agent="a")
        raw = self.read_raw()
        # 坏行原样保留而不是被静默清掉；重复 task_id 被归一为一条。
        self.assertIn("this line is not json", raw)
        self.assertEqual(raw.count('"keep-me"'), 1)
        by_id = {task["task_id"]: task for task in self.read_tasks()}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id["keep-me"]["title"], "重复旧任务")


class ConcurrencyTests(QueryRewriteTestBase):
    def test_concurrent_threads_updating_distinct_tasks_keep_both(self):
        left = Q.create_task("线程任务 A", agent="thread-a")
        right = Q.create_task("线程任务 B", agent="thread-b")
        rounds = 4
        barrier = threading.Barrier(2)
        errors = []

        def worker(task_id, status, prefix):
            try:
                barrier.wait(timeout=5)
                for index in range(rounds):
                    Q.update_task(task_id, status=status, note=f"{prefix}-{index}")
                    time.sleep(0.002)
            except Exception as exc:  # pragma: no cover - 由 errors 报告
                errors.append(exc)

        threads = [
            threading.Thread(target=worker,
                             args=(left["task_id"], "in_progress", "left")),
            threading.Thread(target=worker,
                             args=(right["task_id"], "done", "right")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        by_id = {task["task_id"]: task for task in self.read_tasks()}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id[left["task_id"]]["status"], "in_progress")
        self.assertEqual(by_id[right["task_id"]]["status"], "done")
        # 丢更新会表现为某轮 note 消失：每个任务必须恰好累积 rounds 条。
        self.assertEqual(len(by_id[left["task_id"]]["notes"]), rounds)
        self.assertEqual(len(by_id[right["task_id"]]["notes"]), rounds)
        self.assertEqual(self.temp_files(), [])

    @unittest.skipUnless(getattr(Q, "fcntl", None) is not None,
                         "需要 POSIX fcntl 跨进程锁")
    def test_concurrent_processes_keep_all_tasks(self):
        titles = ["进程任务-0", "进程任务-1", "进程任务-2"]
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from integrations import query as Q; "
            "Q.create_task(sys.argv[2], agent='subprocess')"
        )
        environment = dict(os.environ)
        environment["AGENT_ARCHIVE"] = str(self.archive)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (str(ROOT) if not existing
                                     else str(ROOT) + os.pathsep + existing)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT), title],
            cwd=str(ROOT), env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE) for title in titles]
        for process in processes:
            _stdout, stderr = process.communicate(timeout=90)
            self.assertEqual(process.returncode, 0,
                             stderr.decode("utf-8", "replace")[-2000:])
        stored = sorted(task["title"] for task in self.read_tasks())
        self.assertEqual(stored, sorted(titles))
        self.assertEqual(self.temp_files(), [])
        # 同一把锁内同步派生索引：最后一个写者用完整合并结果提交。
        self.assertEqual(sorted(self.index_rows().values()), sorted(titles))


class IdempotencyTests(QueryRewriteTestBase):
    def test_repeated_update_is_idempotent(self):
        task = Q.create_task("幂等任务", agent="a")
        Q.update_task(task["task_id"], status="done")
        after_first = self.read_raw()
        Q.update_task(task["task_id"], status="done")
        after_second = self.read_raw()
        self.assertEqual(after_first, after_second)
        tasks = self.read_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "done")
        # 无变化时不产生临时文件，也不重复追加 updated_at。
        self.assertEqual(self.temp_files(), [])
        Q.update_task(task["task_id"], status="done", agent="a")
        self.assertEqual(self.read_raw(), after_second)

    def test_cli_create_and_update_keep_single_document(self):
        add_args = SimpleNamespace(
            title="CLI 任务", desc="cli", status="in_progress",
            agent="cli-agent", session="", tags=["x"])
        with contextlib.redirect_stdout(io.StringIO()):
            task_id = Q.cmd_task_add(add_args)
        update_args = SimpleNamespace(
            task_id=task_id, status="done", note="完成", agent="cli-agent")
        with contextlib.redirect_stdout(io.StringIO()):
            Q.cmd_task_update(update_args)
        tasks = self.read_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "done")
        self.assertEqual(len(tasks[0]["notes"]), 1)


class DerivedIndexTests(QueryRewriteTestBase):
    def test_sync_upserts_and_removes_only_stale_rows(self):
        Q.sync_tasks_to_db([
            {"task_id": "a", "title": "A", "status": "open"},
            {"task_id": "b", "title": "B", "status": "open"},
        ])
        conn = sqlite3.connect(self.index_db)
        conn.execute("INSERT INTO tasks (task_id, title, status) VALUES ('stale', '旧', 'open')")
        conn.commit()
        conn.close()
        Q.sync_tasks_to_db([
            {"task_id": "a", "title": "A2", "status": "done"},
            {"task_id": "b", "title": "B", "status": "open"},
        ])
        self.assertEqual(self.index_rows(), {"a": "A2", "b": "B"})

    def test_sync_failure_rolls_back_previous_index_rows(self):
        Q.sync_tasks_to_db([{"task_id": "kept", "title": "保留"}])
        conn = sqlite3.connect(self.index_db)
        conn.execute(
            "CREATE TRIGGER abort_boom BEFORE INSERT ON tasks "
            "WHEN NEW.task_id='boom' BEGIN SELECT RAISE(ABORT, 'boom'); END")
        conn.commit()
        conn.close()
        ok = Q.sync_tasks_to_db([
            {"task_id": "kept", "title": "被覆盖"},
            {"task_id": "boom", "title": "坏"},
        ])
        self.assertFalse(ok)
        # 单事务回滚：旧行既没有被清空，也没有被半更新。
        self.assertEqual(self.index_rows(), {"kept": "保留"})


if __name__ == "__main__":
    unittest.main()
