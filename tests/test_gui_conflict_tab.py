"""Facts 视图「冲突」页签的语义回归（task-6 / G1）。

钉住三件事：

1. 「冲突」页签只显示 state=conflict，不再把 superseded 混进来；
2. 新增的「取代谱系」页签只显示 state=superseded，且不提供裁决命令；
3. 两个页签各自向服务端要对应 state——只改前端过滤条件是修不好这个 bug 的：
   /api/v1/claims 一页最多 200 行、按 asserted_at 倒序，生产库实测那 200 行里
   conflict 行数 = 0（22 条 conflict 全部排在页外）。把 state !== "active"
   改成 state === "conflict" 只会让页签变成空的，比现在更糟。

测试分两层：

* 静态层：扫 facts.js 源码，钉住旧判据不再出现、导出与渲染共用同一处；
* 行为层：把视图真的跑在 node 里（内置最小 DOM 垫片 + 假 daemon），断言渲染
  出来的文本与 fetch 参数。

垫片刻意不引 jsdom：仓库前端零依赖（无构建、无 node_modules），测试也不该为
一次断言拉一个 5MB 依赖。垫片只实现 el()/textContent/classList 用到的那一小撮
API，没有布局与样式——这些测试断言「渲染出了什么」，不断言「长什么样」。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FACTS_JS = ROOT / "hippomind" / "web" / "js" / "views" / "facts.js"
NODE = shutil.which("node")

#: G1 的根因：把「非活跃」当成「冲突」。常量写成拼接形式，避免这个文件
#: 自己成为下一个 grep 的误报源。
OLD_PREDICATE = "state !== " + chr(34) + "active" + chr(34)

DOM_SHIM = r"""
class NodeBase {
  constructor() { this.childNodes = []; this.parentNode = null; }
  get firstChild() { return this.childNodes[0] || null; }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get firstElementChild() { return this.children[0] || null; }
  appendChild(node) {
    if (node && node.nodeType === 11) {
      for (const kid of node.childNodes.slice()) this.appendChild(kid);
      return node;
    }
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this; this.childNodes.push(node); return node;
  }
  append(...nodes) {
    for (const n of nodes) {
      if (n === null || n === undefined || n === false || n === true) continue;
      if (Array.isArray(n)) { this.append(...n); continue; }
      this.appendChild(typeof n === "string" ? new TextNode(n) : n);
    }
  }
  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i >= 0) { this.childNodes.splice(i, 1); node.parentNode = null; }
    return node;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceChildren(...nodes) {
    while (this.childNodes.length) this.removeChild(this.childNodes[0]);
    this.append(...nodes);
  }
  insertBefore(node, ref) {
    const i = this.childNodes.indexOf(ref);
    if (i < 0) return this.appendChild(node);
    this.childNodes.splice(i, 0, node); node.parentNode = this; return node;
  }
  contains(node) {
    let cur = node;
    while (cur) { if (cur === this) return true; cur = cur.parentNode; }
    return false;
  }
  addEventListener() {} removeEventListener() {} dispatchEvent() {}
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(value) {
    this.replaceChildren();
    if (String(value)) this.appendChild(new TextNode(String(value)));
  }
}
class TextNode extends NodeBase {
  constructor(text) { super(); this.nodeType = 3; this._text = String(text); }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
}
class Element extends NodeBase {
  constructor(tag) {
    super(); this.nodeType = 1; this.tagName = String(tag).toUpperCase();
    this.attributes = {}; this.dataset = {};
    this.style = { setProperty(k, v) { this[k] = v; } };
    this.value = ""; this.checked = false; this.disabled = false; this.tabIndex = 0;
    const classes = new Set();
    this._classes = classes;
    this.classList = {
      add: (...c) => c.forEach((x) => classes.add(x)),
      remove: (...c) => c.forEach((x) => classes.delete(x)),
      toggle: (c, on) => {
        if (on === undefined) { if (classes.has(c)) { classes.delete(c); } else { classes.add(c); } }
        else if (on) { classes.add(c); } else { classes.delete(c); }
      },
      contains: (c) => classes.has(c),
    };
  }
  get className() { return Array.from(this._classes).join(" "); }
  set className(v) {
    this._classes.clear();
    String(v).split(/\s+/).filter(Boolean).forEach((c) => this._classes.add(c));
  }
  setAttribute(k, v) {
    if (k === "class") { this.className = v; return; }
    if (k === "dataset") { Object.assign(this.dataset, v); return; }
    this.attributes[k] = String(v);
    if (k.indexOf("data-") === 0) {
      this.dataset[k.slice(5).replace(/-([a-z])/g, (m, c) => c.toUpperCase())] = String(v);
    }
  }
  getAttribute(k) { return this.attributes[k] === undefined ? null : this.attributes[k]; }
  removeAttribute(k) { delete this.attributes[k]; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() {} blur() {}
  getBoundingClientRect() { return { width: 0, height: 0, top: 0, left: 0 }; }
  get isConnected() { let c = this; while (c.parentNode) c = c.parentNode; return c === doc; }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html || ""; }
}
const doc = {
  nodeType: 9, childNodes: [], parentNode: null,
  createElement: (tag) => new Element(tag),
  createElementNS: (ns, tag) => new Element(tag),
  createTextNode: (t) => new TextNode(t),
  createDocumentFragment: () => { const f = new NodeBase(); f.nodeType = 11; return f; },
  getElementById: () => null,
  querySelector: () => null, querySelectorAll: () => [],
  contains: (n) => doc === n || (n && n.isConnected),
  activeElement: null, body: null, head: null, documentElement: null,
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
};
doc.body = new Element("body");
doc.head = new Element("head");
doc.documentElement = new Element("html");
doc.body.parentNode = doc; doc.head.parentNode = doc; doc.documentElement.parentNode = doc;
const win = {
  document: doc, setTimeout: () => 0, clearTimeout: () => {}, requestAnimationFrame: () => 0,
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  location: { hash: "" }, history: { replaceState() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: { clipboard: { writeText: async () => {} } },
  Blob: class { constructor(parts) { this.parts = parts; } },
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
  CustomEvent: class {
    constructor(type, init) { this.type = type; this.detail = init && init.detail; }
  },
};
win.window = win;
function define(name, value) {
  /* node 22 把 navigator 定义成只读 getter，直接赋值会抛 TypeError。 */
  try { globalThis[name] = value; }
  catch (e) {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  }
}
define("Node", NodeBase); define("Element", Element); define("HTMLElement", Element);
define("NodeList", Array); define("Text", TextNode);
define("document", doc); define("window", win);
define("history", win.history); define("location", win.location);
define("localStorage", win.localStorage);
define("navigator", win.navigator); define("Blob", win.Blob); define("URL", win.URL);
define("CustomEvent", win.CustomEvent);
define("requestAnimationFrame", win.requestAnimationFrame);
define("getComputedStyle", win.getComputedStyle);

export { doc, win, Element, TextNode };
"""

#: 假 daemon 的夹具：与生产同形。counts 是全库口径（4771/22/1934 是生产实测值），
#: items 是"一页"——注意 claims 那一页**故意不含任何 conflict 行**，这正是
#: 生产库的真实形态（22 条 conflict 按 asserted_at 倒序全部排在最新 200 行之外）。
HARNESS = r"""
import { pathToFileURL } from "node:url";
import { doc } from %(shim)s;

const calls = [];
const COUNTS = { active: 2815, conflict: 22, superseded: 1934, total: 4771 };
const FIXTURES = {
  "/api/v1/claims?state=all&limit=200": {
    scope: "shared", state: "all", counts: COUNTS,
    items: [
      { claim_id: "a1", subject: "活跃主语", predicate: "status", slot_key: "state",
        value: "v1", state: "active", confidence: 0.8, memory_preview: "活跃行" },
      { claim_id: "s9", subject: "页内历史主语", predicate: "status", slot_key: "state",
        value: "v0", state: "superseded", confidence: 0.5, memory_preview: "历史行" },
    ],
  },
  "/api/v1/claims?state=conflict&limit=200": {
    scope: "shared", state: "conflict", counts: COUNTS,
    items: [
      { claim_id: "c1", subject: "冲突主语甲", predicate: "port", slot_key: "db",
        value: 5432, state: "conflict", confidence: 0.7, memory_id: "mem_aaa",
        memory_preview: "冲突一" },
      { claim_id: "c2", subject: "冲突主语乙", predicate: "port", slot_key: "db",
        value: 5433, state: "conflict", confidence: 0.6, memory_id: "mem_bbb",
        memory_preview: "冲突二" },
    ],
  },
  "/api/v1/claims?state=superseded&limit=200": {
    scope: "shared", state: "superseded", counts: COUNTS,
    items: [
      { claim_id: "p1", subject: "谱系主语丙", predicate: "status", slot_key: "state",
        value: "old", state: "superseded", confidence: 0.4, memory_preview: "旧一" },
    ],
  },
  "/api/v1/facts?limit=500": { scope: "shared", items: [] },
  "/api/v1/skills?limit=200": { scope: "shared", items: [] },
};

globalThis.fetch = async (url) => {
  calls.push(String(url));
  const body = FIXTURES[String(url)];
  if (!body) {
    return { ok: false, status: 404, statusText: "Not Found",
             text: async () => JSON.stringify({ error: "no fixture: " + url }) };
  }
  return { ok: true, status: 200, statusText: "OK", text: async () => JSON.stringify(body) };
};

const mod = await import(pathToFileURL(process.argv[2]).href);

async function renderTab(tab) {
  calls.length = 0;
  const root = doc.createElement("div");
  const context = {
    container: root, params: new URLSearchParams("tab=" + tab), signal: undefined,
    refresh() {}, navigate() {}, setQuery() {}, onCleanup() {},
  };
  await mod.render(context);
  return { root, text: root.textContent, calls: calls.slice() };
}

/* 只回传文本与 fetch 参数：DOM 节点是环状结构，JSON.stringify 会炸，
   而且测试本来也只需要「渲染出了什么」。 */
const out = {};
for (const tab of ["conflicts", "superseded", "claims"]) {
  const rendered = await renderTab(tab);
  out[tab] = { text: rendered.text, calls: rendered.calls };
}
out.byTab = {
  conflicts: mod.claimsForTab({
    claims: { items: [{ state: "active" }, { state: "conflict" }] },
    conflicts: { items: [{ state: "conflict" }, { state: "active" }] },
    superseded: { items: [{ state: "superseded" }] },
  }, "conflicts").map((i) => i.state),
  superseded: mod.claimsForTab({
    claims: { items: [{ state: "active" }, { state: "conflict" }] },
    conflicts: { items: [{ state: "conflict" }] },
    superseded: { items: [{ state: "superseded" }, { state: "conflict" }] },
  }, "superseded").map((i) => i.state),
  claims: mod.claimsForTab({
    claims: { items: [{ state: "active" }, { state: "conflict" }] },
    conflicts: { items: [{ state: "conflict" }] },
    superseded: { items: [{ state: "superseded" }] },
  }, "claims").map((i) => i.state),
};
out.exportConflicts = mod.exportRows({
  claims: { items: [{ subject: "x", state: "active" }] },
  conflicts: { items: [{ subject: "c", state: "conflict" }] },
  superseded: { items: [{ subject: "s", state: "superseded" }] },
}, "conflicts").rows.map((r) => r.subject);
out.exportSuperseded = mod.exportRows({
  claims: { items: [{ subject: "x", state: "active" }] },
  conflicts: { items: [{ subject: "c", state: "conflict" }] },
  superseded: { items: [{ subject: "s", state: "superseded" }] },
}, "superseded").rows.map((r) => r.subject);
out.tabCounts = mod.tabCounts({
  claims: { counts: COUNTS, items: [] }, facts: { items: [] }, skills: { items: [] },
});
/* counts 缺失时退回「该页签自己的列表长度」。夹具必须给全三个列表：
   真实调用里它们来自三次独立的 /api/v1/claims?state=... 请求。 */
out.tabCountsNoCounts = mod.tabCounts({
  claims: { items: [{ state: "active" }, { state: "active" }] },
  conflicts: { items: [{ state: "conflict" }] },
  superseded: { items: [] },
  facts: { items: [] }, skills: { items: [] },
});
console.log(JSON.stringify(out));
"""


def run_all():
    """在 node 里把三个页签各渲染一次，返回 {tab: {text, calls}, ...}。

    一次 node 进程跑完三个页签：启动开销只付一次，而且三个页签共享同一份
    夹具，断言之间不会因为夹具漂移而对不上。
    """
    with tempfile.TemporaryDirectory(prefix="atlas-facts-") as tmp:
        tmp_path = Path(tmp)
        shim_path = tmp_path / "shim.mjs"
        shim_path.write_text(DOM_SHIM, encoding="utf-8")
        harness_path = tmp_path / "harness.mjs"
        harness_path.write_text(
            HARNESS % {"shim": json.dumps(str(shim_path))}, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(harness_path), str(FACTS_JS)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError(
                "node 渲染 facts 视图失败:\n" + (result.stderr or result.stdout)[-3000:])
        return json.loads(result.stdout.strip().splitlines()[-1])


def source_text():
    return FACTS_JS.read_text(encoding="utf-8")


#: JS 注释（块注释 + 行注释）。行注释要求前面是行首或空白——facts.js 里的 URL
#: 都出现在字符串里，不会命中。
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?m)(^|\s)//[^\n]*")


def strip_comments(source):
    """去掉 JS 注释，只留代码——静态判据必须扫代码，不能扫说明文字。"""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


@unittest.skipUnless(NODE, "node 不在 PATH；跳过 facts 视图的行为测试")
class FactsConflictTabTests(unittest.TestCase):
    """冲突页签必须显示真冲突，而不是被 superseded 顶掉。"""

    @classmethod
    def setUpClass(cls):
        cls.all = run_all()

    @property
    def result(self):
        return self.all["conflicts"]

    def test_conflict_tab_shows_only_conflict_rows(self):
        text = self.result["text"]
        self.assertIn("冲突主语甲", text, "冲突页签没有显示 state=conflict 的行")
        self.assertIn("冲突主语乙", text, "冲突页签没有显示第二条 state=conflict 的行")

    def test_conflict_tab_excludes_superseded(self):
        text = self.result["text"]
        # 页内那条 superseded（state=all 的第一页里有）绝不能再混进冲突页签。
        self.assertNotIn("页内历史主语", text,
                         "冲突页签混进了 superseded 行——这正是 G1 的原始 bug")
        self.assertNotIn("谱系主语丙", text,
                         "冲突页签混进了 state=superseded 列表里的行")

    def test_conflict_tab_offers_resolve_command(self):
        self.assertIn("裁决命令", self.result["text"],
                      "冲突页签必须保留可复制的 hippomind resolve 命令列")

    def test_conflict_tab_fetches_conflict_state_from_server(self):
        """只改前端过滤是修不好的：conflict 行根本不在那一页里。

        生产实测 /api/v1/claims?state=all&limit=200 的 conflict 行数 = 0
        （22 条 conflict 按 asserted_at 倒序全部排在页外），所以视图必须
        单独按 state=conflict 拉一次。
        """
        self.assertIn("/api/v1/claims?state=conflict&limit=200", self.result["calls"],
                      "冲突页签没有按 state=conflict 单独拉取；"
                      "只在前端过滤最新 200 行会让页签变空")

    def test_conflict_tab_count_comes_from_server_counts(self):
        """页签计数用 counts（全库口径），不是当前这一页的行数。

        用行数算出来的「冲突 0」和原始 bug 是同一类假象。
        """
        self.assertIn("冲突22", self.result["text"].replace(",", ""),
                      "冲突页签计数没有取 counts.conflict=22（可能取了当前页行数）")


@unittest.skipUnless(NODE, "node 不在 PATH；跳过 facts 视图的行为测试")
class FactsSupersededTabTests(unittest.TestCase):
    """取代谱系是历史，不是待办：独立页签、独立列表、不给裁决命令。"""

    @classmethod
    def setUpClass(cls):
        cls.all = run_all()

    @property
    def result(self):
        return self.all["superseded"]

    def test_superseded_tab_shows_superseded_rows(self):
        self.assertIn("谱系主语丙", self.result["text"],
                      "取代谱系页签没有显示 state=superseded 的行")

    def test_superseded_tab_excludes_conflict_rows(self):
        self.assertNotIn("冲突主语甲", self.result["text"],
                         "取代谱系页签混进了 state=conflict 的行（两个集合必须分开）")

    def test_superseded_tab_has_no_resolve_command(self):
        """superseded 是已定案的历史，对它跑 resolve 只是重放旧结论。"""
        self.assertNotIn("裁决命令", self.result["text"],
                         "取代谱系页签不应提供 hippomind resolve 命令列")

    def test_superseded_tab_fetches_superseded_state(self):
        self.assertIn("/api/v1/claims?state=superseded&limit=200", self.result["calls"],
                      "取代谱系页签没有按 state=superseded 单独拉取")

    def test_superseded_tab_has_own_tab_id(self):
        self.assertIn("取代谱系", self.result["text"],
                      "缺少独立的「取代谱系」页签")


@unittest.skipUnless(NODE, "node 不在 PATH；跳过 facts 视图的行为测试")
class FactsClaimsTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.all = run_all()

    def test_claims_tab_still_shows_all_states(self):
        text = self.all["claims"]["text"]
        self.assertIn("活跃主语", text, "Claims 页签不应只显示冲突")
        self.assertIn("页内历史主语", text, "Claims 页签应显示页内所有状态")


@unittest.skipUnless(NODE, "node 不在 PATH；跳过 facts 视图的行为测试")
class FactsExportMatchesTabTests(unittest.TestCase):
    """CSV 导出必须与屏幕上那个页签逐行一致。

    旧实现自己又写了一遍 state !== "active"：点「导出 CSV」拿到的是
    superseded，而屏幕标题写着「冲突」。
    """

    @classmethod
    def setUpClass(cls):
        cls.all = run_all()

    def test_export_conflicts_uses_conflict_list(self):
        self.assertEqual(["c"], self.all["exportConflicts"],
                         "导出「冲突」页签时没有用 state=conflict 的列表")

    def test_export_superseded_uses_superseded_list(self):
        self.assertEqual(["s"], self.all["exportSuperseded"],
                         "导出「取代谱系」页签时没有用 state=superseded 的列表")

    def test_tab_counts_read_server_counts(self):
        counts = self.all["tabCounts"]
        self.assertEqual(22, counts["conflicts"])
        self.assertEqual(1934, counts["superseded"])
        self.assertEqual(4771, counts["claims"])

    def test_tab_counts_fall_back_to_list_length(self):
        """旧 daemon 不返回 counts 时退回列表长度，绝不显示 NaN。"""
        counts = self.all["tabCountsNoCounts"]
        self.assertEqual(1, counts["conflicts"])
        self.assertEqual(0, counts["superseded"])


class FactsSourceContractTests(unittest.TestCase):
    """静态契约：钉住「两个页签语义分开」这件事本身，不依赖 node。

    GUI 前端是零依赖原生 ES 模块，没有构建步骤，因此这类契约只能靠源码扫描
    钉住（tests/test_gui_frontend.py 的 innerHTML 扫描是同一路数）。
    """

    def setUp(self):
        self.text = source_text()

    def test_old_combined_predicate_is_gone(self):
        """state !== "active" 是 G1 的根因，不允许再出现在 facts.js 的代码里。

        先剥掉注释再扫：文件里刻意保留了「旧代码长什么样」的说明，那是给下一个
        改这里的人看的，不能被自己的检查误伤。
        """
        self.assertNotIn(OLD_PREDICATE, strip_comments(self.text),
                         "facts.js 仍在用 state !== active 判断冲突——"
                         "它会把 superseded（历史）当成 conflict（待办）")

    def test_both_claim_states_are_named_explicitly(self):
        for state in ("conflict", "superseded"):
            with self.subTest(state=state):
                self.assertIn('"' + state + '"', self.text,
                              "facts.js 未显式引用 state=" + state)

    def test_both_tabs_request_their_own_state(self):
        """两个页签各自按 state 拉取——这是这个 bug 的真正修法。"""
        for state in ("conflict", "superseded"):
            with self.subTest(state=state):
                self.assertIn('state: "' + state + '"', self.text,
                              "facts.js 没有按 state=" + state + " 请求 claims")

    def test_export_and_render_share_one_projection(self):
        """导出与渲染必须共用 claimsForTab，否则两条路径会再次分叉。"""
        self.assertIn("exportRows", self.text)
        self.assertIn("claimsForTab", self.text)
        self.assertNotIn("rows.filter((item) => item.state", self.text,
                         "exportRows 里又出现了一份独立的 state 过滤——"
                         "导出与渲染分叉正是 G1 的成因")

    def test_truncation_is_disclosed(self):
        """counts 是全库口径、一页只有 200 行：不一致时必须写清楚。"""
        self.assertIn("truncationNote", self.text,
                      "facts.js 没有披露「只显示了最近 N 条」")


if __name__ == "__main__":
    unittest.main()
