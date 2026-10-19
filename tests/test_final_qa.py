"""Adversarial and QA-standard edge tests for the final HippoMind wrap-up.

Round-3 checklist: oversized records, Chinese paths, corruption variants,
concurrency, scope violations by the DSH write-back credential, install
auto-repair, cross-agent dedup, uninstall/reinstall recovery, a protocol-real
OpenAI-compatible LLM interface, and headless GUI determinism.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from hippomind.agent_install import install_agent
from hippomind.auto_context import AUTO_MAX_CHARS, build_auto_context, render_untrusted_context
from hippomind.context_pack import DEFAULT_MAX_CHARS, ContextPackBuilder, ContextPackOptions
from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths
from hippomind.dsh_install import (
    dsh_integration_status,
    dsh_plugin_target,
    uninstall_dsh_integration,
)
from hippomind.ledger import LedgerError, SQLiteLedger
from hippomind.watcher import ArchiveWatcher
from tests.test_dsh_sessions import (
    NODE_BINARY,
    assistant_event,
    session_event,
    user_event,
    write_zstd_session,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def make_fake_dsh(home: Path) -> Path:
    dsh = home / ".dsh"
    (dsh / "profiles" / "web").mkdir(parents=True, exist_ok=True)
    (dsh / "settings.yaml").write_text("ui-onboarding: {}\n", encoding="utf-8")
    (dsh / "profiles" / "web" / "package.json").write_text(json.dumps({
        "name": "dsh-profile-web", "private": True,
        "dependencies": {}, "dsh": {"profile": {"bundles": []}},
    }), encoding="utf-8")
    return dsh


class OversizedRecordTests(unittest.TestCase):
    def test_multimegabyte_content_stays_bounded_in_storage_and_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SQLiteLedger(Path(tmp) / "hub.sqlite")
            huge = "超长记录压力样本：" + "并发限制回归检查" * 200_000  # ~1.6MB
            result = ledger.ingest({
                "kind": "progress", "content": huge, "source_agent": "codex",
                "task_id": "huge", "project": "hippomind", "scope": "shared",
                "evidence": ["task:huge"],
            })
            # Within the designed 2M-char storage cap the record is kept whole;
            # beyond it the ledger rejects instead of silently truncating.
            self.assertLessEqual(len(str(ledger.get_memory(result.memory_id)["content"])),
                                 2_000_000)
            with self.assertRaises(LedgerError):
                ledger.ingest({"kind": "progress", "content": huge + huge,
                               "source_agent": "codex", "scope": "shared",
                               "evidence": ["task:over"]})
            pack = build_auto_context(ledger, "并发限制回归", cwd="/w/hippomind")
            self.assertLessEqual(len(pack.get("text", "")), AUTO_MAX_CHARS)
            rendered = render_untrusted_context(pack)
            self.assertTrue(rendered.startswith("<MEMORY_DATA_UNTRUSTED>"))

    def test_oversized_user_request_is_clipped_not_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SQLiteLedger(Path(tmp) / "hub.sqlite")
            ledger.ingest({"kind": "task", "content": "验证超长请求处理",
                           "source_agent": "codex", "scope": "shared",
                           "evidence": ["t:1"]})
            request = "继续验证 " * 20_000  # 100KB+ request text
            pack = build_auto_context(ledger, request)
            self.assertIsInstance(pack.get("items"), list)


class ChinesePathTests(unittest.TestCase):
    def test_full_stack_under_chinese_paths(self):
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer) / "项目-海马体-测试"
            home = root / "用户主目录"
            home.mkdir(parents=True)
            make_fake_dsh(home)
            paths = RuntimePaths.for_ledger(root / "账本.sqlite", str(root / "运行目录"))
            ledger = SQLiteLedger(paths.ledger_path)
            installed = install_agent(ledger, paths, "deepseek",
                                      package_root=PACKAGE_ROOT, home=home)
            self.assertTrue(installed["native_dsh_plugin"]["installed"])
            self.assertTrue((home / ".deepseek" / "AGENTS.md").is_file())
            status = dsh_integration_status(home=home, package_root=PACKAGE_ROOT)
            self.assertTrue(status["installed"])
            # Retrieval with a Chinese working directory hint stays clean.
            ledger.ingest({"kind": "task", "content": "中文路径下的检索验证",
                           "source_agent": "codex", "scope": "shared",
                           "evidence": ["t:zh"]})
            pack = build_auto_context(ledger, "继续检索验证", cwd=str(root))
            self.assertTrue(pack["items"])


@unittest.skipIf(NODE_BINARY is None, "node runtime not available")
class CorruptionVariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def add_session(self, name):
        directory = self.root / "--slug--" / name
        directory.mkdir(parents=True)
        return directory / "session.jsonl.zstd"

    def test_empty_no_magic_and_midfile_garbage_each_fail_isolated(self):
        good = self.add_session("session-good")
        write_zstd_session(good, [
            {"type": "session", "id": "session-good", "createdAt": 1, "cwd": "/w/p"},
            user_event(1, "正常会话的问题"),
            assistant_event(2, "正常会话的回复"),
        ])
        empty = self.add_session("session-empty")
        empty.write_bytes(b"")
        plain = self.add_session("session-plain")
        plain.write_bytes("这不是 zstd 数据\n".encode("utf-8") * 100)
        mid = self.add_session("session-midgarbage")
        write_zstd_session(mid, [
            {"type": "session", "id": "session-mid", "createdAt": 1, "cwd": "/w/p"},
            user_event(1, "前半段问题"),
        ])
        with mid.open("ab") as handle:
            handle.write(b"\x28\xb5\x2f\xfd" + os.urandom(512))
        watcher = ArchiveWatcher(self.ledger, archive_root=self.root / "none",
                                 dsh_sessions_root=self.root)
        result = watcher.sync_once()
        dsh = result["sources"]["dsh-sessions"]
        # Empty (just-created) files are a no-op; non-zstd garbage fails once
        # and is isolated; a corrupt tail frame still salvages earlier turns.
        self.assertEqual(dsh["failed"], 1)
        self.assertEqual(dsh["imported"], 2)
        salvaged = self.ledger.search("前半段问题", scope="shared", limit=5,
                                      include_raw_chat=True)
        self.assertTrue(salvaged)
        rows = self.ledger.search("正常会话", scope="shared", limit=10,
                                  include_raw_chat=True)
        self.assertTrue(rows)

    def test_malformed_jsonl_lines_are_skipped_and_cursor_advances(self):
        path = self.add_session("session-badlines")
        good_lines = [
            json.dumps({"type": "session", "id": "session-badlines",
                        "createdAt": 1, "cwd": "/w/p"}),
            "{broken json !!!",
            json.dumps(user_event(1, "坏行之后的用户问题")),
            "not json at all",
            json.dumps(assistant_event(3, "坏行之后的助手回复")),
        ]
        script = (
            "const zlib=require('node:zlib'),fs=require('node:fs');"
            "fs.writeFileSync(process.argv[1],"
            "zlib.zstdCompressSync(Buffer.from(process.argv[2])));"
        )
        subprocess_run([NODE, "-e", script, str(path), "\n".join(good_lines) + "\n"])
        watcher = ArchiveWatcher(self.ledger, archive_root=self.root / "none",
                                 dsh_sessions_root=self.root)
        first = watcher.sync_once()
        self.assertGreaterEqual(first["sources"]["dsh-sessions"]["imported"], 1)
        second = watcher.sync_once()
        self.assertEqual(second["sources"]["dsh-sessions"]["imported"], 0)


def subprocess_run(command):
    import subprocess
    subprocess.run(command, check=True, timeout=30, capture_output=True)


class ConcurrentSyncTests(unittest.TestCase):
    def test_parallel_watchers_and_readers_do_not_corrupt_or_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = SQLiteLedger(root / "hub.sqlite")
            ledger.ingest({"kind": "task", "content": "并发读取的基线记录",
                           "source_agent": "codex", "scope": "shared",
                           "evidence": ["t:base"]})
            errors = []

            def writer():
                try:
                    watcher = ArchiveWatcher(ledger, archive_root=root / "none",
                                             dsh_sessions_root=root / "none-dsh")
                    for _round in range(5):
                        watcher.sync_once()
                except Exception as exc:  # noqa: BLE001 - recorded for assertion
                    errors.append(exc)

            def reader():
                try:
                    for _round in range(10):
                        ContextPackBuilder(ledger).build("并发读取", ContextPackOptions())
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=writer) for _ in range(3)]
            threads += [threading.Thread(target=reader) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            hits = ledger.search("基线记录", scope="shared", limit=50)
            self.assertTrue(hits)


class DSHWriteBackScopeTests(unittest.TestCase):
    def test_plugin_credential_cannot_spoof_or_cross_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = RuntimePaths.for_ledger(Path(tmp) / "hub.sqlite", tmp)
            service = MemoryHubService(paths, watch=False)
            server = MemoryHubHTTPServer(("127.0.0.1", 0), service)
            service._server = server
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                _credential, token = service.ledger.issue_agent_credential(
                    "deepseek", scopes=["shared", "agent:deepseek"],
                    permissions=["read", "ingest", "feedback", "stats", "shared_ingest"])
                service.ledger.ingest({
                    "kind": "progress", "content": "Codex 私有进度", "source_agent": "codex",
                    "scope": "agent:codex", "evidence": ["t:codex-private"],
                })

                def request(path, body):
                    payload = json.dumps(body).encode("utf-8")
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}", data=payload, method="POST")
                    req.add_header("Content-Type", "application/json")
                    req.add_header("X-HippoMind-Token", token)
                    with urllib.request.urlopen(req, timeout=3) as response:
                        return json.loads(response.read().decode("utf-8"))

                # A hostile write-back shape cannot attribute work to codex.
                with self.assertRaises(urllib.error.HTTPError) as spoofed:
                    request("/ingest", {"kind": "result", "content": "冒充",
                                        "source_agent": "codex", "scope": "shared",
                                        "evidence": ["x:1"]})
                self.assertEqual(spoofed.exception.code, 403)
                # Nor read Codex's private scope through the pack endpoint.
                with self.assertRaises(urllib.error.HTTPError) as foreign:
                    request("/context-pack", {"query": "Codex 私有进度",
                                              "options": {"scope": "agent:codex"}})
                self.assertEqual(foreign.exception.code, 403)
                # Legitimate write-back shape is accepted and attributed.
                accepted = request("/ingest", {
                    "kind": "result", "content": "用户要求：部署\n最终回复：完成",
                    "source_agent": "deepseek", "scope": "shared",
                    "session_id": "session-real",
                    "evidence": ["dsh-session://session-real#seq=1-4"],
                })
                self.assertTrue(accepted["inserted"])
                pack = request("/context-pack", {"query": "部署", "options": {}})
                self.assertNotIn("Codex 私有进度", json.dumps(pack, ensure_ascii=False))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class InstallAutoRepairTests(unittest.TestCase):
    def test_broken_symlink_is_detected_and_repaired_on_reinstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True)
            make_fake_dsh(home)
            paths = RuntimePaths.for_ledger(Path(tmp) / "hub.sqlite",
                                            str(Path(tmp) / "runtime"))
            ledger = SQLiteLedger(paths.ledger_path)
            install_agent(ledger, paths, "deepseek", package_root=PACKAGE_ROOT, home=home)
            link = home / ".dsh" / "profiles" / "web" / "node_modules" / "@dsh-external" / "dsh-hippomind"
            self.assertTrue(link.exists())
            link.unlink()  # simulate a lost junction
            self.assertFalse(dsh_integration_status(home=home)["installed"])
            result = install_agent(ledger, paths, "deepseek",
                                   package_root=PACKAGE_ROOT, home=home)
            native = result["native_dsh_plugin"]
            self.assertTrue(native["installed"])
            self.assertTrue(native.get("verified"))
            self.assertTrue(link.exists())
            self.assertTrue(dsh_integration_status(home=home, package_root=PACKAGE_ROOT)["installed"])

    def test_uninstall_then_reinstall_recovers_full_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir(parents=True)
            make_fake_dsh(home)
            paths = RuntimePaths.for_ledger(Path(tmp) / "hub.sqlite",
                                            str(Path(tmp) / "runtime"))
            ledger = SQLiteLedger(paths.ledger_path)
            install_agent(ledger, paths, "deepseek", package_root=PACKAGE_ROOT, home=home)
            uninstall_dsh_integration(home=home)
            self.assertFalse((dsh_plugin_target(home) / "lib" / "index.js").exists())
            result = install_agent(ledger, paths, "deepseek",
                                   package_root=PACKAGE_ROOT, home=home)
            self.assertTrue(result["native_dsh_plugin"]["installed"])
            status = dsh_integration_status(home=home, package_root=PACKAGE_ROOT)
            self.assertTrue(status["installed"])
            self.assertTrue(status["plugin_source_current"])


class CrossAgentDedupTests(unittest.TestCase):
    def test_identical_cross_agent_content_is_deduplicated_in_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SQLiteLedger(Path(tmp) / "hub.sqlite")
            shared_content = "迁移完成：SQLite/WAL 账本统一了事实来源 dedup-marker-8848"
            for agent in ("codex", "claude", "gemini"):
                ledger.ingest({
                    "kind": "progress", "content": shared_content,
                    "source_agent": agent, "scope": "shared",
                    "evidence": [f"t:{agent}"],
                })
            ledger.ingest({
                "kind": "raw_chat", "raw_chat": True, "content": "无关原始聊天 dedup-marker-8848",
                "source_agent": "claude", "scope": "shared",
                "evidence": ["t:raw"],
            })
            pack = ContextPackBuilder(ledger).build("迁移完成 dedup-marker-8848",
                                                    ContextPackOptions())
            self.assertLessEqual(len(pack["text"]), DEFAULT_MAX_CHARS)
            self.assertLessEqual(pack["text"].count("dedup-marker-8848"), 2)
            self.assertNotIn("无关原始聊天", pack["text"])


class RealLLMInterfaceTests(unittest.TestCase):
    """Protocol-real LLM check: an OpenAI-compatible HTTP server on localhost."""

    def test_run_task_over_openai_compatible_http(self):
        from hippomind.hippocampus import Hippocampus
        from hippomind.llm_agent import LLMHippoAgent

        observed = {}

        class CompatibleHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                observed["authorization"] = self.headers.get("Authorization", "")
                observed["model"] = body.get("model", "")
                observed["prompt"] = body["messages"][-1]["content"]
                reply = ("ACTION: 部署\nLESSON: 先跑回归\nRESULT: good\n"
                         "RESULT_TEXT: 已按记忆库经验完成部署。")
                payload = json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": reply}}],
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), CompatibleHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            hippo = Hippocampus(seed=5, consolidate_every=10_000, dream_every=10_000,
                                auto_prune_every=10_000)
            source = hippo.store("部署服务", action="跑回归", valence=0.5)
            agent = LLMHippoAgent(hippo, base_url=f"http://127.0.0.1:{port}/v1",
                                  api_key="qa-interface-key", model="qa-model")
            self.assertTrue(agent.available)
            reply = agent.run_task("部署服务")
            self.assertIn("ACTION: 部署", reply)
            self.assertEqual(observed["authorization"], "Bearer qa-interface-key")
            self.assertEqual(observed["model"], "qa-model")
            self.assertIn("MEMORY_DATA_UNTRUSTED", observed["prompt"])
            # Prediction is not committed until external feedback arrives.
            self.assertEqual(len(hippo.episodic), 1)
            committed = agent.record_feedback(0.8, outcome="部署成功")
            self.assertEqual(len(hippo.episodic), 2)
            self.assertGreater(hippo.episodic[source].utility, 0.0)
            self.assertEqual(hippo.episodic[committed].source, "external-feedback")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_llm_outage_raises_clear_runtime_error(self):
        from hippomind.hippocampus import Hippocampus
        from hippomind.llm_agent import LLMHippoAgent
        hippo = Hippocampus(seed=5, consolidate_every=10_000, dream_every=10_000,
                            auto_prune_every=10_000)
        agent = LLMHippoAgent(hippo, base_url="http://127.0.0.1:9/v1",
                              api_key="k", timeout=2.0)
        with self.assertRaises(RuntimeError):
            agent.run_task("断网检查")


class HeadlessGuiTests(unittest.TestCase):
    def _render(self, seed: int) -> bytes:
        import matplotlib
        matplotlib.use("Agg", force=True)
        import io
        import matplotlib.pyplot as plt
        from hippomind.agent import MemoryAgent
        from hippomind.grid_world import GridWorld
        from hippomind.mind import Mind
        from hippomind.network_visualizer import LayeredMemoryVisualizer

        class DummyHub:
            def stats(self):
                return {"health": {"status": "ok"}, "stats": {"ledger": {
                    "events": 12, "active": 9999, "archived": 8888,
                    "kinds": {"semantic": 7777},
                    "agents": {"codex": 8, "pi": 4},
                }}}

            def context_pack(self, _query):
                return {"text": "任务：GUI 回归\n下一步：保持有界上下文"}

        mind = Mind(seed=seed)
        env = GridWorld(seed=seed)
        agent = MemoryAgent(env, mind, seed=seed)
        for _ in range(30):
            agent.step()
        visualizer = LayeredMemoryVisualizer(mind, env, agent, hub_client=DummyHub(),
                                              max_nodes=100, max_edges=200)
        visualizer.do_context_pack()
        buffer = io.BytesIO()
        visualizer.fig.savefig(buffer, format="png")
        plt.close(visualizer.fig)
        return buffer.getvalue()

    def test_headless_render_is_deterministic_and_within_frame_budget(self):
        started = time.monotonic()
        first = self._render(21)
        elapsed_first = time.monotonic() - started
        started = time.monotonic()
        second = self._render(21)
        elapsed_second = time.monotonic() - started
        self.assertGreater(len(first), 10_000)
        self.assertEqual(first, second)  # visual regression: identical bytes
        self.assertLess(elapsed_second, 10.0)
        self.assertLess(elapsed_first, 15.0)


if __name__ == "__main__":
    unittest.main()
