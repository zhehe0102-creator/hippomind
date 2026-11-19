/**
 * Out-of-process harness for hippomind/web/js/views/recall.js.
 *
 * The Atlas frontend is dependency-free ES modules with no build step, so there
 * is no bundler to run them through and no jsdom in this repo. This file ships
 * a deliberately tiny DOM shim (only the surface the view modules actually
 * touch: el/clear/textContent/listeners/querySelector) plus a fetch stub, then
 * mounts the real view module and reports what it did as one JSON line.
 *
 * Why a real mount instead of a static grep: the thing under test is
 * "the request body carries as_of, and the result area says so". A regex over
 * the source would pass even if the field were built but never read, or if the
 * banner were rendered from the draft instead of the applied value.
 *
 * Usage: node recall_view_harness.mjs <viewPath> <scenarioJsonPath>
 * Output: one JSON object on stdout.
 */
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";

/* --------------------------------------------------------------- DOM shim -- */

class NodeBase {
  constructor() { this.childNodes = []; this.parentNode = null; }
  get children() { return this.childNodes.filter((node) => node instanceof Element); }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get isConnected() { let node = this; while (node.parentNode) node = node.parentNode; return node === globalThis.document; }
  appendChild(node) {
    if (node instanceof DocumentFragment) {
      for (const child of [...node.childNodes]) this.appendChild(child);
      return node;
    }
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node === null || node === undefined) continue;
      this.appendChild(node);
    }
  }
  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index >= 0) this.childNodes.splice(index, 1);
    node.parentNode = null;
    return node;
  }
  replaceWith(node) {
    const parent = this.parentNode;
    if (!parent) return;
    parent.childNodes[parent.childNodes.indexOf(this)] = node;
    node.parentNode = parent;
    this.parentNode = null;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  contains(node) {
    let current = node;
    while (current) { if (current === this) return true; current = current.parentNode; }
    return false;
  }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(value) {
    this.childNodes = [];
    if (value !== "" && value !== null && value !== undefined) {
      this.appendChild(new TextNode(String(value)));
    }
  }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  removeEventListener(type, handler) {
    const list = this.listeners[type] || [];
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }
  dispatchEvent(event) {
    const shape = Object.assign(
      { type: "", target: this, preventDefault() {}, stopPropagation() {} }, event);
    let node = this;
    while (node) {
      for (const handler of [...(node.listeners[shape.type] || [])]) handler.call(node, shape);
      node = node.parentNode;
    }
    return true;
  }
  getClientRects() { return [{}]; }
}

class TextNode extends NodeBase {
  constructor(text) { super(); this.nodeType = 3; this.data = String(text); }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
}

class DocumentFragment extends NodeBase {
  constructor() { super(); this.nodeType = 11; }
}

class Style {
  constructor() { this.values = {}; }
  setProperty(name, value) { this.values[name] = String(value); }
  getPropertyValue(name) { return this.values[name] || ""; }
}

class Element extends NodeBase {
  constructor(tagName, namespace) {
    super();
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.namespaceURI = namespace || null;
    this.attributes = {};
    this.style = new Style();
    this.dataset = {};
    this.listeners = {};
    this.value = "";
    this.disabled = false;
    this.checked = false;
    this.selected = false;
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) {
    return Object.prototype.hasOwnProperty.call(this.attributes, key) ? this.attributes[key] : null;
  }
  removeAttribute(key) { delete this.attributes[key]; }
  hasAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attributes, key); }
  click() { this.dispatchEvent({ type: "click" }); }
  focus() {} select() {} blur() {}
  get classList() {
    const self = this;
    const list = () => (self.getAttribute("class") || "").split(/\s+/).filter(Boolean);
    return {
      add(...names) {
        const next = new Set(list());
        for (const name of names) next.add(name);
        self.setAttribute("class", [...next].join(" "));
      },
      remove(...names) {
        const next = new Set(list());
        for (const name of names) next.delete(name);
        self.setAttribute("class", [...next].join(" "));
      },
      contains(name) { return list().includes(name); },
      toggle(name, force) {
        const active = force === undefined ? !list().includes(name) : Boolean(force);
        if (active) this.add(name); else this.remove(name);
        return active;
      },
    };
  }
  /* Compound selectors matter here: the view marks the "draft differs from
     applied" state with ".vb-asof-note.is-visible", and a shim that only
     understood single classes would silently report that state as absent. */
  matches(selector) {
    return String(selector).split(",").some((part) => this._matchesSimple(part.trim()));
  }
  _matchesSimple(piece) {
    if (!piece) return false;
    const tokens = piece.match(/^[a-zA-Z][a-zA-Z0-9-]*|\.[A-Za-z0-9_-]+|\[[^\]]+\]/g);
    if (!tokens || tokens.join("") !== piece) return false;
    return tokens.every((token) => {
      if (token.startsWith(".")) return this.classList.contains(token.slice(1));
      if (token.startsWith("[")) {
        const match = /^\[([a-zA-Z-]+)(?:="([^"]*)")?\]$/.exec(token);
        if (!match) return false;
        const value = this.getAttribute(match[1]);
        return match[2] === undefined ? value !== null : value === match[2];
      }
      return this.tagName === token.toUpperCase();
    });
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (!(child instanceof Element)) continue;
        if (child.matches(selector)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    let node = this;
    while (node) {
      if (node instanceof Element && node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
}

const document = new Element("#document");
document.createElement = (tag) => new Element(tag);
document.createElementNS = (namespace, tag) => new Element(tag, namespace);
document.createTextNode = (text) => new TextNode(text);
document.createDocumentFragment = () => new DocumentFragment();
document.body = new Element("body");
document.documentElement = document;
document.activeElement = null;
document.getElementById = () => null;

Object.assign(globalThis, {
  document, Node: NodeBase, Element, DocumentFragment,
  CustomEvent: class { constructor(type, options) { this.type = type; this.detail = (options || {}).detail; } },
  window: {
    requestAnimationFrame: (fn) => { fn(); return 1; },
    setTimeout: () => 1,
    clearTimeout: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => true,
    location: { hash: "", pathname: "/ui/", search: "" },
  },
});
globalThis.performance = globalThis.performance || { now: () => Date.now() };

/* ------------------------------------------------------------- scenario -- */

const [, , viewPath, scenarioPath] = process.argv;
if (!viewPath || !scenarioPath) {
  console.error("usage: node recall_view_harness.mjs <viewPath> <scenarioPath>");
  process.exit(2);
}
const scenario = JSON.parse(readFileSync(scenarioPath, "utf8"));

const requests = [];
globalThis.fetch = async (url, options) => {
  const body = options && options.body ? JSON.parse(options.body) : null;
  requests.push({ url: String(url), method: (options && options.method) || "GET", body });
  const hits = scenario.hits || [];
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    text: async () => JSON.stringify({ hits, offset: body ? body.offset || 0 : 0, limit: 20 }),
  };
};

const view = await import(pathToFileURL(viewPath).href);
const applied = [];
const context = {
  params: new URLSearchParams(scenario.query || ""),
  path: "/recall",
  signal: new AbortController().signal,
  setQuery: (params) => applied.push(params),
  onCleanup: () => {},
  navigate: () => {},
  toast: () => {},
  drawer: {},
  confirm: () => true,
};
const root = await view.render(context);

/* The view rebuilds the whole page on a router change, so "did the field
   update" is answered through the setQuery payload the router would receive. */
function findInputByName(name) {
  return root.querySelectorAll("input").find((input) => input.getAttribute("name") === name) || null;
}
const asOfInput = findInputByName("as_of");

function typeInto(input, value) {
  input.value = value;
  input.dispatchEvent({ type: "input" });
}

function pressEnter(input) {
  input.dispatchEvent({ type: "keydown", key: "Enter" });
}

const steps = [];
for (const step of scenario.steps || []) {
  if (step.action === "type" && asOfInput) typeInto(asOfInput, step.value);
  if (step.action === "enter" && asOfInput) pressEnter(asOfInput);
  if (step.action === "clickClear") {
    const clear = root.querySelectorAll("button")
      .find((button) => button.textContent.includes("清空时间点"));
    if (clear) clear.click();
  }
  /* The shell calls module.refresh(context) on its 30s heartbeat. The view
     must reload from the applied filters, never from whatever the operator
     happens to be typing at that moment. */
  if (step.action === "refresh" && typeof view.refresh === "function") {
    await view.refresh(context);
  }
  if (step.action === "snapshot") {
    steps.push({
      label: step.label,
      rootText: root.textContent,
      inputValue: asOfInput ? asOfInput.value : null,
      noteVisible: Boolean(root.querySelector(".vb-asof-note.is-visible")),
    });
  }
}

const sliceBanners = root.querySelectorAll(".vb-recall-slice").map((node) => ({
  text: node.textContent,
  role: node.getAttribute("role"),
}));

process.stdout.write(JSON.stringify({
  ok: true,
  requests,
  applied,
  sliceBanners,
  steps,
  summaryText: (root.querySelector(".vb-advanced-summary") || { textContent: "" }).textContent,
  finalText: root.textContent,
}) + "\n");
