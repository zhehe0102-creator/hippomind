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

/* ------------------------------------------------------------ automation -- */

function numberOrZero(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

/* /api/v1/health reports the injection trace as a rendered detail string
   ("最近 N 次注入 · M 次实际注入 · 最近 <ts>") because it is the only endpoint
   that summarises the trace, and this task may not add one. Parse it strictly;
   when the wording changes the count degrades to "—" and the raw detail is
   still shown verbatim, so a stale parser never invents a number. */
function parseInjectionDetail(detail) {
  const text = String(detail || "");
  const count = text.match(/最近\s*(\d+)\s*次注入/);
  const injected = text.match(/(\d+)\s*次实际注入/);
  const lastAt = text.match(/(\d{4}-\d{2}-\d{2}T[0-9:.+-]+)/);
  return {
    count: count ? Number(count[1]) : null,
    injected: injected ? Number(injected[1]) : null,
    lastAt: lastAt ? lastAt[1] : "",
  };
}

/* "The loop has not run yet" and "the loop ran and found nothing" are
   different facts, and only one of them is a problem. The daemon publishes an
   empty object until a stage completes its first round, so an empty stage is
   rendered as unknown (—) rather than as a confident zero. */
function stageState(raw) {
  if (!raw || typeof raw !== "object" || !Object.keys(raw).length) return "pending";
  if (String(raw.error || "")) return "error";
  return "ran";
}

/* The v2 loop runs unattended, so the pulse page answers "is it actually
   working?" from two payloads it already needs: /overview carries the daemon's
   own capture/distill counters, /health carries the injection trace check.
   They are fetched together so the strip is one snapshot, not two. */
function automationStrip(data, health, context) {
  const daemonHealth = data.daemon_health && typeof data.daemon_health === "object"
    ? data.daemon_health : {};
  const capture = daemonHealth.capture && typeof daemonHealth.capture === "object"
    ? daemonHealth.capture : {};
  const distill = daemonHealth.distill && typeof daemonHealth.distill === "object"
    ? daemonHealth.distill : {};
  const checks = health && Array.isArray(health.checks) ? health.checks : [];
  const injectionCheck = checks.find((check) => check && check.id === "injection") || null;
  const injection = injectionCheck ? parseInjectionDetail(injectionCheck.detail) : null;
  const injectionCount = injection && injection.count !== null ? injection.count : null;
  const injectionInjected = injection && injection.injected !== null ? injection.injected : null;
  const injectionAt = injection ? injection.lastAt : "";

  const captureState = stageState(capture);
  const distillState = stageState(distill);
  const captureError = String(capture.error || "");
  const distillError = String(distill.error || "");
  const captureFiles = numberOrZero(capture.files);
  const captureMessages = numberOrZero(capture.messages);
  const captureInserted = numberOrZero(capture.inserted);
  const distillCandidates = numberOrZero(distill.candidates);
  const distillClaims = numberOrZero(distill.claims);
  const distillDistilled = numberOrZero(distill.distilled);

  const stages = [
    {
      id: "capture",
      eyebrow: "CAPTURE",
      label: "采集",
      tone: captureState === "error" ? "warn" : captureState === "ran" ? "ok" : "neutral",
      primary: captureState === "ran" ? fmtInt(captureFiles) : "—",
      unit: captureState === "ran" ? "个文件" : "",
      detail: captureState === "error" ? captureError
        : captureState === "ran"
          ? (fmtInt(captureMessages) + " 条消息 · 新增 " + fmtInt(captureInserted) + " 条")
          : "等待首轮采集（每 60 秒）",
      title: captureState === "error"
        ? "采集上一轮报错：" + captureError
        : captureState === "ran"
          ? "已扫描 " + fmtInt(captureFiles) + " 个会话文件，读取 " + fmtInt(captureMessages)
            + " 条消息，新增 " + fmtInt(captureInserted) + " 条记忆"
          : "daemon 尚未完成首轮采集；采集循环启动后每 60 秒运行一次",
      stage: "capture",
    },
    {
      id: "distill",
      eyebrow: "DISTILL",
      label: "提炼",
      tone: distillState === "error" ? "warn" : distillState === "ran" ? "ok" : "neutral",
      primary: distillState === "ran" ? fmtInt(distillCandidates) : "—",
      unit: distillState === "ran" ? "个候选" : "",
      detail: distillState === "error" ? distillError
        : distillState === "ran"
          ? (fmtInt(distillClaims) + " 条 claims · 提炼 " + fmtInt(distillDistilled) + " 条")
          : "等待首轮提炼（每 5 分钟）",
      title: distillState === "error"
        ? "提炼上一轮报错：" + distillError
        : distillState === "ran"
          ? "本轮 " + fmtInt(distillCandidates) + " 个候选，产出 " + fmtInt(distillClaims)
            + " 条 claims，落库 " + fmtInt(distillDistilled) + " 条"
          : "daemon 尚未完成首轮提炼；提炼循环启动后每 5 分钟运行一次",
      stage: "distill",
    },
    {
      id: "injection",
      eyebrow: "INJECTION",
      label: "注入",
      tone: injectionCheck && injectionCheck.severity !== "ok" ? "warn"
        : (injectionCheck ? "ok" : "neutral"),
      primary: injectionCount === null ? "—" : fmtInt(injectionCount),
      unit: injectionCount === null ? "" : "次注入",
      detail: injectionAt
        ? ((injectionInjected === null ? "—" : fmtInt(injectionInjected)) + " 次实际注入 · " + relTime(injectionAt))
        : (injectionCheck ? "尚无注入记录" : "daemon 未报告注入检查项"),
      title: injectionCheck && injectionCheck.detail
        ? injectionCheck.detail
        : (injectionAt ? "最近注入于 " + absTime(injectionAt, { seconds: true }) : "尚未记录自动注入"),
      stage: "injection",
    },
  ];

  const cells = stages.map((stage) => el("button", {
    class: "va-auto-cell",
    type: "button",
    dataset: { tone: stage.tone, stage: stage.id },
    title: stage.title + " · 点击查看体检",
    on: { click: () => context.navigate("/health") },
  },
    el("span", { class: "va-auto-cell-head" },
      dot(stage.tone),
      el("span", { class: "va-auto-cell-eyebrow" }, stage.eyebrow),
      el("span", { class: "va-auto-cell-label" }, stage.label)),
    el("span", { class: "va-auto-cell-figure" },
      el("b", {}, stage.primary),
      el("span", { class: "va-auto-cell-unit" }, stage.unit)),
    el("span", { class: "va-auto-cell-detail" }, truncate(stage.detail, 74))));

  return el("div", { class: "va-auto-strip", role: "group", "aria-label": "自动记忆环路" },
    el("div", { class: "va-auto-strip-head" },
      el("span", { class: "eyebrow" }, "AUTOMATION"),
      el("span", { class: "va-auto-strip-note" }, "采集 → 提炼 → 注入，由 daemon 后台自动运行")),
    el("div", { class: "va-auto-strip-cells" }, ...cells));
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

/* --------------------------------------------------------------- activity -- */

function activityCard(activity, context) {
  const items = Array.isArray(activity && activity.items) ? activity.items : [];
  const list = el("div", { class: "va-activity" });
  if (!items.length) {
    list.appendChild(emptyState({
      title: "还没有结构化事件",
      hint: "watcher 采集到的结构化事件会出现在这里；raw/tool/system 事件不会展示。",
      action: { label: "查看端点", icon: "agents", onClick: () => context.navigate("/agents") },
    }));
  } else {
    for (const item of items.slice(0, 8)) {
      list.appendChild(el("div", { class: "va-activity-row" },
        el("span", { class: "va-activity-time", title: absTime(item.occurred_at) }, relTime(item.occurred_at)),
        badge(item.event_type || "event", { tone: "neutral" }),
        el("span", { class: "va-activity-agent" }, item.source_agent || "—"),
        el("span", { class: "va-activity-content" }, truncate(item.content_preview || "", 140))));
    }
  }
  const counts = activity && activity.counts ? activity.counts : {};
  const summary = Object.keys(counts).slice(0, 4)
    .map((key) => key + " " + fmtInt(counts[key])).join(" · ");
  return card({
    eyebrow: "ACTIVITY",
    title: "最近活动",
    actions: { label: "查看年轮", icon: "timeline", ghost: true, onClick: () => context.navigate("/timeline") },
    body: el("div", {}, list,
      summary ? el("div", { class: "va-activity-summary" }, summary) : null),
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

function paint(root, data, activity, health, context) {
  clear(root);
  const ledger = data.ledger || {};
  root.appendChild(heroSection(data));

  const strip = statusStrip(data, context);
  if (strip.children.length) root.appendChild(strip);

  root.appendChild(automationStrip(data, health, context));

  if (!Number(ledger.memories)) root.appendChild(emptyLedgerCard(context));

  root.appendChild(el("div", { class: "atlas-grid atlas-grid--2" },
    compositionCard(data),
    sourcesCard(data)));

  root.appendChild(el("div", { class: "va-pulse-lower" },
    el("div", { class: "va-pulse-main" },
      recentCard(data, context),
      activityCard(activity, context)),
    el("div", { class: "va-pulse-side" },
      healthCard(data, context),
      shortcutCard(context))));

  current.painted = true;
}

/* ------------------------------------------------------------------- entry -- */

/* The automation strip needs the health checks for the injection trace. It is
   an enhancement, never a reason for the first screen to fail: a failed
   /health leaves the strip in its "not reported" state. */
async function fetchHealth(context) {
  try {
    return await api.get("/api/v1/health", null, { signal: context.signal });
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    console.warn("[atlas] pulse automation health unavailable", error);
    return null;
  }
}

/* The automation strip reads overview.daemon_health and health.checks; taking
   both in one parallel batch keeps the three stages describing the same
   moment instead of two snapshots a minute apart. */
async function loadSnapshot(context) {
  const [overview, health] = await Promise.all([
    api.get("/api/v1/overview", { days: OVERVIEW_DAYS }, { signal: context.signal }),
    fetchHealth(context),
  ]);
  return { overview, health };
}

export async function render(context) {
  const root = el("div", { class: "va-view va-pulse" });
  current = { root, painted: false };
  root.appendChild(skeleton(4));
  try {
    const snapshot = await loadSnapshot(context);
    if (context.signal.aborted) return root;
    const data = snapshot.overview;
    let activity = { items: [], counts: {} };
    try {
      activity = await api.get("/api/v1/events", { limit: 8 }, { signal: context.signal });
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      console.warn("[atlas] pulse activity unavailable", error);
    }
    if (context.signal.aborted) return root;
    paint(root, data || {}, activity || {}, snapshot.health, context);
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
    const snapshot = await loadSnapshot(context);
    if (context.signal.aborted) return;
    let activity = { items: [], counts: {} };
    try {
      activity = await api.get("/api/v1/events", { limit: 8 }, { signal: context.signal });
    } catch (error) {
      if (error && error.name === "AbortError") return;
      console.warn("[atlas] pulse activity unavailable", error);
    }
    if (context.signal.aborted) return;
    paint(root, snapshot.overview || {}, activity || { items: [], counts: {} }, snapshot.health, context);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (current.painted) context.toast("脉搏刷新失败：" + String((error && (error.detail || error.message)) || "未知错误"), { tone: "warn" });
    else {
      clear(root);
      root.appendChild(errorState({ title: "脉搏加载失败", error, onRetry: () => context.refresh() }));
    }
  }
}

