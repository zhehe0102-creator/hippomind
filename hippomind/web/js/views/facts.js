/* ============================================================================
   HippoMind Atlas — 06 facts 事实
   Four projections of the same claim graph: conflicts (non-active claims),
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

const TAB_IDS = ["conflicts", "claims", "facts", "skills"];
let live = null;

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

function conflictCommand(item) {
  return "hippomind resolve --claim " + item.claim_id + " --winner " + (item.memory_id || "<memory_id>");
}

function conflictsTable(items) {
  if (!items.length) {
    return emptyState({
      title: "没有待裁决的冲突",
      hint: "当同一 (subject, predicate, slot_key) 出现不同值时，账本会把它们标记为 conflict/superseded，等待人工裁决。",
    });
  }
  const table = el("table", { class: "atlas-table atlas-table--dense" });
  table.appendChild(el("thead", {}, el("tr", {},
    el("th", {}, "主语"),
    el("th", {}, "谓词 / 槽"),
    el("th", {}, "值"),
    el("th", {}, "状态"),
    el("th", { class: "is-num" }, "置信"),
    el("th", {}, "记忆摘要"),
    el("th", {}, "裁决命令"))));
  const body = el("tbody");
  for (const item of items.slice(0, 100)) {
    body.appendChild(el("tr", {},
      el("td", {}, el("span", { class: "vb-claim-subject" }, item.subject || "—")),
      el("td", {}, el("div", { class: "vb-claim-pred" }, item.predicate || "—"),
        el("div", { class: "vb-claim-slot" }, item.slot_key || "")),
      el("td", { class: "vb-claim-value" }, truncate(valueText(item.value), 100)),
      el("td", {}, claimStateBadge(item.state)),
      el("td", { class: "is-num" }, Number(item.confidence || 0).toFixed(2)),
      el("td", { class: "vb-claim-memory" }, truncate(item.memory_preview || "", 110)),
      el("td", {}, copyButton(conflictCommand(item), { label: "复制命令" }))));
  }
  table.appendChild(body);
  return el("div", { class: "atlas-table-wrap" }, table);
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

function exportFacts(payload, tab) {
  if (!payload) return;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  let rows = [];
  let header = [];
  if (tab === "skills") {
    rows = (payload.skills && payload.skills.items) || [];
    header = ["memory_id", "source_agent", "confidence", "created_at", "content"];
    rows = rows.map((item) => ({ memory_id: item.memory_id, source_agent: item.source_agent,
      confidence: item.confidence, created_at: item.created_at, content: item.content }));
  } else if (tab === "facts") {
    rows = (payload.facts && payload.facts.items) || [];
    header = ["subject", "predicate", "slot_key", "value", "confidence", "source_agent", "asserted_at"];
    rows = rows.map((item) => ({ subject: item.subject, predicate: item.predicate,
      slot_key: item.slot_key, value: valueText(item.value), confidence: item.confidence,
      source_agent: item.source_agent, asserted_at: item.asserted_at }));
  } else {
    rows = (payload.claims && payload.claims.items) || [];
    if (tab === "conflicts") rows = rows.filter((item) => item.state !== "active");
    header = ["subject", "predicate", "slot_key", "value", "state", "confidence",
              "asserted_at", "memory_id", "memory_preview"];
    rows = rows.map((item) => ({ subject: item.subject, predicate: item.predicate,
      slot_key: item.slot_key, value: valueText(item.value), state: item.state,
      confidence: item.confidence, asserted_at: item.asserted_at,
      memory_id: item.memory_id, memory_preview: item.memory_preview }));
  }
  if (!rows.length) return;
  const lines = [header.join(",")];
  for (const row of rows) lines.push(header.map((key) => csvCell(row[key])).join(","));
  downloadText("hippomind-" + tab + "-" + stamp + ".csv",
    "\uFEFF" + lines.join("\n"), "text/csv;charset=utf-8");
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
    const claims = (payload && payload.claims && payload.claims.items) || [];
    const conflicts = claims.filter((item) => item.state !== "active");
    return [
      { id: "conflicts", label: "冲突", count: conflicts.length },
      { id: "claims", label: "Claims", count: claims.length },
      { id: "facts", label: "事实投影", count: (payload && payload.facts && payload.facts.items || []).length },
      { id: "skills", label: "技能", count: (payload && payload.skills && payload.skills.items || []).length },
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

  function paintBody() {
    clear(bodyHost);
    if (!payload) return;
    if (activeTab === "conflicts") {
      bodyHost.appendChild(dataCard("冲突与取代谱系", "CONFLICTS",
        conflictsTable((payload.claims.items || []).filter((item) => item.state !== "active"))));
      return;
    }
    if (activeTab === "claims") {
      const items = (payload.claims.items || []).filter((item) => claimFilter === "all" || item.state === claimFilter);
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
      bodyHost.appendChild(dataCard("事实投影", "FACTS", factsTable((payload.facts && payload.facts.items) || [])));
      return;
    }
    bodyHost.appendChild(dataCard("程序性技能", "SKILLS", skillsList((payload.skills && payload.skills.items) || [])));
  }

  try {
    const [claims, facts, skills] = await Promise.all([
      api.get("/api/v1/claims", { state: "all", limit: 200 }, { signal: context.signal }),
      api.get("/api/v1/facts", { limit: 500 }, { signal: context.signal }),
      api.get("/api/v1/skills", { limit: 200 }, { signal: context.signal }),
    ]);
    if (destroyed) return;
    payload = { claims, facts, skills };
    header.appendChild(el("div", { class: "vb-facts-summary" },
      el("span", {}, "claims ", el("b", {}, fmtInt(claims.counts ? claims.counts.total : 0))),
      el("span", {}, "冲突 ", el("b", {}, fmtInt(claims.counts ? claims.counts.conflict : 0))),
      el("span", {}, "事实 ", el("b", {}, fmtInt((facts.items || []).length))),
      el("span", {}, "技能 ", el("b", {}, fmtInt((skills.items || []).length))),
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
