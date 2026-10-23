/* ============================================================================
   HippoMind Atlas — recall (检索)
   One big serif query field over the ledger's FTS + LIKE double channel.
   Hits are ranked, highlighted by wrapping matched text in <mark> nodes (never
   markup), and open the same inspector as the archive.
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import { absTime, fmtInt, fmtMs, relTime, truncate } from "../core/format.js";
import {
  chip, emptyState, errorState, meter, pager, segmented, skeleton,
  textField, toggleField,
} from "../components.js";
import { icon } from "../icons.js";
import { lifecycleBadge, memoryKindBadge, openInspector } from "../components/inspector.js";

export const meta = {
  id: "recall",
  index: "03",
  title: "检索",
  eyebrow: "RECALL",
  subtitle: "用关键词召回记忆：FTS 与 LIKE 双通道，按相关性排序",
};

const SUGGESTIONS = ["验收 标准", "决策 理由", "阻塞", "Context Pack", "token 权限", "FTS 检索", "部署 脚本", "用户偏好"];
const LIMITS = [20, 50, 100];
const DEFAULT_LIMIT = 20;

let live = null;

function isAbort(error) {
  return error && error.name === "AbortError";
}

function readState(params) {
  const limit = Number(params.get("limit"));
  return {
    q: params.get("q") || "",
    offset: Math.max(0, Number(params.get("offset")) || 0),
    limit: LIMITS.includes(limit) ? limit : DEFAULT_LIMIT,
    agent: params.get("agent") || "",
    project: params.get("project") || "",
    from: params.get("from") || "",
    to: params.get("to") || "",
    raw: params.get("raw") === "include",
    requireEvidence: params.get("require_evidence") !== "0",
  };
}

function hasAdvanced(state) {
  return Boolean(state.agent || state.project || state.from || state.to ||
    state.raw || !state.requireEvidence || state.limit !== DEFAULT_LIMIT);
}

function buildQuery(state) {
  const query = {};
  if (state.q) query.q = state.q;
  if (state.offset > 0) query.offset = String(state.offset);
  if (state.limit !== DEFAULT_LIMIT) query.limit = String(state.limit);
  if (state.agent) query.agent = state.agent;
  if (state.project) query.project = state.project;
  if (state.from) query.from = state.from;
  if (state.to) query.to = state.to;
  if (state.raw) query.raw = "include";
  if (!state.requireEvidence) query.require_evidence = "0";
  return query;
}

function apply(instance, patch) {
  const next = Object.assign({}, instance.state, patch);
  instance.context.setQuery(buildQuery(next));
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^$\u007b\u007d()|\[\]\\]/g, "\\$&");
}

/* Every match becomes a real <mark> node: the source text is still a text
   node, so a payload can never become markup. */
function highlight(text, terms) {
  const value = String(text === null || text === undefined ? "" : text);
  const unique = Array.from(new Set(
    (Array.isArray(terms) ? terms : []).map((term) => String(term || "").trim()).filter(Boolean)
  )).sort((a, b) => b.length - a.length);
  if (!unique.length) return document.createTextNode(value);
  const pattern = new RegExp("(" + unique.map(escapeRegExp).join("|") + ")", "gi");
  const fragment = document.createDocumentFragment();
  let cursor = 0;
  let match = pattern.exec(value);
  while (match !== null) {
    if (match.index > cursor) fragment.appendChild(document.createTextNode(value.slice(cursor, match.index)));
    fragment.appendChild(el("mark", { class: "vb-mark" }, match[0]));
    cursor = match.index + match[0].length;
    if (match[0].length === 0) pattern.lastIndex += 1;
    match = pattern.exec(value);
  }
  if (cursor < value.length) fragment.appendChild(document.createTextNode(value.slice(cursor)));
  return fragment;
}

function termsFor(query) {
  const text = String(query || "").trim();
  if (!text) return [];
  const terms = text.split(/\s+/).filter(Boolean);
  if (!terms.includes(text)) terms.push(text);
  return terms;
}

function evidenceList(evidence) {
  const list = el("ol", { class: "vb-evidence" });
  (Array.isArray(evidence) ? evidence : []).forEach((entry, index) => {
    const value = typeof entry === "string" ? entry : (
      entry && typeof entry === "object" ? (entry.ref || entry.memory_id || "") : String(entry)
    );
    if (!value) return;
    list.appendChild(el("li", { class: "vb-evidence-item" },
      el("span", { class: "vb-evidence-index" }, String(index + 1).padStart(2, "0")),
      el("span", { class: "vb-evidence-text" }, value)));
  });
  return list;
}

function renderHit(hit, rank, terms, maxScore, instance) {
  const evidence = Array.isArray(hit.evidence) ? hit.evidence : [];
  const row = el("article", {
    class: "vb-hit",
    tabindex: "0",
    role: "button",
    "aria-label": "打开记忆详情：" + truncate(hit.content || hit.memory_id, 60),
  });

  const head = el("div", { class: "vb-hit-head" },
    memoryKindBadge(hit.kind),
    lifecycleBadge(hit.lifecycle),
    el("span", { class: "vb-hit-agent" }, hit.source_agent || "unknown"),
    hit.project ? el("span", { class: "vb-hit-project" }, hit.project) : null,
    el("span", { class: "vb-hit-time", title: absTime(hit.created_at, { seconds: true }) },
      relTime(hit.created_at)),
    el("span", { class: "vb-hit-evidence-count", title: "证据条数" },
      icon("archive", { size: 12 }), fmtInt(evidence.length)));

  const content = el("div", { class: "vb-hit-content" },
    highlight(truncate(String(hit.content || ""), 480), terms));

  const main = el("div", { class: "vb-hit-main" }, head, content);
  if (evidence.length) {
    main.appendChild(el("details", { class: "vb-hit-evidence" },
      el("summary", {}, "证据 " + fmtInt(evidence.length)),
      evidenceList(evidence)));
  }

  const score = Number(hit.score) || 0;
  const scoreBlock = el("div", { class: "vb-hit-score" },
    meter({
      label: "score",
      value: score,
      max: Math.max(0.001, maxScore),
      tone: "accent",
      valueText: score.toFixed(3),
    }));

  row.append(
    el("div", { class: "vb-hit-rank" }, String(rank).padStart(2, "0")),
    main,
    scoreBlock);

  const open = () => {
    openInspector(hit.memory_id, {
      signal: instance.context.signal,
      onChanged: () => { void load(instance, { silent: true }); },
    });
  };
  row.addEventListener("click", (event) => {
    if (event.target instanceof Element && event.target.closest("a, button, summary, details")) return;
    open();
  });
  row.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });
  return row;
}

function suggestionChips(instance) {
  const wrap = el("div", { class: "vb-suggestions", role: "group", "aria-label": "建议关键词" });
  for (const term of SUGGESTIONS) {
    wrap.appendChild(chip(term, {
      onClick: () => instance.context.setQuery({ q: term }),
    }));
  }
  return wrap;
}

function emptyFor(instance, kind) {
  const { state } = instance;
  if (kind === "noquery") {
    return emptyState({
      icon: "recall",
      title: "输入关键词开始检索",
      hint: "检索走 FTS5 trigram 与 LIKE 双通道，默认只返回带证据的记忆；" +
        "需要原始聊天或放宽证据门槛时可展开「高级选项」。" +
        "命中词会安全高亮，点击结果打开详情抽屉。",
      action: [suggestionChips(instance)],
    });
  }
  return emptyState({
    icon: "search",
    title: "没有召回结果",
    hint: "可以换更短或更常见的词；关闭 require_evidence 会纳入无证据记忆；" +
      "raw 模式会搜索原始聊天（默认排除）。当前筛选：" +
      (state.requireEvidence ? "要求证据" : "不要求证据") + " · " +
      (state.raw ? "含原始聊天" : "排除原始聊天") + "。",
    action: [
      suggestionChips(instance),
      {
        label: "在记忆库中按内容过滤", icon: "archive",
        onClick: () => instance.context.navigate("/archive?q=" + encodeURIComponent(state.q)),
      },
    ],
  });
}

function renderEmpty(instance) {
  const { bodyHost } = instance;
  clear(bodyHost);
  bodyHost.appendChild(emptyFor(instance, instance.state.q ? "noresults" : "noquery"));
}

function buildSearchBar(instance) {
  const { state } = instance;
  const input = el("input", {
    class: "vb-recall-input",
    type: "text",
    value: state.q,
    placeholder: "召回记忆：输入关键词，回车检索…",
    "aria-label": "检索关键词",
    autocomplete: "off",
    spellcheck: "false",
  });
  instance.searchInput = input;
  const submit = () => {
    const query = input.value.trim();
    if (!query) {
      instance.context.setQuery({});
      return;
    }
    apply(instance, { q: query, offset: 0 });
  };
  input.addEventListener("input", () => { state.q = input.value; });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
    if (event.key === "Escape") {
      input.value = "";
      state.q = "";
      instance.context.setQuery({});
    }
  });
  const button = el("button", {
    class: "atlas-btn atlas-btn--primary vb-recall-submit",
    type: "button",
    on: { click: submit },
  }, icon("recall", { size: 16 }), el("span", {}, "检索"));
  return el("div", { class: "vb-recall-bar" },
    el("span", { class: "vb-recall-mark" }, icon("recall", { size: 22 })),
    input, button);
}

function dateInput(label, value, onCommit) {
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

function buildAdvanced(instance) {
  const { state } = instance;
  const arrow = el("span", { class: "vb-advanced-arrow" }, icon("chevron-right", { size: 12 }));
  const toggle = el("button", {
    class: "vb-advanced-toggle",
    type: "button",
    "aria-expanded": "false",
    on: {
      click: () => {
        instance.advancedOpen = !instance.advancedOpen;
        toggle.setAttribute("aria-expanded", instance.advancedOpen ? "true" : "false");
        panel.hidden = !instance.advancedOpen;
        arrow.style.transform = instance.advancedOpen ? "rotate(90deg)" : "none";
      },
    },
  }, arrow, el("span", {}, "高级选项"));
  toggle.setAttribute("aria-expanded", instance.advancedOpen ? "true" : "false");
  arrow.style.transform = instance.advancedOpen ? "rotate(90deg)" : "none";

  const summary = hasAdvanced(state)
    ? "已应用：" + [
      state.agent ? "agent=" + state.agent : "",
      state.project ? "project=" + state.project : "",
      state.from || state.to ? "时间范围" : "",
      state.raw ? "含 raw" : "",
      state.requireEvidence ? "" : "不要求证据",
      state.limit !== DEFAULT_LIMIT ? "limit=" + state.limit : "",
    ].filter(Boolean).join(" · ")
    : "agent / project / 时间范围 / raw / 证据门槛 / 每页";

  const agent = textField({
    label: "agent",
    placeholder: "例如 codex（留空不限）",
    value: state.agent,
    onInput: (event, value) => { state.agent = value; },
    onKeyDown: (event) => { if (event.key === "Enter") apply(instance, { agent: state.agent.trim(), offset: 0 }); },
  });
  const project = textField({
    label: "project",
    placeholder: "例如 atlas-gui（留空不限）",
    value: state.project,
    onInput: (event, value) => { state.project = value; },
    onKeyDown: (event) => { if (event.key === "Enter") apply(instance, { project: state.project.trim(), offset: 0 }); },
  });
  const applyButton = el("button", {
    class: "atlas-btn",
    type: "button",
    on: {
      click: () => apply(instance, {
        agent: state.agent.trim(),
        project: state.project.trim(),
        offset: 0,
      }),
    },
  }, icon("filter", { size: 13 }), el("span", {}, "应用筛选"));

  const rawToggle = toggleField({
    label: "搜索 raw 原始聊天",
    checked: state.raw,
    onChange: (event, checked) => apply(instance, { raw: Boolean(checked), offset: 0 }),
  });
  const evidenceToggle = toggleField({
    label: "要求有证据（require_evidence）",
    checked: state.requireEvidence,
    onChange: (event, checked) => apply(instance, { requireEvidence: Boolean(checked), offset: 0 }),
  });
  const limitSeg = segmented({
    label: "每页条数",
    options: LIMITS.map((value) => ({ value: String(value), label: String(value) })),
    value: String(state.limit),
    onChange: (value) => apply(instance, { limit: Number(value), offset: 0 }),
  });

  const panel = el("div", { class: "vb-advanced-panel" },
    el("div", { class: "vb-advanced-grid" },
      agent, project,
      dateInput("起始 from", state.from, (value) => apply(instance, { from: value, offset: 0 })),
      dateInput("截止 to", state.to, (value) => apply(instance, { to: value, offset: 0 })),
      el("div", { class: "atlas-field vb-advanced-apply" },
        el("span", { class: "atlas-field-label" }, "提交筛选"), applyButton),
      el("div", { class: "atlas-field vb-advanced-seg" },
        el("span", { class: "atlas-field-label" }, "每页"), limitSeg)),
    el("div", { class: "vb-advanced-toggles" }, rawToggle, evidenceToggle));
  panel.hidden = !instance.advancedOpen;

  return el("div", { class: "vb-advanced" },
    el("div", { class: "vb-advanced-head" },
      toggle,
      el("span", { class: "vb-advanced-summary muted" }, summary)),
    panel);
}

function skeletonBlock() {
  return el("div", { class: "vb-loading" }, skeleton(6));
}

function renderResults(instance) {
  const { bodyHost, data, state, timing } = instance;
  clear(bodyHost);
  const hits = Array.isArray(data && data.hits) ? data.hits : [];
  const offset = Math.max(0, Number(data && data.offset) || state.offset);
  const limit = Math.max(1, Number(data && data.limit) || state.limit);

  const metaBar = el("div", { class: "vb-recall-meta" },
    icon("info", { size: 13 }),
    el("span", {},
      "FTS5 trigram + LIKE 双通道召回，score 为融合相关性；" +
      (state.requireEvidence ? "只返回带证据的记忆。" : "包含无证据记忆。")),
    el("span", { class: "vb-recall-timing" },
      "前端耗时 ", el("b", {}, fmtMs(timing)),
      " · 第 ", el("b", {}, fmtInt(offset + 1)), "–",
      el("b", {}, fmtInt(offset + hits.length)), " 条"));
  bodyHost.appendChild(metaBar);

  if (!hits.length) {
    bodyHost.appendChild(emptyFor(instance, "noresults"));
    return;
  }

  const terms = termsFor(state.q);
  const maxScore = hits.reduce((max, hit) => Math.max(max, Number(hit.score) || 0), 0.001);
  const list = el("div", { class: "vb-hits" });
  hits.forEach((hit, index) => {
    list.appendChild(renderHit(hit, offset + index + 1, terms, maxScore, instance));
  });
  bodyHost.appendChild(list);

  const hasMore = hits.length >= limit;
  const total = offset + hits.length + (hasMore ? 1 : 0);
  bodyHost.appendChild(el("div", { class: "vb-recall-foot" },
    pager({
      offset,
      limit,
      total,
      onChange: (nextOffset) => apply(instance, { offset: Math.max(0, nextOffset) }),
    }),
    el("span", { class: "vb-recall-foot-note muted" },
      hasMore ? "还有更多结果，可翻页或缩小关键词范围。" : "已到结果末尾。")));
}

async function load(instance, options = {}) {
  const { context, state, bodyHost } = instance;
  if (instance.destroyed) return;
  if (!state.q) { renderEmpty(instance); return; }
  if (options.initial || !options.silent) {
    clear(bodyHost);
    bodyHost.appendChild(skeletonBlock());
  }
  instance.root.classList.add("is-busy");
  const started = performance.now();
  try {
    const data = await api.post("/recall", {
      query: state.q,
      limit: state.limit,
      offset: state.offset,
      options: {
        scope: "shared",
        project: state.project,
        source_agents: state.agent ? [state.agent] : [],
        from_time: state.from,
        to_time: state.to,
        raw_chat: state.raw,
        require_evidence: state.requireEvidence,
      },
    }, { signal: context.signal });
    if (instance.destroyed) return;
    instance.data = data || {};
    instance.timing = performance.now() - started;
    renderResults(instance);
  } catch (error) {
    if (isAbort(error) || instance.destroyed) return;
    clear(bodyHost);
    bodyHost.appendChild(errorState({
      title: "检索失败",
      error,
      onRetry: () => { void load(instance, { initial: true }); },
    }));
  } finally {
    instance.root.classList.remove("is-busy");
  }
}

export async function render(context) {
  const state = readState(context.params);
  const root = el("div", { class: "vb-view vb-recall" });
  const bodyHost = el("div", { class: "vb-recall-body" });
  const instance = {
    context, state, root, bodyHost,
    data: null, timing: null, searchInput: null,
    advancedOpen: hasAdvanced(state),
    destroyed: false,
  };
  live = instance;
  context.onCleanup(() => {
    instance.destroyed = true;
    if (live === instance) live = null;
  });

  const searchBar = buildSearchBar(instance);
  root.append(searchBar, buildAdvanced(instance), bodyHost);
  window.requestAnimationFrame(() => {
    if (!instance.destroyed && instance.searchInput && instance.searchInput.isConnected) {
      instance.searchInput.focus();
      instance.searchInput.select();
    }
  });

  if (state.q) await load(instance, { initial: true });
  else renderEmpty(instance);
  return root;
}

export async function refresh(context) {
  if (!live || live.context !== context) return;
  if (!live.state.q) return;
  await load(live, { silent: true });
}

