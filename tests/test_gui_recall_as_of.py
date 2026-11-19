"""G2：Recall 视图的 as_of 双时态输入（真实挂载，不是源码 grep）。

背景：Compose 视图早就有 as_of（compose.js 的 "as_of（双时态切片）"），
Recall 没有——同一个后端能力两个视图不一致。补齐之后最危险的不是「输入框
不存在」，而是**输入框存在但结果区不说清楚**：用户会把 as_of 的历史切片
当成当前状态。所以这里的断言分两类：

1. 请求体真的带上了 options.as_of（否则等于没接线）；
2. as_of 生效时结果区必须出现显式横幅，且横幅用的是**真正发出去**的值，
   不是输入框里的草稿。

前端是零依赖原生 ES 模块、没有构建步骤、仓库里也没有 jsdom，所以
tests/recall_view_harness.mjs 自带一个最小 DOM shim 并真实 import 视图模块。
静态断言（第 3 组）只用来钉住「两个视图文案一致」这种跨文件契约。

测试只读、不启动 daemon、不访问网络。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WEB = ROOT / "hippomind" / "web"
RECALL_JS = WEB / "js" / "views" / "recall.js"
COMPOSE_JS = WEB / "js" / "views" / "compose.js"
HARNESS = Path(__file__).resolve().parent / "recall_view_harness.mjs"
NODE = shutil.which("node")

AS_OF_LABEL = "as_of（双时态切片）"
SAMPLE_HIT = {
    "memory_id": "mem_1",
    "kind": "fact",
    "content": "部署端口是 8000",
    "source_agent": "t",
    "created_at": "2026-10-20T00:00:00+00:00",
    "lifecycle": "active",
    "score": 0.9,
    "evidence": ["e1"],
}


def read_text(path):
    return path.read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node 不在 PATH；跳过需要真实挂载视图的用例")
class RecallAsOfMountTests(unittest.TestCase):
    """把 recall.js 真挂到最小 DOM 上，断言它实际发了什么、画了什么。"""

    def run_view(self, *, query="", hits=None, steps=None):
        with tempfile.TemporaryDirectory(prefix="hippo-recall-asof-") as tmp:
            scenario = Path(tmp) / "scenario.json"
            scenario.write_text(json.dumps({
                "query": query,
                "hits": SAMPLE_HIT if hits is None else hits,
                "steps": steps or [],
            }, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [NODE, str(HARNESS), str(RECALL_JS), str(scenario)],
                capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(completed.returncode, 0,
                         "harness 失败：\n" + (completed.stderr or completed.stdout)[-2500:])
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, "harness 没有输出")
        payload = json.loads(lines[-1])
        self.assertTrue(payload.get("ok"), payload)
        return payload

    def options_of(self, payload, index=0):
        requests = payload["requests"]
        self.assertGreater(len(requests), index, "视图没有发出 /recall 请求")
        self.assertEqual(requests[index]["url"], "/recall")
        return requests[index]["body"]["options"]

    # ------------------------------------------------------------ request --

    def test_recall_request_carries_as_of(self):
        """as_of 从 URL 参数进到 options.as_of：接线断了这条就红。"""
        payload = self.run_view(query="q=部署&as_of=2026-10-23T19:00")
        options = self.options_of(payload)
        self.assertEqual(options["as_of"], "2026-10-23T19:00",
                         "recall 的请求体没有携带 as_of，后端永远收到空值")
        self.assertEqual(payload["requests"][0]["body"]["query"], "部署")

    def test_recall_request_defaults_as_of_to_empty(self):
        """没有 as_of 时必须显式发空串，而不是省略字段。

        后端 _options() 用 raw.get("as_of", ...) 取默认值；省略字段虽然也能
        拿到默认空串，但显式空串让「现在」和「漏传」在日志里可区分。
        """
        payload = self.run_view(query="q=部署")
        options = self.options_of(payload)
        self.assertIn("as_of", options)
        self.assertEqual(options["as_of"], "")

    def test_clear_button_drops_as_of_and_keeps_query(self):
        """「清空时间点」只清时间视角，不能连查询词一起丢掉。"""
        payload = self.run_view(
            query="q=部署&as_of=2026-10-23T19:00",
            steps=[{"action": "clickClear"}, {"action": "snapshot", "label": "after"}])
        self.assertEqual(len(payload["applied"]), 1, "点「清空时间点」没有重新应用筛选")
        next_query = payload["applied"][0]
        self.assertNotIn("as_of", next_query, "清空时间点后 URL 里仍带 as_of")
        self.assertEqual(next_query.get("q"), "部署", "清空时间点不该动查询词")

    def test_enter_in_as_of_field_applies_slice(self):
        payload = self.run_view(
            query="q=部署",
            steps=[{"action": "type", "value": "2026-10-23T19:00"},
                   {"action": "enter"},
                   {"action": "snapshot", "label": "after"}])
        self.assertEqual(len(payload["applied"]), 1, "as_of 输入框里按 Enter 没有提交")
        self.assertEqual(payload["applied"][0].get("as_of"), "2026-10-23T19:00")

    # ------------------------------------------------------------- banner --

    def test_slice_banner_is_rendered_when_as_of_applied(self):
        """as_of 生效时结果区必须有显式提示——这是双时态最容易误导的地方。"""
        payload = self.run_view(query="q=部署&as_of=2026-10-23T19:00")
        banners = payload["sliceBanners"]
        self.assertEqual(len(banners), 1, "as_of 生效但结果区没有任何提示横幅")
        text = banners[0]["text"]
        self.assertIn("历史切片", text)
        self.assertIn("as_of=2026-10-23T19:00", text)
        self.assertIn("不是当前状态", text)
        self.assertEqual(banners[0]["role"], "status",
                         "历史切片横幅必须是 role=status，读屏用户也要能听到")

    def test_no_banner_without_as_of(self):
        """没有 as_of 时不能出现「历史切片」——否则提示本身就成了噪声。"""
        payload = self.run_view(query="q=部署")
        self.assertEqual(payload["sliceBanners"], [])
        self.assertNotIn("历史切片", payload["finalText"])

    def test_banner_reflects_applied_value_not_draft(self):
        """草稿态不能污染结果区。

        用户在输入框里打了 as_of 但还没提交时，结果仍然是「现在」的结果；
        如果横幅跟着草稿走，用户会以为看到的是历史切片——正好是最危险的
        误读方向。这里断言：只打字不提交 -> 无横幅；输入框出现「未应用」
        提示。
        """
        payload = self.run_view(
            query="q=部署",
            steps=[{"action": "type", "value": "2026-10-23T19:00"},
                   {"action": "snapshot", "label": "draft"}])
        self.assertEqual(payload["sliceBanners"], [],
                         "只输入未提交就渲染了历史切片横幅")
        draft = payload["steps"][0]
        self.assertTrue(draft["noteVisible"],
                        "草稿与已应用值不同，输入框旁必须提示「未应用」")

    def test_heartbeat_refresh_never_sends_the_uncommitted_draft(self):
        """30s 心跳会调 refresh()；它必须重发「已应用」的 as_of，不能发草稿。

        这是本任务最隐蔽的一条：输入框里打了时间点但没按 Enter 时，用户看到
        的仍是当前状态的结果。若静默重载读草稿，后台刷新会把没提交的 as_of
        发出去——URL 里没有、结果区却变成历史切片，用户完全无法解释。
        """
        payload = self.run_view(
            query="q=部署",
            steps=[{"action": "type", "value": "2026-10-23T19:00"},
                   {"action": "refresh"},
                   {"action": "snapshot", "label": "after_refresh"}])
        self.assertGreaterEqual(len(payload["requests"]), 2,
                                "refresh() 没有触发重载")
        for index, request in enumerate(payload["requests"]):
            with self.subTest(request=index):
                self.assertEqual(
                    request["body"]["options"]["as_of"], "",
                    "心跳重载把输入框草稿当成已应用的 as_of 发了出去")
        self.assertEqual(payload["sliceBanners"], [],
                         "草稿未提交，结果区不应出现历史切片横幅")

    def test_empty_result_hint_mentions_the_slice(self):
        """as_of 下 0 结果最常见的原因是「那一刻还没有这条记忆」。

        不解释的话用户会以为是检索坏了。
        """
        payload = self.run_view(query="q=部署&as_of=2026-06-01T00:00:00Z", hits=[])
        self.assertIn("历史切片", payload["finalText"])
        self.assertIn("as_of=2026-06-01T00:00:00Z", payload["finalText"])

    def test_advanced_summary_lists_as_of(self):
        """折叠状态下也要能看出 as_of 已应用（高级选项默认收起）。"""
        payload = self.run_view(query="q=部署&as_of=2026-10-23T19:00")
        self.assertIn("as_of=2026-10-23T19:00", payload["summaryText"])


class RecallAsOfStaticContractTests(unittest.TestCase):
    """跨文件契约：两个视图的 as_of 必须说同一套话。"""

    def test_recall_and_compose_share_the_as_of_label(self):
        for name, path in (("recall.js", RECALL_JS), ("compose.js", COMPOSE_JS)):
            with self.subTest(file=name):
                self.assertIn(AS_OF_LABEL, read_text(path),
                              "%s 的 as_of 标签与另一个视图不一致" % name)

    def test_recall_sends_as_of_inside_options(self):
        """as_of 必须在 options 里：后端 /recall 只从 options 读它。"""
        text = read_text(RECALL_JS)
        self.assertIn("as_of: asOf", text,
                      "recall.js 的请求体没有把 as_of 放进 options")

    def test_slice_css_hooks_are_themed(self):
        """横幅样式必须走 token，两套主题都成立。"""
        css = read_text(WEB / "css" / "views-b.css")
        for hook in (".vb-recall-slice", ".vb-recall-slice-stamp", ".vb-asof-clear"):
            with self.subTest(hook=hook):
                self.assertIn(hook, css, "css/views-b.css 缺少 as_of 样式钩子 " + hook)
        import re
        for match in re.finditer(r"\.vb-(?:recall-slice|asof)[a-z-]*[^{]*\{([^}]*)\}", css):
            with self.subTest(block=match.group(1)[:60]):
                self.assertNotRegex(
                    match.group(1), r"#[0-9a-fA-F]{3,8}\b",
                    "as_of 样式硬编码了颜色；必须使用 tokens.css 的变量")


if __name__ == "__main__":
    unittest.main()
