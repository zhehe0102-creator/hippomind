/* ============================================================================
   HippoMind Atlas — DOM primitives
   The only sanctioned way to build UI. Every dynamic string becomes a text
   node here, never markup: the daemon's data is evidence, not HTML.
   ========================================================================= */
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";

function appendChildren(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) { appendChildren(node, child); continue; }
    if (child instanceof Node) { node.appendChild(child); continue; }
    node.appendChild(document.createTextNode(String(child)));
  }
}

function setAttribute(node, key, value) {
  if (value === null || value === undefined || value === false) return;
  if (key === "class" || key === "className") {
    const list = Array.isArray(value) ? value.filter(Boolean) : String(value).split(/\s+/);
    node.setAttribute("class", list.filter(Boolean).join(" "));
    return;
  }
  if (key === "text") { node.textContent = String(value); return; }
  if (key === "dataset") {
    for (const [name, entry] of Object.entries(value || {})) {
      if (entry === null || entry === undefined) continue;
      node.dataset[name] = String(entry);
    }
    return;
  }
  if (key === "style") {
    if (typeof value === "string") { node.setAttribute("style", value); return; }
    for (const [name, entry] of Object.entries(value || {})) {
      if (entry === null || entry === undefined) continue;
      if (name.startsWith("--")) node.style.setProperty(name, String(entry));
      else node.style[name] = String(entry);
    }
    return;
  }
  if (key === "on") {
    for (const [type, handler] of Object.entries(value || {})) {
      if (typeof handler === "function") node.addEventListener(type, handler);
    }
    return;
  }
  if (key === "value" && "value" in node) { node.value = value; return; }
  if (key === "checked" && "checked" in node) { node.checked = Boolean(value); return; }
  if (key === "selected" && "selected" in node) { node.selected = Boolean(value); return; }
  if (key === "disabled") { node.disabled = Boolean(value); return; }
  if (key === "html") {
    /* Only for static, audited markup shipped in this app (icons). Never data. */
    node.innerHTML = String(value);
    return;
  }
  node.setAttribute(key, value === true ? "" : String(value));
}

export function el(tag, attrs = null, ...children) {
  const node = document.createElement(tag);
  if (attrs) for (const [key, value] of Object.entries(attrs)) setAttribute(node, key, value);
  appendChildren(node, children);
  return node;
}

export function svg(tag, attrs = null, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "text") { node.textContent = String(value); continue; }
      if (key === "on") {
        for (const [type, handler] of Object.entries(value || {})) {
          if (typeof handler === "function") node.addEventListener(type, handler);
        }
        continue;
      }
      if (value === null || value === undefined || value === false) continue;
      node.setAttribute(key, String(value));
    }
  }
  appendChildren(node, children);
  return node;
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function frag(...children) {
  const fragment = document.createDocumentFragment();
  appendChildren(fragment, children);
  return fragment;
}

/* Escape only for the rare case where plain text must cross a boundary we do
   not control (titles, clipboard text). UI markup itself never uses this. */
export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

export function delegate(root, selector, type, handler) {
  const listener = (event) => {
    const target = event.target instanceof Element ? event.target.closest(selector) : null;
    if (target && root.contains(target)) handler(event, target);
  };
  root.addEventListener(type, listener);
  return () => root.removeEventListener(type, listener);
}

export function isTypingTarget(target) {
  if (!(target instanceof Element)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

/* Copy with a clipboard fallback that works under file://-less local origins. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    document.body.removeChild(area);
    return ok;
  }
}
