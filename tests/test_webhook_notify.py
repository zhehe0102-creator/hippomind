"""watcher webhook 通知（迭代 48）：新 Agent 发现推送 + 首轮静默。"""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class _Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class WebhookNotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-hook-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.archive = self.tmp / "archive"
        (self.archive / "tasks").mkdir(parents=True)
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        from hippomind.watcher import ArchiveWatcher
        self.SQLiteLedger = SQLiteLedger
        self.ArchiveWatcher = ArchiveWatcher

    def test_new_agent_pushes_webhook_after_baseline(self):
        srv = HTTPServer(("127.0.0.1", 0), _Hook)
        srv.received = []
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)

        led = self.SQLiteLedger(self.tmp / "w.sqlite")
        watcher = self.ArchiveWatcher(
            led, archive_root=self.archive,
            webhook_url=f"http://127.0.0.1:{srv.server_address[1]}/hook")

        def add_task(tid, agent):
            with (self.archive / "tasks" / "tasks.jsonl").open("a") as f:
                f.write(json.dumps({"task_id": tid, "agent": agent,
                                    "title": f"任务 {tid}",
                                    "status": "completed"}) + "\n")

        add_task("t0", "baseline-agent")
        watcher.sync_once()          # handler 基线轮：静默
        self.assertEqual(srv.received, [], "基线轮不得通知")
        add_task("t1", "brand-new-agent")
        watcher.sync_once()
        self.assertTrue(srv.received, "新 Agent 必须触发 webhook")
        payload = srv.received[0]
        self.assertEqual(payload["kind"], "hippomind.watch")
        self.assertEqual(payload["new_agents"], ["brand-new-agent"])

    def test_unreachable_webhook_never_breaks_sync(self):
        led = self.SQLiteLedger(self.tmp / "w2.sqlite")
        watcher = self.ArchiveWatcher(led, archive_root=self.archive,
                                 webhook_url="http://127.0.0.1:9/hook")  # 不可达端口
        summary = watcher.sync_once()
        self.assertIsInstance(summary, dict)   # 不抛错即通过
        with (self.archive / "tasks" / "tasks.jsonl").open("a") as f:
            f.write(json.dumps({"task_id": "t0", "agent": "a",
                                "title": "x", "status": "ok"}) + "\n")
        summary = watcher.sync_once()
        self.assertEqual(summary["sources"]["tasks"]["imported"], 1,
                         "webhook 失败不得影响采集")


if __name__ == "__main__":
    unittest.main()
