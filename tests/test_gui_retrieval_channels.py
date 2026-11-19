"""检索通道区块与关系类型语义的静态/行为契约。

这块的验收标准是「让用户知道每路在不在工作」，所以测试钉住三件事：

  1. **不新增端点**：通道状态来自已经在拿的 /api/v1/overview（daemon_health）
     与 /api/v1/health（fts 检查项）。多一个端点就是多一份要维护的契约。
  2. **「已关闭」≠「不可用」**：entity_index 默认 {"state":"disabled"}，这是
     一个可开启的功能。渲染必须把它和 unavailable/error 分开——把正常关闭的
     功能画成故障，比不画更糟。
  3. **关系类型按线型分层**：same_subject(0.9) / same_task(0.7) 原来和
     related_to(1.0) 同色，画布上无法区分。现在靠虚线/点线分开，且现有四类
     分支的顺序与语义必须逐字不变（截图基线与前端契约都依赖它）。
"""

from __future__ import annotations

import re
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger.graph import (  # noqa: E402
    DEFAULT_RELATION_WEIGHT, RELATION_WEIGHTS, relation_weight,
)

NODE = shutil.which("node")
WEB = ROOT / "hippomind" / "web"
PULSE = WEB / "js" / "views" / "pulse.js"
GRAPH = WEB / "js" / "views" / "graph.js"
CANVAS = WEB / "js" / "components" / "graph-canvas.js"


def read(path):
    return path.read_text(encoding="utf-8")


def media_blocks(css):
    """{max_width_px: block_text} for every one-level @media in a stylesheet."""
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
        width = int(match.group(1))
        blocks[width] = blocks.get(width, "") + css[match.end():cursor]
    return blocks


class RetrievalChannelContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(PULSE)

    def test_pulse_calls_only_documented_endpoints(self):
        """The channel block must not introduce a new endpoint."""
        called = set(re.findall(r"""["\x60](/api/v1/[A-Za-z0-9_\-/]*)""", self.text))
        allowed = {"/api/v1/overview", "/api/v1/health", "/api/v1/events",
                   "/api/v1/injection-ab"}
        self.assertEqual(set(), called - allowed,
                         "pulse.js 调用了未登记的端点: %s" % sorted(called - allowed))

    def test_channel_block_reads_daemon_health(self):
        for field in ("daemon_health", "vector_index", "entity_index"):
            with self.subTest(field=field):
                self.assertIn(field, self.text,
                              "pulse.js 的通道区块没有读取 %s" % field)

    def test_channel_block_reads_the_fts_check(self):
        self.assertIn("fts_rows", self.text)
        self.assertIn("unindexed", self.text)

    def test_disabled_is_not_rendered_as_a_fault(self):
        """A default-off feature must not look broken."""
        self.assertRegex(
            self.text, r'disabled:\s*"neutral"',
            '「已关闭」必须映射到 neutral，不能和 unavailable/error 同语气')
        self.assertRegex(self.text, r'unavailable:\s*"warn"')
        self.assertRegex(self.text, r'error:\s*"warn"')

    def test_disabled_state_explains_how_to_enable_it(self):
        """Naming the switch is the point: without it "已关闭" reads as broken.

        Pinned as the whole clause, not just the variable name -- a bare
        assertIn("HIPPOMIND_ENTITY_CHANNEL") also passes after the sentence
        around it has been gutted, which is the mutation that survived the
        first version of this test.
        """
        self.assertRegex(
            self.text, r"HIPPOMIND_ENTITY_CHANNEL=1 可开启",
            "实体通道关闭时必须给出可执行的开启方式（变量名 + 取值），"
            "否则「已关闭」读起来像坏掉了")

    def test_the_three_channels_are_all_present(self):
        for channel in ("lexical", "vector", "entity"):
            with self.subTest(channel=channel):
                self.assertIn('id: "' + channel + '"', self.text)

    def test_fts_status_field_is_read(self):
        """The FTS check publishes `status`, the index loops publish `state`."""
        self.assertIn("raw.status", self.text,
                      "只读 state 会让健康的词法通道渲染成「等待首轮」")

    def test_no_markdown_leaks_into_text_nodes(self):
        """These strings go straight into textContent; ** is not bold here.

        Scanned line by line on purpose: a [^"]* pattern spans newlines and
        happily matches code between two unrelated quotes, which made this
        fire on nothing at all.
        """
        offenders = []
        for number, line in enumerate(self.text.splitlines(), 1):
            for match in re.finditer(r'"([^"]*\*\*[^"]*)"', line):
                offenders.append("%d: %s" % (number, match.group(1)[:60]))
        if offenders:
            self.fail("文案里出现了 markdown 强调符，会被当成字面星号显示：\n"
                      + "\n".join(offenders[:8]))

    def test_css_hooks_exist_and_are_themed(self):
        css = read(WEB / "css" / "views-a.css")
        for hook in (".va-channels", ".va-channel", ".va-channel-detail",
                     ".va-ab", ".va-ab-arms", ".va-ab-verdict"):
            with self.subTest(hook=hook):
                self.assertIn(hook, css, "views-a.css 缺少样式钩子 %s" % hook)
        for match in re.finditer(r"\.va-(?:channel|ab)[a-z0-9-]*[^{]*\{([^}]*)\}", css):
            with self.subTest(block=match.group(1).strip()[:50]):
                self.assertNotRegex(match.group(1), r"#[0-9a-fA-F]{3,8}\b",
                                    "通道/实验样式硬编码了颜色")

    def test_channels_collapse_on_narrow_viewports(self):
        blocks = media_blocks(read(WEB / "css" / "views-a.css"))
        self.assertIn(".va-channels", blocks.get(1080, ""),
                      "三列通道在平板上会挤成一团，需要窄屏单列规则")
        self.assertIn(".va-ab-arms", blocks.get(760, ""),
                      "两臂进度在手机上需要单列")


class RelationTierContractTests(unittest.TestCase):
    """The canvas must distinguish the two structural tiers."""

    @classmethod
    def setUpClass(cls):
        cls.canvas = read(CANVAS)
        cls.graph = read(GRAPH)

    def test_existing_relation_branches_are_unchanged(self):
        """Order and meaning are pinned: the screenshot baseline depends on it."""
        order = [
            'value.indexOf("contradict")',
            'value.indexOf("supersed")',
            'value.indexOf("cause")',
            'value.indexOf("support")',
            'value.indexOf("confirm")',
        ]
        positions = [self.canvas.find(token) for token in order]
        for token, position in zip(order, positions):
            with self.subTest(token=token):
                self.assertGreaterEqual(position, 0, "原有分支被删掉了: %s" % token)
        self.assertEqual(positions, sorted(positions),
                         "原有四类分支的顺序被改动了（截图基线依赖它）")

    def test_new_branches_sit_before_the_fallback(self):
        same_subject = self.canvas.find('value.indexOf("same_subject")')
        fallback = self.canvas.find("return colors.ink3;")
        self.assertGreater(same_subject, 0)
        self.assertGreater(fallback, same_subject,
                           "same_subject 分支必须在 ink3 兜底之前，否则永远命中不到")

    def test_same_subject_has_its_own_colour(self):
        self.assertIn("colors.ink2", self.canvas,
                      "same_subject 需要独立颜色，不能落回 ink3")

    def test_dash_patterns_distinguish_the_tiers(self):
        self.assertIn("relationDash", self.canvas)
        # The source is: value.indexOf("same_subject") >= 0) return [7, 3];
        self.assertRegex(self.canvas,
                         r'same_subject"\)[^;]*return \[7, 3\];')
        self.assertRegex(self.canvas,
                         r'same_task"\)[^;]*return \[2, 3\];')
        self.assertIn("setLineDash", self.canvas)

    def test_dash_is_cleared_after_each_edge(self):
        """Leaking a dash into the next edge would repaint unrelated relations."""
        self.assertGreaterEqual(self.canvas.count("setLineDash"), 2,
                                "必须有一次 setLineDash([]) 复位，否则虚线会串到后续边")

    def test_weight_hints_match_the_ledger(self):
        """The legend prints weights; they must come from the same numbers."""
        match = re.search(r"export const RELATION_WEIGHT_HINTS = \[(.*?)\];",
                          self.canvas, re.S)
        self.assertIsNotNone(match, "graph-canvas.js 必须导出 RELATION_WEIGHT_HINTS")
        block = match.group(1)
        for relation, weight in RELATION_WEIGHTS.items():
            if relation not in ("same_subject", "same_task", "related_to"):
                continue
            with self.subTest(relation=relation):
                pattern = (r'type:\s*"' + re.escape(relation) +
                           r'"\s*,\s*weight:\s*' + re.escape(str(weight)))
                self.assertRegex(block, pattern,
                                 "图例里的 %s 权重与 ledger/graph.py 不一致" % relation)

    def test_unknown_relation_weight_matches_the_ledger_contract(self):
        """Legacy/unknown types must stay at 1.0 -- never silently demoted."""
        self.assertEqual(1.0, DEFAULT_RELATION_WEIGHT)
        self.assertEqual(1.0, relation_weight("related_to"))
        self.assertEqual(1.0, relation_weight("a_type_from_the_future"))
        self.assertEqual(0.9, relation_weight("same_subject"))
        self.assertEqual(0.7, relation_weight("same_task"))

    def test_legend_prints_weights_not_only_colours(self):
        """Every tier label must be followed by its weight, in the legend.

        assertIn(label) alone passes even after the weight is stripped from the
        label, so this pins the concatenation that actually renders the number.
        """
        self.assertIn("RELATION_WEIGHT_HINTS", self.graph,
                      "graph.js 的图例必须消费权重表")
        for relation in ("same_subject", "same_task", "related_to"):
            with self.subTest(relation=relation):
                self.assertRegex(
                    self.graph,
                    r'weightOf\("' + re.escape(relation) + r'"\)',
                    "图例没有为 %s 打印权重" % relation)
        for label in ("同主语", "同任务", "其他"):
            with self.subTest(label=label):
                self.assertIn(label, self.graph)
        # The rendered label is the literal plus the call -- both halves.
        self.assertRegex(self.graph,
                         r'"同主语 " \+ weightOf\("same_subject"\)')

    def test_canvas_has_no_hardcoded_colour_tokens(self):
        """readTheme may only add tokens that exist in tokens.css."""
        declared = set(re.findall(r'pick\("(--[a-z0-9-]+)"', self.canvas))
        tokens = read(WEB / "css" / "tokens.css")
        for token in declared:
            with self.subTest(token=token):
                self.assertIn(token + ":", tokens,
                              "graph-canvas 读取了 tokens.css 里不存在的变量 %s" % token)


if __name__ == "__main__":
    unittest.main()
