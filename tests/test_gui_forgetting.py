"""遗忘审计 GUI 视图（GET /api/v1/forgetting + js/views/forgetting.js）。

这个视图的价值不在「又一个统计仪表盘」，而在把 hippomind/forgetting.py 开头那三条
拒绝放弃的性质带到界面上。所以测试分两层：

API 层（真 daemon 线程 + 真账本）
    * 输出必须与 forgetting_report() 逐键一致——控制台和 CLI 不能是两种口径；
    * 只读：调用前后账本文件字节与写代数都不变；
    * days/limit 被夹紧，缓存按 (账本, 代数, 参数) 命中且 TTL 会过期；
    * 账本不存在时是 503，不是 500 也不是空报告。

视图层
    * 静态契约：常量与 Python 侧逐个对齐（GONE_KIND / COVERAGE_STATE_LABELS /
      ARCHIVE_REASONS / AGE_FIELD_BY_REASON），视图不引入未登记的端点；
    * 行为契约：principleRows() 在 node 里用真实报告形状跑一遍。三条性质必须是
      **从数字推出来的**——同一段文案配不同的数字要给出不同的结论，否则它就退化成
      一句固定免责声明，那正是这个视图最该避免的失败方式。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import forgetting  # noqa: E402
from hippomind.daemon import (  # noqa: E402
    MemoryHubHTTPServer, MemoryHubService, RuntimePaths,
)
from hippomind.gui_api import (  # noqa: E402
    ENDPOINTS, FORGETTING_CACHE_TTL_SECONDS, FORGETTING_MAX_DAYS, GuiApi,
    GuiApiError, MAX_LIST_LIMIT,
)
from hippomind.ledger import SQLiteLedger  # noqa: E402

NODE = shutil.which("node")
WEB = ROOT / "hippomind" / "web"
VIEW_PATH = WEB / "js" / "views" / "forgetting.js"

_SEQ = [0]


def _iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _ingest(ledger, content, **extra):
    _SEQ[0] += 1
    record = {"content": content, "kind": "fact", "source_agent": "claude",
              "scope": "shared", "evidence": ["ev:%d" % _SEQ[0]]}
    record.update(extra)
    return ledger.ingest(record)


def _write_decay(ledger, memory_id, *, reason="weight_floor",
                 occurred_at="", from_lifecycle="active", to_lifecycle="cold",
                 weight_before=0.5, weight_after=0.4, age_days=12.5,
                 row_age_days=30.0, utility=0.3, importance=6,
                 raw_metadata=None):
    """Append one decay event exactly as lifecycle._append_decay_event writes it.

    The field names are the reading contract of hippomind/forgetting.py; if the
    writer renames one, the audit degrades to "未标注/契约外原因" and this test
    is where that shows up.
    """
    _SEQ[0] += 1
    occurred = occurred_at or _iso_ago(1)
    payload = {"memory_id": memory_id, "from_lifecycle": from_lifecycle,
               "to_lifecycle": to_lifecycle, "reason": reason,
               "weight_before": weight_before, "weight_after": weight_after,
               "decay_per_day": 0.008, "age_days": age_days,
               "row_age_days": row_age_days, "utility": utility,
               "importance": importance}
    raw = raw_metadata if raw_metadata is not None else json.dumps(
        payload, ensure_ascii=False)
    event_id = "evt_decay_gui_%d" % _SEQ[0]

    def write(conn):
        conn.execute(
            "INSERT INTO ledger_events(event_id,event_type,occurred_at,ingested_at,"
            "source_agent,session_id,task_id,project,role,scope,evidence_json,"
            "content,content_hash,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "decay", occurred, occurred, "system", "", "", "", "",
             "system", "[]", "", "hash_" + event_id, raw))

    ledger._transaction(write)
    return event_id


def _cold(ledger, memory_id, *, quality_state="accepted", raw_chat=0):
    """Mirror what lifecycle.maintain does: move lifecycle, drop the FTS row."""
    def write(conn):
        conn.execute(
            "UPDATE memories SET lifecycle='cold', quality_state=?, raw_chat=? "
            "WHERE memory_id=?", (quality_state, raw_chat, memory_id))
        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
    ledger._transaction(write)


def media_blocks(css):
    """{max_width_px: block_text} for every one-level @media in a stylesheet.

    A flat regex cannot see inside these blocks: the first nested rule closes
    a brace, so "[^}]*" stops before the second selector.  Counting braces is
    the smallest thing that actually answers "is this rule in that query".
    """
    blocks = {}
    for match in re.finditer(r"@media \(max-width: (\d+)px\)\s*\{", css):
        depth = 1
        cursor = match.end()
        while cursor < len(css) and depth > 0:
            if css[cursor] == "{":
                depth += 1
            elif css[cursor] == "}":
                depth -= 1
            cursor += 1
        blocks[int(match.group(1))] = blocks.get(int(match.group(1)), "") + \
            css[match.end():cursor]
    return blocks


def _api_for(ledger, *, prefetch=False) -> GuiApi:
    """A GuiApi over any ledger: the endpoint only needs .path and .generation.

    prefetch defaults to False here.  The daemon warms the default window on a
    background thread at construction; in a unit test that would race the very
    assertion about cache state, so the prefetch test asks for it explicitly.
    """
    return GuiApi(SimpleNamespace(ledger=ledger), prefetch=prefetch)


def _seed_ledger(ledger: SQLiteLedger) -> dict:
    """Three explained cold rows, one zero-drop eviction, one malformed event,
    one event whose memory is gone, and unexplained cold rows left over -- i.e.
    the 'partial' state the view has to render honestly."""
    ids = {}
    ids["a"] = _ingest(ledger, "冷置样本 A：被指数衰减压到阈值以下").memory_id
    ids["b"] = _ingest(ledger, "冷置样本 B：同样因权重跌破下限").memory_id
    ids["evicted"] = _ingest(ledger, "冷置样本 C：活跃容量超限被挤出").memory_id
    _ingest(ledger, "从未有事件的 cold 行 1")
    _ingest(ledger, "从未有事件的 cold 行 2")
    for memory_id in (ids["a"], ids["b"], ids["evicted"]):
        _cold(ledger, memory_id)
    with ledger._reading() as conn:
        active = [str(row["memory_id"]) for row in conn.execute(
            "SELECT memory_id FROM memories WHERE lifecycle='active'")]
    for memory_id in active:
        _cold(ledger, memory_id)

    _write_decay(ledger, ids["a"], reason="weight_floor",
                 weight_before=0.9, weight_after=0.5, age_days=18.0)
    _write_decay(ledger, ids["b"], reason="weight_floor",
                 weight_before=0.6, weight_after=0.35, age_days=9.0)
    # Zero weight drop: evicted by capacity, then archived for age.  The age
    # rule is the decisive one, so the view must report row_age_days.
    _write_decay(ledger, ids["evicted"], reason="capacity_overflow",
                 weight_before=0.3, weight_after=0.3, age_days=0.0,
                 row_age_days=200.0)
    _write_decay(ledger, ids["evicted"], reason="age_limit",
                 weight_before=0.3, weight_after=0.3, age_days=0.0,
                 row_age_days=200.0)
    # A row the projection no longer holds: only the audit event survived.
    _write_decay(ledger, "mem_deleted_long_ago", reason="weight_floor",
                 weight_before=0.4, weight_after=0.2)
    # Evidence nobody can parse: it must stay in the denominator.
    _write_decay(ledger, ids["a"], reason="weight_floor",
                 raw_metadata="{not json at all")
    return ids


class GuiForgettingApiTests(unittest.TestCase):
    """Wire contract, exercised through a real daemon thread."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hippo-gui-forgetting-")
        root = Path(cls.tmp.name)
        cls.paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        cls.service = MemoryHubService(cls.paths, watch=False)
        cls.ledger = cls.service.ledger
        cls.ids = _seed_ledger(cls.ledger)
        cls.server = MemoryHubHTTPServer(("127.0.0.1", 0), cls.service)
        cls.service._server = cls.server
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.token = cls.service.token

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.service.ledger.close()
        cls.tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def _request(self, path, token="", expect=200):
        headers = {"Accept": "application/json"}
        if token:
            headers["X-HippoMind-Token"] = token
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            payload = error.read()
            self.assertEqual(expect, error.code, payload[:400])
            return json.loads(payload.decode("utf-8"))
        self.assertEqual(expect, response.status)
        return json.loads(response.read().decode("utf-8"))

    def get(self, path, expect=200):
        return self._request(path, token=self.token, expect=expect)

    def _direct_report(self, **kwargs):
        """The CLI's own call, for a key-for-key comparison."""
        with forgetting.ReadOnlyLedger(self.ledger.path) as reader:
            return forgetting.forgetting_report(reader, **kwargs)

    def _table_names(self):
        with self.ledger._reading() as conn:
            return [str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    # -- shape -------------------------------------------------------------

    def test_endpoint_is_registered_in_the_catalogue(self):
        paths = {item["path"] for item in ENDPOINTS}
        self.assertIn("/api/v1/forgetting", paths)
        entry = [item for item in ENDPOINTS if item["path"] == "/api/v1/forgetting"][0]
        self.assertEqual("GET", entry["method"])
        self.assertEqual("admin", entry["auth"])
        meta = self.get("/api/v1/meta")
        self.assertIn("/api/v1/forgetting", {item["path"] for item in meta["endpoints"]})

    def test_requires_admin_token(self):
        self._request("/api/v1/forgetting", expect=401)

    def test_response_is_the_forgetting_report_verbatim(self):
        """The console and scripts/forgetting_report.py --json must agree.

        If the view reshaped the report, an operator cross-checking the screen
        against the CLI would be comparing two different definitions of
        "explained" -- so this asserts key-for-key equality, not a subset.
        """
        body = self.get("/api/v1/forgetting?days=7&limit=20")
        expected = self._direct_report(days=7, limit=20)
        self.assertEqual(set(expected) | {"cache"}, set(body))
        # generated_at and window are the report's own clock readings, taken at
        # a different instant by each call; comparing them would be comparing
        # two timestamps, not two documents.  Their SHAPE is checked instead.
        volatile = {"generated_at", "window"}
        for key in expected:
            with self.subTest(key=key):
                if key in volatile:
                    continue
                self.assertEqual(expected[key], body[key],
                                 "端点改写了 forgetting_report() 的 %s" % key)
        self.assertEqual({"hit", "age_s", "ttl_s", "elapsed_ms"}, set(body["cache"]))

        start = datetime.fromisoformat(body["window"]["start"])
        end = datetime.fromisoformat(body["window"]["end"])
        self.assertEqual(7, (end - start).days)
        self.assertLess(abs((datetime.now(timezone.utc) - end).total_seconds()), 120,
                        "window.end 不是「现在」：报告窗口算错了")
        self.assertLess(abs((datetime.now(timezone.utc)
                             - datetime.fromisoformat(body["generated_at"])).total_seconds()),
                        120)

    def test_partial_coverage_is_reported_as_partial(self):
        body = self.get("/api/v1/forgetting?days=7&limit=20")
        coverage = body["coverage"]
        self.assertEqual("partial", coverage["state"])
        self.assertGreater(coverage["cold_total_now"], 0)
        self.assertGreater(coverage["explained_cold"], 0)
        self.assertGreater(coverage["unexplained_cold"], 0)
        self.assertEqual("部分可解释", body["verdict"]["coverage_label"])
        self.assertTrue(body["verdict"]["explainable"])

    def test_malformed_evidence_stays_in_the_denominator(self):
        body = self.get("/api/v1/forgetting?days=7")
        self.assertEqual(1, body["events"]["malformed"])
        self.assertTrue(any("无法解析" in caveat for caveat in body["verdict"]["caveats"]))

    def test_zero_drop_rows_are_counted_apart_from_decay(self):
        body = self.get("/api/v1/forgetting?days=7")
        self.assertEqual(1, body["unchanged_weight"])
        drops = [item["drop"] for item in body["top_drops"]]
        self.assertIn(0.0, drops, "容量挤出的行必须出现且降幅为 0，而不是被当成衰减")

    def test_deleted_memory_is_marked_and_not_linked(self):
        body = self.get("/api/v1/forgetting?days=7&limit=50")
        gone = [item for item in body["top_drops"] if item["memory_id"] == "mem_deleted_long_ago"]
        self.assertTrue(gone, "已删除记忆的审计事件必须仍然可见")
        self.assertEqual(forgetting.GONE_KIND, gone[0]["kind"])
        self.assertEqual(1, body["recoverability"]["gone"])

    def test_recoverability_is_conditional_on_cold_rows(self):
        body = self.get("/api/v1/forgetting?days=7&limit=50")
        recall = body["recoverability"]
        self.assertGreater(recall["predicate_reachable"], 0)
        self.assertEqual(recall["predicate_reachable"], recall["cold_now"],
                         "这些行都已 cold：无 SQL 阻断的必然就是条件性可达的那些")
        self.assertTrue(body["verdict"]["recall_is_conditional"])

    def test_days_and_limit_are_clamped(self):
        self.assertEqual(1, self.get("/api/v1/forgetting?days=0")["window_days"])
        self.assertEqual(1, self.get("/api/v1/forgetting?days=-5")["window_days"])
        self.assertEqual(FORGETTING_MAX_DAYS,
                         self.get("/api/v1/forgetting?days=99999")["window_days"])
        self.assertEqual(1, self.get("/api/v1/forgetting?limit=0")["limit"])
        self.assertEqual(MAX_LIST_LIMIT,
                         self.get("/api/v1/forgetting?limit=99999")["limit"])
        default = self.get("/api/v1/forgetting")
        self.assertEqual(forgetting.DEFAULT_DAYS, default["window_days"])
        self.assertEqual(forgetting.DEFAULT_LIMIT, default["limit"])

    def test_include_raw_chat_reaches_the_recall_predicate(self):
        """A hardcoded False here made the flag silently do nothing."""
        plain = self.get("/api/v1/forgetting?days=7")
        raw = self.get("/api/v1/forgetting?days=7&include_raw_chat=1")
        self.assertFalse(plain["recoverability"]["include_raw_chat"])
        self.assertTrue(raw["recoverability"]["include_raw_chat"])

    def test_report_does_not_write_to_the_ledger(self):
        """Measuring production must never mutate it (mode=ro + query_only)."""
        path = Path(self.ledger.path)
        before = path.read_bytes()
        generation = self.ledger.generation
        tables_before = self._table_names()
        self.get("/api/v1/forgetting?days=30&limit=50")
        self.assertEqual(before, path.read_bytes(), "报告改写了账本文件")
        self.assertEqual(generation, self.ledger.generation, "报告开启了写事务")
        self.assertEqual(tables_before, self._table_names(), "报告新建了表")


class GuiForgettingEmptyTests(unittest.TestCase):
    """The state the view exists for: cold rows present, zero events."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-gui-forgetting-empty-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        for index in range(3):
            record = _ingest(self.ledger, "从未被解释的 cold 行 %d" % index)
            _cold(self.ledger, record.memory_id)
        self.api = _api_for(self.ledger)

    def test_zero_events_is_reported_as_none_not_as_a_clean_zero(self):
        report = self.api.forgetting({})
        self.assertEqual(0, report["events"]["total"])
        self.assertEqual(3, report["coverage"]["cold_total_now"])
        self.assertEqual(0, report["coverage"]["explained_cold"])
        self.assertEqual(3, report["coverage"]["unexplained_cold"])
        self.assertEqual("none", report["coverage"]["state"])
        self.assertEqual("无法解释", report["verdict"]["coverage_label"])
        self.assertFalse(report["verdict"]["explainable"])
        self.assertIn("却没有留下原因", report["verdict"]["headline"])

    def test_empty_ledger_says_nothing_to_explain(self):
        empty = SQLiteLedger(Path(self.tmp.name) / "empty.sqlite")
        self.addCleanup(empty.close)
        report = _api_for(empty).forgetting({})
        self.assertEqual("empty", report["coverage"]["state"])
        self.assertEqual("无事可解释", report["verdict"]["coverage_label"])

    def test_missing_ledger_is_503_not_an_empty_report(self):
        api = _api_for(SimpleNamespace(path=Path(self.tmp.name) / "nope.sqlite",
                                       generation=0))
        with self.assertRaises(GuiApiError) as caught:
            api.forgetting({})
        self.assertEqual(503, caught.exception.status)


class GuiForgettingCacheTests(unittest.TestCase):
    """The memo exists because a full COUNT on production is seconds, not ms."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-gui-forgetting-cache-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        record = _ingest(self.ledger, "缓存测试用记忆")
        _cold(self.ledger, record.memory_id)
        _write_decay(self.ledger, record.memory_id)
        self.api = _api_for(self.ledger)

    def test_second_identical_request_hits_the_cache(self):
        first = self.api.forgetting({})
        self.assertFalse(first["cache"]["hit"])
        self.assertGreaterEqual(first["cache"]["elapsed_ms"], 0.0)
        second = self.api.forgetting({})
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(0.0, second["cache"]["elapsed_ms"])
        self.assertEqual(first["coverage"], second["coverage"])

    def test_different_parameters_do_not_share_an_entry(self):
        self.api.forgetting({})
        other = self.api.forgetting({"days": ["30"]})
        self.assertFalse(other["cache"]["hit"])
        self.assertEqual(30, other["window_days"])

    def test_a_write_in_this_process_invalidates_the_entry(self):
        self.api.forgetting({})
        _write_decay(self.ledger, "mem_after_cache", reason="expired")
        after = self.api.forgetting({})
        self.assertFalse(after["cache"]["hit"],
                         "generation 变了却仍然命中缓存：报告会端出写之前的账本")

    def test_the_entry_expires_on_the_ttl(self):
        """generation cannot see another process's write (a CLI maintain run)."""
        import hippomind.gui_api as gui_api
        self.api.forgetting({})
        self.assertTrue(self.api.forgetting({})["cache"]["hit"])
        original = gui_api.FORGETTING_CACHE_TTL_SECONDS
        gui_api.FORGETTING_CACHE_TTL_SECONDS = -1.0
        try:
            expired = self.api.forgetting({})
        finally:
            gui_api.FORGETTING_CACHE_TTL_SECONDS = original
        self.assertFalse(expired["cache"]["hit"], "TTL 到期后必须重新测量")
        self.assertEqual(FORGETTING_CACHE_TTL_SECONDS, original)

    def test_cache_is_bounded(self):
        import hippomind.gui_api as gui_api
        original = gui_api.FORGETTING_CACHE_MAX
        gui_api.FORGETTING_CACHE_MAX = 2
        try:
            for days in ("1", "2", "3", "4"):
                self.api.forgetting({"days": [days]})
            self.assertLessEqual(len(self.api._forgetting_cache), 2)
        finally:
            gui_api.FORGETTING_CACHE_MAX = original

    def test_construction_starts_no_background_work(self):
        """Building a GuiApi must not open a second ledger handle.

        Regression this pins: the warm-up used to start in __init__.  It opens
        the ledger read-only, SQLite creates hub.sqlite-shm, and a caller that
        removes its directory right after constructing a service then fails in
        rmtree with ENOTEMPTY (tests/test_mcp_register.py).  The default is
        therefore off, and only a long-lived owner opts in.
        """
        api = _api_for(self.ledger)
        self.assertIsNone(api._prefetch_thread,
                          "构造 GuiApi 就启动了预热线程：它会在 teardown 时留下 -shm")

    def test_prefetch_warms_the_default_window(self):
        """Opt-in warm-up: the first paint then finds a cache hit.

        The scan it hides is the unindexed all-time decay COUNT: measured at
        0.7-4.6 s with a cold page cache on the 881 MiB production ledger.
        """
        api = _api_for(self.ledger, prefetch=True)
        deadline = time.monotonic() + 10.0
        warmed = None
        while time.monotonic() < deadline:
            warmed = api.forgetting({})
            if warmed["cache"]["hit"]:
                break
            time.sleep(0.02)
        self.assertTrue(warmed["cache"]["hit"],
                        "预取没有把默认窗口算好：首屏仍要自己扫全表")
        self.assertEqual(forgetting.DEFAULT_DAYS, warmed["window_days"])

    def test_prefetch_failure_is_contained(self):
        """A broken ledger must not turn the warm-up into a crash."""
        api = _api_for(SimpleNamespace(path=Path(self.tmp.name) / "nope.sqlite",
                                       generation=0), prefetch=True)
        time.sleep(0.2)
        with self.assertRaises(GuiApiError) as caught:
            api.forgetting({})
        self.assertEqual(503, caught.exception.status)

    def test_close_joins_the_prefetch_so_its_handle_is_gone(self):
        """close() is what makes "this service is finished" true.

        Without it the read-only handle outlives the caller and recreates
        hub.sqlite-shm during teardown.
        """
        api = _api_for(self.ledger, prefetch=True)
        api.close()
        self.assertIsNone(api._prefetch_thread)
        # Idempotent: a second close is a no-op, not an error.
        api.close()
        # And the endpoint still works afterwards (the handle was per-call).
        self.assertEqual(forgetting.DEFAULT_DAYS, api.forgetting({})["window_days"])

    def test_single_flight_dedupes_concurrent_builds(self):
        """A prefetch racing a real request must not scan the window twice."""
        import hippomind.gui_api as gui_api
        calls = []
        original = gui_api.forgetting_report

        def counting(*args, **kwargs):
            calls.append(1)
            time.sleep(0.05)  # widen the race window on purpose
            return original(*args, **kwargs)

        gui_api.forgetting_report = counting
        try:
            results = []
            threads = [threading.Thread(
                target=lambda: results.append(self.api.forgetting({})))
                for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            gui_api.forgetting_report = original
        self.assertEqual(5, len(results))
        self.assertEqual(1, len(calls),
                         "5 个并发请求触发了 %d 次扫描：单飞没生效" % len(calls))
        self.assertEqual(1, len({json.dumps(item["coverage"], sort_keys=True)
                                 for item in results}))

    def test_concurrent_requests_are_consistent(self):
        """ThreadingHTTPServer means two tabs can arrive at once."""
        results = []
        errors = []

        def call():
            try:
                results.append(self.api.forgetting({"days": ["7"]}))
            except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual(6, len(results))
        self.assertEqual(1, len({json.dumps(item["coverage"], sort_keys=True)
                                 for item in results}))


class GuiForgettingStaticTests(unittest.TestCase):
    """Static contract: the browser copy of every Python constant stays equal."""

    @classmethod
    def setUpClass(cls):
        cls.text = VIEW_PATH.read_text(encoding="utf-8")

    def test_view_module_exists_and_has_the_contract_shape(self):
        self.assertTrue(VIEW_PATH.is_file(), "缺少 js/views/forgetting.js")
        self.assertRegex(self.text, r"export\s+const\s+meta\b")
        self.assertRegex(self.text, r"export\s+async\s+function\s+render\b")
        self.assertRegex(self.text, r"""\bid:\s*["']forgetting["']""")
        self.assertRegex(self.text, r"""\bindex:\s*["']11["']""")
        self.assertRegex(self.text, r"""\btitle:\s*["']遗忘["']""")
        self.assertRegex(self.text, r"""\beyebrow:\s*["']FORGETTING["']""")

    def test_view_calls_only_the_registered_endpoint(self):
        called = set(re.findall(r"""["'\x60](/api/v1/[A-Za-z0-9_\-/]*)""", self.text))
        self.assertEqual({"/api/v1/forgetting"}, called,
                         "视图调用了未登记的端点: %s" % sorted(called))

    def test_gone_kind_matches_the_python_placeholder(self):
        """A renamed placeholder would turn a deleted memory into a 404 drawer."""
        self.assertIn('"%s"' % forgetting.GONE_KIND, self.text,
                      "视图里的 GONE_KIND 与 forgetting.GONE_KIND 不一致")

    def test_coverage_labels_match_the_python_labels(self):
        for state, label in forgetting.COVERAGE_STATE_LABELS.items():
            with self.subTest(state=state):
                self.assertRegex(
                    self.text, r"""\b%s:\s*["']%s["']""" % (state, re.escape(label)),
                    "视图的覆盖状态文案与 forgetting.COVERAGE_STATE_LABELS 不一致: %s" % state)

    def test_age_field_mapping_matches_python(self):
        """Printing the wrong age makes the report contradict itself."""
        for reason, field in forgetting.AGE_FIELD_BY_REASON.items():
            with self.subTest(reason=reason):
                self.assertRegex(
                    self.text, r"""\b%s:\s*["']%s["']""" % (reason, re.escape(field)),
                    "视图的 AGE_FIELD_BY_REASON 与 Python 不一致: %s" % reason)

    def test_archive_reasons_match_python(self):
        for reason in forgetting.ARCHIVE_REASONS:
            with self.subTest(reason=reason):
                self.assertRegex(self.text, r"""["']%s["']""" % re.escape(reason))

    def test_the_three_principles_are_rendered_as_derived_rows(self):
        for anchor in ("zero-is-not-zero", "recall-is-conditional",
                       "zero-drop-is-eviction"):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, self.text, "视图缺少性质行 %s" % anchor)
        self.assertIn("export function principleRows", self.text,
                      "principleRows 必须导出，否则行为测试无法驱动它")

    def test_view_never_builds_markup_from_data(self):
        for pattern, label in (
                (r"\.innerHTML\s*=", "innerHTML 赋值"),
                (r"insertAdjacentHTML\s*\(", "insertAdjacentHTML"),
                (r"\bouterHTML\s*=", "outerHTML 赋值"),
                (r"(?<![\w$])eval\s*\(", "eval()"),
                (r"new\s+Function\s*\(", "new Function()"),
        ):
            with self.subTest(label=label):
                self.assertNotRegex(self.text, pattern,
                                    "forgetting.js 使用了 %s（契约 §6.1）" % label)

    def test_view_does_not_invent_a_second_wording_for_the_verdict(self):
        """The verdict must be daemon text, not a locally composed sentence."""
        self.assertIn("verdict.headline", self.text)
        self.assertIn("verdict.caveats", self.text)
        self.assertIn("coverageLabel", self.text)

    def test_css_hooks_exist_and_use_tokens_only(self):
        css = (WEB / "css" / "views-b.css").read_text(encoding="utf-8")
        for hook in (".vb-fg-verdict", ".vb-fg-principle", ".vb-fg-toolbar",
                     ".vb-fg-coverage", ".vb-fg-drops", ".vb-fg-recall",
                     ".vb-fg-dist", ".vb-fg-cmd"):
            with self.subTest(hook=hook):
                self.assertIn(hook, css, "css/views-b.css 缺少遗忘视图样式钩子 %s" % hook)
        for match in re.finditer(r"\.vb-fg[a-z0-9-]*[^{]*\{([^}]*)\}", css):
            block = match.group(1)
            with self.subTest(block=block.strip()[:60]):
                self.assertNotRegex(
                    block, r"#[0-9a-fA-F]{3,8}\b",
                    "遗忘视图样式硬编码了颜色；必须使用 tokens.css 的变量")

    def test_css_has_narrow_viewport_rules(self):
        """Every multi-column grid in this view must collapse on a phone."""
        css = (WEB / "css" / "views-b.css").read_text(encoding="utf-8")
        blocks = media_blocks(css)
        for width, hooks in ((760, (".vb-fg-verdict-side", ".vb-fg-toolbar-main")),
                             (900, (".vb-fg-dist", ".vb-fg-coverage-meters")),
                             (1200, (".vb-fg-verdict",))):
            block = blocks.get(width, "")
            for hook in hooks:
                with self.subTest(width=width, hook=hook):
                    self.assertIn(hook, block,
                                  "@media max-width:%d 缺少 %s 的窄屏规则" % (width, hook))
        self.assertRegex(blocks.get(760, ""),
                         r"\.vb-fg-verdict-side\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)",
                         "窄屏下结论卡必须折成单列，否则手机上会挤成一团")

    def test_index_nav_skeleton_covers_every_view(self):
        """The skeleton row count is the only place index.html knows the count."""
        html = (WEB / "index.html").read_text(encoding="utf-8")
        rows = html.count('class="atlas-skeleton nav-skeleton-row"')
        registry = (WEB / "js" / "views" / "registry.js").read_text(encoding="utf-8")
        views = len(re.findall(r"""\bid:\s*["'][a-z_]+["']""", registry))
        self.assertEqual(views, rows,
                         "index.html 导航骨架 %d 行，registry 有 %d 个视图" % (rows, views))


NODE_HARNESS = """
import { readFileSync } from "node:fs";

const view = await import(process.argv[2]);
const cases = JSON.parse(readFileSync(process.argv[3], "utf8"));
const out = { principleRows: {}, decisiveAge: {}, coverage: {}, fmt: {} };

for (const [name, report] of Object.entries(cases.principleRows || {})) {
  out.principleRows[name] = view.principleRows(report).map((row) => ({
    id: row.id, tone: row.tone, title: row.title,
    evidence: row.evidence, reading: row.reading,
  }));
}
for (const [name, row] of Object.entries(cases.decisiveAge || {})) {
  out.decisiveAge[name] = view.decisiveAge(row);
}
for (const state of cases.coverageStates || []) {
  out.coverage[state] = { tone: view.coverageTone(state), label: view.coverageLabel(state) };
}
for (const value of cases.dropValues || []) {
  out.fmt[String(value)] = view.fmtDrop(value);
}
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node 不在 PATH；跳过视图行为检查")
class GuiForgettingBehaviorTests(unittest.TestCase):
    """Drive principleRows() with real report shapes.

    The point of these cases: the three properties must be DERIVED.  If the view
    printed one fixed paragraph, every case below would come back with the same
    tone -- and a static disclaimer is exactly the failure mode this view was
    built to avoid.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hippo-gui-forgetting-node-")
        root = Path(cls.tmp.name)
        harness = root / "harness.mjs"
        harness.write_text(NODE_HARNESS, encoding="utf-8")
        payload = root / "cases.json"
        payload.write_text(json.dumps(cls._cases(), ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [NODE, str(harness), VIEW_PATH.as_uri(), str(payload)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError("node harness failed:\n%s" % (result.stderr[-2000:],))
        cls.out = json.loads(result.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def _report(**overrides):
        base = {
            "events": {"total": 0, "malformed": 0, "truncated": False,
                       "scanned": 0, "memories": 0, "budget": 5000},
            "coverage": {"cold_total_now": 0, "explained_cold": 0,
                         "unexplained_cold": 0, "decay_events_all_time": 0,
                         "decay_events_available": False, "significant_drop": 0.01},
            "recoverability": {"memories": 0, "predicate_reachable": 0, "cold_now": 0,
                               "needs_raw_flag": 0, "blocked": 0, "gone": 0,
                               "blockers": {}, "examples": []},
            "unchanged_weight": 0,
        }
        for key, value in overrides.items():
            if isinstance(value, dict):
                base[key] = dict(base[key], **value)
            else:
                base[key] = value
        return base

    @classmethod
    def _cases(cls):
        return {
            "principleRows": {
                # The production shape today: 255k cold rows, zero events.
                "unexplained_population": cls._report(
                    coverage={"cold_total_now": 255817, "explained_cold": 0,
                              "unexplained_cold": 255817},
                    events={"total": 0, "malformed": 0},
                ),
                # Nothing archived at all: genuinely nothing to explain.
                "empty_ledger": cls._report(),
                # Events exist but none maps onto a current cold row.
                "window_misses_population": cls._report(
                    coverage={"cold_total_now": 40, "explained_cold": 0,
                              "unexplained_cold": 40},
                    events={"total": 12, "malformed": 0, "memories": 9},
                ),
                # Honest partial coverage.
                "partial": cls._report(
                    coverage={"cold_total_now": 100, "explained_cold": 3,
                              "unexplained_cold": 97},
                    events={"total": 5, "malformed": 1, "memories": 3},
                    recoverability={"memories": 3, "predicate_reachable": 3,
                                    "cold_now": 3},
                ),
                # Every cold row explained.
                "complete": cls._report(
                    coverage={"cold_total_now": 7, "explained_cold": 7,
                              "unexplained_cold": 0},
                    events={"total": 9, "malformed": 0, "memories": 7},
                    recoverability={"memories": 7, "predicate_reachable": 7,
                                    "cold_now": 7},
                ),
                # Reachable rows that are still active: recall is not conditional.
                "all_active": cls._report(
                    coverage={"cold_total_now": 0, "explained_cold": 0,
                              "unexplained_cold": 0},
                    events={"total": 3, "malformed": 0, "memories": 3},
                    recoverability={"memories": 3, "predicate_reachable": 3,
                                    "cold_now": 0},
                ),
                # Everything blocked by a predicate.
                "all_blocked": cls._report(
                    coverage={"cold_total_now": 2, "explained_cold": 2,
                              "unexplained_cold": 0},
                    events={"total": 2, "malformed": 0, "memories": 2},
                    recoverability={"memories": 2, "predicate_reachable": 0,
                                    "cold_now": 0, "blocked": 2},
                ),
                # Zero-drop evictions present.
                "evictions": cls._report(
                    coverage={"cold_total_now": 30, "explained_cold": 30,
                              "unexplained_cold": 0},
                    events={"total": 30, "malformed": 0, "memories": 30},
                    unchanged_weight=11,
                ),
            },
            "decisiveAge": {
                "weight_floor_only": {"reasons": ["weight_floor"],
                                      "age_days": 12.5, "row_age_days": 300.0},
                # Two-pass: the archiving rule wins over the drift that preceded it.
                "archive_beats_drift": {"reasons": ["weight_floor", "age_limit"],
                                        "age_days": 0.5, "row_age_days": 200.0},
                "capacity_is_ageless": {"reasons": ["capacity_overflow"],
                                        "age_days": 4.0, "row_age_days": 90.0},
                "raw_retention_uses_row_age": {"reasons": ["raw_retention"],
                                               "age_days": 0.0, "row_age_days": 45.5},
                "missing_field": {"reasons": ["age_limit"], "row_age_days": None},
                "unknown_reason": {"reasons": ["not_a_reason"], "age_days": 3.0},
            },
            "coverageStates": list(forgetting.COVERAGE_STATE_LABELS),
            "dropValues": [0, 0.25, None, "n/a"],
        }

    def _rows(self, case):
        return {row["id"]: row for row in self.out["principleRows"][case]}

    # -- property 1: zero events is a finding ---------------------------------

    def test_zero_events_over_a_cold_population_is_a_danger_verdict(self):
        row = self._rows("unexplained_population")["zero-is-not-zero"]
        self.assertEqual("danger", row["tone"])
        self.assertIn("255,817", row["evidence"])
        self.assertIn("空白", row["reading"])
        self.assertNotIn("没有遗忘", row["reading"])

    def test_empty_ledger_is_neutral_not_reassuring(self):
        row = self._rows("empty_ledger")["zero-is-not-zero"]
        self.assertEqual("neutral", row["tone"])
        self.assertIn("没有可解释的对象", row["reading"])

    def test_events_that_miss_the_population_are_still_danger(self):
        row = self._rows("window_misses_population")["zero-is-not-zero"]
        self.assertEqual("danger", row["tone"])

    def test_partial_coverage_is_a_warning(self):
        row = self._rows("partial")["zero-is-not-zero"]
        self.assertEqual("warn", row["tone"])
        self.assertIn("1 条", row["evidence"])

    def test_the_verdict_is_derived_not_boilerplate(self):
        tones = {case: self._rows(case)["zero-is-not-zero"]["tone"]
                 for case in ("unexplained_population", "empty_ledger", "partial")}
        self.assertEqual(3, len(set(tones.values())),
                         "三种截然不同的覆盖情形给出了同一个结论：这是固定免责声明，不是推导")

    # -- property 2: recall is conditional ------------------------------------

    def test_cold_rows_make_recall_conditional(self):
        row = self._rows("partial")["recall-is-conditional"]
        self.assertEqual("warn", row["tone"])
        self.assertIn("LIKE 救援通道", row["reading"])

    def test_active_rows_do_not_claim_conditionality(self):
        row = self._rows("all_active")["recall-is-conditional"]
        self.assertEqual("ok", row["tone"])
        self.assertNotIn("LIKE", row["reading"])

    def test_no_events_means_recall_is_unknown(self):
        row = self._rows("unexplained_population")["recall-is-conditional"]
        self.assertEqual("neutral", row["tone"])
        self.assertIn("无从判定", row["reading"])

    def test_blocked_rows_are_not_described_as_reachable(self):
        row = self._rows("all_blocked")["recall-is-conditional"]
        self.assertEqual("warn", row["tone"])
        self.assertIn("没有一条能通过", row["reading"])

    # -- property 3: zero drop is eviction ------------------------------------

    def test_zero_drop_rows_are_named_as_eviction(self):
        row = self._rows("evictions")["zero-drop-is-eviction"]
        self.assertEqual("warn", row["tone"])
        self.assertIn("11", row["evidence"])
        for word in ("capacity_overflow", "raw_retention", "age_limit", "expired"):
            with self.subTest(word=word):
                self.assertIn(word, row["reading"])
        self.assertIn("不是指数衰减", row["reading"])

    def test_absent_zero_drop_rows_do_not_claim_eviction(self):
        row = self._rows("partial")["zero-drop-is-eviction"]
        self.assertEqual("neutral", row["tone"])
        self.assertIn("都有权重下降", row["reading"])

    # -- decisive age ---------------------------------------------------------

    def test_the_archiving_rule_wins_the_decisive_age(self):
        decided = self.out["decisiveAge"]["archive_beats_drift"]
        self.assertEqual("row_age_days", decided["field"])
        self.assertEqual(200.0, decided["age"])

    def test_weight_floor_uses_the_decay_exponent_age(self):
        decided = self.out["decisiveAge"]["weight_floor_only"]
        self.assertEqual("age_days", decided["field"])
        self.assertEqual(12.5, decided["age"])

    def test_capacity_overflow_has_no_decisive_age(self):
        decided = self.out["decisiveAge"]["capacity_is_ageless"]
        self.assertIsNone(decided["age"])
        self.assertEqual("", decided["field"])

    def test_raw_retention_uses_row_age(self):
        decided = self.out["decisiveAge"]["raw_retention_uses_row_age"]
        self.assertEqual("row_age_days", decided["field"])
        self.assertEqual(45.5, decided["age"])

    def test_missing_age_field_reports_unknown_instead_of_zero(self):
        decided = self.out["decisiveAge"]["missing_field"]
        self.assertIsNone(decided["age"],
                          "缺字段时不能退化成 0.0：那会读成「刚写的记忆因为太老被归档」")

    def test_contract_unknown_reason_is_ageless(self):
        decided = self.out["decisiveAge"]["unknown_reason"]
        self.assertIsNone(decided["age"])

    # -- formatting -----------------------------------------------------------

    def test_drop_is_a_magnitude_never_signed(self):
        self.assertEqual("0.250", self.out["fmt"]["0.25"])
        self.assertEqual("0.000", self.out["fmt"]["0"])
        self.assertEqual("n/a", self.out["fmt"]["null"])
        self.assertEqual("n/a", self.out["fmt"]["n/a"])

    # -- coverage tones -------------------------------------------------------

    def test_coverage_tones_follow_the_labels(self):
        self.assertEqual("danger", self.out["coverage"]["none"]["tone"])
        self.assertEqual("warn", self.out["coverage"]["partial"]["tone"])
        self.assertEqual("ok", self.out["coverage"]["complete"]["tone"])
        self.assertEqual("neutral", self.out["coverage"]["empty"]["tone"])
        for state, label in forgetting.COVERAGE_STATE_LABELS.items():
            self.assertEqual(label, self.out["coverage"][state]["label"])


#: Headless DOM shim: enough to run the real render() against real report
#: shapes.  The static tests only pin principleRows(); a section builder that
#: dereferences a missing field (an older daemon, a null drop, a gone memory)
#: would still pass every static check and then throw on the operator's screen.
RENDER_SHIM = r"""
/* Minimal DOM shim: enough to run a view's render() headlessly.
   Its purpose is to prove the section builders do not throw on real report
   shapes -- the static tests only pin principleRows(), and a renderer that
   dereferences a missing field would still pass every static check. */

class ShimNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.nodeName = this.tagName;
    this.childNodes = [];
    this.attributes = {};
    this.dataset = {};
    this.style = { setProperty() {}, };
    this.className = "";
    this.textContent = "";
    this.parentNode = null;
    this.isConnected = false;
    this.listeners = {};
  }
  appendChild(child) {
    if (child && child.__fragment) {
      for (const inner of child.childNodes.slice()) this.appendChild(inner);
      child.childNodes.length = 0;
      return child;
    }
    if (child) { child.parentNode = this; this.childNodes.push(child); }
    return child;
  }
  append(...children) { for (const child of children) this.appendChild(child); }
  replaceChildren(...children) {
    this.childNodes.length = 0;
    for (const child of children) this.appendChild(child);
  }
  removeChild(child) {
    const at = this.childNodes.indexOf(child);
    if (at >= 0) this.childNodes.splice(at, 1);
    return child;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attributes, key) ? this.attributes[key] : null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.childNodes[0] || null; }
  get children() { return this.childNodes.filter((node) => node instanceof ShimNode); }
  get classList() {
    const self = this;
    return {
      add(...names) {
        for (const name of names) {
          if (!self.className.includes(name)) self.className = (self.className + " " + name).trim();
        }
      },
      remove() {},
      toggle() {},
      contains(name) { return self.className.split(/\s+/).includes(name); },
    };
  }
  getClientRects() { return []; }
}

class ShimFragment extends ShimNode {
  constructor() { super("#fragment"); this.__fragment = true; }
}

const documentShim = {
  createElement: (tag) => new ShimNode(tag),
  createElementNS: (_ns, tag) => new ShimNode(tag),
  createTextNode: (text) => { const node = new ShimNode("#text"); node.textContent = String(text); return node; },
  createDocumentFragment: () => new ShimFragment(),
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  body: new ShimNode("body"),
  documentElement: new ShimNode("html"),
  visibilityState: "visible",
};

globalThis.document = documentShim;
globalThis.Node = ShimNode;
globalThis.window = {
  location: { origin: "http://127.0.0.1:8765", hash: "", pathname: "/ui/", search: "" },
  addEventListener() {}, removeEventListener() {},
  setInterval: () => 0, clearInterval() {},
  setTimeout: () => 0, clearTimeout() {},
  requestAnimationFrame: (fn) => { fn(); return 0; },
  matchMedia: () => ({ matches: false }),
  dispatchEvent() {},
  navigator: { clipboard: { writeText: async () => {} } },
};
if (!Object.getOwnPropertyDescriptor(globalThis, "navigator")
        || Object.getOwnPropertyDescriptor(globalThis, "navigator").configurable) {
  Object.defineProperty(globalThis, "navigator", {
    value: globalThis.window.navigator, configurable: true, writable: true,
  });
}
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.CustomEvent = class {
  constructor(type, init) { this.type = type; this.detail = (init || {}).detail; }
};
globalThis.performance = { now: () => Date.now() };
globalThis.AbortController = globalThis.AbortController || class {
  constructor() { this.signal = { aborted: false }; }
  abort() {}
};

/* Count every node the render produced, so a silently empty screen fails. */
function countNodes(node) {
  let total = 1;
  for (const child of node.childNodes || []) total += countNodes(child);
  return total;
}

function collectText(node, out = []) {
  if (node.tagName === "#TEXT") { out.push(node.textContent); return out; }
  for (const child of node.childNodes || []) collectText(child, out);
  return out;
}

const { readFileSync } = await import("node:fs");
const view = await import(process.argv[2]);
const payload = JSON.parse(readFileSync(process.argv[3], "utf8"));

const results = {};
for (const [name, report] of Object.entries(payload)) {
  // api.get is what render() calls; stub it to hand back this report.
  const { api } = await import(process.argv[4]);
  api.get = async () => report;
  const context = {
    params: new URLSearchParams(""),
    signal: { aborted: false },
    navigate() {}, setQuery() {}, onCleanup() {},
    toast() {}, confirm() {}, drawer: { open() {}, close() {} },
    refresh() {},
  };
  const root = await view.render(context);
  const text = collectText(root).join(" ");
  results[name] = { nodes: countNodes(root), textLength: text.length, text };
}
process.stdout.write(JSON.stringify(results));

"""

#: Report shapes the renderer must survive.  Every one of these is a state the
#: daemon can actually return: today's production shape (255k cold rows, zero
#: events), a fully populated partial-coverage window, and an empty ledger.
#: Report shapes the renderer must survive, as plain data so a reviewer can
#: diff one field at a time.  Serialized to JSON for the harness at run time.
RENDER_CASES = {
    'production_shape': {
        'ledger': '/Users/zps/Library/Application Support/HippoMind/hippomind.sqlite',
        'generated_at': '2026-10-25T14:00:00+00:00',
        'window_days': 7,
        'window': {
            'start': '2026-10-18T14:00:00+00:00',
            'end': '2026-10-25T14:00:00+00:00',
        },
        'scope': 'shared',
        'limit': 20,
        'events': {
            'total': 0,
            'malformed': 0,
            'truncated': False,
            'scanned': 0,
            'memories': 0,
            'tables': [
                'ledger_events',
            ],
            'budget': 14000,
        },
        'by_reason': [],
        'by_agent': [],
        'by_kind': [],
        'archive': {
            'total': 0,
            'by_target': [],
        },
        'top_drops': [],
        'unchanged_weight': 0,
        'recoverability': {
            'scope': 'shared',
            'include_raw_chat': False,
            'memories': 0,
            'predicate_reachable': 0,
            'cold_now': 0,
            'needs_raw_flag': 0,
            'blocked': 0,
            'gone': 0,
            'blockers': {},
            'examples': [],
        },
        'coverage': {
            'cold_total_now': 255817,
            'explained_cold': 0,
            'unexplained_cold': 255817,
            'decay_events_all_time': 0,
            'decay_events_available': False,
            'significant_drop': 0.01,
            'state': 'none',
            'missing_age_field': {},
            'reasons_seen': [],
        },
        'verdict': {
            'headline': (
                '本窗口没有任何 decay 事件，但账本里有 255817 条 cold 记忆：'
                '遗忘发生了，却没有留下原因'),
            'explainable': False,
            'coverage_state': 'none',
            'coverage_label': '无法解释',
            'explained_rows': 0,
            'unexplained_rows': 255817,
            'recall_is_conditional': False,
            'caveats': [
                '写入侧（lifecycle.maintain）尚未落 decay 事件',
                'cold 行仍在账本里、未被删除，但召回是条件性的',
            ],
        },
        'cache': {
            'hit': False,
            'age_s': 0,
            'ttl_s': 30,
            'elapsed_ms': 2688.4,
        },
    },
    'full_report': {
        'ledger': '/tmp/hub.sqlite',
        'generated_at': '2026-10-25T14:00:00+00:00',
        'window_days': 7,
        'window': {
            'start': '2026-10-18T14:00:00+00:00',
            'end': '2026-10-25T14:00:00+00:00',
        },
        'scope': 'shared',
        'limit': 20,
        'events': {
            'total': 12,
            'malformed': 1,
            'truncated': True,
            'scanned': 13,
            'memories': 5,
            'tables': [
                'ledger_events',
                'ledger_events_archive',
            ],
            'budget': 14000,
        },
        'by_reason': [
            {
                'reason': 'weight_floor',
                'label': '权重跌破冷置下限',
                'rule': '指数衰减把权重压到冷置阈值以下',
                'events': 7,
                'memories': 3,
                'archived': 2,
                'weight_drop': 0.42,
            },
            {
                'reason': 'capacity_overflow',
                'label': '活跃容量超限',
                'rule': '按 权重×重要度+utility 排序挤出末尾',
                'events': 3,
                'memories': 2,
                'archived': 0,
                'weight_drop': 0,
            },
            {
                'reason': 'unknown',
                'label': '未标注/契约外原因',
                'rule': 'metadata.reason 缺失或不在契约取值内——写入侧需要修正',
                'events': 2,
                'memories': 1,
                'archived': 1,
                'weight_drop': 0.1,
            },
        ],
        'by_agent': [
            {
                'agent': 'claude',
                'events': 9,
                'memories': 4,
                'archived': 3,
            },
            {
                'agent': 'codex',
                'events': 3,
                'memories': 1,
                'archived': 0,
            },
        ],
        'by_kind': [
            {
                'kind': 'fact',
                'events': 8,
                'memories': 3,
                'archived': 2,
            },
            {
                'kind': '(记忆已不在账本)',
                'events': 4,
                'memories': 2,
                'archived': 1,
            },
        ],
        'archive': {
            'total': 3,
            'by_target': [
                {
                    'to_lifecycle': 'cold',
                    'events': 2,
                },
                {
                    'to_lifecycle': 'expired',
                    'events': 1,
                },
            ],
        },
        'top_drops': [
            {
                'memory_id': 'mem_aaaaaaaaaaaaaaaaaaaa',
                'kind': 'fact',
                'agent': 'claude',
                'reasons': [
                    'weight_floor',
                ],
                'events': 2,
                'weight_before': 0.9,
                'weight_after': 0.5,
                'drop': 0.4,
                'age_days': 18,
                'row_age_days': 30,
                'utility': 0.3,
                'importance': 6,
                'last_at': '2026-10-24T10:00:00+00:00',
                'archived': True,
            },
            {
                'memory_id': 'mem_bbbbbbbbbbbbbbbbbbbb',
                'kind': 'fact',
                'agent': 'codex',
                'reasons': [
                    'weight_floor',
                    'age_limit',
                ],
                'events': 2,
                'weight_before': 0.6,
                'weight_after': 0.3,
                'drop': 0.3,
                'age_days': 0.5,
                'row_age_days': 200,
                'utility': 0.1,
                'importance': 3,
                'last_at': '2026-10-23T10:00:00+00:00',
                'archived': True,
            },
            {
                'memory_id': 'mem_cccccccccccccccccccc',
                'kind': '(记忆已不在账本)',
                'agent': 'claude',
                'reasons': [
                    'capacity_overflow',
                ],
                'events': 1,
                'weight_before': 0.3,
                'weight_after': 0.3,
                'drop': 0,
                'age_days': 0,
                'row_age_days': 90,
                'utility': 0.2,
                'importance': 4,
                'last_at': '2026-10-22T10:00:00+00:00',
                'archived': True,
            },
            {
                'memory_id': 'mem_dddddddddddddddddddd',
                'kind': 'fact',
                'agent': 'claude',
                'reasons': [
                    'expired',
                ],
                'events': 1,
                'weight_before': 0.5,
                'weight_after': 0.45,
                'drop': 0.05,
                'age_days': None,
                'row_age_days': None,
                'utility': None,
                'importance': None,
                'last_at': '2026-10-21T10:00:00+00:00',
                'archived': True,
            },
            {
                'memory_id': 'mem_eeeeeeeeeeeeeeeeeeee',
                'kind': 'fact',
                'agent': 'claude',
                'reasons': [
                    'unknown',
                ],
                'events': 1,
                'weight_before': None,
                'weight_after': None,
                'drop': None,
                'age_days': None,
                'row_age_days': None,
                'utility': None,
                'importance': None,
                'last_at': '2026-10-20T10:00:00+00:00',
                'archived': False,
            },
        ],
        'unchanged_weight': 1,
        'recoverability': {
            'scope': 'shared',
            'include_raw_chat': False,
            'memories': 5,
            'predicate_reachable': 3,
            'cold_now': 3,
            'needs_raw_flag': 1,
            'blocked': 1,
            'gone': 1,
            'blockers': {
                'quality_state=quarantine': 1,
            },
            'examples': [
                {
                    'memory_id': 'mem_ffffffffffffffffffff',
                    'blockers': [
                        'quality_state=quarantine（未过质量门禁）',
                    ],
                },
            ],
        },
        'coverage': {
            'cold_total_now': 100,
            'explained_cold': 5,
            'unexplained_cold': 95,
            'decay_events_all_time': 12,
            'decay_events_available': True,
            'significant_drop': 0.01,
            'state': 'partial',
            'missing_age_field': {
                'row_age_days': 2,
            },
            'reasons_seen': [
                'age_limit',
                'capacity_overflow',
                'weight_floor',
            ],
        },
        'verdict': {
            'headline': (
                '5 条记忆在本窗口被降权/归档（12 条事件），主因：'
                'weight_floor 权重跌破冷置下限；但 100 条 cold 中只有 5 条有解释，'
                '另 95 条无解释（部分可解释）'),
            'explainable': True,
            'coverage_state': 'partial',
            'coverage_label': '部分可解释',
            'explained_rows': 5,
            'unexplained_rows': 95,
            'recall_is_conditional': True,
            'caveats': [
                '只有本窗口内降幅 ≥1% 或发生 lifecycle 变化的行会落事件',
                '结论只覆盖有事件的那 5 条，不能外推到全部 100 条 cold',
            ],
        },
        'cache': {
            'hit': True,
            'age_s': 12.5,
            'ttl_s': 30,
            'elapsed_ms': 0,
        },
    },
    'empty_ledger': {
        'ledger': '/tmp/empty.sqlite',
        'generated_at': '2026-10-25T14:00:00+00:00',
        'window_days': 7,
        'window': {
            'start': '2026-10-18T14:00:00+00:00',
            'end': '2026-10-25T14:00:00+00:00',
        },
        'scope': 'shared',
        'limit': 20,
        'events': {
            'total': 0,
            'malformed': 0,
            'truncated': False,
            'scanned': 0,
            'memories': 0,
            'tables': [
                'ledger_events',
            ],
            'budget': 14000,
        },
        'by_reason': [],
        'by_agent': [],
        'by_kind': [],
        'archive': {
            'total': 0,
            'by_target': [],
        },
        'top_drops': [],
        'unchanged_weight': 0,
        'recoverability': {
            'scope': 'shared',
            'include_raw_chat': False,
            'memories': 0,
            'predicate_reachable': 0,
            'cold_now': 0,
            'needs_raw_flag': 0,
            'blocked': 0,
            'gone': 0,
            'blockers': {},
            'examples': [],
        },
        'coverage': {
            'cold_total_now': 0,
            'explained_cold': 0,
            'unexplained_cold': 0,
            'decay_events_all_time': 0,
            'decay_events_available': False,
            'significant_drop': 0.01,
            'state': 'empty',
            'missing_age_field': {},
            'reasons_seen': [],
        },
        'verdict': {
            'headline': '本窗口没有遗忘事件，也没有 cold 记忆：无事可解释',
            'explainable': True,
            'coverage_state': 'empty',
            'coverage_label': '无事可解释',
            'explained_rows': 0,
            'unexplained_rows': 0,
            'recall_is_conditional': False,
            'caveats': [
                '若维护任务从未运行过，这是「没有维护」而不是「没有遗忘」',
            ],
        },
        'cache': {
            'hit': False,
            'age_s': 0,
            'ttl_s': 30,
            'elapsed_ms': 58,
        },
    },
}



@unittest.skipUnless(NODE, "node 不在 PATH；跳过渲染检查")
class GuiForgettingRenderTests(unittest.TestCase):
    """render() itself, against real report shapes, in a headless DOM.

    The other view tests check what principleRows() decides; these check that
    the whole screen actually builds from the response -- including the sections
    that read fields the principle rows never touch (by_reason, top_drops,
    recoverability.blockers, archive.by_target).
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hippo-gui-forgetting-render-")
        root = Path(cls.tmp.name)
        (root / "shim.mjs").write_text(RENDER_SHIM, encoding="utf-8")
        (root / "cases.json").write_text(json.dumps(RENDER_CASES, ensure_ascii=False),
                                        encoding="utf-8")
        result = subprocess.run(
            [NODE, str(root / "shim.mjs"), VIEW_PATH.as_uri(),
             str(root / "cases.json"), (WEB / "js" / "core" / "api.js").as_uri()],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            raise AssertionError("render harness failed:\n%s" % (result.stderr[-3000:],))
        cls.out = json.loads(result.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_shape_renders_a_non_empty_screen(self):
        for name, rendered in self.out.items():
            with self.subTest(case=name):
                self.assertGreater(rendered["nodes"], 100,
                                   "%s 只渲染出 %d 个节点：视图是空白的"
                                   % (name, rendered["nodes"]))
                self.assertGreater(rendered["textLength"], 500)

    def test_production_shape_reads_as_unexplainable(self):
        """255k cold rows and zero events must not render as a clean zero."""
        text = self.out["production_shape"]["text"]
        self.assertIn("无法解释", text)
        self.assertIn("255,817", text)
        self.assertIn("却 没有留下原因".replace(" ", ""), text.replace(" ", ""))
        self.assertIn("现在读到的是一个空白，不是一个零", text)
        self.assertNotIn("部分可解释", text)

    def test_production_shape_still_explains_conditional_recall(self):
        """Zero events must not drop the recoverability explanation.

        "LIKE 救援通道" appears in both the principle-row reading and the
        recall card's step list, so it cannot prove the CARD rendered.  These
        three strings are emitted only by the card: the fts-deletion step, the
        rescue-channel step, and the "look it up by id" fallback.
        """
        text = self.out["production_shape"]["text"]
        self.assertIn("冷投影不占热索引", text, "召回卡片缺少「cold 行已离开热索引」这一步")
        self.assertIn("LIKE 救援通道", text)
        self.assertIn("按 memory_id 直查", text, "召回卡片缺少「按 id 直查」的兜底建议")
        self.assertIn("可召回是条件性的", text)

    def test_partial_report_renders_reasons_drops_and_blockers(self):
        text = self.out["full_report"]["text"]
        for probe in ("部分可解释", "weight_floor", "capacity_overflow",
                      "未标注", "决定性年龄", "条件性可达", "归档去向"):
            with self.subTest(probe=probe):
                self.assertIn(probe, text, "完整报告缺少 %s" % probe)

    def test_zero_drop_rows_are_rendered_as_eviction_not_decay(self):
        """The top-drops card must carry its own eviction note.

        "权重没有下降" also appears in the principle-row evidence, so it
        cannot tell this card apart from the summary above it; "这类行不是在
        衰减" is emitted only by the card and is what this test pins.
        """
        text = self.out["full_report"]["text"]
        self.assertIn("这类行不是在衰减", text,
                      "降权最狠卡片缺少「降幅 0 = 挤出」的说明")
        self.assertIn("容量挤出", text)

    def test_malformed_and_missing_age_are_surfaced(self):
        """Both coverage gaps must reach the screen, not just the JSON.

        "无法解析" is shared with the principle-row evidence; "计入分母" is
        only in the coverage card, and that is the claim being made -- a
        malformed event stays in the denominator instead of shrinking it.
        """
        text = self.out["full_report"]["text"]
        self.assertIn("计入分母", text,
                      "无法解析的事件没有在覆盖卡片里说明「仍计入分母」")
        self.assertIn("写入侧版本较旧", text, "缺少 missing_age_field 的提示")
        self.assertIn("row_age_days", text)

    def test_empty_ledger_says_nothing_to_explain(self):
        text = self.out["empty_ledger"]["text"]
        self.assertIn("无事可解释", text)
        self.assertNotIn("部分可解释", text)

    def test_cache_state_is_shown_honestly(self):
        self.assertIn("缓存命中", self.out["full_report"]["text"])
        self.assertIn("本次现测", self.out["production_shape"]["text"])


if __name__ == "__main__":
    unittest.main()
