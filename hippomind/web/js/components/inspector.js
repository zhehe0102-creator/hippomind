/* ============================================================================
   HippoMind Atlas — memory inspector drawer
   The single detail surface shared by archive, recall and timeline. API text
   crosses into the document as text nodes only: provenance is evidence, never
   markup. Actions stay deliberately small: pin, feedback, copy id, copy CLI.
   ========================================================================= */
"use strict";

import { el } from "../core/dom.js";
import { api } from "../core/api.js";
import { absTime, fmtInt, fmtPct, relTime, shortId, truncate } from "../core/format.js";
import {
  badge, copyButton, dataTable, drawer, errorState, meter,
  skeleton, statRow, toast,
} from "../components.js";
import { icon } from "../icons.js";

const KIND_TONES = {
  task: "accent", goal: "accent", progress: "link", decision: "accent",
  blocker: "danger", next_step: "link", result: "ok", semantic: "neutral",
  procedural: "neutral", fact: "ok", file: "neutral", preference: "warn",
  raw_chat: "danger", tool_output: "neutral", system_message: "neutral",
};
const LIFECYCLE_TONES = {
  active: "ok", cold: "neutral", superseded: "danger", expired: "warn",
  archived: "warn", quarantined: "warn",
};
const QUALITY_TONES = { accepted: "ok", quarantine: "warn", rejected: "danger" };
const STATE_TONES = { active: "ok", conflict: "danger", superseded: "warn" };
const RELATION_TONES = {
  caused_by: "accent", derived_from: "link", refines: "ok", contradicts: "danger",
  related: "warn", supersedes: "warn", depends_on: "link",
};

export function memoryKindBadge(kind) {
  return badge(kind || "未知", { tone: KIND_TONES[kind] || "neutral" });
}

export function lifecycleBadge(lifecycle) {
  return badge(lifecycle || "未知", { tone: LIFECYCLE_TONES[lifecycle] || "neutral" });
}

export function qualityBadge(state) {
  return badge(state || "未知", { tone: QUALITY_TONES[state] || "neutral" });
}

export function claimStateBadge(state) {
  return badge(state || "未知", { tone: STATE_TONES[state] || "neutral" });
}

export function relationBadge(type) {
  return badge(type || "related", { tone: RELATION_TONES[type] || "neutral" });
}

function text(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function displayValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number") return Number.isInteger(value) ? fmtInt(value) : String(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function timeText(iso) {
  if (!iso) return "—";
  return absTime(iso) + "  ·  " + relTime(iso);
}

function section(title, hint, ...children) {
  return el("section", { class: "vb-insp-section" },
    el("div", { class: "vb-insp-head" },
      el("h3", { class: "vb-insp-title" }, title),
      hint ? el("span", { class: "vb-insp-hint muted" }, hint) : null),
    ...children);
}

function kvList(entries, options = {}) {
  const wrap = el("div", { class: "vb-kv" + (options.grid ? " vb-kv--grid" : "") });
  let shown = 0;
  for (const entry of entries) {
    if (!entry) continue;
    const [key, value] = entry;
    if (value === null || value === undefined) continue;
    shown += 1;
    wrap.appendChild(el("div", { class: "vb-kv-row" },
      el("span", { class: "vb-kv-key" }, key),
      el("span", { class: "vb-kv-value" }, displayValue(value))));
  }
  if (!shown) return el("p", { class: "vb-empty-line muted" }, "—");
  return wrap;
}

function tagNode(value) {
  const label = displayValue(value);
  if (label === "—") return null;
  return el("span", { class: "vb-tag" }, label);
}

function evidenceNode(entry, index) {
  const value = typeof entry === "string" ? entry : displayValue(entry);
  return el("li", { class: "vb-evidence-item" },
    el("span", { class: "vb-evidence-index" }, String(index + 1).padStart(2, "0")),
    el("span", { class: "vb-evidence-text" }, value));
}

function metadataEntries(metadata) {
  if (!metadata || typeof metadata !== "object") return [];
  return Object.entries(metadata).map(([key, value]) => [key, displayValue(value)]);
}

function signalBadge(signal) {
  const value = Number(signal);
  if (value > 0) return badge("＋" + (Math.round(value * 100) / 100), { tone: "ok" });
  if (value < 0) return badge("－" + (Math.round(Math.abs(value) * 100) / 100), { tone: "danger" });
  return badge("中性", { tone: "neutral" });
}

/* One inspector at a time; every open gesture bumps the sequence so a slow
   response can never overwrite a newer selection. */
let sequence = 0;

export async function openInspector(memoryId, options = {}) {
  const id = String(memoryId || "").trim();
  if (!id) return;
  const seq = (sequence += 1);
  const state = {
    memoryId: id,
    data: null,
    expanded: false,
    pinned: false,
    note: "",
    options,
  };

  function abortable() {
    return options.signal ? { signal: options.signal } : {};
  }

  function isAbort(error) {
    return error && error.name === "AbortError";
  }

  function notifyChanged() {
    if (typeof options.onChanged === "function") {
      try { options.onChanged(); } catch (error) { console.warn("[atlas] inspector onChanged failed", error); }
    }
  }

  function openShell(title, subtitle, content, actions) {
    drawer.open({
      eyebrow: "MEMORY · " + (state.data && state.data.memory ? text(state.data.memory.kind, "record") : "record").toUpperCase(),
      title,
      subtitle,
      content,
      actions,
    });
  }

  function render() {
    if (seq !== sequence || !state.data) return;
    const memory = state.data.memory || {};
    const firstLine = String(memory.content || "").split("\n").map((line) => line.trim()).find(Boolean);
    const title = truncate(firstLine || shortId(id, 12, 6), 56);
    const subtitleParts = [
      shortId(id, 12, 6),
      memory.source_agent,
      memory.scope,
      memory.created_at ? relTime(memory.created_at) : "",
    ].filter(Boolean);
    openShell(title, subtitleParts.join("  ·  "), renderBody(), buildActions());
  }

  function buildActions() {
    const pinLabel = state.pinned ? "取消置顶" : "置顶";
    const actions = [
      {
        label: pinLabel,
        icon: "pin",
        title: state.pinned ? "取消置顶这条记忆" : "置顶这条记忆",
        onClick: () => { void togglePin(); },
      },
      {
        label: "反馈 +1",
        icon: "feedback-up",
        title: "记录一次正反馈（可附注）",
        onClick: () => { void sendFeedback(1, ""); },
      },
      {
        label: "反馈 -1",
        icon: "feedback-down",
        title: "记录一次负反馈（可附注）",
        onClick: () => { void sendFeedback(-1, ""); },
      },
      copyButton(state.memoryId, { label: "复制 ID" }),
      copyButton("hippomind forget --memory-id " + state.memoryId, { label: "复制 forget 命令" }),
    ];
    return actions;
  }

  function renderBody() {
    const data = state.data || {};
    const memory = data.memory || {};
    const claims = Array.isArray(data.claims) ? data.claims : [];
    const relations = data.relations && typeof data.relations === "object" ? data.relations : {};
    const feedback = Array.isArray(data.feedback) ? data.feedback : [];
    const body = el("div", { class: "vb-insp" });

    /* ---- identity strip ---- */
    const strip = el("div", { class: "vb-insp-strip" },
      memoryKindBadge(memory.kind),
      lifecycleBadge(memory.lifecycle),
      qualityBadge(memory.quality_state),
      memory.raw_chat ? badge("raw 原始", { tone: "danger" }) : null,
      memory.pinned
        ? el("span", { class: "vb-insp-pin" }, icon("pin", { size: 12 }), "已置顶")
        : null,
      el("span", { class: "vb-insp-meta" }, text(memory.source_agent, "unknown agent")),
      memory.project ? el("span", { class: "vb-insp-meta" }, "project ", el("b", {}, memory.project)) : null,
      memory.scope ? el("span", { class: "vb-insp-meta" }, "scope ", el("b", {}, memory.scope)) : null,
    );
    body.appendChild(strip);

    /* ---- full content (expand / copy) ---- */
    const rawContent = String(memory.content || "");
    const longContent = rawContent.length > 1600;
    const contentPre = el("div", { class: "vb-content" + (longContent && !state.expanded ? " is-clamped" : "") }, rawContent);
    const expandLabel = el("span", {}, longContent && !state.expanded ? "展开全文" : (longContent ? "收起" : ""));
    const expandButton = longContent ? el("button", {
      class: "atlas-btn atlas-btn--ghost atlas-btn--sm",
      type: "button",
      on: {
        click: () => {
          state.expanded = !state.expanded;
          contentPre.classList.toggle("is-clamped", !state.expanded);
          expandLabel.textContent = state.expanded ? "收起" : "展开全文";
        },
      },
    }, icon("chevron-right", { size: 12 }), expandLabel) : null;
    body.appendChild(section("内容", rawContent.length + " 字符",
      el("div", { class: "vb-insp-actions" },
        expandButton,
        copyButton(rawContent, { label: "复制全文" })),
      contentPre));

    /* ---- provenance & timeline ---- */
    const provenance = [
      ["memory_id", memory.memory_id || state.memoryId],
      ["origin_event_id", memory.origin_event_id],
      ["kind", memory.kind],
      ["source_agent", memory.source_agent],
      ["scope", memory.scope],
      ["project", memory.project],
      ["task_id", memory.task_id],
      ["session_id", memory.session_id],
      ["role", memory.role],
      ["raw_chat", memory.raw_chat ? "是" : "否"],
      ["superseded_by", memory.superseded_by],
    ];
    if (data.origin_event && data.origin_event.event_id) {
      const event = data.origin_event;
      body.appendChild(section("来源与身份", "来源事件 " + shortId(event.event_id),
        kvList(provenance),
        el("div", { class: "vb-insp-subhead" }, "来源事件"),
        kvList([
          ["event_type", event.event_type],
          ["occurred_at", timeText(event.occurred_at)],
          ["ingested_at", timeText(event.ingested_at)],
          ["source_agent", event.source_agent],
          ["session_id", event.session_id],
          ["task_id", event.task_id],
          ["project", event.project],
          ["role", event.role],
          ["scope", event.scope],
        ]),
        event.content_preview
          ? el("div", { class: "vb-event-preview" }, event.content_preview)
          : null,
        event.metadata && Object.keys(event.metadata).length
          ? kvList(metadataEntries(event.metadata), { grid: true })
          : null));
    } else {
      body.appendChild(section("来源与身份", null, kvList(provenance)));
    }

    const timeline = [
      ["created_at", timeText(memory.created_at)],
      ["updated_at", timeText(memory.updated_at)],
      ["last_access_at", timeText(memory.last_access_at)],
      ["last_weighted_at", timeText(memory.last_weighted_at)],
      ["valid_from", timeText(memory.valid_from)],
      ["valid_until", timeText(memory.valid_until)],
      ["asserted_at", timeText(memory.asserted_at)],
    ];
    body.appendChild(section("时间线", null, kvList(timeline)));

    /* ---- quality ---- */
    const qualityMetrics = [
      ["权重 weight", Number(memory.weight).toFixed(3)],
      ["重要度 importance", fmtInt(memory.importance)],
      ["置信 confidence", fmtPct(memory.confidence)],
      ["效用 utility", fmtPct(memory.utility)],
      ["触碰 touch", fmtInt(memory.touch_count)],
      ["成功 / 失败", fmtInt(memory.success_count) + " / " + fmtInt(memory.failure_count)],
    ];
    const qualityGrid = el("div", { class: "vb-quality-grid" });
    for (const [label, value] of qualityMetrics) {
      qualityGrid.appendChild(el("div", { class: "vb-quality-cell" },
        el("span", { class: "vb-quality-label" }, label),
        el("span", { class: "vb-quality-value" }, value)));
    }
    body.appendChild(section("质量指标", null,
      qualityGrid,
      el("div", { class: "vb-quality-meters" },
        meter({ label: "weight", value: Number(memory.weight) || 0, max: 1, valueText: Number(memory.weight || 0).toFixed(3) }),
        meter({ label: "confidence", value: Number(memory.confidence) || 0, max: 1, valueText: fmtPct(memory.confidence) }))));

    /* ---- tags & evidence ---- */
    const tags = Array.isArray(memory.tags) ? memory.tags : [];
    const evidence = Array.isArray(memory.evidence) ? memory.evidence : [];
    const tagsLine = el("div", { class: "vb-tags" }, ...(tags.length ? tags.map(tagNode).filter(Boolean) : [el("span", { class: "vb-empty-line muted" }, "无 tags")]));
    const evidenceList = evidence.length
      ? el("ol", { class: "vb-evidence" }, ...evidence.map(evidenceNode))
      : el("p", { class: "vb-empty-line muted" }, "无 evidence：这条记忆不满足 require_evidence 的召回条件。");
    body.appendChild(section("标签与证据", tags.length + " tags · " + evidence.length + " evidence",
      el("div", { class: "vb-tags" }, el("span", { class: "vb-insp-k", }, "tags"), tagsLine),
      el("div", { class: "vb-tags" }, el("span", { class: "vb-insp-k" }, "evidence"), evidenceList)));

    /* ---- metadata ---- */
    const metadata = memory.metadata && typeof memory.metadata === "object" ? memory.metadata : {};
    const metadataBlock = kvList(metadataEntries(metadata));
    metadataBlock.classList.add("vb-kv--metadata");
    body.appendChild(section("Metadata", null, metadataBlock));

    /* ---- claims ---- */
    const claimRows = claims.map((claim) => Object.assign({}, claim));
    const claimTable = dataTable({
      dense: true,
      columns: [
        { key: "predicate", label: "predicate", mono: true, width: 100 },
        { key: "slot_key", label: "slot", mono: true },
        { key: "value", label: "value", render: (row) => displayValue(row.value) },
        { key: "state", label: "state", width: 84, render: (row) => claimStateBadge(row.state) },
        { key: "confidence", label: "conf", align: "right", width: 54, mono: true, render: (row) => Number(row.confidence || 0).toFixed(2) },
      ],
      rows: claimRows,
      empty: "这条记忆没有结构化 claim。",
    });
    body.appendChild(section("Claims", claimRows.length + " 条", claimTable));

    /* ---- relations ---- */
    const relationTable = (label, rows, direction) => {
      if (!rows.length) return el("p", { class: "vb-empty-line muted" }, label + "：无关联记忆。");
      return el("div", { class: "vb-relation-block" },
        el("div", { class: "vb-insp-subhead" }, label + " · " + rows.length),
        dataTable({
          dense: true,
          columns: [
            { key: "relation_type", label: "关系", width: 104, render: (row) => relationBadge(row.relation_type) },
            { key: "confidence", label: "conf", align: "right", width: 52, mono: true, render: (row) => Number(row.confidence || 0).toFixed(2) },
            { key: "memory", label: "摘要", render: (row) => el("div", { class: "atlas-cell-clamp" }, text(row.memory && row.memory.content, "（无内容）")) },
            { key: "kind", label: "kind", width: 84, mono: true, render: (row) => memoryKindBadge(row.memory && row.memory.kind) },
          ],
          rows,
          onRowClick: (row) => {
            const target = row.memory && row.memory.memory_id;
            if (target) openInspector(target, options);
          },
          rowKey: (row) => direction + ":" + (row.memory && row.memory.memory_id),
        }));
    };
    const parents = Array.isArray(relations.parents) ? relations.parents : [];
    const children = Array.isArray(relations.children) ? relations.children : [];
    body.appendChild(section("关系", parents.length + " 父 · " + children.length + " 子",
      relationTable("父记忆（来源谱系）", parents, "parent"),
      relationTable("子记忆（派生）", children, "child")));

    /* ---- feedback ---- */
    const feedbackRows = feedback.map((entry) => el("div", { class: "vb-feedback-item" },
      el("div", { class: "vb-feedback-head" },
        signalBadge(entry.signal),
        el("span", { class: "vb-feedback-meta mono" }, text(entry.source_agent, "local")),
        el("span", { class: "vb-feedback-meta" }, timeText(entry.created_at))),
      entry.note ? el("p", { class: "vb-feedback-note" }, entry.note) : null));
    const noteInput = el("input", {
      class: "atlas-input vb-feedback-input",
      type: "text",
      maxlength: "400",
      placeholder: "可选：附上一句原因（≤400 字符）",
      "aria-label": "反馈附注",
    });
    noteInput.addEventListener("input", () => { state.note = noteInput.value; });
    const composer = el("div", { class: "vb-feedback-composer" },
      noteInput,
      el("button", {
        class: "atlas-btn atlas-btn--sm",
        type: "button",
        on: { click: () => { void sendFeedback(1, state.note); } },
      }, icon("feedback-up", { size: 12 }), el("span", {}, "记 +1")),
      el("button", {
        class: "atlas-btn atlas-btn--sm",
        type: "button",
        on: { click: () => { void sendFeedback(-1, state.note); } },
      }, icon("feedback-down", { size: 12 }), el("span", {}, "记 -1")));
    body.appendChild(section("反馈", feedback.length + " 条",
      feedbackRows.length ? el("div", { class: "vb-feedback-list" }, ...feedbackRows)
        : el("p", { class: "vb-empty-line muted" }, "还没有反馈记录。"),
      composer,
      el("p", { class: "vb-insp-note muted" },
        "反馈只影响工作记忆权重；遗忘/裁决属于危险操作，界面只提供可复制的 CLI 命令。")));

    return body;
  }

  async function togglePin() {
    const target = !state.pinned;
    try {
      const result = await api.post("/api/v1/pin",
        { memory_id: state.memoryId, pinned: target }, abortable());
      if (seq !== sequence) return;
      state.pinned = Boolean(result && result.pinned);
      if (state.data && state.data.memory) state.data.memory.pinned = state.pinned;
      toast(state.pinned ? "已置顶" : "已取消置顶", { tone: "ok", ms: 1600 });
      render();
      notifyChanged();
    } catch (error) {
      if (isAbort(error) || seq !== sequence) return;
      toast("置顶失败：" + (error && error.detail ? error.detail : "请求失败"), { tone: "danger" });
    }
  }

  async function sendFeedback(signal, note) {
    try {
      await api.post("/api/v1/feedback",
        { memory_id: state.memoryId, signal: signal, note: String(note || "").slice(0, 400) },
        abortable());
      if (seq !== sequence) return;
      state.note = "";
      toast(signal > 0 ? "已记录正反馈" : "已记录负反馈", { tone: "ok", ms: 1600 });
      await load(true);
      notifyChanged();
    } catch (error) {
      if (isAbort(error) || seq !== sequence) return;
      toast("反馈失败：" + (error && error.detail ? error.detail : "请求失败"), { tone: "danger" });
    }
  }

  async function load(silent) {
    if (!silent) {
      openShell("正在读取记忆…", shortId(id, 12, 6), skeleton(6), []);
    }
    try {
      const data = await api.get("/api/v1/memories/" + encodeURIComponent(id), null, abortable());
      if (seq !== sequence) return;
      state.data = data || {};
      state.pinned = Boolean(state.data.memory && state.data.memory.pinned);
      render();
    } catch (error) {
      if (isAbort(error) || seq !== sequence) return;
      drawer.open({
        eyebrow: "MEMORY",
        title: "无法读取该记忆",
        subtitle: shortId(id, 12, 6),
        content: errorState({
          title: "详情加载失败",
          error,
          onRetry: () => { void load(false); },
        }),
        actions: [],
      });
    }
  }

  await load(false);
}

export default { openInspector };

