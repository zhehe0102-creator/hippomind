/* ============================================================================
   HippoMind Atlas — recall (检索)
   One big serif query field over the ledger's FTS + LIKE double channel.
   Hits are ranked, highlighted by wrapping matched text in <mark> nodes (never
   markup), and open the same inspector as the archive.

   as_of（双时态切片）与 compose 视图保持同一套写法：同一个 label、同一个
   「留空 = 现在」语义、同样进请求体的 options.as_of。差别只在于 recall 的
   筛选状态是 URL 参数（可分享、可刷新），compose 是表单内部 state。
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
const AS_OF_LABEL = "as_of（双时态切片）";

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
    asOf: params.get("as_of") || "",
  };
}

function hasAdvanced(state) {
  return Boolean(state.agent || state.project || state.from || state.to ||
    state.raw || !state.requireEvidence || state.limit !== DEFAULT_LIMIT ||
    state.asOf);
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
  if (state.asOf) query.as_of = state.asOf;
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
    hint: (instance.appliedAsOf
      ? "当前是 as_of=" + instance.appliedAsOf + " 的历史切片：那一刻可能还没有这条记忆，" +
        "或它当时尚未生效。清空时间点可回到当前状态。"
      : "可以换更短或更常见的词；关闭 require_evidence 会纳入无证据记忆；" +
        "raw 模式会搜索原始聊天（默认排除）。当前筛选：" +
        (state.requireEvidence ? "要求证据" : "不要求证据") + " · " +
        (state.raw ? "含原始聊天" : "排除原始聊天") + "。"),
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

/* as_of 输入 + 清空时间点。清空按钮是「一键回到现在」：只清 as_of，不动
   关键词和其他筛选——用户改的是时间视角，不是检索条件。compose 的整表单
   「清空」在这里不适用，因为那会连查询词一起丢掉。 */
function asOfCell(instance) {
  const { state } = instance;
  const note = el("span", { class: "vb-asof-note" }, "未应用：按 Enter 或点「应用筛选」");
  const field = textField({
    label: AS_OF_LABEL,
    name: "as_of",
    value: state.asOf,
    placeholder: "ISO 时间（可选）",
    monospace: true,
    hint: "留空 = 现在",
    onInput: (event, value) => {
      state.asOf = value;
      /* 草稿态只影响提示，不影响结果区：结果区显示的是真正发出去的
         as_of，避免「输入框里有值但结果还是现在的」被误读成历史切片。 */
      note.classList.toggle("is-visible", value.trim() !== instance.appliedAsOf);
    },
    onKeyDown: (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        apply(instance, { asOf: state.asOf.trim(), offset: 0 });
      }
    },
  });
  const clear = el("button", {
    class: "atlas-btn atlas-btn--ghost atlas-btn--sm vb-asof-clear",
    type: "button",
    title: "清空时间点，回到当前状态",
    on: {
      click: () => {
        field.input.value = "";
        state.asOf = "";
        note.classList.remove("is-visible");
        apply(instance, { asOf: "", offset: 0 });
      },
    },
  }, icon("refresh", { size: 12 }), el("span", {}, "清空时间点"));
  return el("div", { class: "vb-asof-cell" },
    field,
    el("div", { class: "vb-asof-actions" }, clear, note));
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
      state.asOf ? "as_of=" + state.asOf : "",
      state.raw ? "含 raw" : "",
      state.requireEvidence ? "" : "不要求证据",
      state.limit !== DEFAULT_LIMIT ? "limit=" + state.limit : "",
    ].filter(Boolean).join(" · ")
    : "agent / project / 时间范围 / as_of / raw / 证据门槛 / 每页";

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
        asOf: state.asOf.trim(),
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
      asOfCell(instance),
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

/* as_of 生效时结果区最显眼的元素。双时态最容易误导的地方，就是用户把
   「当时为真」当成「现在为真」——所以这条提示必须整条横幅、带图标、带
   时间戳，而不是缩在筛选摘要里的一小段文字。

   文本只取 instance.appliedAsOf（真正进了请求体的那个值），不取输入框
   草稿：草稿改了没提交时结果仍是当前状态，提示也必须仍是当前状态。 */
function sliceBanner(instance) {
  const asOf = instance.appliedAsOf;
  if (!asOf) return null;
  return el("div", {
    class: "vb-recall-slice",
    role: "status",
    "aria-live": "polite",
  },
    icon("info", { size: 15 }),
    el("div", { class: "vb-recall-slice-body" },
      el("b", {}, "历史切片"),
      el("span", { class: "vb-recall-slice-stamp" }, "as_of=" + asOf),
      el("p", { class: "vb-recall-slice-note" },
        "以下结果按该时刻的 valid_from / valid_until 窗口求值，是「当时为真」的记忆，" +
        "不是当前状态：已被后续事实取代或已过期的行会重新出现。" +
        "清空时间点即可回到现在。")));
}

function renderResults(instance) {
  const { bodyHost, data, state, timing } = instance;
  clear(bodyHost);
  const hits = Array.isArray(data && data.hits) ? data.hits : [];
  const offset = Math.max(0, Number(data && data.offset) || state.offset);
  const limit = Math.max(1, Number(data && data.limit) || state.limit);

  const banner = sliceBanner(instance);
  if (banner) bodyHost.appendChild(banner);

  const channelNote = instance.appliedAsOf
    ? "as_of 切片绕过热索引（memory_fts 只装当前可召回集合），候选由有界 LIKE 扫描生成；score 仍为融合相关性。"
    : "FTS5 trigram + LIKE 双通道召回，score 为融合相关性；";
  const metaBar = el("div", { class: "vb-recall-meta" },
    icon("info", { size: 13 }),
    el("span", {}, channelNote +
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
  /* 发的是 appliedAsOf，不是 state.asOf。state.asOf 是输入框草稿，用户每敲
     一个字符它就变；app 的 30s 心跳会调 refresh() -> load() 做静默重载，
     如果这里读草稿，一次后台刷新就会把用户还没提交的时间点偷偷发出去，
     结果区和 URL 立刻对不上（as_of 从没点过「应用」，结果却变成了历史切片）。
     草稿只有经过 apply() 才会变成 appliedAsOf 并写回 URL。 */
  const asOf = instance.appliedAsOf;
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
        as_of: asOf,
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
    /* 已应用的 as_of。挂载时等于 URL 参数；之后只在 apply() 之后由重新挂载
       更新。草稿态（state.asOf）永远不直接进请求体。 */
    appliedAsOf: state.asOf,
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

