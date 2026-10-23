/* ============================================================================
   HippoMind Atlas — component factories
   Every component returns a real Element built through core/dom.el: API text
   always crosses into the document as a text node, never as markup.
   Callback convention: handlers receive the native DOM event first (so
   event.target.value works) and the extracted value second.
   ========================================================================= */
"use strict";

import { el, clear, copyText } from "./core/dom.js";
import { icon } from "./icons.js";
import { fmtInt, fmtPct } from "./core/format.js";

const TONES = ["neutral", "accent", "ok", "warn", "danger", "link"];
let uid = 0;
const nextId = (prefix) => { uid += 1; return prefix + "-" + uid; };

function tone(value, fallback = "neutral") {
  return TONES.indexOf(value) >= 0 ? value : fallback;
}

function text(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

/* ------------------------------------------------------ actions & helpers -- */

function actionNode(action) {
  if (!action) return null;
  if (action instanceof Node) return action;
  if (typeof action === "string") return el("span", { class: "atlas-action-text" }, action);
  if (Array.isArray(action)) return null;
  const classes = ["atlas-btn"];
  if (action.primary) classes.push("atlas-btn--primary");
  else if (action.danger) classes.push("atlas-btn--danger");
  else if (action.ghost) classes.push("atlas-btn--ghost");
  if (action.size === "sm") classes.push("atlas-btn--sm");
  const children = [];
  if (action.icon) children.push(icon(action.icon, { size: 13 }));
  if (action.label !== null && action.label !== undefined) {
    children.push(el("span", {}, String(action.label)));
  }
  const node = el("button", {
    class: classes,
    type: "button",
    title: action.title || null,
    "aria-label": action.ariaLabel || null,
    disabled: action.disabled || false,
  }, ...children);
  if (typeof action.onClick === "function") {
    node.addEventListener("click", (event) => action.onClick(event, node));
  }
  return node;
}

export function actionNodes(value) {
  if (!value) return [];
  if (Array.isArray(value)) {
    const nodes = [];
    for (const entry of value) nodes.push(...actionNodes(entry));
    return nodes;
  }
  const node = actionNode(value);
  return node ? [node] : [];
}

function focusable(root) {
  return Array.from(root.querySelectorAll(
    "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), " +
    "textarea:not([disabled]), [tabindex]:not([tabindex=\"-1\"])"
  )).filter((node) => node.getClientRects().length > 0);
}

function trapTab(event, root) {
  const nodes = focusable(root);
  if (!nodes.length) { event.preventDefault(); return; }
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/* ------------------------------------------------------------------ toast -- */

const TOAST_ICONS = { ok: "check", warn: "warning", danger: "error", info: "info", neutral: "info" };

export function toast(message, options = {}) {
  const root = document.getElementById("toast-root");
  if (!root) return null;
  const requested = String(options.tone || "info");
  const level = requested === "ok" || requested === "warn" ||
    requested === "danger" || requested === "info" ? requested : "neutral";
  const ms = Number.isFinite(options.ms) ? Math.max(0, Number(options.ms)) : 3200;
  let removed = false;
  const node = el("div", {
    class: "atlas-toast is-entering",
    dataset: { tone: level },
    role: level === "danger" ? "alert" : "status",
    "aria-live": level === "danger" ? "assertive" : "polite",
  },
    icon(TOAST_ICONS[level] || "info", { size: 14 }),
    el("span", { class: "atlas-toast-text" }, text(message, "")),
  );
  const timer = { id: null };
  function dismiss() {
    if (removed) return;
    removed = true;
    if (timer.id !== null) window.clearTimeout(timer.id);
    node.classList.add("is-leaving");
    window.setTimeout(() => {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 200);
  }
  node.appendChild(el("button", {
    class: "atlas-toast-close",
    type: "button",
    "aria-label": "关闭提示",
    on: { click: dismiss },
  }, icon("close", { size: 12 })));
  root.appendChild(node);
  while (root.children.length > 4) root.removeChild(root.firstElementChild);
  window.requestAnimationFrame(() => node.classList.remove("is-entering"));
  if (ms > 0) timer.id = window.setTimeout(dismiss, ms);
  return node;
}

/* --------------------------------------------------------------- skeleton -- */

export function skeleton(rows = 3) {
  const count = Math.max(1, Math.min(12, Number(rows) || 3));
  const stack = el("div", { class: "atlas-skeleton-stack", "aria-hidden": "true" });
  for (let index = 0; index < count; index += 1) {
    stack.appendChild(el("div", { class: "atlas-skeleton" }));
  }
  return stack;
}

/* ------------------------------------------------------------ error state -- */

function describeError(error) {
  if (!error) return "";
  if (typeof error === "string") return error;
  const status = Number(error.status);
  const detail = error.detail || error.message || "";
  if (status === 401) return "401 · " + (detail || "需要本机管理员令牌");
  if (status === 403) return "403 · " + (detail || "权限不足");
  if (status === 0) return detail || "无法连接 daemon";
  if (Number.isFinite(status) && status > 0) return status + " · " + (detail || "请求失败");
  return String(detail || error);
}

export function errorState(options = {}) {
  const node = el("div", { class: "atlas-error", role: "alert" });
  node.appendChild(el("div", { class: "atlas-error-head" },
    el("span", { class: "atlas-error-icon" }, icon("warning", { size: 20 })),
    el("div", {}, el("div", { class: "atlas-error-title" }, options.title || "加载失败"))
  ));
  const detail = describeError(options.error);
  if (detail) node.appendChild(el("div", { class: "atlas-error-detail" }, detail));

  const status = options.error && Number(options.error.status);
  const actions = el("div", { class: "atlas-error-actions" });
  if (status === 401) {
    node.appendChild(el("p", { class: "atlas-error-hint" },
      "daemon 需要本机管理员令牌。输入令牌后即可继续；令牌只保存在当前浏览器里，不会写进日志或页面。"));
    actions.appendChild(el("button", {
      class: "atlas-btn atlas-btn--primary",
      type: "button",
      on: {
        click: () => window.dispatchEvent(new CustomEvent("hippomind:unauthorized")),
      },
    }, icon("info", { size: 13 }), el("span", {}, "输入访问令牌")));
  } else if (status === 0) {
    node.appendChild(el("p", { class: "atlas-error-hint" },
      "确认 hippomindd 正在运行，并使用与 daemon 相同的主机与端口打开本页面。"));
  }
  if (typeof options.onRetry === "function") {
    actions.appendChild(el("button", {
      class: "atlas-btn",
      type: "button",
      on: { click: options.onRetry },
    }, icon("refresh", { size: 13 }), el("span", {}, options.retryLabel || "重试")));
  }
  if (actions.children.length) node.appendChild(actions);
  return node;
}

/* -------------------------------------------------------- badge and chips -- */

export function badge(value, options = {}) {
  return el("span", { class: "atlas-badge", dataset: { tone: tone(options.tone) } }, text(value, ""));
}

export function chip(value, options = {}) {
  const node = el("button", {
    class: "atlas-chip",
    type: "button",
    dataset: { active: options.active ? "true" : "false" },
    "aria-pressed": options.active ? "true" : "false",
    title: options.title || null,
  }, el("span", {}, text(value, "")));
  if (options.count !== null && options.count !== undefined) {
    node.appendChild(el("span", { class: "atlas-chip-count" }, fmtInt(options.count)));
  }
  if (typeof options.onClick === "function") node.addEventListener("click", options.onClick);
  return node;
}

/* ----------------------------------------------------------------- metric -- */

export function metric(options = {}) {
  const node = el("div", { class: "atlas-metric" + (options.hero ? " atlas-metric--hero" : "") +
    (options.small ? " atlas-metric--sm" : "") });
  if (options.tone) node.dataset.tone = tone(options.tone);
  node.appendChild(el("div", { class: "atlas-metric-label" }, text(options.label, "")));
  const value = el("div", { class: "atlas-metric-value" }, text(options.value));
  if (options.unit) value.appendChild(el("span", { class: "atlas-metric-unit" }, options.unit));
  if (options.delta !== null && options.delta !== undefined) {
    const numeric = Number(options.delta);
    const isNumber = Number.isFinite(numeric);
    const sign = isNumber ? (numeric >= 0 ? "up" : "down") : null;
    const display = isNumber ? (numeric >= 0 ? "+" : "") + fmtPct(numeric) : String(options.delta);
    const delta = el("span", { class: "atlas-metric-delta" }, display);
    if (sign) delta.dataset.sign = sign;
    value.appendChild(delta);
  }
  node.appendChild(value);
  if (options.hint) node.appendChild(el("div", { class: "atlas-metric-hint" }, options.hint));
  return node;
}

/* ------------------------------------------------------------------- card -- */

export function card(options = {}) {
  const node = el("section", { class: "atlas-card" + (options.className ? " " + options.className : "") });
  const actions = actionNodes(options.actions);
  if (options.eyebrow || options.title || actions.length) {
    const heading = el("div", { class: "atlas-card-heading" });
    if (options.eyebrow) heading.appendChild(el("div", { class: "atlas-card-eyebrow" }, options.eyebrow));
    if (options.title) heading.appendChild(el("h2", { class: "atlas-card-title" }, options.title));
    node.appendChild(el("header", { class: "atlas-card-head" }, heading,
      actions.length ? el("div", { class: "atlas-card-actions" }, ...actions) : null));
  }
  if (options.body !== null && options.body !== undefined) {
    node.appendChild(el("div", { class: "atlas-card-body" }, options.body));
  }
  return node;
}

/* ------------------------------------------------------------------ table -- */

function columnClass(column) {
  const classes = [];
  if (column.align === "right") classes.push("is-num");
  else if (column.align === "center") classes.push("is-center");
  if (column.mono) classes.push("is-mono");
  if (column.className) classes.push(column.className);
  return classes.join(" ");
}

function cellValue(column, row, rowIndex) {
  if (typeof column.render === "function") {
    const rendered = column.render(row, rowIndex);
    /* A renderer may legitimately have nothing to show; never leak null. */
    if (rendered === null || rendered === undefined || rendered === "") return "—";
    return rendered;
  }
  let value;
  if (typeof column.format === "function") value = column.format(row, rowIndex);
  else value = row[column.key];
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  return value;
}

function cellNode(column, row, rowIndex) {
  const content = cellValue(column, row, rowIndex);
  const classes = columnClass(column);
  const cell = el("td", { class: classes || null });
  if (column.clamp) cell.appendChild(el("div", { class: "atlas-cell-clamp" }, content));
  else if (Array.isArray(content)) content.forEach((entry) => cell.appendChild(entry instanceof Node ? entry : el("span", {}, String(entry))));
  else cell.appendChild(content instanceof Node ? content : document.createTextNode(String(content)));
  return cell;
}

export function dataTable(options = {}) {
  const columns = Array.isArray(options.columns) ? options.columns : [];
  const rows = Array.isArray(options.rows) ? options.rows : [];
  const wrap = el("div", { class: "atlas-table-wrap" + (options.flush ? " atlas-table-wrap--flush" : "") });
  const table = el("table", { class: "atlas-table" + (options.dense ? " atlas-table--dense" : "") });
  const headRow = el("tr");
  for (const column of columns) {
    const cell = el("th", { scope: "col", class: columnClass(column) || null });
    if (column.width) cell.style.width = typeof column.width === "number" ? column.width + "px" : String(column.width);
    cell.appendChild(document.createTextNode(text(column.label === undefined ? column.key : column.label, "")));
    headRow.appendChild(cell);
  }
  table.appendChild(el("thead", {}, headRow));
  const body = el("tbody");
  if (!rows.length) {
    const emptyCell = el("td", { class: "atlas-table-empty", colspan: String(Math.max(1, columns.length)) });
    const empty = options.empty;
    if (empty instanceof Node) emptyCell.appendChild(empty);
    else emptyCell.appendChild(document.createTextNode(empty === undefined || empty === null ? "暂无数据" : String(empty)));
    body.appendChild(el("tr", {}, emptyCell));
  } else {
    rows.forEach((row, rowIndex) => {
      const attrs = {};
      if (options.rowKey) {
        const key = typeof options.rowKey === "function" ? options.rowKey(row, rowIndex) : row[options.rowKey];
        if (key !== null && key !== undefined) attrs["data-row-key"] = String(key);
      }
      if (typeof options.onRowClick === "function") {
        attrs.class = "is-clickable";
        attrs.tabindex = "0";
      }
      const tr = el("tr", attrs);
      for (const column of columns) tr.appendChild(cellNode(column, row, rowIndex));
      if (typeof options.onRowClick === "function") {
        const activate = (event) => {
          if (event.type === "click" && event.target instanceof Element && event.target.closest("button, a, input, select, textarea, label")) return;
          options.onRowClick(row, rowIndex, event);
        };
        tr.addEventListener("click", activate);
        tr.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            activate(event);
          }
        });
      }
      body.appendChild(tr);
    });
  }
  table.appendChild(body);
  wrap.appendChild(table);
  return wrap;
}

/* ------------------------------------------------------------------ pager -- */

export function pager(options = {}) {
  const limit = Math.max(1, Number(options.limit) || 25);
  const total = Math.max(0, Number(options.total) || 0);
  const offset = Math.max(0, Number(options.offset) || 0);
  const onChange = typeof options.onChange === "function" ? options.onChange : null;
  const start = total === 0 ? 0 : Math.min(offset + 1, total);
  const end = Math.min(offset + limit, total);
  const node = el("div", { class: "atlas-pager" },
    el("div", { class: "atlas-pager-info" },
      "第 ", el("b", {}, fmtInt(start)), "–", el("b", {}, fmtInt(end)),
      " 条 · 共 ", el("b", {}, fmtInt(total)), " 条"),
    el("div", { class: "atlas-pager-btns" })
  );
  const buttons = node.querySelector(".atlas-pager-btns");
  const go = (nextOffset) => {
    if (!onChange) return;
    onChange(Math.max(0, Math.min(nextOffset, Math.max(0, total - 1))));
  };
  const prev = el("button", {
    class: "atlas-btn atlas-btn--sm",
    type: "button",
    disabled: offset <= 0,
    on: { click: () => go(Math.max(0, offset - limit)) },
  }, icon("chevron-left", { size: 12 }), el("span", {}, "上一页"));
  const next = el("button", {
    class: "atlas-btn atlas-btn--sm",
    type: "button",
    disabled: offset + limit >= total,
    on: { click: () => go(offset + limit) },
  }, el("span", {}, "下一页"), icon("chevron-right", { size: 12 }));
  buttons.appendChild(prev);
  buttons.appendChild(next);
  return node;
}

/* ------------------------------------------------------------- empty state -- */

export function emptyState(options = {}) {
  const node = el("div", { class: "atlas-empty" + (options.plain ? " atlas-empty--plain" : "") });
  node.appendChild(el("span", { class: "atlas-empty-icon" }, icon(options.icon || "archive", { size: 26 })));
  node.appendChild(el("div", { class: "atlas-empty-title" }, options.title || "暂无记录"));
  if (options.hint) node.appendChild(el("p", { class: "atlas-empty-hint" }, options.hint));
  const action = typeof options.action === "function" ? options.action() : options.action;
  const actions = actionNodes(action);
  if (actions.length) node.appendChild(el("div", { class: "atlas-empty-action" }, ...actions));
  return node;
}

/* ------------------------------------------------------------------ meter -- */

export function meter(options = {}) {
  const max = Number(options.max) > 0 ? Number(options.max) : 1;
  const raw = Number(options.value);
  const value = Number.isFinite(raw) ? Math.max(0, raw) : 0;
  const ratio = Math.max(0, Math.min(1, value / max));
  const node = el("div", { class: "atlas-meter" });
  if (options.tone) node.dataset.tone = tone(options.tone);
  const valueText = options.valueText !== null && options.valueText !== undefined
    ? String(options.valueText)
    : fmtPct(ratio);
  node.appendChild(el("div", { class: "atlas-meter-head" },
    el("span", { class: "atlas-meter-label" }, options.label || ""),
    el("span", { class: "atlas-meter-value" }, valueText)));
  const track = el("div", {
    class: "atlas-meter-track",
    role: "progressbar",
    "aria-label": options.label || "进度",
    "aria-valuemin": "0",
    "aria-valuemax": String(max),
    "aria-valuenow": String(value),
    title: fmtInt(value) + " / " + fmtInt(max),
  });
  const fill = el("span", { class: "atlas-meter-fill" });
  fill.style.width = (ratio * 100).toFixed(2) + "%";
  track.appendChild(fill);
  node.appendChild(track);
  return node;
}

/* --------------------------------------------------------- copy / actions -- */

export function copyButton(value, options = {}) {
  const label = options.label || "复制";
  const labelNode = el("span", { class: "atlas-copy-label" }, label);
  const button = el("button", {
    class: "atlas-copy atlas-btn atlas-btn--ghost atlas-btn--sm",
    type: "button",
    title: options.title || "复制到剪贴板",
  }, icon("copy", { size: 13 }), labelNode);
  let timer = null;
  button.addEventListener("click", async () => {
    const resolved = typeof value === "function" ? value() : value;
    const ok = await copyText(String(resolved === null || resolved === undefined ? "" : resolved));
    button.classList.toggle("is-copied", ok);
    labelNode.textContent = ok ? "已复制" : "复制失败";
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      button.classList.remove("is-copied");
      labelNode.textContent = label;
    }, 1600);
    toast(ok ? "已复制到剪贴板" : "复制失败，请手动选择文本", { tone: ok ? "ok" : "warn", ms: 1600 });
  });
  return button;
}

export function iconButton(name, options = {}) {
  return el("button", {
    class: "atlas-icon-btn" + (options.bordered ? " atlas-icon-btn--bordered" : "") +
      (options.danger ? " atlas-icon-btn--danger" : ""),
    type: "button",
    title: options.title || options.label || null,
    "aria-label": options.label || options.title || name,
    on: options.onClick ? { click: options.onClick } : null,
  }, icon(name, { size: options.size || 15 }));
}

/* ------------------------------------------------------------ confirm box -- */

let activeConfirm = null;

export function confirm(options = {}) {
  return new Promise((resolve) => {
    if (activeConfirm) activeConfirm.finish(false);
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const titleId = nextId("atlas-confirm-title");
    const danger = Boolean(options.danger);
    const scrim = el("div", { class: "atlas-confirm-scrim" });
    const card = el("div", {
      class: "atlas-confirm",
      role: "alertdialog",
      "aria-modal": "true",
      "aria-labelledby": titleId,
    });
    card.appendChild(el("div", { class: "atlas-confirm-head" },
      el("div", { class: "atlas-confirm-title", id: titleId }, options.title || "确认操作")));
    if (options.body) card.appendChild(el("div", { class: "atlas-confirm-body" }, options.body));
    const cancelButton = el("button", { class: "atlas-btn", type: "button" }, options.cancelLabel || "取消");
    const confirmButton = el("button", {
      class: "atlas-btn" + (danger ? " atlas-btn--danger" : " atlas-btn--primary"),
      type: "button",
    }, options.confirmLabel || "确认");
    card.appendChild(el("div", { class: "atlas-confirm-foot" }, cancelButton, confirmButton));
    scrim.appendChild(card);

    let settled = false;
    function cleanup() {
      document.removeEventListener("keydown", onKey, true);
      if (scrim.parentNode) scrim.parentNode.removeChild(scrim);
      if (activeConfirm && activeConfirm.scrim === scrim) activeConfirm = null;
      if (previous && document.contains(previous)) previous.focus();
    }
    function finish(result) {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(Boolean(result));
    }
    function onKey(event) {
      if (event.key === "Escape") { event.preventDefault(); finish(false); return; }
      if (event.key === "Tab") trapTab(event, card);
      if (event.key === "Enter" && document.activeElement === card) { event.preventDefault(); finish(true); }
    }
    cancelButton.addEventListener("click", () => finish(false));
    confirmButton.addEventListener("click", () => finish(true));
    scrim.addEventListener("click", (event) => { if (event.target === scrim) finish(false); });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(scrim);
    activeConfirm = { scrim, finish };
    (danger ? cancelButton : confirmButton).focus();
  });
}

/* ---------------------------------------------------------------- drawer -- */

let drawerEls = null;

function drawerKey(event) {
  if (!drawerEls) return;
  if (event.key === "Escape") {
    drawer.close();
    return;
  }
  if (event.key === "Tab") trapTab(event, drawerEls.panel);
}

function renderDrawer(options) {
  if (!drawerEls) return;
  const eyebrow = options.eyebrow === null || options.eyebrow === undefined ? "" : String(options.eyebrow);
  drawerEls.eyebrow.textContent = eyebrow;
  drawerEls.eyebrow.hidden = !eyebrow;
  drawerEls.title.textContent = options.title || "记录详情";
  const subtitle = options.subtitle === null || options.subtitle === undefined ? "" : String(options.subtitle);
  drawerEls.subtitle.textContent = subtitle;
  drawerEls.subtitle.hidden = !subtitle;
  clear(drawerEls.body);
  if (options.content instanceof Node) drawerEls.body.appendChild(options.content);
  else if (Array.isArray(options.content)) {
    for (const entry of options.content) {
      if (entry instanceof Node) drawerEls.body.appendChild(entry);
      else if (entry !== null && entry !== undefined) drawerEls.body.appendChild(document.createTextNode(String(entry)));
    }
  } else if (options.content !== null && options.content !== undefined) {
    drawerEls.body.appendChild(document.createTextNode(String(options.content)));
  }
  const actions = actionNodes(options.actions);
  clear(drawerEls.foot);
  drawerEls.foot.hidden = !actions.length;
  for (const node of actions) drawerEls.foot.appendChild(node);
}

function closeDrawer() {
  if (!drawerEls) return;
  const current = drawerEls;
  drawerEls = null;
  document.removeEventListener("keydown", drawerKey, true);
  current.scrim.classList.remove("is-open");
  current.panel.classList.remove("is-open");
  current.panel.setAttribute("aria-hidden", "true");
  document.body.classList.remove("drawer-open");
  window.setTimeout(() => {
    if (current.scrim.parentNode) current.scrim.parentNode.removeChild(current.scrim);
    if (current.panel.parentNode) current.panel.parentNode.removeChild(current.panel);
  }, 200);
  if (current.trigger && document.contains(current.trigger)) current.trigger.focus();
}

function openDrawer(options = {}) {
  if (drawerEls) {
    renderDrawer(options);
    drawerEls.panel.focus();
    return drawerEls.panel;
  }
  const root = document.getElementById("drawer-root");
  if (!root) return null;
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const titleId = nextId("atlas-drawer-title");
  const closeButton = el("button", {
    class: "atlas-icon-btn drawer-close",
    type: "button",
    "aria-label": "关闭详情",
    title: "关闭 (Esc)",
    on: { click: closeDrawer },
  }, icon("close", { size: 16 }));
  const eyebrow = el("div", { class: "drawer-eyebrow" });
  const title = el("h2", { class: "drawer-title", id: titleId });
  const subtitle = el("p", { class: "drawer-subtitle" });
  const body = el("div", { class: "drawer-body" });
  const foot = el("footer", { class: "drawer-foot" });
  const scrim = el("div", { class: "drawer-scrim", on: { click: closeDrawer } });
  const panel = el("aside", {
    class: "drawer",
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": titleId,
    "aria-hidden": "true",
    tabindex: "-1",
  },
    el("header", { class: "drawer-head" },
      el("div", { class: "drawer-heading" }, eyebrow, title, subtitle),
      closeButton),
    body,
    foot);
  root.appendChild(scrim);
  root.appendChild(panel);
  document.body.classList.add("drawer-open");
  drawerEls = { root, scrim, panel, body, foot, eyebrow, title, subtitle, trigger };
  document.addEventListener("keydown", drawerKey, true);
  renderDrawer(options);
  window.requestAnimationFrame(() => {
    if (!drawerEls || drawerEls.panel !== panel) return;
    scrim.classList.add("is-open");
    panel.classList.add("is-open");
    panel.setAttribute("aria-hidden", "false");
    panel.focus();
  });
  return panel;
}

export const drawer = Object.freeze({ open: openDrawer, close: closeDrawer });

/* ------------------------------------------------------------------ tabs -- */

export function tabs(options = {}) {
  const items = Array.isArray(options.items) ? options.items : [];
  const list = el("div", {
    class: "atlas-tabs",
    role: "tablist",
    "aria-label": options.label || "分区",
  });
  const buttons = [];
  let active = options.active !== null && options.active !== undefined
    ? options.active
    : (items[0] ? items[0].id : null);
  function select(id, opts = {}) {
    active = id;
    for (const button of buttons) {
      const on = button.dataset.tabId === String(id);
      button.setAttribute("aria-selected", on ? "true" : "false");
      button.tabIndex = on ? 0 : -1;
    }
    if (opts.focus) {
      const target = buttons.find((button) => button.dataset.tabId === String(id));
      if (target) target.focus();
    }
    if (!opts.silent && typeof options.onChange === "function") options.onChange(id);
  }
  items.forEach((item) => {
    const button = el("button", {
      class: "atlas-tab",
      type: "button",
      role: "tab",
      dataset: { tabId: String(item.id) },
      "aria-selected": "false",
      tabindex: "-1",
    }, el("span", {}, text(item.label === undefined ? item.id : item.label, "")));
    if (item.count !== null && item.count !== undefined) {
      button.appendChild(el("span", { class: "atlas-tab-count" }, fmtInt(item.count)));
    }
    button.addEventListener("click", () => select(item.id));
    button.addEventListener("keydown", (event) => {
      const index = buttons.indexOf(button);
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        const delta = event.key === "ArrowRight" ? 1 : -1;
        const next = buttons[(index + delta + buttons.length) % buttons.length];
        if (next) select(next.dataset.tabId, { focus: true });
      } else if (event.key === "Home") {
        event.preventDefault();
        if (buttons[0]) select(buttons[0].dataset.tabId, { focus: true });
      } else if (event.key === "End") {
        event.preventDefault();
        const last = buttons[buttons.length - 1];
        if (last) select(last.dataset.tabId, { focus: true });
      }
    });
    buttons.push(button);
    list.appendChild(button);
  });
  if (buttons.length) select(active, { silent: true });
  return list;
}

/* --------------------------------------------------------------- form API -- */

function fieldShell(options, control) {
  const field = el("div", { class: "atlas-field" + (options.className ? " " + options.className : "") });
  if (options.label) field.appendChild(el("label", { class: "atlas-field-label", for: control.id }, options.label));
  field.appendChild(control);
  if (options.hint) field.appendChild(el("span", { class: "atlas-field-hint" }, options.hint));
  return field;
}

function emitValue(handler, event, value) {
  if (typeof handler !== "function") return;
  event.value = value;
  handler(event, value);
}

export function textField(options = {}) {
  const id = options.id || nextId("atlas-field");
  const input = el("input", {
    id,
    class: "atlas-input" + (options.monospace ? " is-mono" : "") + (options.large ? " atlas-input--lg" : ""),
    type: options.type || "text",
    value: options.value === null || options.value === undefined ? "" : String(options.value),
    placeholder: options.placeholder || "",
    name: options.name || null,
    autocomplete: options.autocomplete || "off",
    spellcheck: "false",
    min: options.min === undefined ? null : options.min,
    max: options.max === undefined ? null : options.max,
    step: options.step === undefined ? null : options.step,
    "aria-label": options.label ? null : (options.ariaLabel || options.placeholder || "输入"),
  });
  input.addEventListener("input", (event) => {
    emitValue(options.onInput, event, input.value);
    emitValue(options.onChange, event, input.value);
  });
  if (typeof options.onKeyDown === "function") input.addEventListener("keydown", options.onKeyDown);
  const field = fieldShell(options, input);
  field.input = input;
  return field;
}

export function textareaField(options = {}) {
  const id = options.id || nextId("atlas-field");
  const area = el("textarea", {
    id,
    class: "atlas-textarea" + (options.monospace ? " is-mono" : ""),
    placeholder: options.placeholder || "",
    name: options.name || null,
    rows: String(options.rows || 4),
    spellcheck: "false",
    "aria-label": options.label ? null : (options.ariaLabel || options.placeholder || "输入"),
  });
  if (options.value !== null && options.value !== undefined) area.value = String(options.value);
  area.addEventListener("input", (event) => {
    emitValue(options.onInput, event, area.value);
    emitValue(options.onChange, event, area.value);
  });
  if (typeof options.onKeyDown === "function") area.addEventListener("keydown", options.onKeyDown);
  const field = fieldShell(options, area);
  field.textarea = area;
  return field;
}

function normalizeOption(option) {
  if (option === null || option === undefined) return { value: "", label: "" };
  if (typeof option === "string" || typeof option === "number") {
    return { value: String(option), label: String(option) };
  }
  const value = option.value !== undefined ? option.value
    : (option.id !== undefined ? option.id : "");
  const label = option.label !== undefined ? option.label : String(value);
  return { value: String(value), label: String(label) };
}

export function selectField(options = {}) {
  const id = options.id || nextId("atlas-field");
  const list = (Array.isArray(options.options) ? options.options : []).map(normalizeOption);
  const select = el("select", {
    id,
    class: "atlas-select",
    name: options.name || null,
    "aria-label": options.label ? null : (options.ariaLabel || "选择"),
  });
  const current = options.value === null || options.value === undefined ? "" : String(options.value);
  for (const item of list) {
    const option = el("option", { value: item.value, selected: item.value === current }, item.label);
    select.appendChild(option);
  }
  select.addEventListener("change", (event) => emitValue(options.onChange, event, select.value));
  const field = fieldShell(options, el("div", { class: "atlas-select-wrap" }, select));
  field.select = select;
  return field;
}

export function toggleField(options = {}) {
  const checked = Boolean(options.checked);
  const button = el("button", {
    class: "atlas-switch",
    type: "button",
    role: "switch",
    "aria-checked": checked ? "true" : "false",
  },
    el("span", { class: "atlas-switch-track" }, el("span", { class: "atlas-switch-knob" })),
    el("span", { class: "atlas-switch-label" }, options.label || ""));
  button.addEventListener("click", (event) => {
    const next = button.getAttribute("aria-checked") !== "true";
    button.setAttribute("aria-checked", next ? "true" : "false");
    emitValue(options.onChange, event, next);
  });
  button.checked = checked;
  return button;
}

export function segmented(options = {}) {
  const list = (Array.isArray(options.options) ? options.options : []).map(normalizeOption);
  const group = el("div", { class: "atlas-segmented", role: "group", "aria-label": options.label || "切换" });
  const buttons = [];
  function setActive(value) {
    for (const button of buttons) {
      button.setAttribute("aria-pressed", button.dataset.value === String(value) ? "true" : "false");
    }
  }
  for (const item of list) {
    const button = el("button", {
      class: "atlas-segmented-btn",
      type: "button",
      dataset: { value: item.value },
      "aria-pressed": "false",
    }, item.label);
    button.addEventListener("click", (event) => {
      setActive(item.value);
      if (typeof options.onChange === "function") options.onChange(item.value, event);
    });
    buttons.push(button);
    group.appendChild(button);
  }
  setActive(options.value === undefined ? "" : options.value);
  return group;
}

/* ------------------------------------------------------ stat row and rule -- */

export function statRow(pairs = []) {
  const list = el("div", { class: "atlas-statrow" });
  for (const pair of Array.isArray(pairs) ? pairs : []) {
    if (!pair) continue;
    list.appendChild(el("div", { class: "atlas-statrow-item" },
      el("span", { class: "atlas-statrow-label" }, text(pair.label, "")),
      el("span", { class: "atlas-statrow-value" }, text(pair.value, "—"))));
  }
  return list;
}

export function rule() {
  return el("hr", { class: "atlas-rule" });
}

/* ------------------------------------------------- optional layout extras -- */

export function sectionTitle(value, options = {}) {
  const actions = actionNodes(options.actions);
  return el("div", { class: "atlas-section-head" },
    el("h2", { class: "atlas-section-title" }, value),
    options.hint ? el("span", { class: "atlas-section-hint muted" }, options.hint) : null,
    actions.length ? el("div", { class: "atlas-action-row" }, ...actions) : null);
}

export function codeBlock(value, options = {}) {
  const node = el("div", { class: "atlas-code-block" });
  if (options.label) node.appendChild(el("div", { class: "atlas-code-label" }, options.label));
  node.appendChild(el("pre", { class: "atlas-code" }, text(value, "")));
  const actions = actionNodes(options.actions);
  if (actions.length) node.appendChild(el("div", { class: "atlas-action-row atlas-code-actions" }, ...actions));
  return node;
}

export default {
  actionNodes, badge, card, chip, codeBlock, confirm, copyButton, dataTable, drawer,
  emptyState, errorState, iconButton, meter, metric, pager, rule, sectionTitle,
  segmented, selectField, skeleton, statRow, tabs, textareaField, textField, toast,
  toggleField,
};
