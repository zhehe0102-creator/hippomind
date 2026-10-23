/* ============================================================================
   HippoMind Atlas — 10 reference 参考
   HTTP endpoint catalogue, local UI settings and version/about notes.
   Data: GET /api/v1/meta + core/store settings (browser local).
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import { store } from "../core/store.js";
import { fmtInt, duration, absTime, relTime } from "../core/format.js";
import {
  badge, card, dataTable, emptyState, errorState, skeleton, tabs, statRow,
  segmented, copyButton,
} from "../components.js";
import { icon } from "../icons.js";
import { openConnect } from "../connect.js";

export const meta = {
  id: "reference",
  index: "10",
  title: "参考",
  eyebrow: "REFERENCE",
  subtitle: "HTTP 接口、界面设置与版本信息",
};

const TAB_IDS = ["api", "settings", "about"];
const SHORTCUTS = [
  { keys: "1 – 0", description: "切换视图（01 脉搏 … 00 参考）" },
  { keys: "⌘K / /", description: "打开命令面板" },
  { keys: "R", description: "刷新当前视图与 daemon 状态" },
  { keys: "T", description: "切换纸本 / 夜档主题" },
  { keys: "Esc", description: "关闭命令面板、抽屉或连接面板" },
  { keys: "?", description: "在状态区提示快捷键" },
];

let current = { root: null, painted: false, meta: null, tab: "api" };

function apiBase() {
  return window.location.origin + "/api/v1";
}

function tokenBadge() {
  return api.hasToken()
    ? badge("已保存令牌", { tone: "ok" })
    : badge("未设置令牌", { tone: "warn" });
}

/* ------------------------------------------------------------------ helpers -- */

function methodTone(method) {
  const value = String(method || "").toUpperCase();
  if (value === "POST") return "accent";
  if (value === "GET") return "link";
  return "neutral";
}

function authTone(auth) {
  const value = String(auth || "").toLowerCase();
  if (value === "admin") return "accent";
  if (value === "none") return "neutral";
  if (value === "token") return "neutral";
  return "link";
}

function authLabel(auth) {
  const value = String(auth || "").toLowerCase();
  if (value === "admin") return "管理员令牌";
  if (value === "token") return "令牌";
  if (value === "none") return "无需令牌";
  if (value === "nonce") return "一次性 nonce";
  return value || "—";
}

function settingRow(label, hint, control) {
  return el("div", { class: "va-setting-row" },
    el("div", { class: "va-setting-label" },
      el("b", {}, label),
      hint ? el("span", {}, hint) : null),
    el("div", { class: "va-setting-control" }, control));
}

function codeValue(value) {
  return el("code", { class: "va-code-value", title: value }, value);
}

/* --------------------------------------------------------------------- api -- */

function apiTab(metaData) {
  const endpoints = Array.isArray(metaData.endpoints) ? metaData.endpoints : [];
  const columns = [
    {
      key: "method", label: "方法", width: 74,
      render: (row) => badge(String(row.method || "").toUpperCase(), { tone: methodTone(row.method) }),
    },
    { key: "path", label: "路径", mono: true },
    { key: "summary", label: "说明" },
    {
      key: "auth", label: "认证", width: 120,
      render: (row) => badge(authLabel(row.auth), { tone: authTone(row.auth) }),
    },
  ];
  const empty = emptyState({
    plain: true,
    icon: "reference",
    title: "没有接口目录",
    hint: "/api/v1/meta 未返回 endpoints 列表；确认 daemon 版本提供该端点。",
  });
  return el("div", { class: "va-ref-tab" },
    card({
      eyebrow: "TRANSPORT",
      title: "API 地址",
      actions: tokenBadge(),
      body: el("div", { class: "va-stack-tight" },
        el("div", { class: "va-api-row" },
          codeValue(apiBase()),
          copyButton(apiBase(), { label: "复制地址" })),
        el("div", { class: "va-api-row" },
          codeValue("X-HippoMind-Token: <token>"),
          copyButton("X-HippoMind-Token", { label: "复制头名" })),
        el("p", { class: "va-note" },
          "所有 /api/v1 接口要求本机管理员令牌；/health 无需令牌。令牌只保存在当前浏览器 localStorage，",
          "不写入页面、日志或 HTML。页面与 daemon 同源，因此无需 CORS 配置。")),
    }),
    card({
      eyebrow: "ENDPOINTS",
      title: "接口目录",
      actions: badge(fmtInt(endpoints.length) + " 个端点", { tone: "neutral" }),
      className: "va-card--flush-body",
      body: dataTable({ columns, rows: endpoints, dense: true, rowKey: "path", empty }),
    }),
    el("p", { class: "va-note" },
      "写接口仅限置顶与反馈；遗忘、裁决等危险操作请使用可复制的 CLI 命令，界面不直接执行。"));
}

/* ---------------------------------------------------------------- settings -- */

function applyTheme(value) {
  store.setSetting("theme", value);
  document.documentElement.dataset.theme = value;
  const button = document.getElementById("theme-btn");
  if (button) {
    button.title = value === "night" ? "切换到纸本浅色 (T)" : "切换到夜档深色 (T)";
  }
}

function settingsTab(context) {
  const settings = store.get("settings");
  const theme = settings.theme === "night" ? "night" : "paper";
  const refreshSeconds = Math.round((Number(settings.refreshMs) || 30000) / 1000);
  const pageSize = Number(settings.pageSize) || 50;

  const themeControl = segmented({
    options: [{ value: "paper", label: "纸本 paper" }, { value: "night", label: "夜档 night" }],
    value: theme,
    label: "主题",
    onChange: (value) => {
      applyTheme(value);
      context.toast("主题已切换为" + (value === "night" ? "夜档" : "纸本"), { tone: "ok", ms: 1600 });
    },
  });

  const refreshControl = segmented({
    options: [
      { value: "15", label: "15 秒" },
      { value: "30", label: "30 秒" },
      { value: "60", label: "60 秒" },
    ],
    value: String(refreshSeconds),
    label: "刷新间隔",
    onChange: (value) => {
      store.setSetting("refreshMs", Number(value) * 1000);
      context.toast("刷新间隔已设为 " + value + " 秒", { tone: "ok", ms: 1600 });
    },
  });

  const pageSizeControl = segmented({
    options: [
      { value: "25", label: "25 条" },
      { value: "50", label: "50 条" },
      { value: "100", label: "100 条" },
    ],
    value: String(pageSize),
    label: "每页条数",
    onChange: (value) => {
      store.setSetting("pageSize", Number(value));
      context.toast("每页条数已设为 " + value + " 条", { tone: "ok", ms: 1600 });
    },
  });

  const changeToken = el("button", { class: "atlas-btn", type: "button" },
    icon("info", { size: 13 }), el("span", {}, "更换令牌"));
  changeToken.addEventListener("click", () => {
    context.toast("输入新令牌后将覆盖当前浏览器的令牌", { tone: "info", ms: 2200 });
    openConnect();
  });

  const clearToken = el("button", { class: "atlas-btn atlas-btn--danger", type: "button" },
    icon("close", { size: 13 }), el("span", {}, "清除本机令牌"));
  clearToken.addEventListener("click", async () => {
    const ok = await context.confirm({
      title: "清除本机令牌",
      body: "清除后本页面将无法读取账本，直到重新粘贴令牌。令牌本身仍保存在 daemon 的令牌文件里。",
      confirmLabel: "清除令牌",
      danger: true,
    });
    if (!ok) return;
    api.clearToken();
    context.toast("已清除本机令牌", { tone: "ok" });
    openConnect();
  });

  const ledgerId = current.meta && current.meta.ledger_id ? String(current.meta.ledger_id) : "";

  const preferences = el("div", { class: "va-settings" },
    settingRow("主题", "纸本为默认；夜档为低光环境准备", themeControl),
    settingRow("刷新间隔", "daemon 状态与当前视图的心跳周期", refreshControl),
    settingRow("每页条数", "记忆库 / 检索等分页视图的默认每页数量", pageSizeControl),
    settingRow("访问令牌", "令牌只保存在当前浏览器 localStorage",
      el("div", { class: "va-inline-actions" }, tokenBadge(), changeToken, clearToken)));

  const ledger = el("div", { class: "va-settings" },
    settingRow("API 地址", "同源 daemon 的只读 API 基地址",
      el("div", { class: "va-inline-actions" },
        codeValue(apiBase()),
        copyButton(apiBase(), { label: "复制" }))),
    settingRow("ledger_id", "账本的身份指纹（daemon 计算）",
      el("div", { class: "va-inline-actions" },
        ledgerId ? codeValue(ledgerId) : el("span", { class: "muted" }, "—"),
        ledgerId ? copyButton(ledgerId, { label: "复制" }) : null)));

  return el("div", { class: "va-ref-tab" },
    card({ eyebrow: "PREFERENCES", title: "界面设置", body: preferences }),
    card({ eyebrow: "LEDGER", title: "账本与地址", body: ledger }));
}

/* ------------------------------------------------------------------- about -- */

function aboutTab(metaData) {
  const ui = metaData.ui || {};
  const versionStats = statRow([
    { label: "界面", value: (ui.name || "HippoMind Atlas") + " · v" + (ui.version || "?") },
    { label: "server_version", value: metaData.server_version || "—" },
    { label: "schema_version", value: "v" + (metaData.schema_version || "—") },
    { label: "ledger_id", value: metaData.ledger_id || "—" },
    { label: "启动时间", value: metaData.started_at ? absTime(metaData.started_at, { seconds: true }) : "—" },
    { label: "运行时长", value: duration(metaData.uptime_s) },
    { label: "pid", value: fmtInt(metaData.pid) },
    { label: "API 版本", value: metaData.api_version || "v1" },
  ]);
  const limits = metaData.limits || {};
  const limitStats = statRow([
    {
      label: "memories_page",
      value: Array.isArray(limits.memories_page) ? limits.memories_page.join(" – ") : "—",
    },
    { label: "content_preview", value: fmtInt(limits.content_preview) + " 字符" },
    { label: "graph_nodes", value: fmtInt(limits.graph_nodes) },
  ]);

  const shortcuts = el("div", { class: "va-shortcuts" });
  for (const entry of SHORTCUTS) {
    shortcuts.appendChild(el("div", { class: "va-shortcut-row" },
      el("kbd", {}, entry.keys),
      el("span", {}, entry.description)));
  }

  const paragraphs = el("div", { class: "va-about-prose" });
  paragraphs.appendChild(el("p", {},
    "HippoMind Atlas 是记忆账本的纸本档案馆界面：浅色纸本为默认、夜档为可选项；",
    "衬线大标题、等宽数据、索引编号与 1px 细线构成信息层级，朱砂色只标记实时与关键信号。"));
  paragraphs.appendChild(el("p", {},
    "前端零构建、零第三方依赖：原生 ES Modules + 同源 daemon 静态服务；",
    "所有接口字符串只经文本节点进入 DOM，不拼接 innerHTML。"));
  paragraphs.appendChild(el("p", {},
    "写操作被限制在置顶与反馈；遗忘、裁决等危险操作只展示可复制的 CLI 命令。",
    "设计契约与 API 细节见仓库 docs/GUI_ATLAS.md。"));
  paragraphs.appendChild(el("p", { class: "muted" },
    "账本 " + (metaData.ledger_id ? String(metaData.ledger_id).slice(0, 12) + "…" : "—"),
    " · 本页渲染于 " + relTime(new Date().toISOString())));

  const versionCard = card({
    eyebrow: "ABOUT",
    title: "版本",
    actions: badge("本地优先", { tone: "ok" }),
    body: el("div", { class: "va-about-grid" },
      versionStats,
      el("div", { class: "va-about-side" },
        el("div", { class: "va-about-title" }, "界面上限"),
        limitStats)),
  });
  const keyboardCard = card({ eyebrow: "KEYBOARD", title: "快捷键", body: shortcuts });
  const designCard = card({ eyebrow: "DESIGN", title: "设计说明", body: paragraphs });

  return el("div", { class: "va-ref-tab" },
    versionCard,
    el("div", { class: "atlas-grid atlas-grid--2 va-start-grid" }, keyboardCard, designCard));
}

/* ------------------------------------------------------------------- paint -- */

function paint(root, metaData, context) {
  clear(root);
  current.meta = metaData || {};
  const endpoints = Array.isArray(current.meta.endpoints) ? current.meta.endpoints : [];
  const host = el("div", { class: "va-ref-body" });

  function renderTab(id) {
    current.tab = TAB_IDS.indexOf(id) >= 0 ? id : "api";
    clear(host);
    if (current.tab === "settings") host.appendChild(settingsTab(context));
    else if (current.tab === "about") host.appendChild(aboutTab(current.meta));
    else host.appendChild(apiTab(current.meta));
  }

  const tabBar = tabs({
    items: [
      { id: "api", label: "接口", count: endpoints.length },
      { id: "settings", label: "设置" },
      { id: "about", label: "关于" },
    ],
    active: TAB_IDS.indexOf(current.tab) >= 0 ? current.tab : "api",
    onChange: (id) => renderTab(id),
  });

  root.appendChild(el("div", { class: "va-ref-meta" },
    el("span", { class: "mono" }, (current.meta.ui && current.meta.ui.name) || "HippoMind Atlas"),
    el("span", {}, "server " + (current.meta.server_version || "—")),
    el("span", {}, "schema v" + (current.meta.schema_version || "—")),
    el("span", { class: "muted" }, fmtInt(endpoints.length) + " endpoints"),
    el("span", { class: "muted" }, "uptime " + duration(current.meta.uptime_s))));
  root.appendChild(tabBar);
  root.appendChild(host);
  renderTab(current.tab);
  current.painted = true;
}

/* ------------------------------------------------------------------- entry -- */

export async function render(context) {
  const root = el("div", { class: "va-view va-reference" });
  current = { root, painted: false, meta: null, tab: "api" };
  root.appendChild(skeleton(4));
  try {
    const metaPayload = await api.get("/api/v1/meta", null, { signal: context.signal });
    if (context.signal.aborted) return root;
    paint(root, metaPayload || {}, context);
  } catch (error) {
    if (error && error.name === "AbortError") return root;
    clear(root);
    root.appendChild(errorState({ title: "参考加载失败", error, onRetry: () => context.refresh() }));
  }
  return root;
}

export async function refresh(context) {
  const root = current.root;
  if (!root || !root.isConnected) return;
  try {
    const metaPayload = await api.get("/api/v1/meta", null, { signal: context.signal });
    if (context.signal.aborted) return;
    paint(root, metaPayload || {}, context);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (current.painted) context.toast("参考刷新失败：" + String((error && (error.detail || error.message)) || "未知错误"), { tone: "warn" });
    else {
      clear(root);
      root.appendChild(errorState({ title: "参考加载失败", error, onRetry: () => context.refresh() }));
    }
  }
}

