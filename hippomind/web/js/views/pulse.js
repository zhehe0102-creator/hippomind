/* ============================================================================
   HippoMind Atlas — 01 pulse 脉搏
   The ledger's vital signs: scale, composition, growth and health signals.
   Data: GET /api/v1/overview?days=30
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import {
  fmtInt, fmtCompact, fmtBytes, fmtPct, relTime, absTime, duration, truncate,
} from "../core/format.js";
import {
  badge, card, metric, emptyState, errorState, skeleton, copyButton,
} from "../components.js";
import { sparkline, barList } from "../charts.js";
import { icon } from "../icons.js";

export const meta = {
  id: "pulse",
  index: "01",
  title: "脉搏",
  eyebrow: "PULSE",
  subtitle: "账本的实时体征：规模、构成、增长与健康信号",
};

const OVERVIEW_DAYS = 30;
const TONES = ["accent", "link", "ok", "warn", "neutral", "danger"];
const WAL_WARN_BYTES = 64 * 1024 * 1024;

let current = { root: null, painted: false };

function toneAt(index) {
  return TONES[Math.abs(index) % TONES.length];
}

function severityTone(severity) {
  if (severity === "error") return "danger";
  if (severity === "warn") return "warn";
  return "ok";
}

function dot(tone) {
  return el("span", { class: "va-dot tone-" + tone });
}

function sortedPairs(record, limit) {
  return Object.entries(record || {})
    .filter(([, value]) => Number(value) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, limit)
    .map(([label, value], index) => ({ label, value: Number(value), tone: toneAt(index) }));
}

function apiBase() {
  return window.location.origin + "/api/v1";
}

/* ------------------------------------------------------------------ hero -- */

function heroSection(data) {
  const server = data.server || {};
  const growth = data.growth || {};
  const ledger = data.ledger || {};
  const buckets = Array.isArray(growth.buckets) ? growth.buckets : [];
  const values = buckets.map((bucket) => Number(bucket.memories) || 0);
  const lastWeek = values.slice(-7).reduce((sum, value) => sum + value, 0);
  const priorWeek = values.slice(-14, -7).reduce((sum, value) => sum + value, 0);
  const delta = priorWeek > 0 ? (lastWeek - priorWeek) / priorWeek : null;
  const days = Number(growth.days) || OVERVIEW_DAYS;

  const lead = el("div", { class: "va-pulse-lead" },
    el("div", { class: "eyebrow" }, "ACTIVE MEMORIES · 当前"),
    metric({
      label: "活跃结构记忆",
      value: fmtInt(ledger.active),
      hero: true,
      delta,
      hint: "账本共 " + fmtInt(ledger.memories) + " 条 · raw 证据 " + fmtInt(ledger.raw_chat) + " 条不参与统计",
    }),
    el("div", { class: "va-pulse-spark" },
      el("div", { class: "va-pulse-spark-head" },
        el("span", { class: "eyebrow" }, "每日入档"),
        el("span", { class: "va-pulse-spark-total" },
          el("b", {}, fmtInt(growth.total_memories)),
          el("span", { class: "muted" }, " 条 / 近 " + fmtInt(days) + " 天"))),
      sparkline({
        values,
        width: 560,
        height: 62,
        tone: "accent",
        label: "近 " + fmtInt(days) + " 天每日入档走势",
        title: "每日结构化记忆入档数",
      })));

  const figures = el("div", { class: "va-pulse-figures" },
    metric({
      label: "EVENTS · 事件",
      value: fmtCompact(ledger.events),
      hint: "近 " + fmtInt(days) + " 天 +" + fmtCompact(growth.total_events) + " 条",
    }),
    metric({
      label: "MEMORY TOTAL · 总量",
      value: fmtCompact(ledger.memories),
      hint: "active " + fmtInt(ledger.active) + " · superseded " + fmtInt(ledger.superseded),
    }),
    metric({
      label: "UPTIME · 运行",
      value: duration(server.uptime_s),
      small: true,
      hint: "v" + (server.server_version || "?") + " · pid " + fmtInt(server.pid),
    }));

  return el("section", { class: "va-pulse-hero", "aria-label": "脉搏头条" }, lead, figures);
}

/* ---------------------------------------------------------- status strip -- */

function statusStrip(data, context) {
  const server = data.server || {};
  const watcher = data.watcher || {};
  const items = [];

  function item(label, detail, tone, titleText) {
    items.push(el("button", {
      class: "va-status-item",
      type: "button",
      title: titleText || (label + " · " + detail),
      on: { click: () => context.navigate("/health") },
    },
      dot(tone),
      el("span", { class: "va-status-label" }, label),
      el("span", { class: "va-status-detail" }, detail)));
  }

  item("daemon", "在线 · v" + (server.server_version || "?"), "ok",
    "daemon 运行中（pid " + fmtInt(server.pid) + "）");
  item(
    "watcher",
    watcher.available ? "轮询 " + fmtInt(watcher.poll_interval) + "s" : "不可用",
    watcher.available ? "ok" : "warn",
    watcher.available ? "归档 watcher 正在轮询" : "归档 watcher 未启动",
  );
  if (Number(data.wal_bytes) > WAL_WARN_BYTES) {
    item("WAL", fmtBytes(data.wal_bytes), "warn", "WAL 侧车超过 64 MiB 阈值");
  }
  const flags = Array.isArray(data.health_flags) ? data.health_flags : [];
  for (const flag of flags) {
    if (!flag || !flag.id) continue;
    const word = flag.severity === "error" ? "错误" : flag.severity === "warn" ? "警告" : "通过";
    item(flag.label || flag.id, word, severityTone(flag.severity), flag.detail || "");
  }
  return el("div", { class: "va-status-strip", role: "group", "aria-label": "daemon 与健康状态" }, ...items);
}

/* ------------------------------------------------------------ composition -- */

function compositionCard(data) {
  const ledger = data.ledger || {};
  const total = Number(ledger.memories) || 0;
  const raw = Number(ledger.raw_chat) || 0;
  const structured = Math.max(0, total - raw);
  const items = sortedPairs(ledger.kinds, 9);
  const body = el("div", { class: "va-composition" },
    barList({ items, total: total || undefined, maxBars: 9, label: "kind 构成" }),
    el("p", { class: "va-note" },
      "结构记忆 " + fmtInt(structured) + " 条，raw 证据 " + fmtInt(raw) + " 条（" +
      fmtPct(total > 0 ? raw / total : 0) + "）。raw/工具输出默认不进入召回，",
      "口径上「结构记忆」= 全部记忆 − raw 证据。"));
  return card({
    eyebrow: "COMPOSITION",
    title: "记忆构成",
    body,
    className: "va-card--pulse",
  });
}

/* ---------------------------------------------------------------- sources -- */

function sourcesCard(data) {
  const ledger = data.ledger || {};
  const items = sortedPairs(ledger.agents, 8);
  const body = el("div", { class: "va-sources" },
    barList({ items, total: Number(ledger.memories) || undefined, mono: true, maxBars: 8, label: "Agent 来源分布" }),
    el("p", { class: "va-note" },
      fmtInt(ledger.trusted_agents) + " 个 Agent 有信任基线 · " +
      fmtInt(ledger.cursors) + " 个采集游标 · " +
      fmtInt(ledger.agent_credentials) + " 个有效凭据"));
  return card({
    eyebrow: "PROVENANCE",
    title: "来源分布",
    body,
  });
}

/* ------------------------------------------------------------ recent slips -- */

function slip(item, index, context) {
  const stamp = item.updated_at || item.created_at || "";
  const metaRow = el("span", { class: "va-slip-meta" },
    badge(item.kind || "unknown", { tone: "neutral" }),
    el("span", { class: "va-slip-agent" }, item.source_agent || "unknown"),
    el("span", { class: "va-slip-time", title: absTime(stamp) }, relTime(stamp)),
    item.pinned ? el("span", { class: "va-slip-mark" }, icon("pin", { size: 11 }), "置顶") : null,
    item.has_claims ? el("span", { class: "va-slip-mark" }, "claim") : null);
  return el("button", {
    class: "va-slip",
    type: "button",
    title: "打开详情：" + (item.content || "").slice(0, 60),
    on: { click: () => context.navigate("/archive?focus=" + encodeURIComponent(String(item.memory_id || ""))) },
  },
    el("span", { class: "va-slip-index" }, String(index + 1).padStart(2, "0")),
    el("span", { class: "va-slip-main" },
      metaRow,
      el("span", { class: "va-slip-content" }, truncate(item.content || "", 260))));
}

function recentCard(data, context) {
  const recent = Array.isArray(data.recent) ? data.recent : [];
  const body = el("div", { class: "va-slips" });
  if (!recent.length) {
    body.appendChild(emptyState({
      title: "还没有最近入档",
      hint: "daemon 采集到第一条结构化记忆后，这里会列出最近的 6 条。",
      action: { label: "查看端点", icon: "agents", onClick: () => context.navigate("/agents") },
    }));
  } else {
    recent.slice(0, 6).forEach((item, index) => body.appendChild(slip(item, index, context)));
  }
  return card({
    eyebrow: "RECENT ACCESSIONS",
    title: "最近入档",
    actions: { label: "全部记忆", icon: "archive", ghost: true, onClick: () => context.navigate("/archive") },
    body,
  });
}

/* ------------------------------------------------------------ health flags -- */

function healthCard(data, context) {
  const flags = Array.isArray(data.health_flags) ? data.health_flags : [];
  const list = el("div", { class: "va-flags" });
  if (!flags.length) {
    list.appendChild(emptyState({
      title: "未报告健康信号",
      hint: "daemon 本次没有返回健康 flags。打开体检页可查看完整检查项。",
      plain: true,
      action: { label: "打开体检", icon: "health", onClick: () => context.navigate("/health") },
    }));
  } else {
    for (const flag of flags) {
      const tone = severityTone(flag.severity);
      list.appendChild(el("button", {
        class: "va-flag",
        type: "button",
        title: (flag.label || flag.id) + " · " + (flag.detail || ""),
        on: { click: () => context.navigate("/health") },
      },
        dot(tone),
        el("span", { class: "va-flag-label" }, flag.label || flag.id || "check"),
        badge(flag.severity === "error" ? "错误" : flag.severity === "warn" ? "警告" : "通过", { tone }),
        el("span", { class: "va-flag-detail" }, truncate(flag.detail || "", 120))));
    }
  }
  return card({
    eyebrow: "HEALTH FLAGS",
    title: "体检摘要",
    actions: { label: "打开体检", icon: "health", ghost: true, onClick: () => context.navigate("/health") },
    body: list,
  });
}

/* --------------------------------------------------------------- shortcuts -- */

function shortcutCard(context) {
  const base = apiBase();
  const actions = [
    el("button", { class: "atlas-btn atlas-btn--block", type: "button", on: { click: () => context.navigate("/recall") } },
      icon("recall", { size: 14 }), el("span", {}, "检索记忆")),
    el("button", { class: "atlas-btn atlas-btn--block", type: "button", on: { click: () => context.navigate("/compose") } },
      icon("compose", { size: 14 }), el("span", {}, "组装上下文")),
    el("button", { class: "atlas-btn atlas-btn--block", type: "button", on: { click: () => context.navigate("/archive") } },
      icon("archive", { size: 14 }), el("span", {}, "浏览记忆库")),
  ];
  const copyRow = el("div", { class: "va-copy-row" },
    el("code", { class: "va-api-base", title: base }, base));
  copyRow.appendChild(copyButton(base, { label: "复制 API 地址" }));
  return card({
    eyebrow: "SHORTCUTS",
    title: "快捷动作",
    body: el("div", { class: "va-quick-actions" }, ...actions,
      el("div", { class: "va-quick-sep" }),
      el("div", { class: "va-note" }, "API 基地址（同源，需管理员令牌头 X-HippoMind-Token）"),
      copyRow),
  });
}

/* ------------------------------------------------------------------- empty -- */

function emptyLedgerCard(context) {
  return card({
    body: emptyState({
      icon: "archive",
      title: "账本里还没有结构化记忆",
      hint: "daemon 已连接，但账本为空。可以先去端点页确认 Agent 接入，或用 CLI 写入第一条记忆。",
      action: [
        { label: "查看端点", icon: "agents", primary: true, onClick: () => context.navigate("/agents") },
        { label: "浏览记忆库", icon: "archive", onClick: () => context.navigate("/archive") },
      ],
    }),
  });
}

/* ------------------------------------------------------------------- paint -- */

function paint(root, data, context) {
  clear(root);
  const ledger = data.ledger || {};
  root.appendChild(heroSection(data));

  const strip = statusStrip(data, context);
  if (strip.children.length) root.appendChild(strip);

  if (!Number(ledger.memories)) root.appendChild(emptyLedgerCard(context));

  root.appendChild(el("div", { class: "atlas-grid atlas-grid--2" },
    compositionCard(data),
    sourcesCard(data)));

  root.appendChild(el("div", { class: "va-pulse-lower" },
    recentCard(data, context),
    el("div", { class: "va-pulse-side" },
      healthCard(data, context),
      shortcutCard(context))));

  current.painted = true;
}

/* ------------------------------------------------------------------- entry -- */

export async function render(context) {
  const root = el("div", { class: "va-view va-pulse" });
  current = { root, painted: false };
  root.appendChild(skeleton(4));
  try {
    const data = await api.get("/api/v1/overview", { days: OVERVIEW_DAYS }, { signal: context.signal });
    if (context.signal.aborted) return root;
    paint(root, data || {}, context);
  } catch (error) {
    if (error && error.name === "AbortError") return root;
    clear(root);
    root.appendChild(errorState({
      title: "脉搏加载失败",
      error,
      onRetry: () => context.refresh(),
    }));
  }
  return root;
}

export async function refresh(context) {
  const root = current.root;
  if (!root || !root.isConnected) return;
  try {
    const data = await api.get("/api/v1/overview", { days: OVERVIEW_DAYS }, { signal: context.signal });
    if (context.signal.aborted) return;
    paint(root, data || {}, context);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (current.painted) context.toast("脉搏刷新失败：" + String((error && (error.detail || error.message)) || "未知错误"), { tone: "warn" });
    else {
      clear(root);
      root.appendChild(errorState({ title: "脉搏加载失败", error, onRetry: () => context.refresh() }));
    }
  }
}

