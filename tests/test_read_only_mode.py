"""daemon --read-only 只读模式（前景轮六，LAN 镜像使能器；WP-B 加固）。

只读模式的验收契约（审计复现第 2 条 + 第 13 条）：
- 账本是只读连接（mode=ro + query_only），服务构造/查询/后台活动期间主库
  size 与 mtime_ns 不变；
- watcher、discovery（自动安装）、capture、distill、vector、sleep、
  metrics-trend、warmup 一个都不启动（start_* 内部门禁也要挡住手工调用）；
- Agent 配置（临时 HOME 下的假配置）启动前后逐字节不变；
- 数据面（ingest/sync/reload）被拒绝，/metrics 需要本地凭据，/health 免鉴权；
- maintenance_checkpoint / _maybe_sleep_consolidate / append_metrics_trend
  在只读模式下是空操作。
"""

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import daemon as daemon_module
from hippomind.daemon import (
    SLEEP_IDLE_SECONDS,
    MemoryHubHTTPServer,
    MemoryHubService,
    RuntimePaths,
)
from hippomind.ledger import LedgerError, SQLiteLedger

#: 只读模式下绝不能出现的后台写循环线程名。
FORBIDDEN_THREADS = {
    "hippomind-watcher", "hippomind-discovery", "hippomind-transcripts",
    "hippomind-distill", "hippomind-vector-index", "hippomind-metrics-trend",
    "hippomind-sleep", "hippomind-warmup",
}
#: start_* 必须全部记录为 read_only 跳过。
FORBIDDEN_BACKGROUND_KEYS = (
    "watcher", "discovery", "capture", "distill",
    "vector_index", "sleep", "metrics_trend", "warmup",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_state(path: Path):
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


def _seed_ledger(path: Path) -> str:
    ledger = SQLiteLedger(path)
    try:
        result = ledger.ingest({"content": "只读模式探针", "kind": "fact",
                                "source_agent": "t", "evidence": ["t:1"]})
    finally:
        ledger.close()
    return result.memory_id


class ReadOnlyModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-ro-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        # 临时 HOME：只读服务启动不得改动任何 Agent 配置文件。预置两个假配置，
        # 启动前后做逐字节 sha256 对比（真实 HOME 绝不参与）。
        self.home = root / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".claude" / "CLAUDE.md").write_text(
            "fake claude rules\n", encoding="utf-8")
        (self.home / ".codex" / "config.toml").write_text(
            "fake codex config\n", encoding="utf-8")
        self.agent_hashes = {
            path: _sha256(path)
            for path in sorted(self.home.rglob("*")) if path.is_file()
        }
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "HIPPOMIND_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)

        self.ledger_path = root / "hub.sqlite"
        self.memory_id = _seed_ledger(self.ledger_path)
        # 只读服务构造前的主库快照：构造与运行都不得改动它。
        self.before_stat = _file_state(self.ledger_path)
        self.before_hash = _sha256(self.ledger_path)
        self.paths = RuntimePaths.for_ledger(self.ledger_path, root / "rt")
        # watch=True 是刻意为之：watch 开关本身不能绕过只读门禁。
        self.service = MemoryHubService(self.paths, watch=True, read_only=True)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.service.ledger.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    # -- HTTP helpers ---------------------------------------------------
    def _get(self, path, token=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if token:
            request.add_header("X-HippoMind-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _post(self, path, body, token=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        if token:
            request.add_header("X-HippoMind-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    # -- data plane -----------------------------------------------------
    def test_reads_allowed_writes_rejected(self):
        self.assertEqual(200, self._get("/health")[0])
        self.assertEqual(200, self._get("/stats", self.service.token)[0])
        # 无凭据的写请求先被鉴权挡住。
        self.assertEqual(401, self._post("/ingest", {"records": [
            {"content": "x", "kind": "fact", "source_agent": "t",
             "evidence": ["x"]}]})[0])
        # 有凭据时被只读策略挡住。
        body = {"records": [{"content": "只读写入尝试", "kind": "fact",
                             "source_agent": "t", "evidence": ["t:2"]}]}
        self.assertIn(self._post("/ingest", body, self.service.token)[0], (401, 403))
        self.assertIn(self._post("/sync", {}, self.service.token)[0], (401, 403))
        self.assertIn(self._post("/admin/reload", {}, self.service.token)[0],
                      (401, 403))
        self.assertEqual(1, self.service.ledger.stats()["memories"])

    def test_read_only_ledger_rejects_writes(self):
        self.assertTrue(self.service.ledger.read_only)
        self.assertTrue(self.service.read_only)
        with self.assertRaises(LedgerError):
            self.service.ledger.ingest({
                "content": "y", "kind": "fact",
                "source_agent": "t", "evidence": ["y"]})
        with self.assertRaises(LedgerError):
            self.service.ledger._transaction(lambda conn: None)
        with self.assertRaises(LedgerError):
            self.service.ledger._connect(write=True)
        summary = self.service.ledger.maintenance_checkpoint()
        self.assertIs(summary.get("read_only"), True)

    # -- metrics auth ---------------------------------------------------
    def test_metrics_requires_token_health_stays_anonymous(self):
        self.assertEqual(401, self._get("/metrics")[0])
        self.assertEqual(401, self._get("/metrics", token="wrong-token")[0])
        status, body = self._get("/metrics", token=self.service.token)
        self.assertEqual(200, status)
        text = body.decode()
        self.assertIn("# TYPE hippomind_memories_total", text)
        # /health 保持免鉴权，并声明只读模式（launcher/运维可观测）。
        status, body = self._get("/health")
        self.assertEqual(200, status)
        health = json.loads(body)
        self.assertEqual("ok", health["status"])
        self.assertIs(health["read_only"], True)

    # -- background writers --------------------------------------------
    def test_no_background_writer_starts(self):
        service = self.service
        events = []
        service.watcher.run = lambda: events.append("watcher.run")
        service.ledger.maintain = (
            lambda **kwargs: events.append(("maintain", dict(kwargs))) or {})
        service.ledger.consolidate = (
            lambda **kwargs: events.append("consolidate") or [])
        service.ledger.maintenance_checkpoint = (
            lambda: events.append("checkpoint") or {})
        service._auto_install_detected = (
            lambda *args, **kwargs: events.append("auto_install") or {})

        # 手工把每个 start_* 都调一遍：内部门禁必须全部挡住。
        service.start_watcher()
        service.start_warmup()
        service.start_discovery_loop()
        service.start_capture_loop()
        service.start_vector_index_loop()
        service.start_entity_index_loop()
        service.start_distill_loop()
        service.start_metrics_trend_thread()
        service.start_sleep_loop()

        # 直接调用空闲巩固/趋势写入口：也必须空操作（不能摸账本）。
        service._last_activity_monotonic -= SLEEP_IDLE_SECONDS * 4
        service._last_sleep_run = None
        self.assertIsNone(service._maybe_sleep_consolidate())
        service._last_metrics_log -= 7200
        self.assertIsNone(service.append_metrics_trend())
        time.sleep(0.25)

        self.assertEqual([], events, "只读模式后台循环绝不能触碰账本")
        names = {thread.name for thread in threading.enumerate()}
        self.assertEqual(set(), names & FORBIDDEN_THREADS)
        self.assertEqual({}, service.auto_install)
        trend = self.paths.state_path.parent / "metrics.jsonl"
        self.assertFalse(trend.exists(), "只读模式不得写 metrics.jsonl")

        background = service.health["background"]
        for key in FORBIDDEN_BACKGROUND_KEYS:
            self.assertEqual("skipped:read_only", background.get(key), background)
        self.assertIs(service.health["read_only"], True)

    def test_agent_configs_are_byte_identical(self):
        # 直接调用发现/自动安装/热重载入口：只读模式一个都不能落地写操作。
        report = {"known": {"claude": {"detected": True},
                            "codex": {"detected": True}}}
        outcome = self.service._auto_install_detected(report)
        self.assertEqual("read_only", outcome.get("skipped"), outcome)
        self.assertEqual({}, self.service.auto_install)
        reloaded = self.service.reload()
        self.assertEqual("read_only", reloaded.get("skipped"), reloaded)
        self.service.start_discovery_loop()
        time.sleep(0.1)
        for path, digest in self.agent_hashes.items():
            self.assertTrue(path.is_file(), f"Agent 配置被删除: {path}")
            self.assertEqual(digest, _sha256(path),
                             f"Agent 配置被改写: {path}")
        self.assertEqual(self.agent_hashes, {
            path: _sha256(path)
            for path in sorted(self.home.rglob("*")) if path.is_file()})

    def test_ledger_file_is_unchanged(self):
        # 构造 + 多轮读请求 + 后台入口尝试之后，主库逐字节/时间戳不变。
        for path in ("/health", "/stats"):
            self._get(path, self.service.token)
        self._get("/metrics", self.service.token)
        self.service.start_watcher()
        self.service.start_sleep_loop()
        time.sleep(0.1)
        self.assertEqual(self.before_stat, _file_state(self.ledger_path),
                         "只读服务不得改动主库 size/mtime")
        self.assertEqual(self.before_hash, _sha256(self.ledger_path),
                         "只读服务不得改动主库内容")

    def test_stats_exposes_read_only_health(self):
        self.service.start_watcher()
        status, body = self._get("/stats", self.service.token)
        self.assertEqual(200, status)
        stats = json.loads(body)
        self.assertIs(stats["health"]["read_only"], True)
        self.assertEqual("skipped:read_only",
                         stats["health"]["background"]["watcher"])
        self.assertEqual(200, self._get("/stats", self.service.token)[0])

    def test_admin_token_downgraded_policy(self):
        policy = self.service.access_policy(self.service.token)
        self.assertIsNotNone(policy)
        self.assertFalse(policy.admin, "只读模式下管理员必须被降级")


class RunDaemonReadOnlyWiringTests(unittest.TestCase):
    """run_daemon 的只读接线：后台写循环一个都不许被拉起。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-ro-daemon-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "HIPPOMIND_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ledger_path = root / "hub.sqlite"
        _seed_ledger(self.ledger_path)
        self.before_stat = _file_state(self.ledger_path)
        self.paths = RuntimePaths.for_ledger(self.ledger_path, root / "rt")

    @staticmethod
    def _free_port() -> int:
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def test_run_daemon_read_only_starts_no_writer(self):
        instances = []

        class CapturingService(MemoryHubService):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                instances.append(self)

        port = self._free_port()
        with mock.patch.object(daemon_module, "MemoryHubService",
                               CapturingService):
            worker = threading.Thread(
                target=daemon_module.run_daemon,
                kwargs={"ledger_path": self.ledger_path,
                        "runtime_dir": self.paths.state_path.parent,
                        "host": "127.0.0.1", "port": port,
                        "watch": True, "read_only": True},
                name="hippo-ro-run-daemon", daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline and not instances:
                    time.sleep(0.02)
                self.assertTrue(instances, "read-only daemon never started")
                service = instances[0]
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    try:
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{port}/health")
                        with urllib.request.urlopen(request, timeout=2) as response:
                            health = json.loads(response.read())
                        break
                    except (urllib.error.URLError, OSError, ValueError):
                        time.sleep(0.05)
                else:
                    self.fail("read-only daemon /health never answered")
                self.assertIs(health["read_only"], True)
                names = {thread.name for thread in threading.enumerate()}
                self.assertEqual(set(), names & FORBIDDEN_THREADS)
                background = service.health["background"]
                for key in FORBIDDEN_BACKGROUND_KEYS:
                    self.assertNotIn("running", str(background.get(key, "")))
                self.assertEqual(self.before_stat, _file_state(self.ledger_path))
            finally:
                if instances:
                    instances[0].shutdown()
                worker.join(timeout=15)
        self.assertFalse(worker.is_alive(), "read-only daemon did not stop")
        self.assertEqual(self.before_stat, _file_state(self.ledger_path),
                         "只读 daemon 启动/停机不得改动主库 size/mtime")


if __name__ == "__main__":
    unittest.main()
