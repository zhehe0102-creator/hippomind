/* ============================================================================
   HippoMind Atlas — 06 facts 事实
   Four projections of the same claim graph: conflicts (state=conflict, the
   queue that needs adjudication), superseded (the lineage, pure history),
   all claims, active facts, and procedural skills. Reads are bounded; the
   only actions are copyable CLI commands, never hidden writes.
   ========================================================================= */
"use strict";

import { el, clear, downloadText } from "../core/dom.js";
import { api } from "../core/api.js";
import { absTime, fmtInt, relTime, truncate } from "../core/format.js";
import {
  badge, card, emptyState, errorState, skeleton, tabs, segmented, copyButton,
} from "../components.js";
import { icon } from "../icons.js";
import { claimStateBadge } from "../components/inspector.js";
import { openClaimForm } from "../components/claim-form.js";

export const meta = {
  id: "facts",
  index: "06",
  title: "事实",
  eyebrow: "FACTS",
  subtitle: "claims、冲突谱系、事实投影与程序性技能",
};

const TAB_IDS = ["conflicts", "superseded", "claims", "facts", "skills"];

/* 两个「非活跃」页签各自对应的 claim 状态。
   它们语义不同、来源也不同：conflict 是**待办队列**，superseded 是**历史**。

   踩过的坑：旧代码用 state !== "active" 同时表示这两件事，于是 1,934 条
   superseded 把 22 条 conflict 整个盖住——生产库页签里 6 条全是 superseded，
   用户会以为「没有冲突要处理」。比没有这个页签更糟。

   第二个坑（只改过滤条件修不掉）：/api/v1/claims 按 asserted_at 倒序、
   一页最多 200 行，生产库那 200 行里 conflict 行数实测 = 0。所以两个页签
   必须各自向服务端要 state=conflict / state=superseded，不能从 claims
   那一页里筛——只把 !== 改成 === 会让页签变成空的。 */
const CLAIM_TABS = {
  conflicts: "conflict",
  superseded: "superseded",
};

function isAbort(error) { return error && error.name === "AbortError"; }

function valueText(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value); } catch (error) { return String(value); }
}

function shortDate(iso) {
  if (!iso) return "—";
  return el("span", { class: "vb-time", title: absTime(iso) }, relTime(iso));
}

function listItems(payload, key) {
  return (payload && payload[key] && payload[key].items) || [];
}

function claimItems(payload) {
  return listItems(payload, "claims");
}

/* 页签 -> 该显示的 claim 行。渲染与 CSV 导出共用这一处，
   否则两条路径迟早再次分叉（这个 bug 本身就是分叉造成的）。

   服务端已按 state 过滤过；这里再筛一次是**双保险**：旧 daemon 会把不认识的
   state 静默降级成 all，那样冲突页签会重新混进 active 行。 */
export function claimsForTab(payload, tab) {
  const state = CLAIM_TABS[tab];
  if (!state) return claimItems(payload);
  return listItems(payload, tab).filter((item) => item.state === state);
}

/* 页签计数用服务端 counts（全库口径），不是当前这一页的行数：
   用行数算出来的「冲突 0」和这个 bug 是同一类假象。counts 缺失时
   （旧 daemon / 空账本）退回列表长度，绝不显示 NaN。 */
export function tabCounts(payload) {
  const counts = (payload && payload.claims && payload.claims.counts) || {};
  const pick = (value, fallback) => {
    const number = Number(value);
    return value === null || value === undefined || value === "" || !Number.isFinite(number)
      ? fallback : number;
  };
  return {
    conflicts: pick(counts.conflict, claimsForTab(payload, "conflicts").length),
    superseded: pick(counts.superseded, claimsForTab(payload, "superseded").length),
    claims: pick(counts.total, claimItems(payload).length),
    facts: listItems(payload, "facts").length,
    skills: listItems(payload, "skills").length,
  };
}

/* 服务端一页最多 200 行，而 counts 是全库口径。两者不一致时必须说出来：
   「取代谱系 1934」配一张 200 行的表，不写清楚就是另一个谎。 */
export function truncationNote(payload, tab) {
  const state = CLAIM_TABS[tab];
  if (!state) return null;
  const total = tabCounts(payload)[tab];
  const shown = claimsForTab(payload, tab).length;
  if (!Number.isFinite(total) || total <= shown) return null;
  return "仅显示最近 " + fmtInt(shown) + " 条（全库 " + fmtInt(total) + " 条，按断言时间倒序）";
}

function conflictCommand(item) {
  return "hippomind resolve --claim " + item.claim_id + " --winner " + (item.memory_id || "<memory_id>");
}

/* 只有 state=conflict 才配「裁决命令」。superseded 是已定案的历史，
   对它跑 resolve 只是重放一次旧结论，把命令摆在它旁边会误导操作者。 */
function claimTable(items, options) {
  const opts = options || {};
  if (!items.length) return emptyState(opts.empty || { title: "没有记录" });
  const table = el("table", { class: "atlas-table atlas-table--dense" });
  table.appendChild(el("thead", {}, el("tr", {},
    el("th", {}, "主语"),
    el("th", {}, "谓词 / 槽"),
    el("th", {}, "值"),
    el("th", {}, "状态"),
    el("th", { class: "is-num" }, "置信"),
    el("th", {}, "记忆摘要"),
    opts.withCommand ? el("th", {}, "裁决命令") : null)));
  const body = el("tbody");
  for (const item of items.slice(0, opts.limit || 100)) {
    body.appendChild(el("tr", {},
      el("td", {}, el("span", { class: "vb-claim-subject" }, item.subject || "—")),
      el("td", {}, el("div", { class: "vb-claim-pred" }, item.predicate || "—"),
        el("div", { class: "vb-claim-slot" }, item.slot_key || "")),
      el("td", { class: "vb-claim-value" }, truncate(valueText(item.value), 100)),
      el("td", {}, claimStateBadge(item.state)),
      el("td", { class: "is-num" }, Number(item.confidence || 0).toFixed(2)),
      el("td", { class: "vb-claim-memory" }, truncate(item.memory_preview || "", 110)),
      opts.withCommand ? el("td", {}, copyButton(conflictCommand(item), { label: "复制命令" })) : null));
  }
  table.appendChild(body);
  return el("div", { class: "atlas-table-wrap" }, table);
}

function conflictsTable(items) {
  return claimTable(items, {
    withCommand: true,
    limit: 200,
    empty: {
      title: "没有待裁决的冲突",
      hint: "当同一 (subject, predicate, slot_key) 出现不同值时，账本会把它们标记为 conflict，等待人工裁决。",
    },
  });
}

function supersededTable(items) {
  return claimTable(items, {
    limit: 200,
    empty: {
      title: "没有取代谱系",
      hint: "裁决或新事实顶替旧事实时，败方 claim 会转为 superseded 并保留在这里——它是历史，不是待办。",
    },
  });
}

function claimsTable(items) {
  if (!items.length) {
    return emptyState({
      title: "没有 claims",
      hint: "claims 来自 ingest 记录里的 subject/predicate/slot_key/value 字段。",
    });
  }
  const table = el("table", { class: "atlas-table atlas-table--dense" });
  table.appendChild(el("thead", {}, el("tr", {},
    el("th", {}, "主语"),
    el("th", {}, "谓词 / 槽"),
    el("th", {}, "值"),
    el("th", {}, "状态"),
    el("th", { class: "is-num" }, "置信"),
    el("th", { class: "is-num" }, "断言时间"),
    el("th", {}, "记忆摘要"))));
  const body = el("tbody");
  for (const item of items.slice(0, 200)) {
    body.appendChild(el("tr", {},
      el("td", {}, el("span", { class: "vb-claim-subject" }, item.subject || "—")),
      el("td", {}, el("div", { class: "vb-claim-pred" }, item.predicate || "—"),
        el("div", { class: "vb-claim-slot" }, item.slot_key || "")),
      el("td", { class: "vb-claim-value" }, truncate(valueText(item.value), 100)),
      el("td", {}, claimStateBadge(item.state)),
      el("td", { class: "is-num" }, Number(item.confidence || 0).toFixed(2)),
      el("td", { class: "is-num" }, shortDate(item.asserted_at)),
      el("td", { class: "vb-claim-memory" }, truncate(item.memory_preview || "", 110))));
  }
  table.appendChild(body);
  return el("div", { class: "atlas-table-wrap" }, table);
}

function factsTable(items) {
  if (!items.length) {
    return emptyState({
      title: "没有活跃事实",
      hint: "事实投影只展示 state=active 且记忆 accepted/active 的 claims。",
    });
  }
  const table = el("table", { class: "atlas-table atlas-table--dense" });
  table.appendChild(el("thead", {}, el("tr", {},
    el("th", {}, "主语"), el("th", {}, "谓词"), el("th", {}, "槽"),
    el("th", {}, "值"), el("th", { class: "is-num" }, "置信"),
    el("th", {}, "来源"), el("th", { class: "is-num" }, "断言时间"))));
  const body = el("tbody");
  for (const item of items.slice(0, 300)) {
    body.appendChild(el("tr", {},
      el("td", {}, item.subject || "—"),
      el("td", {}, item.predicate || "—"),
      el("td", { class: "vb-mono" }, item.slot_key || "—"),
      el("td", { class: "vb-claim-value" }, truncate(valueText(item.value), 100)),
      el("td", { class: "is-num" }, Number(item.confidence || 0).toFixed(2)),
      el("td", {}, item.source_agent || "—"),
      el("td", { class: "is-num" }, shortDate(item.asserted_at))));
  }
  table.appendChild(body);
  return el("div", { class: "atlas-table-wrap" }, table);
}

function skillCard(item) {
  const details = el("details", { class: "vb-skill" });
  const summary = el("summary", { class: "vb-skill-summary" },
    el("span", { class: "vb-skill-title" }, truncate(item.content, 120)),
    el("span", { class: "vb-skill-meta" },
      badge(item.source_agent || "unknown", { tone: "link" }),
      el("span", { class: "vb-skill-confidence" }, Number(item.confidence || 0).toFixed(2))));
  details.appendChild(summary);
  const body = el("div", { class: "vb-skill-body" });
  const section = (label, values, ordered) => {
    if (!values || !values.length) return null;
    const list = el(ordered ? "ol" : "ul", { class: "vb-skill-list" });
    values.forEach((entry) => {
      const text = typeof entry === "string" ? entry
        : (entry && entry.action ? entry.action : JSON.stringify(entry));
      list.appendChild(el("li", {}, text));
    });
    return el("div", { class: "vb-skill-section" },
      el("div", { class: "vb-skill-label" }, label), list);
  };
  body.append(
    section("触发条件", item.trigger_pattern || [], false),
    section("前置条件", item.preconditions || [], false),
    section("执行步骤", item.execution_pipeline || [], true),
    section("验证断言", item.verification_assertion || [], false));
  if (!body.childNodes.length) body.appendChild(el("p", { class: "vb-muted" }, "这条技能没有结构化字段。"));
  details.appendChild(body);
  return details;
}

function skillsList(items) {
  if (!items.length) {
    return emptyState({
      title: "没有程序性技能",
      hint: "kind=procedural 且带 procedural 字段的记忆会出现在这里。",
    });
  }
  return el("div", { class: "vb-skills" }, items.slice(0, 100).map(skillCard));
}

function csvCell(value) {
  return '"' + String(value === null || value === undefined ? "" : value).replace(/"/g, '""') + '"';
}

/* 导出的行必须与屏幕上那个页签逐行一致：页签显示什么就导出什么。
   旧实现自己又写了一遍 state !== "active"，与渲染分叉后两边都错——
   点「导出 CSV」拿到的是 superseded，而屏幕标题写着「冲突」。 */
export function exportRows(payload, tab) {
  if (tab === "skills") {
    const rows = listItems(payload, "skills");
    return {
      header: ["memory_id", "source_agent", "confidence", "created_at", "content"],
      rows: rows.map((item) => ({ memory_id: item.memory_id, source_agent: item.source_agent,
        confidence: item.confidence, created_at: item.created_at, content: item.content })),
    };
  }
  if (tab === "facts") {
    const rows = listItems(payload, "facts");
    return {
      header: ["subject", "predicate", "slot_key", "value", "confidence", "source_agent", "asserted_at"],
      rows: rows.map((item) => ({ subject: item.subject, predicate: item.predicate,
        slot_key: item.slot_key, value: valueText(item.value), confidence: item.confidence,
        source_agent: item.source_agent, asserted_at: item.asserted_at })),
    };
  }
  return {
    header: ["subject", "predicate", "slot_key", "value", "state", "confidence",
             "asserted_at", "memory_id", "memory_preview"],
    rows: claimsForTab(payload, tab).map((item) => ({
      subject: item.subject, predicate: item.predicate,
      slot_key: item.slot_key, value: valueText(item.value), state: item.state,
      confidence: item.confidence, asserted_at: item.asserted_at,
      memory_id: item.memory_id, memory_preview: item.memory_preview })),
  };
}

export function csvText(exported) {
  const lines = [exported.header.join(",")];
  for (const row of exported.rows) {
    lines.push(exported.header.map((key) => csvCell(row[key])).join(","));
  }
  return "\uFEFF" + lines.join("\n");
}

function exportFacts(payload, tab) {
  if (!payload) return;
  const exported = exportRows(payload, tab);
  if (!exported.rows.length) return;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  downloadText("hippomind-" + tab + "-" + stamp + ".csv", csvText(exported), "text/csv;charset=utf-8");
}

function dataCard(title, eyebrow, body) {
  return card({ eyebrow, title, body });
}

export async function render(context) {
  const root = context.container;
  clear(root);
  const shell = el("div", { class: "vb-view vb-facts" });
  const header = el("div", { class: "vb-facts-head" });
  const tabHost = el("div", { class: "vb-facts-tabs" });
  const bodyHost = el("div", { class: "vb-facts-body" });
  shell.append(header, tabHost, bodyHost);
  root.appendChild(shell);

  let destroyed = false;
  let payload = null;
  let activeTab = TAB_IDS.includes(context.params.get("tab")) ? context.params.get("tab") : "conflicts";
  let claimFilter = "all";

  function tabItems() {
    const counts = tabCounts(payload);
    return [
      { id: "conflicts", label: "冲突", count: counts.conflicts },
      { id: "superseded", label: "取代谱系", count: counts.superseded },
      { id: "claims", label: "Claims", count: counts.claims },
      { id: "facts", label: "事实投影", count: counts.facts },
      { id: "skills", label: "技能", count: counts.skills },
    ];
  }

  function paintTabs() {
    clear(tabHost);
    tabHost.appendChild(tabs({
      items: tabItems(),
      active: activeTab,
      onChange: (id) => {
        activeTab = id;
        // Local tab switch: rewrite the address bar without a router round
        // trip, so switching tabs does not refetch every projection.
        try { history.replaceState(null, "", "#/facts?tab=" + id); } catch (error) { /* ignore */ }
        paintTabs();
        paintBody();
      },
    }));
  }

  function withNote(tab, card_) {
    const note = truncationNote(payload, tab);
    if (!note) return card_;
    return el("div", {}, el("p", { class: "vb-facts-note-line" }, note), card_);
  }

  function paintBody() {
    clear(bodyHost);
    if (!payload) return;
    if (activeTab === "conflicts") {
      bodyHost.appendChild(withNote("conflicts", dataCard("待裁决冲突", "CONFLICTS",
        conflictsTable(claimsForTab(payload, "conflicts")))));
      return;
    }
    if (activeTab === "superseded") {
      bodyHost.appendChild(el("p", { class: "vb-facts-note-line" },
        "被新事实或人工裁决顶替的 claim。它们是谱系（历史），不需要处理。"));
      bodyHost.appendChild(withNote("superseded", dataCard("取代谱系", "SUPERSEDED",
        supersededTable(claimsForTab(payload, "superseded")))));
      return;
    }
    if (activeTab === "claims") {
      const items = claimItems(payload).filter((item) => claimFilter === "all" || item.state === claimFilter);
      const filter = segmented({
        options: [
          { value: "all", label: "全部" }, { value: "active", label: "活跃" },
          { value: "conflict", label: "冲突" }, { value: "superseded", label: "已取代" },
        ],
        value: claimFilter,
        onChange: (value) => { claimFilter = value; paintBody(); },
      });
      bodyHost.appendChild(el("div", { class: "vb-facts-filter" }, filter));
      bodyHost.appendChild(dataCard("全部 Claims", "CLAIMS", claimsTable(items)));
      return;
    }
    if (activeTab === "facts") {
      bodyHost.appendChild(dataCard("事实投影", "FACTS",
        factsTable(listItems(payload, "facts"))));
      return;
    }
    bodyHost.appendChild(dataCard("程序性技能", "SKILLS", skillsList(listItems(payload, "skills"))));
  }

  try {
    /* 三个 claim 页签各自的列表都要拉：见 CLAIM_TABS 的注释——把 22 条
       conflict 从「最新 200 行」里筛是筛不出来的。limit 与 claims 那页一致。 */
    const [claims, conflictClaims, supersededClaims, facts, skills] = await Promise.all([
      api.get("/api/v1/claims", { state: "all", limit: 200 }, { signal: context.signal }),
      api.get("/api/v1/claims", { state: "conflict", limit: 200 }, { signal: context.signal }),
      api.get("/api/v1/claims", { state: "superseded", limit: 200 }, { signal: context.signal }),
      api.get("/api/v1/facts", { limit: 500 }, { signal: context.signal }),
      api.get("/api/v1/skills", { limit: 200 }, { signal: context.signal }),
    ]);
    if (destroyed) return;
    payload = { claims, conflicts: conflictClaims, superseded: supersededClaims, facts, skills };
    header.appendChild(el("div", { class: "vb-facts-summary" },
      el("span", {}, "claims ", el("b", {}, fmtInt(claims.counts ? claims.counts.total : 0))),
      el("span", {}, "冲突 ", el("b", {}, fmtInt(claims.counts ? claims.counts.conflict : 0))),
      el("span", {}, "已取代 ", el("b", {}, fmtInt(claims.counts ? claims.counts.superseded : 0))),
      el("span", {}, "事实 ", el("b", {}, fmtInt(listItems(payload, "facts").length))),
      el("span", {}, "技能 ", el("b", {}, fmtInt(listItems(payload, "skills").length))),
      el("span", { class: "vb-facts-note" }, "裁决/遗忘仍只提供可复制 CLI 命令；写入一条带证据的事实是唯一可直接执行的写操作")));
    header.appendChild(el("div", { class: "vb-facts-actions" },
      el("button", {
        class: "atlas-btn atlas-btn--primary atlas-btn--sm", type: "button",
        on: { click: () => openClaimForm({ onCreated: () => context.refresh() }) },
      }, icon("plus", { size: 13 }), "记录事实"),
      el("button", {
        class: "atlas-btn atlas-btn--ghost atlas-btn--sm", type: "button",
        on: { click: () => exportFacts(payload, activeTab) },
      }, icon("copy", { size: 12 }), "导出 CSV")));
    paintTabs();
    paintBody();
  } catch (error) {
    if (isAbort(error)) return;
    bodyHost.appendChild(errorState({ title: "事实数据加载失败", error, onRetry: () => context.refresh() }));
  }

  context.onCleanup(() => { destroyed = true; });
}

export async function refresh(context) {
  return render(context);
}
