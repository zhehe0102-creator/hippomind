/* ============================================================================
   HippoMind Atlas — 09 health 体检
   Ledger verdict, per-check severity, storage meters, on-demand integrity
   quick_check and the copyable maintenance command sheet.
   Data: GET /api/v1/health[?integrity=1]
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import { fmtInt, fmtBytes, relTime, absTime, duration, fmtMs } from "../core/format.js";
import {
  badge, card, metric, meter, statRow, emptyState, errorState, skeleton,
  sectionTitle, copyButton, rule,
} from "../components.js";
import { icon } from "../icons.js";

export const meta = {
  id: "health",
  index: "09",
  title: "体检",
  eyebrow: "HEALTH",
  subtitle: "账本完整性、索引同步、容量与可执行的维护建议",
};

const WAL_WARN_BYTES = 64 * 1024 * 1024;
const COMMANDS = [
  { command: "hippomind checkup", label: "深度体检", readOnly: true,
    description: "完整性 / FTS 同步 / 游标 / claims 一致性。默认只读，--repair 才会修复。" },
  { command: "hippomind maintain", label: "维护", readOnly: false,
    description: "执行 raw 保留期、容量与压缩归档；可能写归档并 VACUUM。" },
  { command: "hippomind doctor", label: "服务检查", readOnly: true,
    description: "以不泄露凭据的方式检查 daemon 是否可用。" },
  { command: "hippomind backup ~/hippomind-backup.db", label: "备份", readOnly: false,
    description: "创建 SQLite 原子备份；不改动在线账本。" },
];

let current = { root: null, painted: false, integrity: null, running: false };

function severityTone(severity) {
  if (severity === "error") return "danger";
  if (severity === "warn") return "warn";
  return "ok";
}

function severityLabel(severity) {
  if (severity === "error") return "错误";
  if (severity === "warn") return "警告";
  return "通过";
}

function dot(tone) {
  return el("span", { class: "va-dot tone-" + tone });
}

function fetchHealth(context, withIntegrity) {
  const params = withIntegrity ? { integrity: "1" } : null;
  return api.get("/api/v1/health", params, { signal: context.signal });
}

/* ---------------------------------------------------------------- verdict -- */

function verdictSection(data) {
  const ready = Boolean(data.ready);
  const errors = Array.isArray(data.errors) ? data.errors : [];
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const checks = Array.isArray(data.checks) ? data.checks : [];
  const daemon = data.daemon || {};
  const okCount = checks.filter((check) => check.severity === "ok").length;
  const warnCount = checks.filter((check) => check.severity === "warn").length;
  const errorCount = checks.filter((check) => check.severity === "error").length;
  const tone = ready ? "ok" : "danger";

  const main = el("div", { class: "va-verdict-main" },
    el("div", { class: "eyebrow" }, "LEDGER VERDICT"),
    el("div", { class: "va-verdict-word" }, ready ? "READY" : "NEEDS ATTENTION"),
    el("div", { class: "va-verdict-meta" },
      el("span", {}, "生成于 " + relTime(data.generated_at)),
      el("span", { class: "muted", title: absTime(data.generated_at, { seconds: true }) }, absTime(data.generated_at, { seconds: true })),
      el("span", {}, "daemon v" + (daemon.version || "?") + " · " + duration(daemon.uptime_s)),
      el("span", {}, "pid " + fmtInt(daemon.pid))));

  const messages = el("div", { class: "va-verdict-messages" });
  for (const detail of errors.slice(0, 3)) {
    messages.appendChild(el("p", { class: "va-verdict-error" }, "错误 · " + detail));
  }
  for (const detail of warnings.slice(0, 3)) {
    messages.appendChild(el("p", { class: "va-verdict-warning" }, "警告 · " + detail));
  }
  if (!errors.length && !warnings.length) {
    messages.appendChild(el("p", { class: "va-verdict-ok" }, "所有检查项通过：账本、索引与容量均无告警。"));
  }
  main.appendChild(messages);

  const side = el("div", { class: "va-verdict-side" },
    metric({ label: "CHECKS", value: fmtInt(checks.length), small: true, hint: "检查项总数", tone }),
    metric({ label: "PASS / WARN / ERROR", value: fmtInt(okCount) + " / " + fmtInt(warnCount) + " / " + fmtInt(errorCount), small: true, hint: "severity 分布" }),
    metric({ label: "DB SIZE", value: fmtBytes((data.storage || {}).db_bytes), small: true, hint: "账本主文件" }));

  return el("section", { class: "va-verdict", dataset: { tone }, "aria-label": "账本体检结论" }, main, side);
}

/* ----------------------------------------------------------------- checks -- */

function checkCard(check) {
  const tone = severityTone(check.severity);
  const node = el("article", { class: "va-check", dataset: { tone } },
    el("div", { class: "va-check-head" },
      dot(tone),
      el("h3", { class: "va-check-title" }, check.label || check.id || "check"),
      badge(severityLabel(check.severity), { tone })),
    el("p", { class: "va-check-detail" }, check.detail || ""));
  if (check.fix && check.fix.command) {
    node.appendChild(el("div", { class: "va-fix" },
      el("span", { class: "va-fix-label" }, check.fix.label || "建议"),
      el("code", { class: "va-fix-command", title: check.fix.command }, check.fix.command),
      copyButton(check.fix.command, { label: "复制" })));
  }
  return node;
}

function checksSection(data) {
  const checks = Array.isArray(data.checks) ? data.checks : [];
  const host = el("div", { class: "va-checks" });
  if (!checks.length) {
    host.appendChild(emptyState({
      plain: true,
      icon: "health",
      title: "没有检查项",
      hint: "daemon 本次返回的检查项为空；请稍后刷新或查看 daemon 日志。",
    }));
  } else {
    for (const check of checks) host.appendChild(checkCard(check));
  }
  return host;
}

/* ---------------------------------------------------------------- storage -- */

function storageCard(data) {
  const storage = data.storage || {};
  const db = Number(storage.db_bytes) || 0;
  const wal = Number(storage.wal_bytes) || 0;
  const reclaim = Number(storage.reclaimable_bytes) || 0;
  const archive = Number(storage.archive_bytes) || 0;
  const total = Math.max(db + wal + archive, 1);
  const walTone = wal > WAL_WARN_BYTES ? "warn" : "link";
  const meters = el("div", { class: "va-meters" },
    meter({ label: "DB · 数据库文件", value: db, max: total, tone: "accent", valueText: fmtBytes(db) }),
    meter({ label: "WAL · 侧车日志", value: wal, max: total, tone: walTone, valueText: fmtBytes(wal) }),
    meter({ label: "可回收 · DB 空闲页", value: reclaim, max: total, tone: "warn", valueText: fmtBytes(reclaim) }),
    meter({ label: "ARCHIVE · 冷归档", value: archive, max: total, tone: "neutral", valueText: fmtBytes(archive) }));
  const stats = statRow([
    { label: "DB 文件大小", value: fmtBytes(db) },
    { label: "WAL 侧车", value: fmtBytes(wal) + (wal > WAL_WARN_BYTES ? " · 超过 64 MiB 阈值" : "") },
    { label: "freelist 页数", value: fmtInt(storage.freelist) },
    { label: "归档文件", value: fmtInt(storage.archive_files) + " 个 · " + fmtBytes(archive) },
    { label: "raw 保留期", value: fmtInt(storage.retention_days) + " 天" },
    { label: "raw 容量上限", value: fmtInt(storage.capacity) + " 条活跃 raw" },
  ]);
  return card({
    eyebrow: "STORAGE",
    title: "存储与容量",
    body: el("div", { class: "va-storage" },
      meters,
      rule(),
      stats,
      el("p", { class: "va-note" },
        "可回收空间位于数据库文件内部，执行 hippomind maintain 的 VACUUM 后才会真正释放；",
        "WAL 由维护周期检查点归零，持续超过 64 MiB 说明检查点被长读阻塞。")),
  });
}

/* ------------------------------------------------------------- projections -- */

function projectionsCard(data) {
  const fts = data.fts || {};
  const claims = data.claims || {};
  const overview = data.overview || {};
  const ftsHint = typeof fts.hint === "string" && fts.hint ? fts.hint : "";
  const stats = statRow([
    { label: "FTS 状态", value: fts.status || "—" },
    { label: "索引行 / 应索引", value: fmtInt(fts.fts_rows) + " / " + fmtInt(fts.projections) },
    { label: "未索引 / 陈旧 / 孤儿", value: fmtInt(fts.unindexed) + " / " + fmtInt(fts.stale) + " / " + fmtInt(fts.orphan) },
    { label: "Claims 状态", value: claims.status || "—" },
    { label: "活跃 / 悬空 claim", value: fmtInt(claims.active_claims) + " / " + fmtInt(claims.dangling) },
    { label: "schema 版本", value: "v" + fmtInt(overview.schema_version) },
    { label: "记忆 / 事件", value: fmtInt(overview.memories) + " / " + fmtInt(overview.events) },
    { label: "隔离 / 置顶", value: fmtInt(overview.quarantined) + " / " + fmtInt(overview.pinned) },
  ]);
  const body = el("div", { class: "va-projections" },
    stats,
    ftsHint ? el("p", { class: "va-note" }, "FTS 提示：" + ftsHint) : null);
  return card({ eyebrow: "PROJECTIONS", title: "索引与投影", body });
}

/* ---------------------------------------------------------------- commands -- */

function commandsCard() {
  const rows = COMMANDS.map((entry) => el("div", { class: "va-command-row" },
    el("div", { class: "va-command-main" },
      el("div", { class: "va-command-title" },
        el("span", { class: "va-command-label" }, entry.label),
        badge(entry.readOnly ? "只读" : "会写盘", { tone: entry.readOnly ? "neutral" : "warn" })),
      el("code", { class: "va-command-name", title: entry.command }, entry.command)),
    el("p", { class: "va-command-desc" }, entry.description),
    copyButton(entry.command, { label: "复制" })));
  return card({
    eyebrow: "COMMAND SHEET",
    title: "维护命令",
    actions: badge("CLI", { tone: "neutral" }),
    body: el("div", { class: "va-command-list" }, ...rows),
  });
}

/* --------------------------------------------------------------- integrity -- */

function integrityBlock(context) {
  const host = el("div", { class: "va-integrity" });
  const bodyHost = el("div", { class: "va-integrity-body" });
  const runLabel = el("span", {}, "运行完整性检查");
  const runButton = el("button", { class: "atlas-btn atlas-btn--primary", type: "button" },
    icon("health", { size: 13 }), runLabel);

  function renderBody() {
    clear(bodyHost);
    if (current.running) {
      bodyHost.appendChild(el("p", { class: "va-note" },
        "正在执行 PRAGMA quick_check，大账本可能需要数秒；该操作只读，不修改账本。"));
      runButton.disabled = true;
      runLabel.textContent = "检查中…";
      return;
    }
    runButton.disabled = false;
    runLabel.textContent = "运行完整性检查";
    if (!current.integrity) {
      bodyHost.appendChild(el("p", { class: "va-note" },
        "按需执行 SQLite 完整性快速检查（PRAGMA quick_check）。它只读、可能耗时，结果包含检查状态与耗时。"));
      return;
    }
    const integrity = current.integrity;
    const ok = integrity.ok === true;
    bodyHost.appendChild(el("div", { class: "va-integrity-result" },
      el("div", { class: "va-integrity-line" },
        el("span", { class: "va-integrity-word " + (ok ? "tone-ok" : "tone-danger") }, ok ? "PASS" : "FAIL"),
        badge(String(integrity.status || "unknown"), { tone: ok ? "ok" : "danger" })),
      el("div", { class: "va-integrity-meta" },
        el("span", {}, "耗时 " + fmtMs(integrity.elapsed_ms)),
        el("span", { class: "muted" }, "ran=" + String(Boolean(integrity.ran))))));
  }

  runButton.addEventListener("click", async () => {
    if (current.running) return;
    current.running = true;
    renderBody();
    try {
      const payload = await api.get("/api/v1/health", { integrity: "1" }, { signal: context.signal });
      if (context.signal.aborted) return;
      current.integrity = payload && payload.integrity ? payload.integrity : null;
      context.toast(current.integrity && current.integrity.ok ? "完整性检查通过" : "完整性检查未通过", {
        tone: current.integrity && current.integrity.ok ? "ok" : "danger",
      });
    } catch (error) {
      if (error && error.name === "AbortError") return;
      context.toast("完整性检查失败：" + String((error && (error.detail || error.message)) || "未知错误"), { tone: "danger" });
    } finally {
      current.running = false;
      renderBody();
    }
  });

  host.appendChild(el("div", { class: "va-integrity-head" },
    el("div", {},
      el("div", { class: "eyebrow" }, "INTEGRITY"),
      el("h3", { class: "va-integrity-title" }, "完整性快速检查")),
    runButton));
  host.appendChild(bodyHost);
  renderBody();
  return host;
}

/* ------------------------------------------------------------------- paint -- */

function paint(root, data, context) {
  clear(root);
  const checks = Array.isArray(data.checks) ? data.checks : [];
  const tone = data.ready ? "ok" : "danger";
  root.appendChild(verdictSection(data));
  root.appendChild(el("div", { class: "va-section-row" },
    sectionTitle("检查项", {
      hint: fmtInt(checks.length) + " 项 · 由 daemon 实时计算",
      actions: badge(data.ready ? "READY" : "NEEDS ATTENTION", { tone }),
    })));
  root.appendChild(checksSection(data));
  root.appendChild(el("div", { class: "atlas-grid atlas-grid--2 va-start-grid" },
    storageCard(data),
    el("div", { class: "va-health-side" },
      projectionsCard(data),
      integrityBlock(context))));
  root.appendChild(commandsCard());
  current.painted = true;
}

/* ------------------------------------------------------------------- entry -- */

export async function render(context) {
  const root = el("div", { class: "va-view va-health" });
  current = { root, painted: false, integrity: null, running: false };
  root.appendChild(skeleton(5));
  try {
    const data = await fetchHealth(context, false);
    if (context.signal.aborted) return root;
    paint(root, data || {}, context);
  } catch (error) {
    if (error && error.name === "AbortError") return root;
    clear(root);
    root.appendChild(errorState({ title: "体检加载失败", error, onRetry: () => context.refresh() }));
  }
  return root;
}

export async function refresh(context) {
  const root = current.root;
  if (!root || !root.isConnected) return;
  try {
    const data = await fetchHealth(context, false);
    if (context.signal.aborted) return;
    paint(root, data || {}, context);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (current.painted) context.toast("体检刷新失败：" + String((error && (error.detail || error.message)) || "未知错误"), { tone: "warn" });
    else {
      clear(root);
      root.appendChild(errorState({ title: "体检加载失败", error, onRetry: () => context.refresh() }));
    }
  }
}

