/* ============================================================================
   HippoMind Atlas — 07 timeline 年轮
   The ledger's growth over time: agent-stacked buckets, drill-down into the
   archive, and a parallel event series. Filters live in the URL query.
   Data: GET /api/v1/timeline?days=&bucket=&agent=&kind=&raw=
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import { fmtInt } from "../core/format.js";
import {
  card, metric, selectField, segmented, toggleField, emptyState, errorState, skeleton,
} from "../components.js";
import { sparkline, stackedBars, legend, barList } from "../charts.js";

export const meta = {
  id: "timeline",
  index: "07",
  title: "年轮",
  eyebrow: "TIMELINE",
  subtitle: "记忆随时间的生长：按日/周堆叠，可下钻到具体时段",
};

const DAY_CHOICES = [30, 90, 365];
const SERIES_TONES = ["accent", "link", "ok", "warn", "neutral", "danger"];
const MAX_SERIES = 5;

let current = { root: null, filters: null, hidden: new Set(), painted: false };
const knownAgents = new Set();
const knownKinds = new Set();

function readFilters(params) {
  const rawDays = Number(params.get("days"));
  const days = DAY_CHOICES.indexOf(rawDays) >= 0 ? rawDays : 90;
  const bucket = params.get("bucket") === "week" ? "week" : "day";
  return {
    days,
    bucket,
    agent: params.get("agent") || "",
    kind: params.get("kind") || "",
    raw: params.get("raw") === "include" ? "include" : "exclude",
  };
}

function setFilters(context, filters, patch) {
  const next = Object.assign({}, filters, patch || {});
  context.setQuery({
    days: String(next.days),
    bucket: next.bucket,
    agent: next.agent,
    kind: next.kind,
    raw: next.raw,
  });
}

function fetchTimeline(context, filters) {
  const params = { days: filters.days, bucket: filters.bucket, raw: filters.raw };
  if (filters.agent) params.agent = filters.agent;
  if (filters.kind) params.kind = filters.kind;
  return api.get("/api/v1/timeline", params, { signal: context.signal });
}

function formatAverage(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return Number.isInteger(number) ? fmtInt(number) : number.toFixed(1);
}

/* --------------------------------------------------------------- controls -- */

function controlBar(context, filters) {
  const agents = new Set(knownAgents);
  if (filters.agent) agents.add(filters.agent);
  const agentOptions = [{ value: "", label: "全部 Agent" }].concat(
    Array.from(agents).sort().map((name) => ({ value: name, label: name })));

  const kinds = new Set(knownKinds);
  if (filters.kind) kinds.add(filters.kind);
  const kindOptions = [{ value: "", label: "全部 kind" }].concat(
    Array.from(kinds).sort().map((name) => ({ value: name, label: name })));

  return el("div", { class: "va-controls", role: "group", "aria-label": "年轮筛选" },
    el("div", { class: "va-control" },
      el("span", { class: "va-control-label" }, "时间范围"),
      segmented({
        options: DAY_CHOICES.map((value) => ({ value: String(value), label: value + " 天" })),
        value: String(filters.days),
        onChange: (value) => setFilters(context, filters, { days: Number(value) }),
      })),
    el("div", { class: "va-control" },
      el("span", { class: "va-control-label" }, "粒度"),
      segmented({
        options: [{ value: "day", label: "按日" }, { value: "week", label: "按周" }],
        value: filters.bucket,
        onChange: (value) => setFilters(context, filters, { bucket: value }),
      })),
    selectField({
      label: "Agent",
      options: agentOptions,
      value: filters.agent,
      onChange: (event, value) => setFilters(context, filters, { agent: value }),
    }),
    selectField({
      label: "Kind",
      options: kindOptions,
      value: filters.kind,
      onChange: (event, value) => setFilters(context, filters, { kind: value }),
    }),
    toggleField({
      label: "含 raw 证据",
      checked: filters.raw === "include",
      onChange: (event, checked) => setFilters(context, filters, { raw: checked ? "include" : "exclude" }),
    }));
}

/* ----------------------------------------------------------------- series -- */

function seriesFor(data) {
  const agents = Array.isArray(data.agents) ? data.agents : [];
  const top = agents.slice(0, MAX_SERIES);
  const rest = agents.slice(MAX_SERIES);
  const series = top.map((entry, index) => ({
    id: entry.agent,
    label: entry.agent,
    tone: SERIES_TONES[index % SERIES_TONES.length],
  }));
  if (rest.length) {
    series.push({ id: "__other", label: "其他 " + fmtInt(rest.length) + " 个", tone: "neutral" });
  }
  return series;
}

function labelFor(date, bucket) {
  const text = String(date || "");
  if (text.length >= 10) return bucket === "week" ? text.slice(5) + " 周" : text.slice(5);
  return text;
}

function bucketsFor(data, series) {
  const topIds = series.filter((item) => item.id !== "__other").map((item) => item.id);
  const hasOther = series.some((item) => item.id === "__other");
  const rows = Array.isArray(data.buckets) ? data.buckets : [];
  return rows.map((bucket) => {
    const parts = {};
    let topSum = 0;
    for (const id of topIds) {
      const value = Number(bucket.agents && bucket.agents[id] ? bucket.agents[id] : 0);
      if (value > 0) { parts[id] = value; topSum += value; }
    }
    if (hasOther) {
      const other = Math.max(0, Number(bucket.memories || 0) - topSum);
      if (other > 0) parts.__other = other;
    }
    return {
      key: String(bucket.date || ""),
      label: labelFor(bucket.date, data.bucket),
      total: Number(bucket.memories || 0),
      parts,
      events: Number(bucket.events || 0),
    };
  });
}

function drillTarget(filters, bucket) {
  const from = bucket && bucket.key;
  if (!from) return "";
  let to = from;
  if (filters.bucket === "week") {
    const date = new Date(from + "T00:00:00Z");
    if (!Number.isNaN(date.getTime())) {
      date.setUTCDate(date.getUTCDate() + 6);
      to = date.toISOString().slice(0, 10);
    }
  }
  const query = new URLSearchParams();
  query.set("from", from);
  query.set("to", to);
  if (filters.kind) query.set("kind", filters.kind);
  if (filters.agent) query.set("agent", filters.agent);
  if (filters.raw === "include") query.set("raw", "include");
  return "/archive?" + query.toString();
}

/* ------------------------------------------------------------------ chart -- */

function chartCard(data, context, filters) {
  const allSeries = seriesFor(data);
  const buckets = bucketsFor(data, allSeries);
  const chartHost = el("div", { class: "va-chart-host" });
  const legendHost = el("div", { class: "va-chart-legend" });

  function paintChart() {
    clear(chartHost);
    const visible = allSeries.filter((item) => !current.hidden.has(item.id));
    if (!buckets.length || Number(data.totals && data.totals.memories) === 0 && !buckets.some((b) => b.total > 0)) {
      chartHost.appendChild(emptyState({
        icon: "timeline",
        title: filters.agent || filters.kind || filters.raw === "include"
          ? "当前筛选下没有入档"
          : "这段时间没有入档",
        hint: "调整时间范围或筛选条件，或到记忆库查看全部记录。",
        plain: true,
        action: [
          { label: "清除筛选", icon: "filter", onClick: () => setFilters(context, filters, { agent: "", kind: "", raw: "exclude" }) },
          { label: "记忆库", icon: "archive", onClick: () => context.navigate("/archive") },
        ],
      }));
      return;
    }
    if (!visible.length) {
      chartHost.appendChild(el("p", { class: "va-note" }, "所有 Agent 序列已被图例隐藏。点击下方图例恢复显示。"));
      return;
    }
    chartHost.appendChild(stackedBars({
      buckets,
      series: visible,
      width: 1040,
      height: 300,
      label: "按 Agent 分层的入档数量年轮图",
      onSelect: (bucket) => {
        const target = drillTarget(filters, bucket);
        if (target) context.navigate(target);
      },
    }));
  }

  function paintLegend() {
    clear(legendHost);
    if (!allSeries.length) return;
    legendHost.appendChild(legend({
      items: allSeries.map((item) => ({
        id: item.id,
        label: item.label,
        tone: item.tone,
        active: !current.hidden.has(item.id),
      })),
      onToggle: (id, active) => {
        if (active) current.hidden.delete(id);
        else current.hidden.add(id);
        paintChart();
      },
      label: "Agent 图例（点击显隐）",
    }));
  }

  paintChart();
  paintLegend();

  return card({
    eyebrow: "GROWTH · " + (data.bucket === "week" ? "按周" : "按日"),
    title: "入档年轮",
    body: el("div", { class: "va-timeline-body" },
      chartHost,
      legendHost,
      el("p", { class: "va-note" },
        "柱体按 Agent 分层，其余来源合并为「其他」。悬停查看分解，点击柱体进入该时段的记忆库。")),
  });
}

/* ------------------------------------------------------------------- side -- */

function eventsCard(data) {
  const rows = Array.isArray(data.buckets) ? data.buckets : [];
  const values = rows.map((bucket) => Number(bucket.events) || 0);
  const total = Number(data.totals && data.totals.events) || 0;
  return card({
    eyebrow: "EVENT FLOW",
    title: "事件流",
    body: el("div", { class: "va-events" },
      metric({
        label: "事件总量",
        value: fmtInt(total),
        small: true,
        hint: "与入档桶同范围 · " + (data.bucket === "week" ? "按周汇总" : "按日汇总"),
      }),
      el("div", { class: "va-events-spark" },
        sparkline({
          values,
          width: 520,
          height: 56,
          tone: "link",
          label: "事件数量趋势",
          title: "每个时间桶的事件数",
        }))),
  });
}

function sourcesCard(data) {
  const agents = Array.isArray(data.agents) ? data.agents : [];
  const items = agents.slice(0, 10).map((entry, index) => ({
    label: entry.agent,
    value: Number(entry.total) || 0,
    tone: SERIES_TONES[index % SERIES_TONES.length],
  }));
  const total = Number(data.totals && data.totals.memories) || 0;
  return card({
    eyebrow: "AGENT SHARE",
    title: "来源份额",
    body: el("div", { class: "va-sources" },
      barList({ items, total: total || undefined, mono: true, maxBars: 10, label: "当前时间范围内的 Agent 份额" }),
      el("p", { class: "va-note" }, "仅统计当前筛选范围内的入档；raw 证据默认排除。")),
  });
}

/* ------------------------------------------------------------------- paint -- */

function updateKnown(data, filters) {
  if (!filters.agent && Array.isArray(data.agents)) {
    for (const entry of data.agents) {
      if (entry && entry.agent) knownAgents.add(String(entry.agent));
    }
  }
  if (!filters.kind && Array.isArray(data.buckets)) {
    for (const bucket of data.buckets) {
      for (const kind of Object.keys((bucket && bucket.kinds) || {})) {
        if (kind) knownKinds.add(String(kind));
      }
    }
  }
  for (const item of seriesFor(data)) {
    if (item.id !== "__other") knownAgents.add(item.id);
  }
}

function paint(root, data, context, filters) {
  clear(root);
  updateKnown(data, filters);
  root.appendChild(controlBar(context, filters));
  const totals = data.totals || {};
  root.appendChild(el("div", { class: "atlas-grid atlas-grid--4" },
    metric({ label: "TOTAL · 入档总量", value: fmtInt(totals.memories), hint: fmtInt(data.days) + " 天 · " + (data.bucket === "week" ? "按周" : "按日") }),
    metric({ label: "AVERAGE · 日均", value: formatAverage(totals.average), hint: "每个时间桶的平均入档" }),
    metric({ label: "PEAK · 峰值", value: fmtInt(totals.peak), hint: "单桶最高入档数" }),
    metric({ label: "EVENTS · 事件", value: fmtInt(totals.events), hint: "同范围内的 ledger_events" })));
  root.appendChild(chartCard(data, context, filters));
  root.appendChild(el("div", { class: "atlas-grid atlas-grid--2 va-start-grid" },
    eventsCard(data),
    sourcesCard(data)));
  current.painted = true;
}

/* ------------------------------------------------------------------- entry -- */

export async function render(context) {
  const root = el("div", { class: "va-view va-timeline" });
  const filters = readFilters(context.params);
  current = { root, filters, hidden: new Set(), painted: false };
  root.appendChild(skeleton(4));
  try {
    const data = await fetchTimeline(context, filters);
    if (context.signal.aborted) return root;
    paint(root, data || {}, context, filters);
  } catch (error) {
    if (error && error.name === "AbortError") return root;
    clear(root);
    root.appendChild(errorState({ title: "年轮加载失败", error, onRetry: () => context.refresh() }));
  }
  return root;
}

export async function refresh(context) {
  const root = current.root;
  if (!root || !root.isConnected) return;
  const filters = current.filters || readFilters(context.params);
  try {
    const data = await fetchTimeline(context, filters);
    if (context.signal.aborted) return;
    paint(root, data || {}, context, filters);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (current.painted) context.toast("年轮刷新失败：" + String((error && (error.detail || error.message)) || "未知错误"), { tone: "warn" });
    else {
      clear(root);
      root.appendChild(errorState({ title: "年轮加载失败", error, onRetry: () => context.refresh() }));
    }
  }
}

