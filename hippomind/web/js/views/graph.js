/* ============================================================================
   HippoMind Atlas — 05 graph 图谱
   Claim subjects as an entity index; the selected neighbourhood is drawn by a
   local canvas renderer (no graph library). Claims for the selected subject
   are fetched from /api/v1/claims?subject=... for the side panel.
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import { fmtInt, truncate } from "../core/format.js";
import {
  badge, card, chip, emptyState, errorState, skeleton, segmented, textField,
} from "../components.js";
import { icon } from "../icons.js";
import {
  createGraphCanvas, RELATION_WEIGHT_HINTS,
} from "../components/graph-canvas.js";
import { claimStateBadge } from "../components/inspector.js";

export const meta = {
  id: "graph",
  index: "05",
  title: "图谱",
  eyebrow: "GRAPH",
  subtitle: "实体与记忆的关系网络：主语、claim 与因果连线",
};

let live = null;

function isAbort(error) { return error && error.name === "AbortError"; }

function subjectOf(params) { return (params.get("subject") || "").trim(); }
function hopsOf(params) { return params.get("hops") === "2" ? 2 : 1; }

function valueText(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value); } catch (error) { return String(value); }
}

function countBadge(count, label, tone) {
  return badge(fmtInt(count) + " " + label, { tone: tone || "neutral" });
}

function subjectRow(item, active, onPick) {
  const conflicts = Number(item.conflicts) || 0;
  return el("button", {
    class: "vb-subject-item" + (active ? " is-active" : ""),
    type: "button",
    title: item.subject,
    on: { click: () => onPick(item.subject) },
  },
    el("span", { class: "vb-subject-name" }, truncate(item.subject, 34)),
    el("span", { class: "vb-subject-counts" },
      el("span", {}, fmtInt(item.claims) + " claims"),
      conflicts ? el("span", { class: "vb-subject-conflict" }, fmtInt(conflicts) + " 冲突") : null));
}

function subjectList(subjects, active, filter, onPick) {
  const query = String(filter || "").toLowerCase();
  const visible = subjects.filter((item) => !query || String(item.subject).toLowerCase().includes(query));
  if (!visible.length) {
    return el("div", { class: "vb-subject-empty" },
      query ? "没有匹配的主语" : "还没有 claim 主语");
  }
  return el("div", { class: "vb-subject-list" }, visible.slice(0, 300).map((item) =>
    subjectRow(item, item.subject === active, onPick)));
}

/* The legend has to teach two different things, and conflating them was the
   original bug:
     * WHAT the relation is  -> hue (causal / support / conflict / structural)
     * HOW MUCH it counts    -> the weight from ledger/graph.py RELATION_WEIGHTS
   same_subject(0.9) and same_task(0.7) are separate tiers in associative
   expansion; related_to and every legacy type sit at 1.0.  A reader who only
   sees colours cannot tell that, which is why the weights are printed. */
function legendItem(className, label, hint) {
  return el("span", { class: "vb-legend-item", title: hint || label },
    el("i", { class: className }), label);
}

function legend() {
  const weightOf = (type) => {
    const entry = RELATION_WEIGHT_HINTS.find((item) => item.type === type);
    return entry ? entry.weight.toFixed(1) : "1.0";
  };
  return el("div", { class: "vb-graph-legend" },
    legendItem("vb-dot vb-dot--link", "因果",
      "caused_by / causes / follows — 权重 " + weightOf("related_to")),
    legendItem("vb-dot vb-dot--ok", "支持/确认",
      "support / confirm — 权重 " + weightOf("related_to")),
    legendItem("vb-dot vb-dot--danger", "冲突/取代",
      "contradict / supersedes — 权重 " + weightOf("related_to")),
    legendItem("vb-dot vb-dot--ink2", "同主语 " + weightOf("same_subject"),
      "same_subject — 虚线 · 联想扩展权重 " + weightOf("same_subject") +
      "（同主语只证明在谈同一件事，比显式因果弱一档）"),
    legendItem("vb-dot vb-dot--dotted", "同任务 " + weightOf("same_task"),
      "same_task — 点线 · 联想扩展权重 " + weightOf("same_task") +
      "（同一任务里本来就有大量无关记忆，是最弱的一层关联）"),
    legendItem("vb-dot vb-dot--ink", "其他 " + weightOf("related_to"),
      "related_to 及历史/未知类型 — 实线 · 权重恒为 1.0（存量边逐字节不变）"),
    el("span", { class: "vb-graph-hint" }, "滚轮缩放 · 拖拽平移 · 点击节点 · 双击复位"));
}

function claimsTable(items) {
  if (!items.length) {
    return el("p", { class: "vb-muted" }, "该主语暂无 claims。");
  }
  const wrap = el("div", { class: "vb-graph-claims" });
  const table = el("table", { class: "atlas-table atlas-table--dense" });
  table.appendChild(el("thead", {}, el("tr", {},
    el("th", {}, "谓词 / 槽"),
    el("th", {}, "值"),
    el("th", { class: "is-num" }, "置信"),
    el("th", {}, "状态"))));
  const body = el("tbody");
  for (const item of items.slice(0, 80)) {
    body.appendChild(el("tr", {},
      el("td", {}, el("div", { class: "vb-claim-pred" }, item.predicate || "—"),
        el("div", { class: "vb-claim-slot" }, item.slot_key || "")),
      el("td", { class: "vb-claim-value" }, truncate(valueText(item.value), 90)),
      el("td", { class: "is-num" }, Number(item.confidence || 0).toFixed(2)),
      el("td", {}, claimStateBadge(item.state))));
  }
  table.appendChild(body);
  wrap.appendChild(table);
  return wrap;
}

function entityPanel(subject, node, claims, loading, error) {
  const body = el("div", { class: "vb-entity" });
  if (!subject) {
    body.appendChild(el("p", { class: "vb-muted" }, "点击图上的节点或左侧主语，查看实体详情。"));
    return body;
  }
  body.appendChild(el("div", { class: "vb-entity-title" }, subject));
  body.appendChild(el("div", { class: "vb-entity-counts" },
    node ? countBadge(node.claims, "claims", "link") : null,
    node ? countBadge(node.memories, "记忆", "neutral") : null,
    node && node.conflicts ? countBadge(node.conflicts, "冲突", "danger") : null));
  if (loading) body.appendChild(skeleton(2));
  else if (error) body.appendChild(errorState({ title: "claims 加载失败", error, onRetry: () => {} }));
  else body.appendChild(claimsTable(claims || []));
  body.appendChild(el("div", { class: "vb-entity-actions" },
    el("button", {
      class: "atlas-btn atlas-btn--ghost atlas-btn--sm", type: "button",
      on: { click: () => { window.location.hash = "#/facts?tab=claims"; } },
    }, icon("facts", { size: 13 }), "在事实页查看"),
    el("button", {
      class: "atlas-btn atlas-btn--ghost atlas-btn--sm", type: "button",
      on: { click: async () => {
        try { await navigator.clipboard.writeText(subject); } catch (err) { /* ignore */ }
      } },
    }, icon("copy", { size: 13 }), "复制主语")));
  return body;
}

export async function render(context) {
  const root = context.container;
  clear(root);
  const subjectsState = { items: [], filter: "", active: subjectOf(context.params) };
  const graphState = { data: null, node: null, claims: [], loading: false, error: null };
  let canvas = null;
  let destroyed = false;

  const shell = el("div", { class: "vb-view vb-graph" });
  const leftCard = card({ eyebrow: "SUBJECTS", title: "实体索引", body: skeleton(4) });
  const mainCard = card({ eyebrow: "ENTITY GRAPH", title: "关系网络", body: skeleton(3) });
  const sideCard = card({ eyebrow: "ENTITY", title: "实体", body: entityPanel("", null, [], false, null) });
  shell.append(leftCard, mainCard, sideCard);
  root.appendChild(shell);

  function pickSubject(subject) {
    if (!subject) return;
    subjectsState.active = subject;
    context.setQuery({ subject, hops: String(hopsOf(context.params)) });
  }

  function paintSubjects() {
    const body = el("div", { class: "vb-graph-left-body" });
    const search = textField({
      label: "过滤主语", value: subjectsState.filter, placeholder: "输入主语片段…",
      onInput: (event, value) => { subjectsState.filter = value; paintSubjectList(); },
    });
    body.appendChild(search);
    const listHost = el("div", { class: "vb-subject-host" });
    body.appendChild(listHost);
    leftCard.querySelector(".atlas-card-body").replaceChildren(body);
    function paintSubjectList() {
      clear(listHost);
      listHost.appendChild(subjectList(subjectsState.items, subjectsState.active,
        subjectsState.filter, pickSubject));
    }
    paintSubjectList();
  }

  function paintGraph() {
    const data = graphState.data;
    const body = el("div", { class: "vb-graph-main-body" });
    const title = data && data.subject ? data.subject : "选择主语";
    mainCard.querySelector(".atlas-card-title").textContent = title;
    const toolbar = el("div", { class: "vb-graph-toolbar" },
      segmented({
        options: [{ value: "1", label: "1 跳" }, { value: "2", label: "2 跳" }],
        value: String(hopsOf(context.params)),
        onChange: (value) => context.setQuery({ subject: subjectsState.active, hops: String(value) }),
      }),
      el("div", { class: "vb-graph-zoom" },
        el("button", { class: "atlas-btn atlas-btn--ghost atlas-btn--sm", type: "button", title: "放大",
          on: { click: () => canvas && canvas.zoom(1.2) } }, "+"),
        el("button", { class: "atlas-btn atlas-btn--ghost atlas-btn--sm", type: "button", title: "缩小",
          on: { click: () => canvas && canvas.zoom(0.83) } }, "−"),
        el("button", { class: "atlas-btn atlas-btn--ghost atlas-btn--sm", type: "button", title: "复位",
          on: { click: () => canvas && canvas.fit() } }, "复位")));
    body.appendChild(toolbar);
    const canvasHost = el("div", { class: "vb-graph-stage" });
    body.appendChild(canvasHost);
    body.appendChild(legend());
    if (data && data.found) {
      const counts = el("div", { class: "vb-graph-counts" },
        countBadge(data.nodes.length, "实体", "link"),
        countBadge(data.edges.length, "关系", "neutral"));
      body.insertBefore(counts, canvasHost);
    }
    mainCard.querySelector(".atlas-card-body").replaceChildren(body);
    if (canvas) { canvas.destroy(); canvas = null; }
    if (data && data.found && data.nodes.length) {
      canvas = createGraphCanvas({
        label: "实体关系图：" + data.subject,
        onSelect: (node) => { graphState.node = node; paintEntity(node ? node.subject : null); },
        onHover: () => {},
      });
      canvasHost.appendChild(canvas.element);
      canvas.setData(data);
    } else if (data && !data.found) {
      canvasHost.appendChild(emptyState({
        title: "该主语暂无活跃 claim",
        hint: "图谱只展示 state=active/conflict 的 claim。确认主语拼写，或先在账本里写入带 claims 的记忆。",
        action: { label: "记录一条事实", icon: "plus", primary: true,
                  onClick: () => context.navigate("/facts?tab=claims") },
      }));
    }
  }

  async function paintEntity(subject) {
    const sideBody = sideCard.querySelector(".atlas-card-body");
    sideBody.replaceChildren(entityPanel(subject, graphState.node, graphState.claims,
      graphState.loading, graphState.error));
    if (!subject) return;
    graphState.loading = true;
    graphState.claims = [];
    graphState.error = null;
    sideBody.replaceChildren(entityPanel(subject, graphState.node, [], true, null));
    try {
      const result = await api.get("/api/v1/claims", { subject, limit: 100, state: "all" },
        { signal: context.signal });
      if (destroyed || subjectsState.active !== subject) return;
      graphState.claims = (result && result.items) || [];
      graphState.loading = false;
      sideBody.replaceChildren(entityPanel(subject, graphState.node, graphState.claims, false, null));
    } catch (error) {
      if (isAbort(error)) return;
      graphState.loading = false;
      graphState.error = error;
      sideBody.replaceChildren(entityPanel(subject, graphState.node, [], false, error));
    }
  }

  async function load(subject) {
    try {
      const [subjects, graphData] = await Promise.all([
        api.get("/api/v1/subjects", { limit: 400 }, { signal: context.signal }),
        subject ? api.get("/api/v1/graph",
          { subject, hops: hopsOf(context.params), limit: 300 }, { signal: context.signal })
          : Promise.resolve(null),
      ]);
      if (destroyed) return;
      subjectsState.items = (subjects && subjects.items) || [];
      const active = subject || (subjectsState.items[0] && subjectsState.items[0].subject) || "";
      if (!subject && active) {
        context.setQuery({ subject: active, hops: String(hopsOf(context.params)) });
        return;
      }
      subjectsState.active = active;
      paintSubjects();
      if (!active) {
        mainCard.querySelector(".atlas-card-body").replaceChildren(emptyState({
          title: "还没有实体索引",
          hint: "claims 由带 claims 的 ingest 记录产生。先写入一条带 subject/predicate/slot_key/value 的记忆，或运行 hippomind graph --subject <name> 检查账本。",
          action: { label: "记录一条事实", icon: "plus", primary: true,
                    onClick: () => context.navigate("/facts?tab=claims") },
        }));
        sideCard.querySelector(".atlas-card-body").replaceChildren(entityPanel("", null, [], false, null));
        return;
      }
      graphState.data = graphData;
      paintGraph();
      await paintEntity(active);
    } catch (error) {
      if (isAbort(error)) return;
      mainCard.querySelector(".atlas-card-body").replaceChildren(errorState({
        title: "图谱加载失败", error, onRetry: () => context.refresh(),
      }));
    }
  }

  await load(subjectsState.active);
  context.onCleanup(() => { destroyed = true; if (canvas) canvas.destroy(); });
}

export async function refresh(context) {
  const subject = subjectOf(context.params);
  if (!subject) return render(context);
  const body = context.container.querySelector(".vb-graph");
  if (!body) return render(context);
  return render(context);
}
