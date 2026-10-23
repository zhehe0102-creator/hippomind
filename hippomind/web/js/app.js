/* ============================================================================
   HippoMind Atlas — application shell
   Owns: boot, navigation, routing, the refresh heartbeat, the status bar,
   keyboard surface and the connection gate. Views own only their container.
   ========================================================================= */
"use strict";

import { el, $, clear, isTypingTarget, copyText } from "./core/dom.js";
import { api, ApiError } from "./core/api.js";
import { store } from "./core/store.js";
import { router } from "./core/router.js";
import { VIEWS } from "./views/registry.js";
import { fmtInt, duration, fmtMs, relTime } from "./core/format.js";
import { toast, errorState, drawer, confirm } from "./components.js";
import { icon } from "./icons.js";
import { initPalette, openPalette, closePalette } from "./palette.js";
import { initConnect, openConnect, closeConnect } from "./connect.js";

const current = {
  entry: VIEWS[0],
  route: router.parse(),
  module: null,
  context: null,
  controller: null,
  cleanups: [],
  intervals: [],
  renderedAt: 0,
};

function teardown() {
  for (const clearInterval of current.intervals.splice(0)) clearInterval();
  for (const cleanup of current.cleanups.splice(0)) {
    try { cleanup(); } catch (error) { console.error("[atlas] cleanup failed", error); }
  }
  if (current.controller) current.controller.abort();
  current.controller = null;
  current.module = null;
  current.context = null;
  drawer.close();
}

function buildContext(route) {
  const controller = new AbortController();
  current.controller = controller;
  const context = {
    container: $("#view-root"),
    params: route.params,
    path: route.path,
    signal: controller.signal,
    navigate: (path) => router.navigate(path),
    setQuery: (params) => router.navigate(route.path + router.query(params), { replace: true }),
    interval: (fn, ms) => {
      const id = window.setInterval(fn, ms);
      current.intervals.push(() => window.clearInterval(id));
      return id;
    },
    onCleanup: (fn) => { if (typeof fn === "function") current.cleanups.push(fn); },
    toast,
    confirm,
    drawer,
    refresh: () => refreshView({ manual: true }),
  };
  return context;
}

function applyChrome(entry) {
  $("#view-eyebrow").textContent = entry.index + " / " + entry.eyebrow;
  $("#view-title").textContent = entry.title;
  $("#view-subtitle").textContent = entry.subtitle;
  document.title = entry.title + " · HippoMind Atlas";
  document.querySelectorAll(".nav-item").forEach((node) => {
    node.classList.toggle("is-active", node.dataset.view === entry.id);
  });
}

async function renderRoute(route, options = {}) {
  const entry = route.id ? (VIEWS.find((view) => view.id === route.id) || VIEWS[0]) : VIEWS[0];
  if (!route.id || !VIEWS.some((view) => view.id === route.id)) {
    router.navigate("/" + entry.id, { replace: true });
    return;
  }
  teardown();
  current.entry = entry;
  current.route = route;
  store.patch({ view: entry.id });
  applyChrome(entry);
  const root = $("#view-root");
  const scrollTop = options.keepScroll ? root.scrollTop : 0;
  clear(root);
  const skeleton = document.createElement("div");
  skeleton.className = "view-loading";
  skeleton.innerHTML = '<div class="atlas-skeleton atlas-skeleton--title"></div><div class="atlas-skeleton"></div><div class="atlas-skeleton"></div>';
  root.appendChild(skeleton);

  try {
    const module = await entry.load();
    if (current.entry !== entry) return;
    current.module = module;
    const context = buildContext(route);
    current.context = context;
    const rendered = await module.render(context);
    if (current.entry !== entry) return;
    if (rendered instanceof Node) {
      clear(root);
      root.appendChild(rendered);
    }
    root.scrollTop = scrollTop;
    current.renderedAt = Date.now();
  } catch (error) {
    if (error && error.name === "AbortError") return;
    console.error("[atlas] view failed", entry.id, error);
    clear(root);
    root.appendChild(errorState({
      title: entry.title + " 加载失败",
      error,
      onRetry: () => renderRoute(current.route),
    }));
  }
}

async function refreshView({ manual = false } = {}) {
  if (current.module && typeof current.module.refresh === "function" && current.context) {
    try {
      await current.module.refresh(current.context);
      if (manual) toast("已刷新", { tone: "ok", ms: 1400 });
      return;
    } catch (error) {
      if (error && error.name === "AbortError") return;
      console.warn("[atlas] refresh failed", error);
    }
  }
  await renderRoute(current.route, { keepScroll: true });
  if (manual) toast("已刷新", { tone: "ok", ms: 1400 });
}

/* ----------------------------- status bar ----------------------------- */
function renderStatus() {
  const bar = $("#statusbar");
  if (!bar) return;
  const status = store.get("status");
  const meta = status.meta || {};
  clear(bar);
  const dot = el("span", { class: "statusbar-dot " + (status.online ? "is-on" : "is-off") });
  const parts = [
    dot,
    el("span", { class: "statusbar-part" },
      "daemon ", el("b", {}, status.online ? "v" + (meta.server_version || status.health?.server_version || "?") : "离线")),
  ];
  if (status.online && meta.schema_version) {
    parts.push(el("span", { class: "statusbar-sep" }, "·"));
    parts.push(el("span", { class: "statusbar-part" }, "schema ", el("b", {}, "v" + meta.schema_version)));
  }
  if (status.online && meta.ledger_id) {
    parts.push(el("span", { class: "statusbar-sep" }, "·"));
    parts.push(el("span", { class: "statusbar-part" }, "ledger ", el("code", {}, String(meta.ledger_id).slice(0, 10))));
  }
  parts.push(el("span", { class: "statusbar-sep" }, "·"));
  parts.push(el("span", { class: "statusbar-part" },
    status.online ? "延迟 " + (status.latencyMs === null ? "—" : fmtMs(status.latencyMs)) : "无法连接"));
  if (status.error) {
    parts.push(el("button", {
      class: "statusbar-retry", type: "button",
      on: { click: () => { ping(); refreshView({ manual: true }); } },
    }, "重试"));
  }
  parts.push(el("span", { class: "statusbar-spacer" }));
  parts.push(el("span", { class: "statusbar-hint" }, "1–0 视图 · ⌘K 命令 · R 刷新 · T 主题"));
  bar.append(...parts);
}

/* ------------------------------ heartbeat ----------------------------- */
let pingInFlight = false;
async function ping() {
  if (pingInFlight) return;
  pingInFlight = true;
  const started = performance.now();
  try {
    const health = await api.get("/health");
    let meta = store.get("status").meta;
    try { meta = await api.get("/api/v1/meta", null, { signal: AbortSignal.timeout(8000) }); }
    catch (error) { if (!(error instanceof ApiError) || error.status !== 401) console.warn("[atlas] meta unavailable", error); }
    const latencyMs = performance.now() - started;
    store.patch({ status: { online: true, checkedAt: Date.now(), latencyMs, health, meta, error: null } });
  } catch (error) {
    store.patch({ status: Object.assign({}, store.get("status"), { online: false, checkedAt: Date.now(), latencyMs: null, error }) });
  } finally {
    pingInFlight = false;
    renderStatus();
  }
}

function scheduleHeartbeat() {
  let timer = null;
  const arm = () => {
    if (timer) window.clearInterval(timer);
    const ms = Math.max(5000, Number(store.get("settings").refreshMs) || 30000);
    timer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        ping();
        if (current.module) refreshView();
      }
    }, ms);
  };
  arm();
  store.subscribe(() => arm());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") ping();
  });
}

/* ------------------------------ navigation ----------------------------- */
function buildNav() {
  const nav = $("#nav");
  clear(nav);
  for (const entry of VIEWS) {
    nav.appendChild(el("a", {
      class: "nav-item", href: "#/" + entry.id, "data-view": entry.id,
      title: entry.title + " · " + entry.eyebrow,
    },
      el("span", { class: "nav-index" }, entry.index),
      el("span", { class: "nav-label" }, entry.title),
      el("span", { class: "nav-en" }, entry.eyebrow),
    ));
  }
}

function applyTheme() {
  const theme = store.get("settings").theme === "night" ? "night" : "paper";
  document.documentElement.dataset.theme = theme;
  const button = $("#theme-btn");
  if (button) button.title = theme === "night" ? "切换到纸本浅色 (T)" : "切换到夜档深色 (T)";
}

function toggleTheme() {
  const next = store.get("settings").theme === "night" ? "paper" : "night";
  store.setSetting("theme", next);
  applyTheme();
}

function bindGlobalSurface() {
  $("#search-trigger").addEventListener("click", () => openPalette());
  $("#refresh-btn").addEventListener("click", () => { ping(); refreshView({ manual: true }); });
  $("#theme-btn").addEventListener("click", toggleTheme);

  window.addEventListener("hippomind:unauthorized", () => openConnect());

  document.addEventListener("keydown", (event) => {
    const paletteOpen = document.querySelector("#palette-root .palette") !== null;
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      paletteOpen ? closePalette() : openPalette();
      return;
    }
    if (event.key === "/" && !isTypingTarget(event.target)) {
      event.preventDefault();
      openPalette();
      return;
    }
    if (event.key === "Escape") {
      if (paletteOpen) { closePalette(); return; }
      if (document.body.classList.contains("drawer-open")) { drawer.close(); return; }
      closeConnect();
      return;
    }
    if (isTypingTarget(event.target) || paletteOpen) return;
    if (event.key >= "0" && event.key <= "9") {
      const index = event.key === "0" ? 9 : Number(event.key) - 1;
      const entry = VIEWS[index];
      if (entry) { event.preventDefault(); router.navigate("/" + entry.id); }
      return;
    }
    const key = event.key.toLowerCase();
    if (key === "r") { event.preventDefault(); ping(); refreshView({ manual: true }); }
    if (key === "t") { event.preventDefault(); toggleTheme(); }
    if (key === "?") {
      event.preventDefault();
      toast("⌘K 命令 · 1–0 切换视图 · R 刷新 · T 主题 · Esc 关闭", { tone: "info", ms: 4200 });
    }
  });
}

function registerCommands() {
  const commands = VIEWS.map((entry) => ({
    id: "nav:" + entry.id,
    label: entry.index + "  " + entry.title,
    hint: entry.eyebrow,
    keywords: entry.eyebrow + " " + entry.id,
    run: () => router.navigate("/" + entry.id),
  }));
  commands.push(
    { id: "action:refresh", label: "刷新当前视图", hint: "R", run: () => { ping(); refreshView({ manual: true }); } },
    { id: "action:theme", label: "切换主题（纸本/夜档）", hint: "T", run: toggleTheme },
    { id: "action:token", label: "更换访问令牌", hint: "", run: () => openConnect() },
    { id: "action:copy-token-path", label: "复制令牌文件路径", hint: "",
      run: async () => {
        const ok = await copyText("~/Library/Application Support/HippoMind/hippomindd.token");
        toast(ok ? "令牌文件路径已复制" : "复制失败，请手动选择", { tone: ok ? "ok" : "warn" });
      } },
  );
  initPalette({ commands });
}

/* ------------------------------- bootstrap ----------------------------- */
async function exchangeBootToken(route) {
  const nonce = route.params.get("boot");
  if (!nonce) return false;
  try {
    const result = await api.post("/api/v1/ui-token/exchange", { nonce });
    if (result && result.token) {
      api.setToken(result.token);
      const params = new URLSearchParams(route.params);
      params.delete("boot");
      router.navigate(route.path + router.query(params), { replace: true });
      return true;
    }
  } catch (error) {
    console.warn("[atlas] boot token exchange failed", error);
  }
  return false;
}

async function boot() {
  buildNav();
  applyTheme();
  bindGlobalSurface();
  registerCommands();
  renderStatus();

  // Boot nonce exchange MUST happen before any authenticated request: a 401
  // from the first paint would open the connection sheet even though the URL
  // already carried a valid one-time token (observed on /ui#/pulse?boot=...).
  let first = router.parse();
  if (first.params.get("boot")) {
    await exchangeBootToken(first);
    first = router.parse();
  }

  initConnect({ onConnected: async () => { await ping(); await renderRoute(router.parse()); } });
  scheduleHeartbeat();
  router.start((route) => { renderRoute(route); });
  ping();
  if (!api.hasToken()) {
    window.setTimeout(() => openConnect(), 400);
  }
  window.__ATLAS__ = { store, api, router, renderRoute, refreshView, ping };
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}
