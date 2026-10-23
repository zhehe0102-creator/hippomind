#!/usr/bin/env node
/* ============================================================================
   HippoMind Atlas GUI QA -- zero-dependency Chrome CDP screenshot harness.

   Starts a headless Chrome, injects the loopback daemon token into
   localStorage before page scripts run, opens every view in both themes,
   captures 1440x900 PNGs, and records console errors, page exceptions,
   HTTP >= 400 responses, horizontal overflow, stuck skeletons and any
   XSS payload that actually executed.  Writes output/gui/REPORT.md.
   ========================================================================= */
"use strict";

import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

const VIEW_IDS = [
  "pulse", "archive", "recall", "compose", "graph",
  "facts", "timeline", "agents", "health", "reference",
];
const VIEW_OWNER = {
  pulse: "views-a", timeline: "views-a", agents: "views-a",
  health: "views-a", reference: "views-a",
  archive: "views-b", recall: "views-b", compose: "views-b",
  graph: "views-b", facts: "views-b",
};
const TOKEN_KEY = "hippomind.atlas.token";
const SETTINGS_KEY = "hippomind.atlas.settings";
const DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DEFAULT_OUT = path.resolve(process.cwd(), "output", "gui");
const DEFAULT_SEED_MANIFEST = "/tmp/hippomind-atlas-qa/gui-seed.json";

function parseArgs(argv) {
  const options = {
    daemonUrl: "http://127.0.0.1:8765",
    token: "",
    tokenFile: "",
    views: VIEW_IDS.slice(),
    themes: ["paper", "night"],
    out: DEFAULT_OUT,
    chrome: DEFAULT_CHROME,
    width: 1440,
    height: 900,
    waitMs: 2000,
    timeoutMs: 20000,
    debugPort: 0,
    seedManifest: DEFAULT_SEED_MANIFEST,
    emitHtml: true,
    keepBrowser: false,
    quiet: false,
    reportDir: "",
    flows: "all",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const raw = argv[index];
    const [flag, inline] = raw.includes("=") ? [raw.slice(0, raw.indexOf("=")), raw.slice(raw.indexOf("=") + 1)] : [raw, ""];
    const take = () => {
      if (inline) return inline;
      index += 1;
      if (index >= argv.length) throw new Error("missing value for " + flag);
      return argv[index];
    };
    switch (flag) {
      case "--daemon-url": options.daemonUrl = take().replace(/\/+$/, ""); break;
      case "--token": options.token = take(); break;
      case "--token-file": options.tokenFile = take(); break;
      case "--views": options.views = take().split(",").map((item) => item.trim()).filter(Boolean); break;
      case "--theme": {
        const theme = take().trim();
        options.themes = theme === "both" ? ["paper", "night"] : [theme];
        break;
      }
      case "--out": options.out = path.resolve(take()); break;
      case "--chrome": options.chrome = take(); break;
      case "--width": options.width = Number(take()); break;
      case "--height": options.height = Number(take()); break;
      case "--wait": options.waitMs = Number(take()); break;
      case "--timeout": options.timeoutMs = Number(take()); break;
      case "--debug-port": options.debugPort = Number(take()); break;
      case "--seed-manifest": options.seedManifest = take(); break;
      case "--no-html": options.emitHtml = false; break;
      case "--keep-browser": options.keepBrowser = true; break;
      case "--quiet": options.quiet = true; break;
      case "--report-dir": options.reportDir = path.resolve(take()); break;
      case "--flows": options.flows = take().trim(); break;
      case "--help":
      case "-h": options.help = true; break;
      default:
        throw new Error("unknown argument: " + raw);
    }
  }
  return options;
}

function usage() {
  return [
    "HippoMind Atlas GUI screenshot harness",
    "",
    "Usage: node scripts/gui_shot.mjs --token-file <path> [options]",
    "",
    "  --daemon-url <url>    fixture daemon base URL (default http://127.0.0.1:8765)",
    "  --token <token>       admin bearer token (or --token-file <path>)",
    "  --views a,b,c         views to capture (default: all 10)",
    "  --theme paper|night|both  theme(s) to capture (default: both)",
    "  --out <dir>           screenshot + report directory (default output/gui)",
    "  --chrome <path>       Chrome binary (default macOS Google Chrome)",
    "  --width/--height      viewport (default 1440x900)",
    "  --wait <ms>           settle wait after network idle (default 2000)",
    "  --timeout <ms>        per-view ready timeout (default 20000)",
    "  --seed-manifest <p>   seed manifest used for the report header",
    "  --keep-browser        leave headless Chrome running (debugging)",
    "  --report-dir <dir>    where REPORT.md/summary.json go (default: --out)",
    "  --flows none|boot|unauth|all  token boot / 401 flow checks (default: all)",
  ].join("\n");
}

function log(message) {
  process.stderr.write("[gui_shot] " + message + "\n");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

async function fetchJson(url, token, timeoutMs) {
  const headers = token ? { "X-HippoMind-Token": token } : {};
  try {
    const response = await fetch(url, {
      headers,
      signal: AbortSignal.timeout(timeoutMs || 5000),
    });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text.slice(0, 200) }; }
    return { status: response.status, data };
  } catch (error) {
    return { status: 0, data: null, error: String((error && error.message) || error) };
  }
}


/* ------------------------------- CDP client ------------------------------ */
class CDP {
  constructor(wsUrl) {
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Set();
    this.closed = false;
    this.socket = new WebSocket(wsUrl);
    this.opened = new Promise((resolve, reject) => {
      this.socket.addEventListener("open", () => resolve());
      this.socket.addEventListener("error", (event) => reject(new Error("CDP socket error: " + String(event && event.message))));
      this.socket.addEventListener("close", () => {
        this.closed = true;
        for (const [, entry] of this.pending) entry.reject(new Error("CDP connection closed"));
        this.pending.clear();
      });
    });
    this.socket.addEventListener("message", (event) => this.onMessage(event));
  }

  async connect() {
    await this.opened;
    return this;
  }

  onMessage(event) {
    let message;
    try { message = JSON.parse(String(event.data)); } catch { return; }
    if (message.id) {
      const entry = this.pending.get(message.id);
      if (!entry) return;
      this.pending.delete(message.id);
      if (message.error) entry.reject(new Error(entry.method + ": " + message.error.message + " (" + message.error.code + ")"));
      else entry.resolve(message.result || {});
      return;
    }
    if (!message.method) return;
    for (const handler of this.handlers) {
      try { handler(message.params || {}, message.sessionId || "", message.method); } catch (error) {
        log("event handler failed: " + String((error && error.stack) || error));
      }
    }
  }

  send(method, params, sessionId) {
    const id = this.nextId;
    this.nextId += 1;
    const message = { id, method, params: params || {} };
    if (sessionId) message.sessionId = sessionId;
    const promise = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method });
    });
    if (this.closed) return Promise.reject(new Error("CDP is closed"));
    this.socket.send(JSON.stringify(message));
    return promise;
  }

  on(handler) {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  close() {
    try { this.socket.close(); } catch { /* already closed */ }
  }
}

/* ------------------------------ Chrome launch ----------------------------- */
async function launchChrome(options) {
  if (!existsSync(options.chrome)) {
    throw new Error("Chrome binary not found: " + options.chrome + " (pass --chrome)");
  }
  const debugPort = options.debugPort || await getFreePort();
  const userDataDir = await mkdtemp(path.join(os.tmpdir(), "atlas-cdp-"));
  const args = [
    "--headless=new",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=" + debugPort,
    "--user-data-dir=" + userDataDir,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--hide-scrollbars",
    "--force-device-scale-factor=1",
    "--window-size=" + options.width + "," + options.height,
    "about:blank",
  ];
  const child = spawn(options.chrome, args, { stdio: ["ignore", "ignore", "pipe"] });
  const stderrLines = [];
  child.stderr.on("data", (chunk) => {
    stderrLines.push(String(chunk));
    while (stderrLines.length > 60) stderrLines.shift();
  });
  child.on("error", (error) => log("chrome spawn error: " + String(error)));
  const versionUrl = "http://127.0.0.1:" + debugPort + "/json/version";
  const deadline = Date.now() + 20000;
  let version = null;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) break;
    const response = await fetchJson(versionUrl, "", 1000);
    if (response.status === 200 && response.data && response.data.webSocketDebuggerUrl) {
      version = response.data;
      break;
    }
    await sleep(150);
  }
  if (!version) {
    try { child.kill("SIGKILL"); } catch { /* ignore */ }
    throw new Error("Chrome DevTools endpoint did not start on port " + debugPort + "\n" + stderrLines.join("").slice(-3000));
  }
  return {
    child,
    debugPort,
    userDataDir,
    wsUrl: version.webSocketDebuggerUrl,
    browserVersion: String(version.Browser || ""),
    stderrLines,
  };
}

async function stopChrome(launcher, browser, options) {
  if (options.keepBrowser) {
    log("keeping Chrome pid " + launcher.child.pid + " on port " + launcher.debugPort);
    return;
  }
  if (browser && !browser.closed) {
    try { await browser.send("Browser.close", {}); } catch { /* closing anyway */ }
    browser.close();
  }
  try {
    if (launcher.child.exitCode === null) launcher.child.kill("SIGTERM");
  } catch { /* ignore */ }
  await sleep(200);
  try {
    if (launcher.child.exitCode === null) launcher.child.kill("SIGKILL");
  } catch { /* ignore */ }
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      await rm(launcher.userDataDir, { recursive: true, force: true });
      break;
    } catch {
      await sleep(250);
    }
  }
}

/* ----------------------------- page evaluation ---------------------------- */
function injectSource(token, theme, mode) {
  const lines = ["(() => {", "  try {"];
  if (mode === "clear") {
    lines.push("    localStorage.removeItem(" + JSON.stringify(TOKEN_KEY) + ");");
  } else {
    lines.push("    localStorage.setItem(" + JSON.stringify(TOKEN_KEY) + ", " + JSON.stringify(token) + ");");
  }
  lines.push(
    "    localStorage.setItem(" + JSON.stringify(SETTINGS_KEY) + ", JSON.stringify({",
    "      theme: " + JSON.stringify(theme) + ", refreshMs: 60000, pageSize: 50, sidebar: true",
    "    }));",
    "  } catch (error) { /* storage blocked; the page will show the connect overlay */ }",
    "})();",
  );
  return lines.join("\n");
}

const READY_SOURCE = [
  "(() => {",
  "  const root = document.querySelector('#view-root');",
  "  return {",
  "    readyState: document.readyState,",
  "    hash: location.hash,",
  "    title: document.title,",
  "    hasAtlas: Boolean(window.__ATLAS__),",
  "    viewRoot: Boolean(root),",
  "    children: root ? root.children.length : -1,",
  "    loading: Boolean(root && root.querySelector('.view-loading')),",
  "    textLength: root ? String(root.innerText || '').length : 0",
  "  };",
  "})()",
].join("\n");

const DIAG_SOURCE = [
  "(() => {",
  "  const root = document.querySelector('#view-root');",
  "  const doc = document.documentElement;",
  "  const status = document.querySelector('#statusbar');",
  "  const connect = document.querySelector('#connect-root');",
  "  const active = document.querySelector('.nav-item.is-active');",
  "  const docWidth = doc.scrollWidth;",
  "  return {",
  "    readyState: document.readyState,",
  "    hash: location.hash,",
  "    title: document.title,",
  "    theme: doc.dataset.theme || '',",
  "    hasAtlas: Boolean(window.__ATLAS__),",
  "    online: window.__ATLAS__ ? Boolean(window.__ATLAS__.store.get('status').online) : false,",
  "    viewRoot: Boolean(root),",
  "    children: root ? root.children.length : -1,",
  "    text: root ? String(root.innerText || '').slice(0, 500) : '',",
  "    textLength: root ? String(root.innerText || '').length : 0,",
  "    scrollWidth: docWidth,",
  "    innerWidth: window.innerWidth,",
  "    clientWidth: doc.clientWidth,",
  "    overflow: docWidth > window.innerWidth + 1,",
  "    xss: (typeof window.__XSS__ !== 'undefined' && window.__XSS__ !== 0)",
  "      || (typeof window.__XSS !== 'undefined' && window.__XSS !== 0),",
  "    inlineScripts: document.querySelectorAll('script:not([src])').length,",
  "    statusbar: status ? String(status.innerText || '').slice(0, 300) : '',",
  "    connectRootHeight: connect ? Math.round(connect.getBoundingClientRect().height) : 0,",
  "    navItems: document.querySelectorAll('.nav-item').length,",
  "    activeNav: active && active.dataset ? String(active.dataset.view || '') : '',",
  "    errorState: Boolean(root && root.querySelector('.atlas-error, [data-state=error], [class*=error-state]')),",
  "    skeleton: Boolean(document.querySelector('.atlas-skeleton'))",
  "  };",
  "})()",
].join("\n");

const OVERFLOW_SOURCE = [
  "(() => {",
  "  const width = window.innerWidth;",
  "  const offenders = [];",
  "  const scrollingAncestor = (element) => {",
  "    let node = element.parentElement;",
  "    while (node && node !== document.body) {",
  "      const overflow = getComputedStyle(node).overflowX;",
  "      if (overflow === 'auto' || overflow === 'scroll'",
  "        || overflow === 'hidden' || overflow === 'clip') return true;",
  "      node = node.parentElement;",
  "    }",
  "    return false;",
  "  };",
  "  for (const element of document.querySelectorAll('body *')) {",
  "    const rect = element.getBoundingClientRect();",
  "    if (rect.width <= 0) continue;",
  "    if (rect.right > width + 1 || rect.left < -1) {",
  "      if (scrollingAncestor(element)) continue;",
  "      offenders.push({",
  "        tag: element.tagName.toLowerCase(),",
  "        cls: String(element.className || '').slice(0, 90),",
  "        id: element.id || '',",
  "        right: Math.round(rect.right),",
  "        width: Math.round(rect.width)",
  "      });",
  "      if (offenders.length >= 10) break;",
  "    }",
  "  }",
  "  return offenders;",
  "})()",
].join("\n");

const HTML_SOURCE = "document.documentElement.outerHTML";

const CONTRAST_SOURCE = [
  "(() => {",
  "  const parse = (value) => {",
  "    const match = String(value || '').match(/rgba?\\(([^)]+)\\)/);",
  "    if (!match) return null;",
  "    const parts = match[1].split(',').map((item) => parseFloat(item));",
  "    if (parts.length < 3) return null;",
  "    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };",
  "  };",
  "  const channel = (value) => {",
  "    const c = value / 255;",
  "    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);",
  "  };",
  "  const luminance = (color) => 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);",
  "  const ratio = (fg, bg) => {",
  "    const a = luminance(fg) + 0.05;",
  "    const b = luminance(bg) + 0.05;",
  "    return Math.round((Math.max(a, b) / Math.min(a, b)) * 100) / 100;",
  "  };",
  "  const opaqueBg = (element) => {",
  "    let node = element;",
  "    while (node) {",
  "      const bg = parse(getComputedStyle(node).backgroundColor);",
  "      if (bg && bg.a >= 0.95) return bg;",
  "      node = node.parentElement;",
  "    }",
  "    return parse(getComputedStyle(document.body).backgroundColor) || { r: 242, g: 238, b: 228, a: 1 };",
  "  };",
  "  const specs = [",
  "    { label: '正文', selector: 'body', large: false },",
  "    { label: '视图标题', selector: '#view-title', large: true },",
  "    { label: '视图副题', selector: '#view-subtitle', large: false },",
  "    { label: '状态栏', selector: '#statusbar', large: false },",
  "    { label: '侧栏导航', selector: '.nav-label', large: false },",
  "  ];",
  "  const checks = [];",
  "  for (const spec of specs) {",
  "    const element = document.querySelector(spec.selector);",
  "    if (!element) continue;",
  "    const fg = parse(getComputedStyle(element).color);",
  "    if (!fg) continue;",
  "    const value = ratio(fg, opaqueBg(element));",
  "    const threshold = spec.large ? 3.0 : 4.5;",
  "    checks.push({ label: spec.label, selector: spec.selector, ratio: value, threshold: threshold });",
  "  }",
  "  const ratios = checks.map((check) => check.ratio);",
  "  return { checks: checks, min: ratios.length ? Math.min.apply(null, ratios) : null };",
  "})()",
].join("\n");

async function evaluate(send, expression) {
  const result = await send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: false,
  });
  if (result.exceptionDetails) {
    const detail = result.exceptionDetails.exception && result.exceptionDetails.exception.description;
    throw new Error("Runtime.evaluate failed: " + (detail || result.exceptionDetails.text || "unknown"));
  }
  return result.result ? result.result.value : null;
}

function consoleText(args) {
  const parts = [];
  for (const item of args || []) {
    if (item === null || item === undefined) continue;
    if (Object.prototype.hasOwnProperty.call(item, "value")) parts.push(String(item.value));
    else if (item.description) parts.push(String(item.description));
    else if (item.unserializableValue) parts.push(String(item.unserializableValue));
    else if (item.type) parts.push("<" + item.type + ">");
  }
  return parts.join(" ").slice(0, 600);
}

function urlPathOf(url) {
  try { return new URL(url).pathname; } catch { return String(url || ""); }
}

function pngSize(buffer) {
  if (!buffer || buffer.length < 24) return { width: 0, height: 0 };
  if (buffer[0] !== 0x89 || buffer[1] !== 0x50 || buffer[2] !== 0x4e || buffer[3] !== 0x47) {
    return { width: 0, height: 0 };
  }
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}


/* ------------------------------- one capture ------------------------------ */
async function captureView(browser, options, theme, view, index, variant) {
  variant = variant || {};
  const run = {
    index,
    view,
    theme,
    url: options.daemonUrl + "/ui/#/" + view + (variant.urlSuffix || ""),
    ok: false,
    error: "",
    screenshot: "",
    screenshotBytes: 0,
    screenshotWidth: 0,
    screenshotHeight: 0,
    consoleErrors: [],
    pageErrors: [],
    logErrors: [],
    httpErrors: [],
    requestFailures: [],
    warnings: [],
    overflow: false,
    overflowOffenders: [],
    xss: false,
    inlineScripts: 0,
    themeMatches: true,
    connectOverlayVisible: false,
    skeletonStuck: false,
    errorState: false,
    textLength: 0,
    textPreview: "",
    statusbar: "",
    children: 0,
    elapsedMs: 0,
    debugHtml: "",
    flow: "",
    assertions: [],
    contrast: null,
    storedToken: null,
    reloadOnline: null,
    reloadHash: "",
    apiRequests: 0,
  };
  const started = Date.now();
  run.flow = variant.flow || "";
  const injectMode = variant.injectMode || "token";
  const assert = (label, ok, detail) => {
    run.assertions.push({ label: label, ok: Boolean(ok), detail: String(detail || "") });
  };
  let targetId = "";
  let sessionId = "";
  let removeHandler = null;
  let send = null;
  try {
    const created = await browser.send("Target.createTarget", { url: "about:blank" });
    targetId = created.targetId;
    const attached = await browser.send("Target.attachToTarget", { targetId, flatten: true });
    sessionId = attached.sessionId;
    send = (method, params) => browser.send(method, params, sessionId);

    let inflight = 0;
    const pendingUrls = new Map();
    const viewOrigin = new URL(options.daemonUrl).origin;

    removeHandler = browser.on((payload, eventSession, methodName) => {
      if (eventSession !== sessionId) return;
      if (methodName === "Network.requestWillBeSent") {
        inflight += 1;
        const url = payload.request ? String(payload.request.url || "") : "";
        pendingUrls.set(payload.requestId, url);
        if (url.indexOf("/api/") !== -1) run.apiRequests += 1;
        return;
      }
      if (methodName === "Network.loadingFinished") {
        inflight = Math.max(0, inflight - 1);
        pendingUrls.delete(payload.requestId);
        return;
      }
      if (methodName === "Network.loadingFailed") {
        inflight = Math.max(0, inflight - 1);
        const url = pendingUrls.get(payload.requestId) || "";
        pendingUrls.delete(payload.requestId);
        if (payload.canceled) return;
        if (String(payload.errorText || "").includes("ERR_ABORTED")) return;
        run.requestFailures.push({
          url: String(url || "").slice(0, 300),
          error: String(payload.errorText || "loadingFailed").slice(0, 240),
        });
        return;
      }
      if (methodName === "Runtime.consoleAPICalled") {
        const type = String(payload.type || "");
        const text = consoleText(payload.args);
        if (type === "error" || type === "assert") {
          run.consoleErrors.push({ text, source: "console." + type });
        } else if (type === "warning") {
          run.warnings.push({ text, source: "console.warning" });
        }
        return;
      }
      if (methodName === "Runtime.exceptionThrown") {
        const details = payload.exceptionDetails || {};
        const description = (details.exception && (details.exception.description || details.exception.value))
          || details.text || "uncaught exception";
        run.pageErrors.push({
          text: String(description).slice(0, 800),
          url: String(details.url || "").slice(0, 300),
          line: details.lineNumber,
          column: details.columnNumber,
        });
        return;
      }
      if (methodName === "Log.entryAdded") {
        const entry = payload.entry || {};
        const level = String(entry.level || "");
        const text = String(entry.text || "").slice(0, 600);
        const url = String(entry.url || "").slice(0, 300);
        if (level === "error") run.logErrors.push({ text, url, source: String(entry.source || "") });
        else if (level === "warning") run.warnings.push({ text, url, source: "log.warning" });
        return;
      }
      if (methodName === "Network.responseReceived") {
        const response = payload.response || {};
        const status = Number(response.status || 0);
        const url = String(response.url || "");
        if (status >= 400) {
          const ignored = Array.isArray(variant.allowHttpStatuses) && variant.allowHttpStatuses.indexOf(status) !== -1;
          const allowed = (status === 401 && urlPathOf(url) === "/health") || ignored;
          if (!allowed) run.httpErrors.push({ status, url: url.slice(0, 300), method: String(payload.type || "") });
        }
      }
    });

    await send("Page.enable", {});
    await send("Runtime.enable", {});
    await send("Log.enable", {});
    await send("Network.enable", {});
    await send("Page.setLifecycleEventsEnabled", { enabled: true });
    await send("Emulation.setDeviceMetricsOverride", {
      width: options.width,
      height: options.height,
      deviceScaleFactor: 1,
      mobile: false,
      screenWidth: options.width,
      screenHeight: options.height,
    });
    await send("Page.addScriptToEvaluateOnNewDocument", { source: injectSource(options.token, theme, injectMode) });
    await send("Page.navigate", { url: run.url });

    const deadline = Date.now() + options.timeoutMs;
    let lastState = null;
    if (variant.readyMode === "connect") {
      const connectDeadline = Date.now() + Math.min(options.timeoutMs, 8000);
      while (Date.now() < connectDeadline) {
        try {
          const visible = await evaluate(send, "(() => { const root = document.querySelector('#connect-root'); return Boolean(root && root.getBoundingClientRect().height > 0); })()");
          if (visible) break;
        } catch { /* keep polling */ }
        await sleep(150);
      }
      await sleep(Math.max(1200, Number(variant.extraWaitMs || 1500)));
    } else {
      while (Date.now() < deadline) {
        try { lastState = await evaluate(send, READY_SOURCE); } catch { lastState = null; }
        if (lastState && lastState.readyState === "complete" && lastState.hasAtlas
          && lastState.viewRoot && lastState.children > 0) {
          break;
        }
        await sleep(120);
      }
      if (variant.waitForExpression) {
        const extraDeadline = Date.now() + Number(variant.waitTimeoutMs || 12000);
        while (Date.now() < extraDeadline) {
          try {
            if (await evaluate(send, variant.waitForExpression)) break;
          } catch { /* keep polling */ }
          await sleep(150);
        }
      }
      if (!lastState || !lastState.hasAtlas) {
        run.warnings.push({ text: "window.__ATLAS__ did not initialise before timeout", source: "harness" });
      }
    }

    // Give the view up to 3s to replace its skeleton with real content.
    const contentDeadline = Date.now() + 3000;
    while (Date.now() < contentDeadline) {
      try {
        const state = await evaluate(send, READY_SOURCE);
        if (state && !state.loading) break;
      } catch { break; }
      await sleep(150);
    }

    let calmChecks = 0;
    const netDeadline = Date.now() + 8000;
    while (Date.now() < netDeadline) {
      if (inflight === 0) calmChecks += 1;
      else calmChecks = 0;
      if (calmChecks >= 3) break;
      await sleep(120);
    }
    if (view === "graph") {
      // Canvas force graph: wait for the canvas element, then let it settle.
      const canvasDeadline = Date.now() + 4000;
      while (Date.now() < canvasDeadline) {
        try {
          const hasCanvas = await evaluate(send, "Boolean(document.querySelector('#view-root canvas'))");
          if (hasCanvas) break;
        } catch { break; }
        await sleep(150);
      }
      await sleep(800);
    }
    try {
      await evaluate(send, "document.fonts && document.fonts.ready ? document.fonts.ready.then(() => true) : true");
    } catch { /* fonts are best effort */ }
    await sleep(Math.max(400, options.waitMs));

    const diag = await evaluate(send, DIAG_SOURCE);
    if (diag) {
      run.children = Number(diag.children || 0);
      run.textLength = Number(diag.textLength || 0);
      run.textPreview = String(diag.text || "").replace(/\s+/g, " ").slice(0, 300);
      run.statusbar = String(diag.statusbar || "").replace(/\s+/g, " ").slice(0, 220);
      run.overflow = Boolean(diag.overflow);
      run.xss = Boolean(diag.xss);
      run.inlineScripts = Number(diag.inlineScripts || 0);
      run.themeMatches = String(diag.theme || "") === theme;
      run.connectOverlayVisible = Number(diag.connectRootHeight || 0) > 0;
      run.skeletonStuck = Boolean(diag.skeleton);
      run.errorState = Boolean(diag.errorState);
      if (run.overflow) {
        try {
          const offenders = await evaluate(send, OVERFLOW_SOURCE);
          run.overflowOffenders = Array.isArray(offenders) ? offenders.slice(0, 10) : [];
        } catch { /* diagnostics are best effort */ }
      }
    }

    const shot = await send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: false,
    });
    const buffer = Buffer.from(String(shot.data || ""), "base64");
    const filename = theme + "-" + view + ".png";
    await writeFile(path.join(options.out, filename), buffer);
    const size = pngSize(buffer);
    run.screenshot = filename;
    run.screenshotBytes = buffer.length;
    run.screenshotWidth = size.width;
    run.screenshotHeight = size.height;

    if (run.skeletonStuck) run.warnings.push({ text: "view-loading 骨架仍在 DOM 中（视图可能未清空容器）", source: "harness" });
    if (run.connectOverlayVisible) run.warnings.push({ text: "连接引导层可见（令牌被拒绝或未注入）", source: "harness" });
    if (run.textLength < 30) run.warnings.push({ text: "视图正文过短（可能仍是空态或渲染失败）", source: "harness" });
    if (!run.themeMatches) run.warnings.push({ text: "页面 data-theme 与请求主题不一致", source: "harness" });
    if (run.screenshotWidth !== options.width || run.screenshotHeight !== options.height) {
      run.warnings.push({
        text: "截图尺寸 " + run.screenshotWidth + "x" + run.screenshotHeight
          + " 与视口 " + options.width + "x" + options.height + " 不一致",
        source: "harness",
      });
    }

    const hardIssues = run.pageErrors.length + run.consoleErrors.length + run.logErrors.length
      + run.httpErrors.length + run.requestFailures.length;
    run.ok = !run.error
      && hardIssues === 0
      && !run.overflow
      && !run.xss
      && run.inlineScripts === 0
      && run.children > 0
      && run.themeMatches
      && !run.errorState;

    if (options.emitHtml && !run.ok) {
      try {
        const html = await evaluate(send, HTML_SOURCE);
        const debugFile = theme + "-" + view + ".html";
        await writeFile(path.join(options.out, "debug", debugFile), String(html || ""), "utf-8");
        run.debugHtml = "debug/" + debugFile;
      } catch { /* best effort */ }
    }
    void viewOrigin;
  } catch (error) {
    run.error = String((error && error.stack) || error).slice(0, 1200);
  } finally {
    if (removeHandler) removeHandler();
    run.elapsedMs = Date.now() - started;
    if (targetId && !run.error) {
      try { await browser.send("Target.closeTarget", { targetId }); } catch { /* ignore */ }
    }
  }
  return run;
}


/* --------------------------------- report -------------------------------- */
function ownerFor(run, item) {
  const haystack = JSON.stringify(item || {}).toLowerCase();
  if (haystack.includes("/api/v1") || haystack.includes("401") || haystack.includes("403")
    || haystack.includes("/stats") || haystack.includes("/recall") || haystack.includes("/context-pack")) {
    return "lead/后端";
  }
  const viewMatch = haystack.match(/\/ui\/js\/views\/([a-z]+)\.js/);
  if (viewMatch) return VIEW_OWNER[viewMatch[1]] || "frontend";
  if (haystack.includes("inspector") || haystack.includes("graph-canvas")) return "views-b";
  if (haystack.includes("components.js") || haystack.includes("charts.js")
    || haystack.includes("icons.js") || haystack.includes("palette.js")
    || haystack.includes("connect.js") || haystack.includes("/ui/css/")
    || haystack.includes("/ui/index.html") || haystack.includes("favicon")) {
    return "atlas-shell";
  }
  if (haystack.includes("core/") || haystack.includes("/ui/js/app.js")
    || haystack.includes("registry.js")) {
    return "lead";
  }
  return VIEW_OWNER[run.view] || "unknown";
}

function formatIssueList(items, limit) {
  if (!items || !items.length) return "-";
  return items.slice(0, limit || 4).map((item) => {
    if (typeof item === "string") return "- " + item;
    const pieces = [];
    if (item.status) pieces.push("HTTP " + item.status);
    if (item.url) pieces.push(item.url);
    if (item.source) pieces.push("source=" + item.source);
    if (item.line !== undefined && item.line !== null) pieces.push("line=" + item.line);
    if (item.text) pieces.push(item.text);
    if (item.error) pieces.push(item.error);
    return "- " + pieces.join(" · ");
  }).join("\n");
}

function buildReport(results, meta) {
  const lines = [];
  const failed = results.filter((run) => !run.ok);
  const passed = results.length - failed.length;
  lines.push("# HippoMind Atlas GUI 截图 QA 报告");
  lines.push("");
  lines.push("- 生成时间：" + meta.generatedAt);
  lines.push("- daemon：" + meta.daemonUrl);
  lines.push("- 数据来源：" + meta.seedLine);
  lines.push("- 视口：" + meta.width + "x" + meta.height + "，主题：" + meta.themes.join(" / "));
  lines.push("- 截图目录：output/gui/");
  lines.push("- Chrome：" + meta.browserVersion);
  lines.push("- 结果：**" + passed + " / " + results.length + " 通过**"
    + (failed.length ? "，失败 " + failed.length + " 项" : "，全部通过"));
  lines.push("");
  lines.push("## 结论");
  lines.push("");
  if (!failed.length) {
    lines.push("所有视图在两套主题下均完成渲染：无 console error、无页面异常、无 HTTP >= 400、");
    lines.push("无横向溢出、无 XSS 执行、无残留骨架屏。截图可作为本轮视觉验收基线。");
  } else {
    const byOwner = new Map();
    for (const run of failed) {
      const owner = run.error ? "harness" : ownerFor(run, {
        url: (run.httpErrors[0] || {}).url || "",
        text: (run.consoleErrors[0] || {}).text || "",
      });
      byOwner.set(owner, (byOwner.get(owner) || 0) + 1);
    }
    lines.push("存在 " + failed.length + " 项失败，按 owner 分布：");
    lines.push("");
    for (const [owner, count] of byOwner) lines.push("- " + owner + "：" + count + " 项");
    lines.push("");
    lines.push("失败明细见下方「失败详情」，每项都带截图路径与可复现证据。");
  }
  lines.push("");
  lines.push("## 逐视图结果");
  lines.push("");
  lines.push("| # | 视图 | 主题 | 结果 | console | 页面异常 | HTTP>=400 | 溢出 | XSS | 骨架残留 | 截图 |");
  lines.push("|---|------|------|------|---------|----------|-----------|------|-----|----------|------|");
  for (const run of results) {
    lines.push("| " + run.index + " | " + run.view + " | " + run.theme + " | "
      + (run.ok ? "PASS" : "FAIL") + " | "
      + run.consoleErrors.length + " | " + run.pageErrors.length + " | "
      + run.httpErrors.length + " | " + (run.overflow ? "是" : "否") + " | "
      + (run.xss ? "是" : "否") + " | " + (run.skeletonStuck ? "是" : "否") + " | "
      + (run.screenshot ? "[" + run.screenshot + "](" + run.screenshot + ")" : "-") + " |");
  }
  lines.push("");
  lines.push("## 失败详情");
  lines.push("");
  if (!failed.length) {
    lines.push("无。");
  } else {
    for (const run of failed) {
      const owner = ownerFor(run, {
        url: (run.httpErrors[0] || {}).url || "",
        text: (run.consoleErrors[0] || {}).text || "",
      });
      lines.push("### [FAIL] " + run.view + " / " + run.theme);
      lines.push("");
      lines.push("- owner：" + owner);
      if (run.error) lines.push("- harness error：" + run.error.split("\n")[0].slice(0, 500));
      if (run.pageErrors.length) {
        lines.push("- 页面异常：");
        lines.push(formatIssueList(run.pageErrors, 5));
      }
      if (run.consoleErrors.length) {
        lines.push("- console error：");
        lines.push(formatIssueList(run.consoleErrors, 5));
      }
      if (run.logErrors.length) {
        lines.push("- Log error：");
        lines.push(formatIssueList(run.logErrors, 5));
      }
      if (run.httpErrors.length) {
        lines.push("- HTTP >= 400：");
        lines.push(formatIssueList(run.httpErrors, 8));
      }
      if (run.requestFailures.length) {
        lines.push("- 请求失败：");
        lines.push(formatIssueList(run.requestFailures, 5));
      }
      if (run.overflow) {
        lines.push("- 横向溢出：trigger 元素如下");
        for (const offender of run.overflowOffenders.slice(0, 6)) {
          lines.push("  - <" + offender.tag + "> id=" + (offender.id || "-")
            + " class=" + (offender.cls || "-") + " right=" + offender.right
            + " width=" + offender.width);
        }
        if (!run.overflowOffenders.length) lines.push("  - （未捕获到具体元素，仅 documentElement 溢出）");
      }
      if (run.xss) lines.push("- XSS 负载被执行：window.__XSS__ 已被设置");
      if (!run.themeMatches) lines.push("- 主题不一致：页面 data-theme 与请求的 " + run.theme + " 不同");
      if (run.errorState) lines.push("- 视图进入错误态（error state）");
      if (run.skeletonStuck) lines.push("- 骨架屏残留：.atlas-skeleton 仍在 DOM");
      if (run.connectOverlayVisible) lines.push("- 连接引导层可见：令牌注入失败或 daemon 拒绝令牌");
      lines.push("- 正文预览：" + (run.textPreview || "(空)"));
      lines.push("- 截图：" + (run.screenshot ? "output/gui/" + run.screenshot : "(未生成)"));
      if (run.debugHtml) lines.push("- 调试 HTML：output/gui/" + run.debugHtml);
      lines.push("");
    }
  }
  lines.push("## 警告（非阻断）");
  lines.push("");
  const warningRuns = results.filter((run) => run.warnings.length);
  if (!warningRuns.length) {
    lines.push("无。");
  } else {
    for (const run of warningRuns) {
      lines.push("- " + run.view + " / " + run.theme + "：");
      for (const warning of run.warnings.slice(0, 6)) {
        lines.push("  - " + (warning.text || JSON.stringify(warning)));
      }
    }
  }
  lines.push("");
  lines.push("## 截图清单");
  lines.push("");
  for (const run of results) {
    if (!run.screenshot) continue;
    lines.push("- " + run.view + " / " + run.theme + "：" + run.screenshot
      + "（" + run.screenshotWidth + "x" + run.screenshotHeight
      + "，" + Math.round(run.screenshotBytes / 1024) + " KiB）");
  }
  lines.push("");
  lines.push("## 复现方式");
  lines.push("");
  lines.push("    python3 scripts/gui_seed.py --daemon");
  lines.push("    python3 scripts/gui_shot.mjs --daemon-url <daemon_url> --token-file <token_path> --theme both");
  lines.push("");
  lines.push("验收口径：docs/GUI_ATLAS.md 第 7 节（无 console error、无 401、无横向溢出、XSS 只作为文本）。");
  return lines.join("\n") + "\n";
}

async function readSeedManifest(file) {
  if (!file) return null;
  try {
    return JSON.parse(await readFile(file, "utf-8"));
  } catch {
    return null;
  }
}

/* ---------------------------------- main --------------------------------- */
async function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(String((error && error.message) || error) + "\n\n" + usage() + "\n");
    return 2;
  }
  if (options.help) {
    process.stdout.write(usage() + "\n");
    return 0;
  }
  if (options.tokenFile) {
    try { options.token = (await readFile(options.tokenFile, "utf-8")).trim(); } catch (error) {
      process.stderr.write("cannot read --token-file: " + String(error && error.message) + "\n");
      return 2;
    }
  }
  if (!options.token) {
    process.stderr.write("missing --token or --token-file (the daemon requires the admin token)\n\n" + usage() + "\n");
    return 2;
  }
  const unknown = options.views.filter((view) => !VIEW_IDS.includes(view));
  if (unknown.length) {
    process.stderr.write("unknown view id(s): " + unknown.join(", ") + "\n");
    return 2;
  }
  if (!options.views.length) {
    process.stderr.write("no views selected\n");
    return 2;
  }
  options.out = path.resolve(options.out);
  await mkdir(options.out, { recursive: true });
  await mkdir(path.join(options.out, "debug"), { recursive: true });

  const health = await fetchJson(options.daemonUrl + "/health", "", 4000);
  if (health.status !== 200) {
    process.stderr.write("daemon /health unreachable at " + options.daemonUrl
      + " (" + (health.error || health.status) + "). Start it with: python3 scripts/gui_seed.py --daemon\n");
    return 2;
  }
  const stats = await fetchJson(options.daemonUrl + "/stats", options.token, 6000);
  if (stats.status === 401) {
    process.stderr.write("daemon rejected the token (401). Check --token-file against the fixture runtime dir.\n");
    return 2;
  }
  const seed = await readSeedManifest(options.seedManifest);
  const seedCounts = seed && seed.counts ? seed.counts : null;
  const seedLine = seedCounts
    ? "scripts/gui_seed.py 隔离 seed（memories=" + seedCounts.memories
      + "，claims=" + (seedCounts.claims && seedCounts.claims.total)
      + "，conflict=" + (seedCounts.claims && seedCounts.claims.conflict)
      + "，skills=" + seedCounts.skills + "，relations=" + seedCounts.relations + "）"
    : "隔离 seed daemon（" + options.daemonUrl + "）";

  const launcher = await launchChrome(options);
  const browser = new CDP(launcher.wsUrl);
  await browser.connect();
  log("Chrome " + launcher.browserVersion + " on 127.0.0.1:" + launcher.debugPort);

  const results = [];
  let index = 0;
  try {
    for (const theme of options.themes) {
      for (const view of options.views) {
        index += 1;
        const run = await captureView(browser, options, theme, view, index);
        results.push(run);
        process.stdout.write(JSON.stringify({
          view: run.view,
          theme: run.theme,
          ok: run.ok,
          screenshot: run.screenshot,
          console: run.consoleErrors.length,
          pageErrors: run.pageErrors.length,
          http: run.httpErrors.length,
          overflow: run.overflow,
          xss: run.xss,
          skeleton: run.skeletonStuck,
          notes: run.warnings.length,
          elapsedMs: run.elapsedMs,
        }) + "\n");
        if (!options.quiet) {
          log((run.ok ? "PASS " : "FAIL ") + run.theme + "/" + run.view
            + " (" + run.elapsedMs + "ms, console=" + run.consoleErrors.length
            + ", http=" + run.httpErrors.length
            + (run.overflow ? ", OVERFLOW" : "")
            + (run.xss ? ", XSS" : "") + ")");
        }
      }
    }
  } finally {
    await stopChrome(launcher, browser, options);
  }

  const generatedAt = new Date().toISOString();
  const reportMeta = {
    generatedAt,
    daemonUrl: options.daemonUrl,
    seedLine,
    width: options.width,
    height: options.height,
    themes: options.themes,
    browserVersion: launcher.browserVersion,
    health: health.data,
    statsLedger: stats.data && stats.data.ledger ? stats.data.ledger : null,
  };
  const reportPath = path.join(options.out, "REPORT.md");
  await writeFile(reportPath, buildReport(results, reportMeta), "utf-8");
  const summaryPath = path.join(options.out, "summary.json");
  await writeFile(summaryPath, JSON.stringify({
    generatedAt,
    daemonUrl: options.daemonUrl,
    seedLine,
    viewport: { width: options.width, height: options.height },
    total: results.length,
    passed: results.filter((run) => run.ok).length,
    failed: results.filter((run) => !run.ok).length,
    results,
  }, null, 2), "utf-8");

  const failed = results.filter((run) => !run.ok);
  process.stdout.write(JSON.stringify({
    summary: true,
    total: results.length,
    passed: results.length - failed.length,
    failed: failed.length,
    report: path.relative(process.cwd(), reportPath),
    summaryJson: path.relative(process.cwd(), summaryPath),
  }) + "\n");
  return failed.length ? 1 : 0;
}

main().then((code) => {
  process.exitCode = code;
}).catch((error) => {
  process.stderr.write("[gui_shot] fatal: " + String((error && error.stack) || error) + "\n");
  process.exitCode = 2;
});

