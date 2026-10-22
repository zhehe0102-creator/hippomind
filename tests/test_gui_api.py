"""HippoMind Atlas read API (/api/v1) — contract tests.

The console depends on a stable JSON shape: this file seeds a temporary ledger
through the real ingest path (claims, relations, feedback, pinning included),
serves it from a real daemon thread, and asserts the wire format from the
contract, plus the security rules (admin token only, bounded lists).
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths


def _iso_days_ago(days: int, hour: int = 12) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


class GuiApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hippo-gui-api-")
        root = Path(cls.tmp.name)
        cls.paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        cls.service = MemoryHubService(cls.paths, watch=False)
        cls.server = MemoryHubHTTPServer(("127.0.0.1", 0), cls.service)
        cls.service._server = cls.server
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.token = cls.service.token
        cls.ids = cls._seed()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.service.ledger.close()
        cls.tmp.cleanup()

    @classmethod
    def _seed(cls) -> dict:
        ledger = cls.service.ledger
        ids = {}
        parent = ledger.ingest({
            "content": "任务：把记忆系统 GUI 重构为纸本档案馆风格",
            "kind": "task", "source_agent": "codex", "session_id": "s-1",
            "task_id": "task-atlas", "project": "atlas", "scope": "shared",
            "evidence": ["codex:session-1"], "tags": ["gui", "refactor"],
            "timestamp": _iso_days_ago(8), "importance": 8, "confidence": 0.9,
        })
        ids["parent"] = parent.memory_id
        claim = ledger.ingest({
            "content": "HippoMind 的账本使用 SQLite + WAL",
            "kind": "decision", "source_agent": "codex", "session_id": "s-1",
            "task_id": "task-atlas", "project": "atlas", "scope": "shared",
            "evidence": ["codex:session-1"], "timestamp": _iso_days_ago(7),
            "confidence": 0.8,
            "claims": [{"subject": "HippoMind", "predicate": "uses",
                        "slot_key": "database", "value": "SQLite"}],
        })
        ids["claim"] = claim.memory_id
        conflict = ledger.ingest({
            "content": "另一种说法：HippoMind 的账本使用 Postgres",
            "kind": "decision", "source_agent": "claude", "session_id": "s-2",
            "task_id": "task-atlas", "project": "atlas", "scope": "shared",
            "evidence": ["claude:session-1"], "timestamp": _iso_days_ago(6),
            "confidence": 0.8,
            "claims": [{"subject": "HippoMind", "predicate": "uses",
                        "slot_key": "database", "value": "Postgres"}],
        })
        ids["conflict"] = conflict.memory_id
        child = ledger.ingest({
            "content": "进度：完成纸本设计 token 与信息架构评审",
            "kind": "progress", "source_agent": "codex", "session_id": "s-1",
            "task_id": "task-atlas", "project": "atlas", "scope": "shared",
            "evidence": ["codex:session-1"], "timestamp": _iso_days_ago(5),
            "causal_parents": [{"memory_id": parent.memory_id,
                                "relation_type": "caused_by", "confidence": 0.7}],
        })
        ids["child"] = child.memory_id
        skill = ledger.ingest({
            "content": "技能：排查 FTS 索引漂移时先跑 maintain 再复查 checkup",
            "kind": "procedural", "source_agent": "dsh", "session_id": "s-3",
            "scope": "shared", "evidence": ["dsh:session-1"],
            "timestamp": _iso_days_ago(3), "confidence": 0.82,
            "procedural": {
                "trigger_pattern": ["FTS 漂移", "checkup 报 stale"],
                "preconditions": ["daemon 正在运行"],
                "execution_pipeline": ["运行 hippomind maintain", "立即再跑 hippomind checkup"],
                "verification_assertion": ["drift=False", "stale=0"],
            },
        })
        ids["skill"] = skill.memory_id
        raw = ledger.ingest({
            "content": "用户：今天的对话原文（raw evidence）",
            "kind": "raw_chat", "source_agent": "claude", "session_id": "s-2",
            "role": "user", "scope": "shared", "raw_chat": True,
            "evidence": ["claude:raw:1"], "timestamp": _iso_days_ago(1),
        })
        ids["raw"] = raw.memory_id
        for index in range(4):
            ledger.ingest({
                "content": "批量进度记录 %s：GUI 视图联调 %s" % (index, index),
                "kind": "progress", "source_agent": "gemini" if index % 2 else "pi",
                "session_id": "s-batch", "task_id": "task-atlas", "project": "atlas",
                "scope": "shared", "evidence": ["batch:%s" % index],
                "timestamp": _iso_days_ago(index + 1), "confidence": 0.6,
            })
        ledger.ingest({
            "content": "XSS 内容：<img src=x onerror=window.__XSS=1>",
            "kind": "semantic", "source_agent": "unknown", "scope": "shared",
            "evidence": ["xss:1"], "timestamp": _iso_days_ago(2),
        })
        return ids

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _request(self, path: str, method: str = "GET", body=None,
                 token: str = "", expect: int = 200):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-HippoMind-Token"] = token
        request = urllib.request.Request(self.base + path, data=data,
                                         headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as error:
            payload = error.read()
            self.assertEqual(expect, error.code, payload[:400])
            return json.loads(payload.decode("utf-8"))
        self.assertEqual(expect, response.status)
        return json.loads(response.read().decode("utf-8"))

    def get(self, path: str, token: str = None, expect: int = 200):
        return self._request(path, token=self.token if token is None else token,
                             expect=expect)

    def post(self, path: str, body, token: str = None, expect: int = 200):
        return self._request(path, method="POST", body=body,
                             token=self.token if token is None else token,
                             expect=expect)

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------
    def test_requires_admin_token(self):
        body = self._request("/api/v1/meta", expect=401)
        self.assertIn("error", body)
        body = self._request("/api/v1/meta", token="wrong-token", expect=401)
        self.assertIn("error", body)

    def test_scoped_agent_credential_is_refused(self):
        _credential, raw_token = self.service.ledger.issue_agent_credential(
            "scoped-bot", scopes=["shared"], permissions=["read", "stats"])
        body = self._request("/api/v1/meta", token=raw_token, expect=403)
        self.assertIn("管理员", body["error"])

    # ------------------------------------------------------------------
    # meta / overview
    # ------------------------------------------------------------------
    def test_meta_catalogue(self):
        meta = self.get("/api/v1/meta")
        self.assertEqual("v1", meta["api_version"])
        self.assertEqual(meta["schema_version"], 8)
        self.assertEqual({"memories_page", "content_preview", "detail_content",
                          "graph_nodes"}, set(meta["limits"]))
        paths = {item["path"] for item in meta["endpoints"]}
        self.assertIn("/api/v1/overview", paths)
        self.assertIn("/api/v1/health", paths)
        self.assertEqual("HippoMind Atlas", meta["ui"]["name"])

    def test_overview_shape(self):
        overview = self.get("/api/v1/overview?days=7")
        self.assertEqual(7, len(overview["growth"]["buckets"]))
        self.assertGreaterEqual(overview["ledger"]["memories"], 8)
        self.assertIsInstance(overview["recent"], list)
        self.assertTrue(overview["health_flags"])
        self.assertIn("server", overview)
        self.assertIn("fts", {flag["id"] for flag in overview["health_flags"]})

    # ------------------------------------------------------------------
    # archive
    # ------------------------------------------------------------------
    def test_memories_pagination_and_filters(self):
        page = self.get("/api/v1/memories?limit=2&offset=0")
        self.assertEqual(2, len(page["items"]))
        self.assertGreaterEqual(page["total"], 8)
        second = self.get("/api/v1/memories?limit=2&offset=2")
        self.assertNotEqual(page["items"][0]["memory_id"], second["items"][0]["memory_id"])

        decisions = self.get("/api/v1/memories?kind=decision")
        self.assertTrue(decisions["items"])
        self.assertTrue(all(item["kind"] == "decision" for item in decisions["items"]))

        raw_only = self.get("/api/v1/memories?raw=only")
        self.assertEqual(1, raw_only["total"])
        self.assertTrue(raw_only["items"][0]["raw_chat"])
        default = self.get("/api/v1/memories?limit=100")
        self.assertFalse(any(item["raw_chat"] for item in default["items"]))

        search = self.get("/api/v1/memories?q=" + quote("纸本"))
        self.assertGreaterEqual(search["total"], 1)
        facets = self.get("/api/v1/memories?facets=1&limit=1")
        self.assertTrue(facets["facets"]["kinds"])
        self.assertTrue(facets["facets"]["agents"])
        self.assertIn("active", [item["value"] for item in facets["facets"]["lifecycles"]])

        clamped = self.get("/api/v1/memories?limit=9999&offset=-5")
        self.assertLessEqual(clamped["limit"], 100)
        self.assertEqual(0, clamped["offset"])

    def test_memory_detail_provenance(self):
        detail = self.get("/api/v1/memories/" + self.ids["child"])
        memory = detail["memory"]
        self.assertEqual(self.ids["child"], memory["memory_id"])
        self.assertEqual("progress", memory["kind"])
        self.assertTrue(memory["content"].startswith("进度"))
        self.assertEqual("task-atlas", memory["task_id"])
        self.assertTrue(detail["origin_event"]["event_id"])
        self.assertTrue(detail["origin_event"]["content_preview"])
        self.assertEqual(1, len(detail["relations"]["parents"]))
        self.assertEqual(self.ids["parent"],
                         detail["relations"]["parents"][0]["memory"]["memory_id"])
        self.assertEqual("caused_by", detail["relations"]["parents"][0]["relation_type"])

        claimed = self.get("/api/v1/memories/" + self.ids["claim"])
        self.assertEqual(1, len(claimed["claims"]))
        self.assertEqual("HippoMind", claimed["claims"][0]["subject"])

    def test_memory_detail_404(self):
        self.get("/api/v1/memories/mem_does-not-exist", expect=404)

    # ------------------------------------------------------------------
    # timeline / knowledge
    # ------------------------------------------------------------------
    def test_timeline_days_and_filter(self):
        timeline = self.get("/api/v1/timeline?days=30")
        self.assertEqual(30, len(timeline["buckets"]))
        self.assertGreaterEqual(timeline["totals"]["memories"], 8)
        self.assertTrue(timeline["agents"])
        filtered = self.get("/api/v1/timeline?days=30&agent=gemini")
        self.assertTrue(all(item["agent"] == "gemini" for item in filtered["agents"]))
        weekly = self.get("/api/v1/timeline?days=30&bucket=week")
        self.assertLessEqual(len(weekly["buckets"]), 30)
        self.assertTrue(weekly["buckets"])

    def test_claims_conflicts_facts_skills_subjects(self):
        claims = self.get("/api/v1/claims?limit=50")
        self.assertGreaterEqual(claims["counts"]["total"], 2)
        self.assertIn("conflict", claims["counts"])
        self.assertTrue(claims["items"])
        self.assertTrue(any(item.get("memory_preview") for item in claims["items"]))

        conflicts = self.get("/api/v1/conflicts")
        self.assertTrue(conflicts["items"])
        self.assertLessEqual(len(conflicts["items"][0]["content"]), 120)

        facts = self.get("/api/v1/facts")
        self.assertIsInstance(facts["items"], list)
        skills = self.get("/api/v1/skills")
        self.assertTrue(any(item["memory_id"] == self.ids["skill"] for item in skills["items"]))
        skill = next(item for item in skills["items"] if item["memory_id"] == self.ids["skill"])
        self.assertTrue(skill["trigger_pattern"])
        self.assertTrue(skill["execution_pipeline"])

        subjects = self.get("/api/v1/subjects")
        self.assertIn("HippoMind", [item["subject"] for item in subjects["items"]])

    def test_graph_endpoint(self):
        graph = self.get("/api/v1/graph?subject=HippoMind&hops=1")
        self.assertTrue(graph["found"])
        self.assertTrue(graph["nodes"])
        self.assertIn("HippoMind", [node["subject"] for node in graph["nodes"]])
        self.get("/api/v1/graph", expect=400)

    # ------------------------------------------------------------------
    # agents / health
    # ------------------------------------------------------------------
    def test_agents_endpoint(self):
        agents = self.get("/api/v1/agents")
        names = {item["agent"] for item in agents["agents"]}
        self.assertTrue({"codex", "claude", "gemini"}.issubset(names))
        codex = next(item for item in agents["agents"] if item["agent"] == "codex")
        self.assertGreaterEqual(codex["memories"], 2)
        self.assertGreaterEqual(codex["events"], 1)
        self.assertIn("trust", codex)
        self.assertIn("cursors", agents)
        self.assertIn("discovery", agents)
        self.assertIn("watcher", agents)
        self.assertIn("injection", agents)

    def test_health_endpoint(self):
        health = self.get("/api/v1/health")
        ids = {item["id"] for item in health["checks"]}
        self.assertTrue({"ledger", "daemon", "fts", "claims", "storage"}.issubset(ids))
        self.assertIn("storage", health)
        self.assertIn("overview", health)
        self.assertFalse(health["integrity"]["ran"])
        deep = self.get("/api/v1/health?integrity=1")
        self.assertTrue(deep["integrity"]["ran"])
        self.assertIn(deep["integrity"]["ok"], (True, False))
        self.assertIsNotNone(deep["integrity"]["elapsed_ms"])

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def test_pin_and_feedback(self):
        pinned = self.post("/api/v1/pin", {"memory_id": self.ids["parent"], "pinned": True})
        self.assertTrue(pinned["pinned"])
        memories = self.get("/api/v1/memories?q=" + quote("纸本档案馆"))
        target = next(item for item in memories["items"]
                      if item["memory_id"] == self.ids["parent"])
        self.assertTrue(target["pinned"])
        unpinned = self.post("/api/v1/pin", {"memory_id": self.ids["parent"], "pinned": False})
        self.assertFalse(unpinned["pinned"])
        self.post("/api/v1/pin", {"memory_id": "mem_missing"}, expect=400)

        feedback = self.post("/api/v1/feedback",
                             {"memory_id": self.ids["child"], "signal": 1, "note": "yes"})
        self.assertTrue(feedback["updated"])
        detail = self.get("/api/v1/memories/" + self.ids["child"])
        self.assertEqual(1, len(detail["feedback"]))
        self.assertEqual(1.0, detail["feedback"][0]["signal"])

    # ------------------------------------------------------------------
    # UI bootstrap nonce
    # ------------------------------------------------------------------
    def test_ui_token_flow_is_single_use(self):
        minted = self.post("/api/v1/ui-token", {})
        self.assertTrue(minted["nonce"])
        self.assertIn("/ui#", minted["url"])
        exchanged = self._request("/api/v1/ui-token/exchange", method="POST",
                                  body={"nonce": minted["nonce"]}, expect=200)
        self.assertEqual(self.token, exchanged["token"])
        self._request("/api/v1/ui-token/exchange", method="POST",
                      body={"nonce": minted["nonce"]}, expect=401)
        self._request("/api/v1/ui-token/exchange", method="POST",
                      body={"nonce": "not-a-real-nonce"}, expect=401)

    def test_unknown_endpoint(self):
        body = self.get("/api/v1/nope", expect=404)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
