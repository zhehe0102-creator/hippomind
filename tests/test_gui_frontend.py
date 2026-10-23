"""HippoMind Atlas 前端静态完整性测试（只读、无网络）。

对应 docs/GUI_ATLAS.md 第 7 节第 4 条与第 6 节架构契约：

- index.html 引用的每个资源必须存在，且无内联脚本、无 http(s) 外链；
- hippomind/web/js 下每个 ES 模块必须通过 node --check；
- registry.js 的 10 个视图 id 与契约一致，每个视图模块导出 meta/render；
- 动态文本不得拼进 innerHTML（只允许无插值的静态字面量）；
- 无 eval / new Function / 内联事件属性；契约组件导出名齐全；
- 静态模块图的相对 import 必须能解析到真实文件。

测试只读，不启动 daemon、不访问网络。缺少文件时用 subTest 给出可操作的
缺失说明，便于集成期定位 owner（shell / views-a / views-b / lead）。
"""
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "hippomind" / "web"
NODE = shutil.which("node")

VIEW_IDS = [
    "pulse", "archive", "recall", "compose", "graph",
    "facts", "timeline", "agents", "health", "reference",
]

VIEW_OWNER = {
    "pulse": "views-a", "timeline": "views-a", "agents": "views-a",
    "health": "views-a", "reference": "views-a",
    "archive": "views-b", "recall": "views-b", "compose": "views-b",
    "graph": "views-b", "facts": "views-b",
}

REQUIRED_DOM_IDS = [
    "app", "rail", "nav", "topbar", "view-root", "statusbar",
    "drawer-root", "palette-root", "toast-root", "connect-root",
    "view-eyebrow", "view-title", "view-subtitle",
    "search-trigger", "refresh-btn", "theme-btn",
]

CONTRACT_FILES = [
    "index.html",
    "favicon.svg",
    "css/tokens.css",
    "css/shell.css",
    "css/components.css",
    "css/views-a.css",
    "css/views-b.css",
    "js/core/dom.js",
    "js/core/format.js",
    "js/core/api.js",
    "js/core/store.js",
    "js/core/router.js",
    "js/components.js",
    "js/charts.js",
    "js/icons.js",
    "js/palette.js",
    "js/connect.js",
    "js/app.js",
    "js/views/registry.js",
    "js/components/inspector.js",
    "js/components/graph-canvas.js",
] + ["js/views/" + view + ".js" for view in VIEW_IDS]

CORE_EXPORTS = {
    "js/core/dom.js": ["el", "svg", "$", "$$", "clear", "esc", "delegate"],
    "js/core/format.js": [
        "fmtInt", "fmtCompact", "fmtBytes", "fmtPct", "fmtMs", "relTime",
        "absTime", "shortId", "truncate", "titleCase", "duration",
    ],
    "js/core/api.js": ["api", "ApiError", "TOKEN_KEY"],
    "js/core/store.js": ["store"],
    "js/core/router.js": ["parse", "query", "href", "navigate", "start", "router"],
}

SHELL_EXPORTS = {
    "js/components.js": [
        "badge", "chip", "metric", "card", "dataTable", "pager", "emptyState",
        "skeleton", "errorState", "meter", "copyButton", "toast", "confirm",
        "drawer", "tabs", "textField", "selectField", "toggleField",
        "segmented", "statRow", "rule",
    ],
    "js/charts.js": ["sparkline", "barList", "donut", "stackedBars", "legend"],
    "js/icons.js": ["icon"],
    "js/palette.js": ["initPalette", "openPalette", "closePalette"],
    "js/connect.js": ["initConnect", "openConnect", "closeConnect"],
}

ICON_NAMES = [
    "pulse", "archive", "recall", "compose", "graph", "facts", "timeline",
    "agents", "health", "reference", "search", "refresh", "theme", "copy",
    "check", "close", "chevron-left", "chevron-right", "arrow-up", "arrow-down",
    "pin", "feedback-up", "feedback-down", "external", "filter", "plus",
    "minus", "warning", "error", "info", "menu",
]

PAPER_TOKENS = {
    "--paper": "#f2eee4",
    "--paper-2": "#e9e3d5",
    "--surface": "#fbf8f0",
    "--ink": "#1a1916",
    "--ink-2": "#4d483d",
    "--ink-3": "#7c7466",
    "--rule": "#d9d1bf",
    "--rule-strong": "#bcb29c",
    "--accent": "#c4471e",
    "--link": "#1e43b2",
    "--ok": "#2e6a4d",
    "--warn": "#9a640f",
    "--danger": "#9e2626",
    "--highlight": "#f1dea6",
}

NIGHT_TOKENS = {
    "--paper": "#14120e",
    "--paper-2": "#1b1812",
    "--surface": "#1f1b14",
    "--ink": "#f2ebd9",
    "--ink-2": "#ccc2ac",
    "--ink-3": "#968b74",
    "--rule": "#332d22",
    "--accent": "#f06b3a",
    "--link": "#8fa8ff",
    "--ok": "#6fbf97",
    "--warn": "#e0a74c",
    "--danger": "#e07070",
}

OLD_OBSERVATORY_FILES = ["app.css", "app.js"]

EXTERNAL_URL_RE = re.compile(r"""(?:\b(?:href|src)\s*=\s*["']\s*(?:https?:)?//)|"""
                             r"""(?:url\(\s*["']?\s*(?:https?:)?//)|"""
                             r"""(?:\bfrom\s*["'](?:https?:)?//)|"""
                             r"""(?:import\(\s*["'](?:https?:)?//)""")
EXPORT_DECL_RE = re.compile(
    r"export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)")
EXPORT_LIST_RE = re.compile(r"export\s*\{([^}]*)\}")
INNER_HTML_RE = re.compile(r"\.innerHTML\s*(\+?=)\s*([^\n;]*)")
IMPORT_FROM_RE = re.compile(r"""\bfrom\s*["']([^"']+)["']""")
IMPORT_CALL_RE = re.compile(r"""import\(\s*["']([^"']+)["']\s*\)""")
INLINE_EVENT_RE = re.compile(r"^on[a-z]+$")


def read_text(path):
    return path.read_text(encoding="utf-8")


def web_path(relative_path):
    return WEB / relative_path


def is_appledouble(path):
    return path.name.startswith("._")


def js_files():
    return sorted(path for path in WEB.rglob("*.js") if not is_appledouble(path))


def relative(path):
    try:
        return str(path.relative_to(WEB))
    except ValueError:
        return str(path)


def line_of(text, offset):
    line = text.count("\n", 0, offset) + 1
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return line, text[start:end].strip()[:180]


def exported_names(text):
    names = set(EXPORT_DECL_RE.findall(text))
    for block in EXPORT_LIST_RE.findall(text):
        for piece in block.split(","):
            piece = piece.strip()
            if not piece:
                continue
            names.add(piece.split(" as ")[-1].strip())
    return names


def is_static_literal(expression):
    """Return True only for a single static string literal without interpolation."""
    expression = expression.strip()
    if not expression:
        return False
    if expression[:1] not in {"'", '"'}:
        return False
    if chr(96) in expression or ("$" + "{") in expression:
        return False
    if "+" in expression:
        return False
    if expression[-1:] != expression[:1]:
        return False
    return expression.count(expression[:1]) >= 2


class IndexParser(HTMLParser):
    """Collect tag/attrs with source line numbers from index.html."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.ids = set()
        self.inline_script_text = []
        self._script_attrs = None

    def handle_starttag(self, tag, attrs):
        attributes = {name.lower(): (value if value is not None else "") for name, value in attrs}
        self.elements.append((tag, attributes, self.getpos()[0]))
        if "id" in attributes:
            self.ids.add(attributes["id"])
        if tag == "script":
            self._script_attrs = attributes

    def handle_data(self, data):
        if self._script_attrs is not None and data.strip():
            self.inline_script_text.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "script":
            self._script_attrs = None


class GuiFrontendStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_path = web_path("index.html")
        cls.index_text = read_text(cls.index_path) if cls.index_path.exists() else ""
        cls.parser = IndexParser()
        if cls.index_text:
            cls.parser.feed(cls.index_text)

    def test_index_html_exists_and_parses(self):
        self.assertTrue(self.index_path.exists(), "缺少 hippomind/web/index.html（shell 负责）")
        self.assertGreater(len(self.parser.ids), 10, "index.html 解析出的 id 太少，文件可能损坏")

    def test_contract_files_exist(self):
        missing = []
        for rel_file in CONTRACT_FILES:
            with self.subTest(file=rel_file):
                path = web_path(rel_file)
                if not path.exists():
                    missing.append(rel_file)
                    self.fail("缺少契约文件（集成未完成，请找 owner 补齐）: " + rel_file)
        self.assertEqual(missing, [], "契约文件缺失: " + ", ".join(missing))

    def test_index_required_dom_ids(self):
        for element_id in REQUIRED_DOM_IDS:
            with self.subTest(element_id=element_id):
                self.assertIn(
                    element_id, self.parser.ids,
                    "index.html 缺少契约要求的 id=#" + element_id
                    + "（docs/GUI_ATLAS.md §6.4，shell 负责）")

    def test_index_assets_exist_and_are_local(self):
        module_scripts = 0
        for tag, attributes, line in self.parser.elements:
            if tag == "link" and attributes.get("href"):
                href = attributes["href"].strip()
                if href.startswith("#") or href.startswith("data:"):
                    continue
                with self.subTest(tag=tag, line=line):
                    self.assertFalse(
                        re.match(r"^(?:https?:)?//", href),
                        "index.html:%d 出现外链样式/资源: %s（契约禁止外链）" % (line, href))
                    self.assertTrue(
                        web_path(href).exists(),
                        "index.html:%d 引用了不存在的文件: %s" % (line, href))
            elif tag == "script":
                source = attributes.get("src", "").strip()
                script_type = attributes.get("type", "")
                if script_type == "module":
                    module_scripts += 1
                with self.subTest(tag=tag, line=line):
                    self.assertTrue(source, "index.html:%d 存在内联 <script>（契约禁止内联脚本）" % line)
                    self.assertFalse(
                        re.match(r"^(?:https?:)?//", source),
                        "index.html:%d 出现外链脚本: %s" % (line, source))
                    if source:
                        self.assertTrue(
                            web_path(source).exists(),
                            "index.html:%d 引用了不存在的脚本: %s" % (line, source))
        self.assertEqual(self.parser.inline_script_text, [], "index.html 不应包含带内容的内联 script 块")
        self.assertGreaterEqual(module_scripts, 1, "index.html 必须用 type=module 加载 js/app.js")

    def test_index_has_no_inline_event_handlers(self):
        for tag, attributes, line in self.parser.elements:
            for name in attributes:
                with self.subTest(tag=tag, attr=name, line=line):
                    self.assertIsNone(
                        INLINE_EVENT_RE.match(name),
                        "index.html:%d <%s> 使用内联事件属性 %s=，CSP script-src 'self' 会拦截"
                        % (line, tag, name))

    def test_old_observatory_assets_removed(self):
        for name in OLD_OBSERVATORY_FILES:
            with self.subTest(file=name):
                self.assertFalse(
                    web_path(name).exists(),
                    "旧 Observatory 资源仍存在: hippomind/web/" + name
                    + "（task-2 要求先删除旧设计再重建）")

    def test_no_external_resources_in_web_assets(self):
        targets = [("index.html", self.index_text)]
        for path in sorted(WEB.rglob("*")):
            if not path.is_file() or is_appledouble(path):
                continue
            if path.suffix not in {".js", ".css", ".html", ".svg"}:
                continue
            if path.name == "index.html":
                continue
            targets.append((relative(path), read_text(path)))
        for name, text in targets:
            for match in EXTERNAL_URL_RE.finditer(text):
                line, snippet = line_of(text, match.start())
                with self.subTest(file=name, line=line):
                    self.fail("%s:%d 引用外部资源（契约禁止 http(s) 外链）: %s"
                              % (name, line, snippet))

    @unittest.skipUnless(NODE, "node 不在 PATH；跳过 ES 模块语法检查")
    def test_js_modules_pass_node_check(self):
        files = js_files()
        self.assertTrue(files, "hippomind/web/js 下没有任何 .js 文件")
        for path in files:
            with self.subTest(file=relative(path)):
                result = subprocess.run(
                    [NODE, "--check", str(path)],
                    capture_output=True, text=True, timeout=60,
                )
                detail = (result.stderr or result.stdout or "").strip()
                self.assertEqual(
                    result.returncode, 0,
                    "node --check 失败: %s\n%s" % (relative(path), detail[-1200:]))

    def test_module_import_graph_resolves(self):
        for path in js_files():
            text = read_text(path)
            specs = IMPORT_FROM_RE.findall(text) + IMPORT_CALL_RE.findall(text)
            for spec in specs:
                if not spec.startswith("."):
                    continue
                target = (path.parent / spec).resolve()
                with self.subTest(file=relative(path), import_spec=spec):
                    self.assertTrue(
                        target.is_file(),
                        "%s 的相对 import 指向不存在的文件: %s" % (relative(path), spec))

    def test_registry_view_contract(self):
        path = web_path("js/views/registry.js")
        self.assertTrue(path.exists(), "缺少 js/views/registry.js（lead 负责）")
        text = read_text(path)
        ids = re.findall(r"""\bid:\s*["']([a-z_]+)["']""", text)
        self.assertEqual(
            ids, VIEW_IDS,
            "registry.js 视图顺序/数量与契约不一致: %r（契约: %r）" % (ids, VIEW_IDS))
        imports = re.findall(r"""import\(\s*["']\./([a-z]+)\.js["']\s*\)""", text)
        self.assertEqual(
            imports, VIEW_IDS,
            "registry.js 的 load() 动态导入顺序与契约不一致: %r" % (imports,))
        self.assertIn("export const VIEWS", text, "registry.js 必须导出 VIEWS")
        self.assertIn("export function viewById", text, "registry.js 必须导出 viewById")

    def test_view_modules_export_meta_and_render(self):
        for view in VIEW_IDS:
            path = web_path("js/views/" + view + ".js")
            owner = VIEW_OWNER[view]
            with self.subTest(view=view, owner=owner):
                if not path.exists():
                    self.fail("缺少视图模块 js/views/%s.js（owner=%s）" % (view, owner))
                text = read_text(path)
                self.assertRegex(
                    text, r"export\s+const\s+meta\b",
                    "js/views/%s.js 必须 export const meta（owner=%s）" % (view, owner))
                self.assertTrue(
                    re.search(r"export\s+async\s+function\s+render\b", text)
                    or re.search(r"export\s+function\s+render\b", text),
                    "js/views/%s.js 必须 export (async) function render（owner=%s）" % (view, owner))
                for field, pattern in (
                        ("id", r"""\bid:\s*["']%s["']""" % re.escape(view)),
                        ("index", r"""\bindex:\s*["']\d+["']"""),
                        ("title", r"""\btitle:\s*["'][^"']+["']"""),
                        ("eyebrow", r"""\beyebrow:\s*["'][A-Z]+["']"""),
                ):
                    with self.subTest(view=view, field=field):
                        self.assertRegex(
                            text, pattern,
                            "js/views/%s.js 的 meta 缺少/格式不符字段 %s" % (view, field))

    def test_no_innerhtml_with_data(self):
        allowed = {
            ("js/core/dom.js", 'node.innerHTML = String(value)'):
                "core/dom.js 的 html 属性是唯一受审计的静态标记边界",
        }
        violations = []
        for path in js_files():
            name = relative(path)
            text = read_text(path)
            for match in INNER_HTML_RE.finditer(text):
                operator, expression = match.group(1), match.group(2).strip()
                line, snippet = line_of(text, match.start())
                key = (name, snippet.rstrip(";"))
                if key in allowed:
                    continue
                if operator == "+=":
                    violations.append((name, line, "innerHTML += 追加动态内容", snippet))
                    continue
                if not is_static_literal(expression):
                    violations.append((name, line, "innerHTML 赋值为动态表达式", snippet))
            for pattern, label in (
                    (r"insertAdjacentHTML\s*\(", "insertAdjacentHTML 插入动态标记"),
                    (r"\bouterHTML\s*=", "outerHTML 赋值"),
                    (r"document\.write\s*\(", "document.write"),
            ):
                for match in re.finditer(pattern, text):
                    line, snippet = line_of(text, match.start())
                    violations.append((name, line, label, snippet))
        if violations:
            detail = "\n".join(
                "%s:%d  %s\n    %s" % (name, line, label, snippet)
                for name, line, label, snippet in violations[:20])
            self.fail("发现把数据拼进 HTML 的写法（改用 el()/textContent/esc，契约 §6.1）：\n" + detail)

    def test_no_eval_and_no_dynamic_function(self):
        violations = []
        for path in js_files():
            name = relative(path)
            text = read_text(path)
            for pattern, label in (
                    (r"(?<![\w$])eval\s*\(", "eval()"),
                    (r"new\s+Function\s*\(", "new Function()"),
                    (r"setTimeout\s*\(\s*[\"']", "setTimeout 字符串形式"),
                    (r"setInterval\s*\(\s*[\"']", "setInterval 字符串形式"),
            ):
                for match in re.finditer(pattern, text):
                    line, snippet = line_of(text, match.start())
                    violations.append((name, line, label, snippet))
        if violations:
            detail = "\n".join(
                "%s:%d  %s\n    %s" % (name, line, label, snippet)
                for name, line, label, snippet in violations[:20])
            self.fail("CSP script-src 'self' 下禁止动态代码执行：\n" + detail)

    def test_component_contract_exports(self):
        for rel_file, names in list(CORE_EXPORTS.items()) + list(SHELL_EXPORTS.items()):
            path = web_path(rel_file)
            with self.subTest(file=rel_file):
                if not path.exists():
                    self.fail("缺少契约模块 %s（无法校验导出名）" % rel_file)
                available = exported_names(read_text(path))
                missing = sorted(set(names) - available)
                self.assertEqual(
                    missing, [],
                    "%s 缺少契约导出: %s（实际导出: %s）"
                    % (rel_file, ", ".join(missing), ", ".join(sorted(available))[:600]))

    def test_icon_name_set_covers_contract(self):
        path = web_path("js/icons.js")
        self.assertTrue(path.exists(), "缺少 js/icons.js（shell 负责）")
        text = read_text(path)
        missing = []
        for name in ICON_NAMES:
            pattern = r"(?m)^\s*[\"']?" + re.escape(name) + r"[\"']?\s*:\s*\["
            if not re.search(pattern, text):
                missing.append(name)
        self.assertEqual(missing, [], "js/icons.js 缺少契约图标名: " + ", ".join(missing))

    def test_tokens_css_contract(self):
        path = web_path("css/tokens.css")
        self.assertTrue(path.exists(), "缺少 css/tokens.css（shell 负责）")
        text = read_text(path).lower()
        night_marker = 'data-theme="night"'
        paper_block = text[:text.find(night_marker)] if night_marker in text else text
        for token, value in PAPER_TOKENS.items():
            with self.subTest(theme="paper", token=token):
                self.assertRegex(
                    paper_block,
                    re.escape(token) + r"\s*:\s*" + re.escape(value) + r"\s*;",
                    "tokens.css 的 paper 主题缺少/不符合契约 token: %s: %s" % (token, value))
        night_match = re.search(
            r"""\[\s*data-theme\s*=\s*["']night["']\s*\][^{]*\{(.*?)\n\}""", text, re.S)
        self.assertIsNotNone(night_match, "tokens.css 缺少 html[data-theme=\"night\"] 主题块")
        night_block = night_match.group(1) if night_match else ""
        for token, value in NIGHT_TOKENS.items():
            with self.subTest(theme="night", token=token):
                self.assertRegex(
                    night_block,
                    re.escape(token) + r"\s*:\s*" + re.escape(value) + r"\s*;",
                    "tokens.css 的 night 主题缺少/不符合契约 token: %s: %s" % (token, value))

    def test_favicon_is_inline_svg_without_external_refs(self):
        path = web_path("favicon.svg")
        self.assertTrue(path.exists(), "缺少 hippomind/web/favicon.svg（shell 负责）")
        text = read_text(path)
        self.assertIn("<svg", text, "favicon.svg 必须是内联 SVG")
        # xmlns="http://www.w3.org/2000/svg" is an XML namespace identifier,
        # not a network fetch; strip declarations before the external-URL check.
        stripped = re.sub(r"""xmlns(?::[a-z]+)?\s*=\s*["'][^"']*["']""", "", text)
        self.assertNotRegex(stripped, r"https?://", "favicon.svg 不应引用外部资源")


if __name__ == "__main__":
    unittest.main()

