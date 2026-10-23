/* ============================================================================
   HippoMind Atlas — archive (记忆库)
   Browsable projection of the ledger: filters in the URL, rows in a dense
   table, provenance in the shared inspector. Raw chat is excluded by default
   and the UI says so: that is a privacy decision, not a bug.
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import { store } from "../core/store.js";
import { absTime, fmtInt, relTime } from "../core/format.js";
import {
  badge, dataTable, emptyState, errorState, pager, segmented, selectField,
  skeleton, textField,
} from "../components.js";
import { icon } from "../icons.js";
import {
  lifecycleBadge, memoryKindBadge, openInspector,
} from "../components/inspector.js";

export const meta = {
  id: "archive",
  index: "02",
  title: "记忆库",
  eyebrow: "ARCHIVE",
  subtitle: "逐条翻阅账本中的结构化记忆，查看来源与谱系",
};

const SORTS = [
  { value: "updated", label: "最近更新" },
  { value: "created", label: "最近创建" },
  { value: "weight", label: "权重" },
];
const RAW_MODES = [
  { value: "exclude", label: "排除" },
  { value: "include", label: "包含" },
  { value: "only", label: "仅原始" },
];
const PAGE_SIZES = [25, 50, 100];

let live = null;

function isAbort(error) {
  return error && error.name === "AbortError";
}

function readState(params) {
  const settings = store.get("settings") || {};
  const raw = params.get("raw");
  const sort = params.get("sort");
  const pageSize = Number(settings.pageSize);
  return {
    q: params.get("q") || "",
    kind: params.get("kind") || "",
    agent: params.get("agent") || "",
    lifecycle: params.get("lifecycle") || "",
    project: params.get("project") || "",
    from: params.get("from") || "",
    to: params.get("to") || "",
    raw: RAW_MODES.some((mode) => mode.value === raw) ? raw : "exclude",
    sort: SORTS.some((item) => item.value === sort) ? sort : "updated",
    offset: Math.max(0, Number(params.get("offset")) || 0),
    limit: PAGE_SIZES.includes(pageSize) ? pageSize : 50,
    focus: params.get("focus") || "",
  };
}

function buildQuery(state) {
  const query = {};
  for (const key of ["q", "kind", "agent", "lifecycle", "project", "from", "to"]) {
    if (state[key]) query[key] = state[key];
  }
  if (state.raw !== "exclude") query.raw = state.raw;
  if (state.sort !== "updated") query.sort = state.sort;
  if (state.offset > 0) query.offset = String(state.offset);
  return query;
}

function apply(instance, patch) {
  const next = Object.assign({}, instance.state, patch);
  instance.context.setQuery(buildQuery(next));
}

function buildParams(state) {
  const params = {
    limit: state.limit,
    offset: state.offset,
    raw: state.raw,
    sort: state.sort,
    order: "desc",
    facets: 1,
  };
  if (state.q) params.q = state.q;
  if (state.kind) params.kind = state.kind;
  if (state.agent) params.agent = state.agent;
  if (state.lifecycle) params.lifecycle = state.lifecycle;
  if (state.project) params.project = state.project;
  if (state.from) params.from = state.from;
  if (state.to) params.to = state.to;
  return params;
}

function facetOptions(items, current, allLabel, withCount) {
  const options = [{ value: "", label: allLabel }];
  const seen = new Set([""]);
  for (const item of Array.isArray(items) ? items : []) {
    const value = String(item.value);
    if (!value || seen.has(value)) continue;
    seen.add(value);
    options.push({
      value,
      label: withCount ? value + "（" + fmtInt(item.count) + "）" : value,
    });
  }
  if (current && !seen.has(current)) options.push({ value: current, label: current });
  return options;
}

function dateField(label, value, onCommit) {
  const input = el("input", {
    class: "atlas-input",
    type: "date",
    value: value || "",
    "aria-label": label,
  });
  input.addEventListener("change", () => onCommit(input.value));
  return el("label", { class: "atlas-field vb-date-field" },
    el("span", { class: "atlas-field-label" }, label), input);
}

function segmentedField(label, options, value, onChange) {
  return el("div", { class: "atlas-field vb-seg-field" },
    el("span", { class: "atlas-field-label" }, label),
    segmented({ options, value, onChange }));
}

function buildToolbar(instance) {
  const { state, facets, context } = instance;
  const toolbar = el("div", { class: "vb-toolbar" });

  const search = textField({
    placeholder: "按内容过滤（LIKE，≤200 字符）",
    value: state.q,
    large: true,
    ariaLabel: "按内容过滤",
    onInput: (event, value) => { state.q = value; },
    onKeyDown: (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        apply(instance, { q: state.q.trim(), offset: 0 });
      }
    },
  });
  const submit = el("button", {
    class: "atlas-btn atlas-btn--primary",
    type: "button",
    on: { click: () => apply(instance, { q: state.q.trim(), offset: 0 }) },
  }, icon("search", { size: 14 }), el("span", {}, "过滤"));

  const reset = el("button", {
    class: "atlas-btn atlas-btn--ghost",
    type: "button",
    title: "清空筛选条件",
    on: { click: () => context.setQuery({}) },
  }, icon("refresh", { size: 13 }), el("span", {}, "重置"));

  toolbar.appendChild(el("div", { class: "vb-toolbar-search" },
    el("div", { class: "vb-toolbar-search-field" }, search),
    submit, reset));

  const filters = el("div", { class: "vb-filter-grid" });
  filters.append(
    selectField({
      label: "kind",
      options: facetOptions(facets && facets.kinds, state.kind, "全部 kind", true),
      value: state.kind,
      onChange: (event, value) => apply(instance, { kind: value, offset: 0 }),
    }),
    selectField({
      label: "agent",
      options: facetOptions(facets && facets.agents, state.agent, "全部 agent", true),
      value: state.agent,
      onChange: (event, value) => apply(instance, { agent: value, offset: 0 }),
    }),
    selectField({
      label: "lifecycle",
      options: facetOptions(facets && facets.lifecycles, state.lifecycle, "全部 lifecycle", true),
      value: state.lifecycle,
      onChange: (event, value) => apply(instance, { lifecycle: value, offset: 0 }),
    }),
    selectField({
      label: "project",
      options: facetOptions(facets && facets.projects, state.project, "全部 project", true),
      value: state.project,
      onChange: (event, value) => apply(instance, { project: value, offset: 0 }),
    }),
    dateField("起始 from", state.from, (value) => apply(instance, { from: value, offset: 0 })),
    dateField("截止 to", state.to, (value) => apply(instance, { to: value, offset: 0 })),
    segmentedField("raw 原始聊天", RAW_MODES, state.raw,
      (value) => apply(instance, { raw: value, offset: 0 })),
    segmentedField("排序", SORTS, state.sort,
      (value) => apply(instance, { sort: value, offset: 0 })),
  );
  toolbar.appendChild(filters);

  toolbar.appendChild(el("div", { class: "vb-note" },
    icon("info", { size: 13 }),
    el("span", {},
      "默认不含 raw 原始聊天（raw_chat=0）：原始证据只用于审计与取证，不进入日常浏览。",
      "这是有意的隐私选择，需要时可切换到「包含 / 仅原始」。")));
  return toolbar;
}

function contentCell(row) {
  const wrap = el("div", { class: "vb-content-cell" });
  const text = el("div", { class: "atlas-cell-clamp" }, row.content || "（无内容）");
  wrap.appendChild(text);
  if (row.raw_chat) {
    wrap.appendChild(el("span", { class: "vb-raw-flag", title: "raw 原始聊天" }, "raw"));
  }
  if (row.has_claims && !row.pinned) {
    wrap.appendChild(el("span", { class: "vb-claims-flag", title: "含结构化 claim" }, "claim"));
  }
  return wrap;
}

function timeCell(row) {
  const iso = row.updated_at || row.created_at;
  return el("div", { class: "vb-time", title: absTime(iso, { seconds: true }) },
    el("span", { class: "vb-time-rel" }, relTime(iso)),
    el("span", { class: "vb-time-abs" }, absTime(iso)));
}

function buildTable(instance, data) {
  const { context, state } = instance;
  const items = Array.isArray(data.items) ? data.items : [];
  const total = Math.max(0, Number(data.total) || 0);
  const offset = Math.max(0, Number(data.offset) || 0);
  const limit = Math.max(1, Number(data.limit) || state.limit);

  const empty = total === 0
    ? emptyState({
      title: state.offset > 0 ? "这一页没有记录" : "没有符合条件的记忆",
      hint: state.offset > 0
        ? "当前 offset 超出结果范围，回到第一页继续。"
        : "默认浏览不含 raw 原始聊天；可以清空筛选、切换 raw 模式，或到检索里搜索全文。",
      action: [
        {
          label: "清空筛选", icon: "refresh",
          onClick: () => context.setQuery({}),
        },
        {
          label: "去检索", icon: "recall",
          onClick: () => context.navigate("/recall" + (state.q ? "?q=" + encodeURIComponent(state.q) : "")),
        },
      ],
    })
    : emptyState({ plain: true, title: "本页为空" });

  const columns = [
    {
      key: "updated_at", label: "更新", width: 104,
      render: (row) => timeCell(row),
    },
    {
      key: "content", label: "摘要",
      render: (row) => contentCell(row),
    },
    {
      key: "kind", label: "kind", width: 104,
      render: (row) => memoryKindBadge(row.kind),
    },
    {
      key: "source_agent", label: "agent", width: 128, mono: true,
      render: (row) => el("span", { class: "vb-agent" }, row.source_agent || "—"),
    },
    {
      key: "lifecycle", label: "lifecycle", width: 104,
      render: (row) => lifecycleBadge(row.lifecycle),
    },
    {
      key: "weight", label: "weight", align: "right", width: 72, mono: true,
      render: (row) => Number(row.weight || 0).toFixed(2),
    },
    {
      key: "pinned", label: "", width: 36, align: "center",
      render: (row) => (row.pinned
        ? el("span", { class: "vb-pin", title: "已置顶" }, icon("pin", { size: 12 }))
        : el("span", { class: "vb-pin" })),
    },
  ];

  const table = dataTable({
    columns,
    rows: items,
    empty,
    onRowClick: (row) => {
      openInspector(row.memory_id, {
        signal: context.signal,
        onChanged: () => { void load(instance, { silent: true }); },
      });
    },
    rowKey: (row) => row.memory_id,
  });

  const pageSize = segmented({
    label: "每页条数",
    options: PAGE_SIZES.map((size) => ({ value: String(size), label: String(size) })),
    value: String(state.limit),
    onChange: (value) => {
      const size = Number(value);
      if (!PAGE_SIZES.includes(size)) return;
      store.setSetting("pageSize", size);
      if (state.offset > 0) {
        apply(instance, { offset: 0 });
        return;
      }
      state.limit = size;
      void load(instance, { silent: true });
    },
  });

  const footer = el("div", { class: "vb-table-foot" },
    pager({
      offset,
      limit,
      total,
      onChange: (nextOffset) => apply(instance, { offset: Math.max(0, nextOffset) }),
    }),
    el("div", { class: "vb-pagesize" },
      el("span", { class: "atlas-field-label" }, "每页"), pageSize));

  const wrap = el("div", { class: "vb-table-block" });
  wrap.append(table, footer);
  return wrap;
}

function skeletonBlock() {
  return el("div", { class: "vb-loading" },
    skeleton(2),
    el("div", { class: "vb-loading-gap" }),
    skeleton(6));
}

function maybeOpenFocus(instance) {
  const { state, context } = instance;
  if (!state.focus || instance.focusHandled) return;
  instance.focusHandled = true;
  void openInspector(state.focus, {
    signal: context.signal,
    onChanged: () => { void load(instance, { silent: true }); },
  });
}

function renderFull(instance, data) {
  const { root } = instance;
  clear(root);
  const toolbar = buildToolbar(instance);
  const body = el("div", { class: "vb-body" });
  body.appendChild(buildTable(instance, data));
  instance.toolbarHost = toolbar;
  instance.bodyHost = body;
  root.append(toolbar, body);
  maybeOpenFocus(instance);
}

function renderBodyOnly(instance, data) {
  const body = instance.bodyHost;
  if (!body) { renderFull(instance, data); return; }
  clear(body);
  body.appendChild(buildTable(instance, data));
  maybeOpenFocus(instance);
}

async function load(instance, options = {}) {
  const { context, state, root } = instance;
  if (instance.destroyed) return;
  if (options.initial) {
    clear(root);
    root.appendChild(skeletonBlock());
  }
  instance.root.classList.toggle("is-busy", Boolean(options.silent));
  try {
    const data = await api.get("/api/v1/memories", buildParams(state), { signal: context.signal });
    if (instance.destroyed) return;
    if (data && data.facets) instance.facets = data.facets;
    if (options.silent) renderBodyOnly(instance, data || {});
    else renderFull(instance, data || {});
  } catch (error) {
    if (isAbort(error) || instance.destroyed) return;
    clear(root);
    root.appendChild(errorState({
      title: "记忆库加载失败",
      error,
      onRetry: () => { void load(instance, { initial: true }); },
    }));
  } finally {
    instance.root.classList.remove("is-busy");
  }
}

export async function render(context) {
  const state = readState(context.params);
  const root = el("div", { class: "vb-view vb-archive" });
  const instance = {
    context, state, root,
    facets: null,
    focusHandled: false,
    destroyed: false,
    root: root,
    bodyHost: null,
    toolbarHost: null,
  };
  live = instance;
  context.onCleanup(() => {
    instance.destroyed = true;
    if (live === instance) live = null;
  });
  await load(instance, { initial: true });
  return root;
}

export async function refresh(context) {
  if (!live || live.context !== context) return;
  await load(live, { silent: true });
}

