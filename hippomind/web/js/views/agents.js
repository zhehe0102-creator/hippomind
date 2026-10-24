/* ============================================================================
   HippoMind Atlas — 08 agents 端点
   Source agents, collection cursors, discovery and controlled auto-install.
   Data: GET /api/v1/agents · POST /admin/reload (admin, confirmed)
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import { fmtInt, fmtPct, relTime, absTime, truncate } from "../core/format.js";
import {
  badge, card, chip, metric, dataTable, emptyState, errorState, skeleton, sectionTitle,
} from "../components.js";
import { icon } from "../icons.js";

export const meta = {
  id: "agents",
  index: "08",
  title: "端点",
  eyebrow: "AGENTS",
  subtitle: "来源 Agent、采集游标、接入发现与自动安装状态",
};

let current = { root: null, painted: false };

const INSTALL_LABELS = {
  installed: "已安装",
  already_installed: "已就绪",
  degraded: "降级",
  error: "错误",
};

function installTone(state) {
  if (state === "installed" || state === "already_installed") return "ok";
  if (state === "degraded" || state === "error") return "warn";
  return "neutral";
}

function dot(tone) {
  return el("span", { class: "va-dot tone-" + tone });
}

function trustTone(value) {
  const trust = Number(value);
  if (!Number.isFinite(trust)) return "neutral";
  if (trust >= 0.7) return "ok";
  if (trust >= 0.5) return "neutral";
  return "warn";
}

function timeCell(iso) {
  if (!iso) return el("span", { class: "va-cell-time" }, el("span", { class: "muted" }, "—"));
  return el("span", { class: "va-cell-time" },
    el("span", {}, relTime(iso)),
    el("small", { title: absTime(iso) }, String(iso).slice(0, 10)));
}

function agentStatus(agent) {
  if (!agent.last_seen) return { label: "无记录", tone: "neutral" };
  const stamp = Date.parse(agent.last_seen);
  if (!Number.isFinite(stamp)) return { label: "无记录", tone: "neutral" };
  const days = (Date.now() - stamp) / 86400000;
  if (days <= 1) return { label: "今日活跃", tone: "accent" };
  if (days <= 7) return { label: "近期活跃", tone: "ok" };
  if (days <= 30) return { label: "沉寂", tone: "warn" };
  return { label: "长期沉寂", tone: "danger" };
}

/* -------------------------------------------------------------- metric band -- */

function metricBand(data) {
  const watcher = data.watcher || {};
  const injection = data.injection || {};
  const discovery = data.discovery || {};
  const autoInstall = data.auto_install || {};
  const installAgents = autoInstall.agents && typeof autoInstall.agents === "object"
    ? Object.values(autoInstall.agents) : [];
  const installCounts = { installed: 0, already_installed: 0, degraded: 0, error: 0, other: 0 };
  for (const entry of installAgents) {
    const state = String((entry && entry.state) || "other");
    if (Object.prototype.hasOwnProperty.call(installCounts, state)) installCounts[state] += 1;
    else installCounts.other += 1;
  }
  const installHint = installAgents.length
    ? "已装 " + fmtInt(installCounts.installed) + " · 就绪 " + fmtInt(installCounts.already_installed) +
      " · 降级 " + fmtInt(installCounts.degraded) + " · 错误 " + fmtInt(installCounts.error)
    : "本轮未触发自动安装";
  const scanText = discovery.available ? relTime(discovery.scanned_at) : "不可用";
  const scanHint = discovery.available
    ? absTime(discovery.scanned_at) + " · 每 " + fmtInt(Math.round((Number(discovery.interval_seconds) || 0) / 60)) + " 分钟"
    : "daemon 未提供发现摘要";

  return el("div", { class: "va-metric-band", role: "group", "aria-label": "端点指标" },
    metric({
      label: "WATCHER",
      value: watcher.available ? "在线" : "不可用",
      small: true,
      tone: watcher.available ? "ok" : "warn",
      hint: watcher.poll_interval ? "轮询间隔 " + fmtInt(watcher.poll_interval) + "s" : "归档 watcher 未启动",
    }),
    metric({
      label: "INJECTION · 注入",
      value: injection.enabled ? "开启" : "关闭",
      small: true,
      tone: injection.enabled ? "ok" : "warn",
      hint: "agent=" + (injection.agent || "all") + (injection.overridden ? " · 单独覆盖" : ""),
    }),
    metric({
      label: "DISCOVERY · 扫描",
      value: scanText,
      small: true,
      hint: scanHint,
    }),
    metric({
      label: "AUTO INSTALL · 自动安装",
      value: fmtInt(installAgents.length) + " 个 Agent",
      small: true,
      hint: installHint,
    }),
    metric({
      label: "CREDENTIALS · 凭据",
      value: fmtInt(data.credentials),
      small: true,
      hint: "未撤销的 Agent 凭据总数",
    }));
}

/* ------------------------------------------------------------- automation -- */

/* /api/v1/agents is the endpoint page, and the automatic loop is what turns
   these sources into memory. The strip summarises the two loops this payload
   can actually evidence — the capture cursors per source and the injection
   switch — plus the discovery scan that decides which sources exist at all.
   Nothing here is invented: each cell states the endpoint field it reads. */
function automationBand(data) {
  const cursors = Array.isArray(data.cursors) ? data.cursors : [];
  const failing = cursors.filter((row) => row && row.last_error);
  const healthy = cursors.length - failing.length;
  const newest = cursors.reduce((latest, row) => {
    const stamp = Date.parse((row && (row.last_success_at || row.updated_at)) || "");
    return Number.isFinite(stamp) && stamp > latest ? stamp : latest;
  }, 0);
  const injection = data.injection && typeof data.injection === "object" ? data.injection : {};
  const discovery = data.discovery && typeof data.discovery === "object" ? data.discovery : {};
  const detected = Array.isArray(discovery.known_detected) ? discovery.known_detected : [];
  const absent = Array.isArray(discovery.known_absent) ? discovery.known_absent : [];
  const agents = Array.isArray(data.agents) ? data.agents : [];
  const seenRecently = agents.filter((row) => {
    const stamp = Date.parse((row && row.last_seen) || "");
    return Number.isFinite(stamp) && (Date.now() - stamp) / 86400000 <= 1;
  }).length;

  const cells = [
    {
      eyebrow: "CAPTURE",
      label: "采集游标",
      tone: failing.length ? "warn" : (cursors.length ? "ok" : "neutral"),
      primary: cursors.length ? fmtInt(healthy) : "—",
      unit: cursors.length ? "/ " + fmtInt(cursors.length) + " 个健康" : "",
      detail: failing.length
        ? fmtInt(failing.length) + " 个游标最近失败 · " + truncate(String(failing[0].last_error || ""), 40)
        : (newest ? "最近成功 " + relTime(new Date(newest).toISOString()) : "还没有游标记录"),
      title: failing.length
        ? "失败的游标：" + failing.slice(0, 3).map((row) => String(row.source_id || "")).join("、")
        : "所有采集游标最近一次增量同步都成功",
    },
    {
      eyebrow: "SOURCES",
      label: "接入来源",
      tone: seenRecently ? "ok" : "neutral",
      primary: fmtInt(agents.length),
      unit: "个 Agent",
      detail: fmtInt(seenRecently) + " 个今日活跃 · 发现 " + fmtInt(detected.length)
        + (absent.length ? " · 缺失 " + fmtInt(absent.length) : ""),
      title: "已检测：" + (detected.slice(0, 8).join("、") || "无")
        + (absent.length ? "；未检测：" + absent.slice(0, 8).join("、") : ""),
    },
    {
      eyebrow: "INJECTION",
      label: "自动注入",
      tone: injection.enabled ? "ok" : "warn",
      primary: injection.enabled ? "开启" : "关闭",
      unit: injection.agent ? "agent=" + injection.agent : "",
      detail: injection.enabled
        ? "相关性门槛通过后才注入上下文"
        : "自动注入已关闭，Agent 会话不会收到上下文",
      title: "注入开关来源：/api/v1/agents · injection"
        + (injection.path ? "（" + injection.path + "）" : ""),
    },
  ];

  return el("div", { class: "va-auto-band", role: "group", "aria-label": "自动记忆环路状态" },
    el("div", { class: "va-auto-band-head" },
      el("span", { class: "eyebrow" }, "AUTOMATION LOOP"),
      el("span", { class: "va-auto-band-note" }, "采集游标 → 提炼 → 注入；每项均取自本页端点")),
    el("div", { class: "va-auto-band-cells" },
      ...cells.map((cell) => el("div", { class: "va-auto-band-cell", dataset: { tone: cell.tone } },
        el("div", { class: "va-auto-band-cell-head" },
          dot(cell.tone),
          el("span", { class: "va-auto-band-eyebrow" }, cell.eyebrow),
          el("span", { class: "va-auto-band-label" }, cell.label)),
        el("div", { class: "va-auto-band-figure" },
          el("b", {}, cell.primary),
          cell.unit ? el("span", { class: "va-auto-band-unit" }, cell.unit) : null),
        el("div", { class: "va-auto-band-detail", title: cell.title }, truncate(cell.detail, 90))))));
}

/* ------------------------------------------------------------------ tables -- */

function agentsTable(data, context) {
  const agents = Array.isArray(data.agents) ? data.agents : [];
  const columns = [
    { key: "agent", label: "Agent", mono: true },
    { key: "memories", label: "记忆", align: "right", format: (row) => fmtInt(row.memories) },
    { key: "active", label: "活跃", align: "right", format: (row) => fmtInt(row.active) },
    { key: "raw", label: "raw", align: "right", format: (row) => fmtInt(row.raw) },
    { key: "events", label: "事件", align: "right", format: (row) => fmtInt(row.events) },
    { key: "last_seen", label: "最近", render: (row) => timeCell(row.last_seen) },
    { key: "trust", label: "信任", align: "right", render: (row) => badge(fmtPct(row.trust), { tone: trustTone(row.trust) }) },
    { key: "credentials", label: "凭据", align: "right", format: (row) => fmtInt(row.credentials) },
    { key: "status", label: "状态", render: (row) => {
      const status = agentStatus(row);
      return badge(status.label, { tone: status.tone });
    } },
  ];
  const empty = emptyState({
    plain: true,
    icon: "agents",
    title: "还没有来源 Agent",
    hint: "daemon 尚未采集到任何记忆来源。确认 watcher 已启动，或到体检页查看 daemon 状态。",
    action: { label: "刷新发现", icon: "refresh", onClick: () => context.refresh() },
  });
  return card({
    eyebrow: "AGENT REGISTRY",
    title: "Agent 主表",
    actions: badge(fmtInt(agents.length) + " 个来源", { tone: "neutral" }),
    className: "va-card--flush-body",
    body: dataTable({
      columns,
      rows: agents,
      rowKey: "agent",
      dense: true,
      empty,
    }),
  });
}

function cursorsTable(data) {
  const cursors = Array.isArray(data.cursors) ? data.cursors : [];
  const columns = [
    { key: "source_id", label: "source_id", mono: true },
    { key: "last_success_at", label: "最近成功", render: (row) => timeCell(row.last_success_at) },
    { key: "last_error", label: "最近错误", render: (row) => {
      if (!row.last_error) return el("span", { class: "va-cursor-ok" }, "无");
      return el("span", { class: "va-cursor-error", title: row.last_error }, truncate(row.last_error, 60));
    } },
    { key: "updated_at", label: "更新时间", render: (row) => timeCell(row.updated_at) },
    { key: "cursor_length", label: "游标长度", align: "right", format: (row) => fmtInt(row.cursor_length) },
  ];
  const empty = emptyState({
    plain: true,
    icon: "archive",
    title: "还没有采集游标",
    hint: "运行 hippomind sync 或等待 watcher 首次同步后，这里会显示每个来源的增量游标。",
  });
  return card({
    eyebrow: "CURSORS",
    title: "采集游标",
    actions: badge(fmtInt(cursors.length) + " 个来源", { tone: "neutral" }),
    className: "va-card--flush-body",
    body: dataTable({ columns, rows: cursors, dense: true, empty }),
  });
}

/* --------------------------------------------------------------- discovery -- */

function chipGroup(label, names) {
  const list = Array.isArray(names) ? names.filter(Boolean) : [];
  const row = el("div", { class: "va-chip-row" });
  if (!list.length) {
    row.appendChild(el("span", { class: "va-note" }, "无"));
  } else {
    for (const name of list) {
      row.appendChild(chip(String(name), { title: label + " · " + String(name) }));
    }
  }
  return el("div", { class: "va-discovery-group" },
    el("div", { class: "va-discovery-label" }, label + " " + fmtInt(list.length)),
    row);
}

function discoveryCard(data) {
  const discovery = data.discovery || {};
  const body = el("div", { class: "va-discovery" });
  if (!discovery.available) {
    body.appendChild(emptyState({
      plain: true,
      icon: "agents",
      title: "发现数据不可用",
      hint: "daemon 未能提供 Agent 发现摘要。稍后刷新，或检查 daemon 状态目录权限。",
    }));
  } else {
    body.appendChild(el("div", { class: "va-discovery-meta" },
      el("span", {}, "扫描于 ", el("b", {}, relTime(discovery.scanned_at))),
      el("span", { class: "muted", title: absTime(discovery.scanned_at) }, absTime(discovery.scanned_at)),
      el("span", { class: "muted" }, "间隔 " + fmtInt(Math.round((Number(discovery.interval_seconds) || 0) / 60)) + " 分钟")));
    body.appendChild(chipGroup("已检测", discovery.known_detected));
    body.appendChild(chipGroup("未检测", discovery.known_absent));
    const unknown = Array.isArray(discovery.unknown_reported) ? discovery.unknown_reported : [];
    if (unknown.length) body.appendChild(chipGroup("仅报告（未知运行时）", unknown));
  }
  return card({ eyebrow: "DISCOVERY", title: "Agent 发现", body });
}

function autoInstallCard(data) {
  const outcome = data.auto_install || {};
  const entries = outcome.agents && typeof outcome.agents === "object" ? Object.entries(outcome.agents) : [];
  const body = el("div", { class: "va-install" });
  if (!entries.length) {
    body.appendChild(emptyState({
      plain: true,
      icon: "check",
      title: "本轮没有自动安装",
      hint: "daemon 启动或刷新发现时，会为已检测到的 Agent 安装并校验集成。",
    }));
  } else {
    if (outcome.ran_at) {
      body.appendChild(el("p", { class: "va-note" }, "运行于 " + relTime(outcome.ran_at) + " · " + absTime(outcome.ran_at)));
    }
    const list = el("div", { class: "va-install-list" });
    for (const [name, info] of entries.sort((a, b) => String(a[0]).localeCompare(String(b[0])))) {
      const state = String((info && info.state) || "unknown");
      list.appendChild(el("div", { class: "va-install-row" },
        el("span", { class: "mono va-install-agent" }, name),
        badge(INSTALL_LABELS[state] || state, { tone: installTone(state) }),
        info && info.error
          ? el("span", { class: "va-cursor-error", title: info.error }, truncate(info.error, 70))
          : null));
    }
    body.appendChild(list);
    if (outcome.ran_at) {
      const agents = data.agents || [];
      const withCredentials = agents.filter((entry) => Number(entry.credentials) > 0).length;
      body.appendChild(el("p", { class: "va-note" },
        fmtInt(withCredentials) + " / " + fmtInt(agents.length) + " 个 Agent 持有有效凭据。"));
    }
  }
  return card({ eyebrow: "AUTO INSTALL", title: "集成自动安装", body });
}

/* ------------------------------------------------------------ refresh button -- */

function refreshButton(context) {
  const label = el("span", {}, "刷新发现");
  const button = el("button", { class: "atlas-btn", type: "button" }, icon("refresh", { size: 13 }), label);
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    const ok = await context.confirm({
      title: "刷新 Agent 发现",
      body: "将强制重扫本机 Agent 运行时并触发一轮受控自动安装。只会更新 daemon 的发现缓存与集成状态，不改动记忆账本。",
      confirmLabel: "开始刷新",
    });
    if (!ok) return;
    button.disabled = true;
    label.textContent = "刷新中…";
    try {
      const result = await api.post("/admin/reload", {}, { signal: context.signal });
      if (result && result.error) {
        context.toast("发现刷新部分失败：" + String(result.error), { tone: "warn", ms: 5200 });
      } else {
        context.toast("Agent 发现已刷新", { tone: "ok" });
      }
      await context.refresh();
    } catch (error) {
      if (error && error.name === "AbortError") return;
      context.toast("刷新失败：" + String((error && (error.detail || error.message)) || "未知错误"), { tone: "danger" });
    } finally {
      button.disabled = false;
      label.textContent = "刷新发现";
    }
  });
  return button;
}

/* ------------------------------------------------------------------- paint -- */

function paint(root, data, context) {
  clear(root);
  root.appendChild(metricBand(data));
  root.appendChild(automationBand(data));
  root.appendChild(sectionTitle("Agent 总览", {
    hint: "来源、游标、发现与自动安装状态均由 daemon 实时提供",
    actions: refreshButton(context),
  }));
  root.appendChild(agentsTable(data, context));
  root.appendChild(el("div", { class: "atlas-grid atlas-grid--2 va-start-grid" },
    discoveryCard(data),
    autoInstallCard(data)));
  root.appendChild(cursorsTable(data));
  current.painted = true;
}

/* ------------------------------------------------------------------- entry -- */

export async function render(context) {
  const root = el("div", { class: "va-view va-agents" });
  current = { root, painted: false };
  root.appendChild(skeleton(5));
  try {
    const data = await api.get("/api/v1/agents", null, { signal: context.signal });
    if (context.signal.aborted) return root;
    paint(root, data || {}, context);
  } catch (error) {
    if (error && error.name === "AbortError") return root;
    clear(root);
    root.appendChild(errorState({ title: "端点加载失败", error, onRetry: () => context.refresh() }));
  }
  return root;
}

export async function refresh(context) {
  const root = current.root;
  if (!root || !root.isConnected) return;
  try {
    const data = await api.get("/api/v1/agents", null, { signal: context.signal });
    if (context.signal.aborted) return;
    paint(root, data || {}, context);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (current.painted) context.toast("端点刷新失败：" + String((error && (error.detail || error.message)) || "未知错误"), { tone: "warn" });
    else {
      clear(root);
      root.appendChild(errorState({ title: "端点加载失败", error, onRetry: () => context.refresh() }));
    }
  }
}

