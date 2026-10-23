/* ============================================================================
   HippoMind Atlas — command palette
   initPalette({commands}) + openPalette()/closePalette(). Commands are plain
   objects {id,label,hint,keywords,run}; filtering, arrow keys, Enter and Esc
   are handled here. app.js owns the keyboard shortcut itself.
   ========================================================================= */
"use strict";

import { el, clear } from "./core/dom.js";
import { icon } from "./icons.js";
import { toast } from "./components.js";

let registered = [];
let dynamicProvider = null;
let state = null;

function normalizeCommand(command, index) {
  const item = command || {};
  const id = item.id !== undefined ? String(item.id) : "command-" + index;
  const label = item.label !== undefined ? String(item.label) : id;
  const hint = item.hint !== undefined ? String(item.hint) : "";
  const keywords = item.keywords !== undefined ? String(item.keywords) : "";
  return {
    id,
    label,
    hint,
    keywords,
    run: typeof item.run === "function" ? item.run : null,
    haystack: (label + " " + hint + " " + keywords + " " + id).toLowerCase(),
    order: index,
  };
}

export function initPalette(options = {}) {
  registered = (Array.isArray(options.commands) ? options.commands : [])
    .filter((item) => item !== null && item !== undefined)
    .map(normalizeCommand);
  dynamicProvider = typeof options.dynamic === "function" ? options.dynamic : null;
  if (state) render(state.input.value);
}

function fuzzyIndex(needle, haystack) {
  let position = 0;
  for (const char of needle) {
    position = haystack.indexOf(char, position);
    if (position < 0) return -1;
    position += 1;
  }
  return position;
}

function filterCommands(query) {
  const text = String(query || "").trim().toLowerCase();
  if (!text) return registered.slice();
  const tokens = text.split(/\s+/).filter(Boolean);
  const scored = [];
  for (const command of registered) {
    let score = -1;
    if (tokens.every((token) => command.haystack.indexOf(token) >= 0)) {
      const label = command.label.toLowerCase();
      score = tokens.reduce((best, token) => {
        const index = label.indexOf(token);
        return index >= 0 && (best < 0 || index < best) ? index : best;
      }, -1);
      score = score < 0 ? 400 : score;
    } else {
      const squeeze = fuzzyIndex(text.replace(/\s+/g, ""), command.haystack);
      if (squeeze >= 0) score = 800 + squeeze;
    }
    if (score >= 0) scored.push({ command, score });
  }
  scored.sort((a, b) => (a.score - b.score) || (a.command.order - b.command.order));
  return scored.map((entry) => entry.command);
}

function optionId(index) {
  return "atlas-palette-option-" + index;
}

function updateSelection(nextIndex, scroll) {
  if (!state || !state.filtered.length) return;
  const count = state.filtered.length;
  state.selected = ((nextIndex % count) + count) % count;
  const items = state.list.querySelectorAll(".palette-item");
  items.forEach((item, index) => {
    const selected = index === state.selected;
    item.classList.toggle("is-selected", selected);
    item.setAttribute("aria-selected", selected ? "true" : "false");
  });
  state.input.setAttribute("aria-activedescendant", optionId(state.selected));
  if (scroll !== false) {
    const target = items[state.selected];
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ block: "nearest" });
    }
  }
}

function dynamicCommands(query) {
  if (!dynamicProvider) return [];
  const text = String(query || "").trim();
  if (!text) return [];
  try {
    const items = dynamicProvider(text);
    return (Array.isArray(items) ? items : [])
      .filter((item) => item !== null && item !== undefined)
      .map((item, index) => normalizeCommand(item, registered.length + index));
  } catch (error) {
    console.warn("[atlas] palette dynamic commands failed", error);
    return [];
  }
}

function render(query) {
  if (!state) return;
  const filtered = filterCommands(query).concat(dynamicCommands(query));
  state.filtered = filtered;
  state.selected = 0;
  clear(state.list);
  if (!filtered.length) {
    state.input.removeAttribute("aria-activedescendant");
    state.list.appendChild(el("div", { class: "palette-empty" },
      "没有匹配的命令。" + (registered.length ? "换个关键词试试。" : "命令尚未注册，请刷新页面。")));
    return;
  }
  filtered.forEach((command, index) => {
    const item = el("button", {
      class: "palette-item",
      type: "button",
      role: "option",
      id: optionId(index),
      "aria-selected": "false",
      tabindex: "-1",
    },
      el("span", { class: "palette-label" }, command.label),
      command.hint ? el("span", { class: "palette-hint" }, command.hint) : null);
    item.addEventListener("mousemove", () => updateSelection(index, false));
    item.addEventListener("mousedown", (event) => event.preventDefault());
    item.addEventListener("click", () => run(index));
    state.list.appendChild(item);
  });
  updateSelection(0, false);
}

function reportError(error) {
  console.error("[atlas] palette command failed", error);
  const message = error && (error.detail || error.message) ? (error.detail || error.message) : "未知错误";
  toast("命令执行失败：" + message, { tone: "danger", ms: 3600 });
}

function run(index) {
  if (!state) return;
  const command = state.filtered[index];
  if (!command) return;
  const runner = command.run;
  closePalette();
  if (!runner) return;
  try {
    const result = runner();
    if (result && typeof result.catch === "function") result.catch(reportError);
  } catch (error) {
    reportError(error);
  }
}

function onInputKeyDown(event) {
  if (!state) return;
  if (event.key === "ArrowDown" || (event.key === "j" && event.ctrlKey)) {
    event.preventDefault();
    updateSelection(state.selected + 1);
  } else if (event.key === "ArrowUp" || (event.key === "k" && event.ctrlKey)) {
    event.preventDefault();
    updateSelection(state.selected - 1);
  } else if (event.key === "PageDown") {
    event.preventDefault();
    updateSelection(state.selected + 5);
  } else if (event.key === "PageUp") {
    event.preventDefault();
    updateSelection(state.selected - 5);
  } else if (event.key === "Home") {
    event.preventDefault();
    updateSelection(0);
  } else if (event.key === "End") {
    event.preventDefault();
    updateSelection(state.filtered.length - 1);
  } else if (event.key === "Enter") {
    event.preventDefault();
    run(state.selected);
  } else if (event.key === "Escape") {
    event.preventDefault();
    closePalette();
  } else if (event.key === "Tab") {
    event.preventDefault();
    if (state.filtered.length) updateSelection(state.selected + (event.shiftKey ? -1 : 1));
  }
}

export function openPalette(initialQuery) {
  const root = document.getElementById("palette-root");
  if (!root) return;
  if (state) {
    state.input.focus();
    if (typeof initialQuery === "string" && initialQuery) {
      state.input.value = initialQuery;
      render(initialQuery);
    }
    return;
  }
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const list = el("div", {
    class: "palette-list",
    id: "atlas-palette-list",
    role: "listbox",
    "aria-label": "命令",
  });
  const input = el("input", {
    class: "palette-input",
    type: "search",
    placeholder: "搜索视图、命令或动作…",
    autocomplete: "off",
    spellcheck: "false",
    role: "combobox",
    "aria-expanded": "true",
    "aria-controls": "atlas-palette-list",
    "aria-autocomplete": "list",
    "aria-label": "命令面板",
  });
  const panel = el("div", {
    class: "palette",
    role: "dialog",
    "aria-modal": "true",
    "aria-label": "命令面板",
  },
    el("div", { class: "palette-field" }, icon("search", { size: 15 }), input),
    list,
    el("div", { class: "palette-foot" },
      el("span", {}, el("kbd", {}, "↑"), el("kbd", {}, "↓"), "选择"),
      el("span", {}, el("kbd", {}, "↵"), "执行"),
      el("span", {}, el("kbd", {}, "esc"), "关闭"),
      el("span", { class: "statusbar-spacer" }),
      el("span", {}, registered.length + " 条命令")));
  const scrim = el("div", { class: "palette-scrim", role: "presentation" }, panel);

  state = { root, scrim, panel, input, list, trigger, filtered: [], selected: 0 };
  input.addEventListener("input", () => render(input.value));
  input.addEventListener("keydown", onInputKeyDown);
  scrim.addEventListener("click", (event) => {
    if (event.target === scrim) closePalette();
  });

  if (typeof initialQuery === "string" && initialQuery) input.value = initialQuery;
  render(input.value);
  root.appendChild(scrim);
  window.requestAnimationFrame(() => {
    if (!state || state.scrim !== scrim) return;
    scrim.classList.add("is-open");
    input.focus();
  });
}

export function closePalette() {
  if (!state) return;
  const current = state;
  state = null;
  current.scrim.classList.remove("is-open");
  window.setTimeout(() => {
    if (current.scrim.parentNode) current.scrim.parentNode.removeChild(current.scrim);
  }, 160);
  if (current.trigger && document.contains(current.trigger) && typeof current.trigger.focus === "function") {
    current.trigger.focus();
  }
}

export default { initPalette, openPalette, closePalette };
