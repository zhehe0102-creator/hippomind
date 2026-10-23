/* ============================================================================
   HippoMind Atlas — compose (上下文)
   Left: the request form. Right: the rendered Context Pack with its real
   budget, injection status, structured items and the text/XML projections.
   The XML view is rendered locally from the structured items (the daemon does
   not emit XML); the panel says so instead of pretending it is an API field.
   ========================================================================= */
"use strict";

import { el, clear, esc } from "../core/dom.js";
import { api } from "../core/api.js";
import { fmtInt, fmtMs } from "../core/format.js";
import {
  badge, codeBlock, copyButton, emptyState, errorState, meter,
  skeleton, tabs, textareaField, textField, toggleField, toast,
} from "../components.js";
import { icon } from "../icons.js";
import { openInspector } from "../components/inspector.js";

export const meta = {
  id: "compose",
  index: "04",
  title: "上下文",
  eyebrow: "COMPOSE",
  subtitle: "为当前任务组装最小、带证据、可注入的 Context Pack",
};

const FIELD_ORDER = ["goal", "progress", "key_decisions", "related_files", "blockers", "next_steps", "preferences"];
const FIELD_LABELS = {
  task: "任务",
  goal: "目标",
  progress: "当前进度",
  key_decisions: "关键决定",
  related_files: "相关文件",
  blockers: "阻塞",
  next_steps: "下一步",
  preferences: "用户偏好",
  evidence: "证据",
};
const OUTPUT_TABS = [
  { id: "structured", label: "结构化" },
  { id: "text", label: "纯文本" },
  { id: "xml", label: "XML" },
];

let live = null;

function isAbort(error) {
  return error && error.name === "AbortError";
}

function clampInt(value, low, high, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(low, Math.min(high, Math.trunc(number)));
}

function estimateTokens(text) {
  const value = String(text || "");
  const cjk = (value.match(/[\u3400-\u9fff]/g) || []).length;
  const nonAscii = (value.match(/[^\x00-\x7f\u3400-\u9fff\s]/g) || []).length;
  const ascii = Math.max(0, value.length - cjk - nonAscii);
  const whitespace = (value.match(/\s+/g) || []).length;
  return Math.max(1, Math.ceil(ascii / 4) + Math.ceil(cjk * 1.25) +
    Math.ceil(nonAscii / 2) + whitespace);
}

function readState(params) {
  return {
    query: params.get("q") || "",
    agent: "all",
    scope: "shared",
    project: "",
    taskId: "",
    maxItems: 8,
    maxTokens: 6000,
    maxChars: 16000,
    rawChat: false,
    requireEvidence: true,
    asOf: "",
    debug: false,
    activeTab: "structured",
  };
}

function buildBody(state) {
  return {
    query: state.query.trim(),
    agent: state.agent.trim() || "all",
    options: {
      max_items: clampInt(state.maxItems, 1, 20, 8),
      max_tokens: clampInt(state.maxTokens, 128, 6000, 6000),
      max_chars: clampInt(state.maxChars, 200, 16000, 16000),
      raw_chat: Boolean(state.rawChat),
      require_evidence: Boolean(state.requireEvidence),
      scope: state.scope.trim() || "shared",
      project: state.project.trim(),
      task_id: state.taskId.trim(),
      as_of: state.asOf.trim(),
      debug: Boolean(state.debug),
    },
  };
}

/* ------------------------------------------------------------ form side -- */

function numberField(label, value, low, high, onInput, hint) {
  return textField({
    label,
    type: "number",
    min: low,
    max: high,
    value: String(value),
    hint,
    onInput: (event, next) => onInput(next),
  });
}

function buildForm(instance, onSubmit) {
  const { state } = instance;
  const form = el("form", { class: "vb-compose-form", autocomplete: "off" });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    onSubmit();
  });

  const query = textareaField({
    label: "任务描述 query",
    rows: 6,
    value: state.query,
    placeholder: "例如：继续实现 Atlas 图谱视图；当前阻塞是 canvas 交互…",
    hint: "Context Pack 会把任务状态、关键决定与证据压缩进预算内。",
    onInput: (event, value) => { state.query = value; },
    onKeyDown: (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        onSubmit();
      }
    },
  });
  instance.queryField = query;
  form.appendChild(query);

  const grid = el("div", { class: "vb-form-grid" });
  grid.append(
    textField({
      label: "agent",
      value: state.agent,
      placeholder: "all",
      monospace: true,
      hint: "注入开关主体，all 表示默认",
      onInput: (event, value) => { state.agent = value; },
    }),
    textField({
      label: "scope",
      value: state.scope,
      placeholder: "shared",
      monospace: true,
      onInput: (event, value) => { state.scope = value; },
    }),
    textField({
      label: "project",
      value: state.project,
      placeholder: "留空不限",
      onInput: (event, value) => { state.project = value; },
    }),
    textField({
      label: "task_id",
      value: state.taskId,
      placeholder: "留空自动锚定",
      monospace: true,
      onInput: (event, value) => { state.taskId = value; },
    }),
    numberField("max_items", state.maxItems, 1, 20,
      (value) => { state.maxItems = value; }, "1–20（服务端有效上限 16）"),
    numberField("max_tokens", state.maxTokens, 128, 6000,
      (value) => { state.maxTokens = value; }, "主预算，默认 6000"),
    numberField("max_chars", state.maxChars, 200, 16000,
      (value) => { state.maxChars = value; }, "传输上限，默认 16000"),
    textField({
      label: "as_of（双时态切片）",
      value: state.asOf,
      placeholder: "ISO 时间（可选）",
      monospace: true,
      hint: "留空 = 现在",
      onInput: (event, value) => { state.asOf = value; },
    }));
  form.appendChild(grid);

  const toggles = el("div", { class: "vb-form-toggles" },
    toggleField({
      label: "raw_chat 原始聊天",
      checked: state.rawChat,
      onChange: (event, checked) => { state.rawChat = Boolean(checked); },
    }),
    toggleField({
      label: "require_evidence 要求证据",
      checked: state.requireEvidence,
      onChange: (event, checked) => { state.requireEvidence = Boolean(checked); },
    }),
    toggleField({
      label: "debug diagnostics",
      checked: state.debug,
      onChange: (event, checked) => { state.debug = Boolean(checked); },
    }));
  form.appendChild(toggles);

  instance.submitButton = el("button", {
    class: "atlas-btn atlas-btn--primary",
    type: "submit",
  }, icon("compose", { size: 14 }), el("span", {}, "组装上下文"));
  const resetButton = el("button", {
    class: "atlas-btn atlas-btn--ghost",
    type: "button",
    on: {
      click: () => {
        const fresh = readState(new URLSearchParams());
        Object.assign(state, { query: "", project: "", taskId: "", asOf: "" });
        state.maxItems = fresh.maxItems;
        state.maxTokens = fresh.maxTokens;
        state.maxChars = fresh.maxChars;
        state.rawChat = false;
        state.requireEvidence = true;
        state.debug = false;
        const nextForm = buildForm(instance, onSubmit);
        instance.form.replaceWith(nextForm);
        instance.form = nextForm;
        if (instance.queryField) {
          const area = instance.queryField.querySelector("textarea");
          if (area) area.focus();
        }
      },
    },
  }, icon("refresh", { size: 13 }), el("span", {}, "清空"));

  form.appendChild(el("div", { class: "vb-form-actions" }, instance.submitButton, resetButton));
  form.appendChild(el("p", { class: "vb-note" },
    icon("info", { size: 13 }),
    el("span", {},
      "默认要求证据、默认不含原始聊天；预算与注入状态来自真实响应，不做前端猜测。",
      "Ctrl/⌘ + Enter 也可提交。")));
  return form;
}

/* ---------------------------------------------------------- output side -- */

function renderItem(item) {
  const node = el("section", { class: "vb-pack-item" });
  node.appendChild(el("header", { class: "vb-pack-item-head" },
    el("span", { class: "vb-pack-task-label" }, "任务"),
    el("span", { class: "vb-pack-task" }, item.task || "未标注任务")));

  const body = el("div", { class: "vb-pack-fields" });
  for (const field of FIELD_ORDER) {
    const values = item[field];
    if (!values) continue;
    const list = Array.isArray(values) ? values : [values];
    if (!list.length) continue;
    const row = el("div", { class: "vb-pack-field" },
      el("span", { class: "vb-pack-field-label" }, FIELD_LABELS[field] || field));
    const valueList = el("div", { class: "vb-pack-field-values" });
    for (const value of list) {
      valueList.appendChild(el("p", { class: "vb-pack-field-value" }, String(value)));
    }
    row.appendChild(valueList);
    body.appendChild(row);
  }
  if (body.children.length) node.appendChild(body);

  const evidence = Array.isArray(item.evidence) ? item.evidence : [];
  if (evidence.length) {
    const list = el("ul", { class: "vb-pack-evidence" });
    for (const entry of evidence) {
      const ref = entry && entry.ref ? String(entry.ref) : "";
      if (!ref) continue;
      const chips = [el("span", { class: "vb-evidence-index" }, "证据")];
      if (entry.source_agent) chips.push(el("span", { class: "vb-pack-evidence-agent" }, entry.source_agent));
      if (entry.memory_id) {
        chips.push(el("button", {
          class: "vb-pack-evidence-link",
          type: "button",
          title: "打开这条记忆的详情",
          on: {
            click: () => openInspector(String(entry.memory_id), {
              signal: instanceSignal(),
            }),
          },
        }, ref));
      } else {
        chips.push(el("span", { class: "vb-pack-evidence-text" }, ref));
      }
      list.appendChild(el("li", {}, ...chips));
    }
    if (list.children.length) node.appendChild(list);
  }
  return node;
}

let currentInstance = null;
function instanceSignal() {
  return currentInstance ? currentInstance.context.signal : undefined;
}

function budgetBlock(instance, pack) {
  const state = instance.state;
  const text = String(pack.text || "");
  const chars = text.length;
  const debug = pack.debug && typeof pack.debug === "object" ? pack.debug : null;
  const constraints = debug && debug.constraints ? debug.constraints : {};
  const tokens = Number(constraints.estimated_tokens) || estimateTokens(text);
  const maxChars = clampInt(state.maxChars, 200, 16000, 16000);
  const maxTokens = clampInt(state.maxTokens, 128, 6000, 6000);
  const tokenSource = Number(constraints.estimated_tokens) ? "服务端估算" : "前端估算";
  const ratioTone = (ratio) => (ratio >= 0.9 ? "danger" : ratio >= 0.7 ? "warn" : "ok");
  const charRatio = maxChars ? chars / maxChars : 0;
  const tokenRatio = maxTokens ? tokens / maxTokens : 0;
  return el("div", { class: "vb-budget" },
    meter({
      label: "字符预算",
      value: chars,
      max: maxChars,
      tone: ratioTone(charRatio),
      valueText: fmtInt(chars) + " / " + fmtInt(maxChars),
    }),
    meter({
      label: "Token 预算（" + tokenSource + "）",
      value: tokens,
      max: maxTokens,
      tone: ratioTone(tokenRatio),
      valueText: fmtInt(tokens) + " / " + fmtInt(maxTokens),
    }),
    el("div", { class: "vb-budget-meta" },
      el("span", {}, "items ", el("b", {}, fmtInt((pack.items || []).length))),
      el("span", {}, "injection ",
        badge(pack.injection_enabled ? "开启" : "关闭",
          { tone: pack.injection_enabled ? "ok" : "danger" })),
      el("span", { class: "mono" }, "agent ", String(pack.injection_agent || state.agent || "all"))));
}

function toXml(pack) {
  const lines = ['<?xml version="1.0" encoding="UTF-8"?>'];
  lines.push('<context-pack injection="' + (pack.injection_enabled ? "true" : "false") +
    '" agent="' + esc(pack.injection_agent || "") + '">');
  const items = Array.isArray(pack.items) ? pack.items : [];
  for (const item of items) {
    lines.push('  <item task="' + esc(item.task || "") + '">');
    for (const field of FIELD_ORDER) {
      const values = item[field];
      if (!values) continue;
      lines.push('    <field name="' + field + '">');
      for (const value of (Array.isArray(values) ? values : [values])) {
        lines.push("      <value>" + esc(value) + "</value>");
      }
      lines.push("    </field>");
    }
    const evidence = Array.isArray(item.evidence) ? item.evidence : [];
    for (const entry of evidence) {
      lines.push('    <evidence ref="' + esc(entry.ref || "") +
        '" memory_id="' + esc(entry.memory_id || "") +
        '" source_agent="' + esc(entry.source_agent || "") + '"/>');
    }
    lines.push("  </item>");
  }
  lines.push("</context-pack>");
  return lines.join("\n");
}

function structuredPayload(pack) {
  return JSON.stringify({
    injection_enabled: Boolean(pack.injection_enabled),
    injection_agent: pack.injection_agent || "",
    items: Array.isArray(pack.items) ? pack.items : [],
    text: String(pack.text || ""),
  }, null, 2);
}

function currentPayload(instance) {
  const pack = instance.pack || {};
  const tab = instance.state.activeTab;
  if (tab === "text") return { content: String(pack.text || ""), ext: "txt", mime: "text/plain;charset=utf-8", label: "纯文本" };
  if (tab === "xml") return { content: toXml(pack), ext: "xml", mime: "application/xml;charset=utf-8", label: "XML" };
  return { content: structuredPayload(pack), ext: "json", mime: "application/json;charset=utf-8", label: "结构化 JSON" };
}

function downloadPayload(instance) {
  const payload = currentPayload(instance);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const filename = "context-pack-" + stamp + "." + payload.ext;
  const blob = new Blob([payload.content], { type: payload.mime });
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: filename });
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1500);
  toast("已下载 " + filename, { tone: "ok" });
}

function renderTabBody(instance, host) {
  const pack = instance.pack || {};
  clear(host);
  const tab = instance.state.activeTab;
  if (tab === "text") {
    const area = el("textarea", {
      class: "atlas-textarea vb-pack-output",
      rows: "18",
      readonly: true,
      spellcheck: "false",
      "aria-label": "Context Pack 纯文本",
    });
    area.value = String(pack.text || "");
    host.appendChild(area);
    host.appendChild(el("p", { class: "vb-hint" },
      "text 字段与注入 daemon 的文本完全一致（" + fmtInt(String(pack.text || "").length) + " 字符）。"));
    return;
  }
  if (tab === "xml") {
    const area = el("textarea", {
      class: "atlas-textarea vb-pack-output",
      rows: "18",
      readonly: true,
      spellcheck: "false",
      "aria-label": "Context Pack XML",
    });
    area.value = toXml(pack);
    host.appendChild(area);
    host.appendChild(el("p", { class: "vb-hint" },
      "XML 由本页根据结构化 items 本地渲染（daemon 不返回 XML 字段）；字段值均已转义。"));
    return;
  }
  const items = Array.isArray(pack.items) ? pack.items : [];
  if (!items.length) {
    host.appendChild(emptyState({
      plain: true,
      icon: "compose",
      title: "本次没有可注入的条目",
      hint: pack.injection_enabled
        ? "预算内没有找到同时满足证据与筛选条件的记忆；可以放宽 require_evidence、清理筛选或换任务描述。"
        : "注入当前关闭，daemon 返回了空包。开启注入后再试，或直接查看检索结果。",
    }));
  } else {
    const list = el("div", { class: "vb-pack-items" });
    items.forEach((item) => list.appendChild(renderItem(item)));
    host.appendChild(list);
  }
  if (pack.debug && typeof pack.debug === "object") {
    const details = el("details", { class: "vb-pack-debug" },
      el("summary", {}, "diagnostics"));
    const debug = pack.debug;
    const summary = [
      ["candidate_count", debug.candidate_count],
      ["task_anchor", debug.task_anchor],
      ["dominant_task", debug.dominant_task],
      ["filtered", Array.isArray(debug.filtered) ? debug.filtered.length : 0],
      ["constraints", debug.constraints ? JSON.stringify(debug.constraints) : ""],
    ];
    const list = el("div", { class: "vb-kv" });
    for (const pair of summary) {
      if (pair[1] === undefined || pair[1] === null || pair[1] === "") continue;
      list.appendChild(el("div", { class: "vb-kv-row" },
        el("span", { class: "vb-kv-key" }, pair[0]),
        el("span", { class: "vb-kv-value" }, String(pair[1]))));
    }
    details.appendChild(list);
    details.appendChild(codeBlock(JSON.stringify(debug, null, 2), { label: "debug" }));
    host.appendChild(details);
  }
}

function renderResult(instance, elapsedMs) {
  const panel = instance.outputHost;
  const pack = instance.pack || {};
  clear(panel);

  const header = el("div", { class: "vb-compose-result-head" },
    el("div", { class: "vb-compose-result-title" },
      el("h2", { class: "atlas-section-title" }, "Context Pack"),
      el("span", { class: "vb-hint" },
        "前端耗时 " + fmtMs(elapsedMs) + " · " +
        (pack.injection_enabled ? "注入开启" : "注入关闭"))),
    badge(pack.injection_enabled ? "可以注入" : "注入关闭",
      { tone: pack.injection_enabled ? "ok" : "danger" }));

  const notice = pack.injection_enabled
    ? null
    : el("div", { class: "vb-compose-notice", role: "alert" },
      icon("warning", { size: 16 }),
      el("div", {},
        el("b", {}, "上下文注入当前关闭。"),
        el("span", {},
          "daemon 返回了空包（agent=" + String(pack.injection_agent || instance.state.agent || "all") +
          "）。开启注入后再组装，或查看检索结果。")));

  const actions = el("div", { class: "vb-compose-actions" },
    copyButton(() => currentPayload(instance).content, { label: "复制当前输出" }),
    el("button", {
      class: "atlas-btn atlas-btn--ghost atlas-btn--sm",
      type: "button",
      on: { click: () => downloadPayload(instance) },
    }, icon("archive", { size: 13 }), el("span", {}, "下载")));

  const tabBar = el("div", { class: "vb-compose-tabs" },
    tabs({
      items: OUTPUT_TABS.map((item) => ({
        id: item.id,
        label: item.label,
        count: item.id === "structured" ? (pack.items || []).length : undefined,
      })),
      active: instance.state.activeTab,
      onChange: (id) => {
        instance.state.activeTab = id;
        renderTabBody(instance, instance.tabHost);
      },
    }),
    actions);

  const body = el("div", { class: "vb-compose-output-body" });
  instance.tabHost = body;
  renderTabBody(instance, body);

  const parts = [header];
  if (notice) parts.push(notice);
  parts.push(budgetBlock(instance, pack), tabBar, body);
  panel.append(...parts);
}

function renderEmptyOutput(instance) {
  clear(instance.outputHost);
  instance.outputHost.appendChild(emptyState({
    icon: "compose",
    title: "还没有组装上下文",
    hint: "Context Pack 默认要求证据、默认不含原始聊天；在左侧填写任务描述后点击「组装上下文」。",
    action: {
      label: "用示例任务试试", icon: "compose",
      onClick: () => {
        const area = instance.queryField ? instance.queryField.querySelector("textarea") : null;
        if (area) {
          area.value = "继续实现 Atlas 图谱视图，当前阻塞是 canvas 交互";
          instance.state.query = area.value;
          area.focus();
        }
      },
    },
  }));
}

async function submit(instance) {
  const { context, state } = instance;
  if (instance.destroyed) return;
  if (!state.query.trim()) {
    toast("先写一句任务描述", { tone: "warn" });
    const area = instance.queryField ? instance.queryField.querySelector("textarea") : null;
    if (area) area.focus();
    return;
  }
  instance.submitting = true;
  if (instance.submitButton) {
    instance.submitButton.disabled = true;
    instance.submitButton.classList.add("is-busy");
  }
  clear(instance.outputHost);
  instance.outputHost.appendChild(el("div", { class: "vb-loading" }, skeleton(7)));
  const started = performance.now();
  try {
    const pack = await api.post("/context-pack", buildBody(state), { signal: context.signal });
    if (instance.destroyed) return;
    instance.pack = pack || {};
    instance.elapsed = performance.now() - started;
    instance.state.activeTab = "structured";
    renderResult(instance, instance.elapsed);
  } catch (error) {
    if (isAbort(error) || instance.destroyed) return;
    clear(instance.outputHost);
    instance.outputHost.appendChild(errorState({
      title: "Context Pack 组装失败",
      error,
      onRetry: () => { void submit(instance); },
    }));
  } finally {
    instance.submitting = false;
    if (instance.submitButton) {
      instance.submitButton.disabled = false;
      instance.submitButton.classList.remove("is-busy");
    }
  }
}

export async function render(context) {
  const state = readState(context.params);
  const root = el("div", { class: "vb-view vb-compose" });
  const left = el("section", { class: "vb-compose-pane vb-compose-left" });
  const right = el("section", { class: "vb-compose-pane vb-compose-right" });
  const outputHost = el("div", { class: "vb-compose-output" });
  const instance = {
    context, state, root, outputHost,
    pack: null, elapsed: null, submitting: false,
    queryField: null, submitButton: null,
    form: null, tabHost: null,
    destroyed: false,
  };
  currentInstance = instance;
  live = instance;
  context.onCleanup(() => {
    instance.destroyed = true;
    if (live === instance) live = null;
    if (currentInstance === instance) currentInstance = null;
  });

  const onSubmit = () => { void submit(instance); };
  const form = buildForm(instance, onSubmit);
  instance.form = form;
  left.appendChild(form);
  right.appendChild(outputHost);
  root.append(left, right);
  renderEmptyOutput(instance);

  if (state.query.trim()) {
    await submit(instance);
  } else {
    window.requestAnimationFrame(() => {
      const area = instance.queryField ? instance.queryField.querySelector("textarea") : null;
      if (!instance.destroyed && area && area.isConnected) area.focus();
    });
  }
  return root;
}

export async function refresh() {
  /* Keep the operator's draft and result: remounting would wipe the form. */
}

