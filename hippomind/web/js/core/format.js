/* ============================================================================
   HippoMind Atlas — formatting
   One place for every number, byte, timestamp and identifier the UI shows.
   ========================================================================= */
"use strict";

const INT = new Intl.NumberFormat("en-US");

export function fmtInt(value) {
  const n = Number(value);
  return Number.isFinite(n) ? INT.format(Math.trunc(n)) : "—";
}

export function fmtCompact(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e8) return (n / 1e8).toFixed(abs >= 1e9 ? 0 : 1).replace(/\.0$/, "") + "亿";
  if (abs >= 1e4) return (n / 1e4).toFixed(abs >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "万";
  return INT.format(Math.trunc(n));
}

export function fmtBytes(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  let current = n;
  while (current >= 1024 && index < units.length - 1) { current /= 1024; index += 1; }
  const digits = index === 0 ? 0 : current >= 100 ? 0 : 1;
  return current.toFixed(digits) + " " + units[index];
}

export function fmtPct(fraction, digits = 1) {
  const n = Number(fraction);
  if (!Number.isFinite(n)) return "—";
  return (n * 100).toFixed(digits).replace(/\.0$/, "") + "%";
}

export function fmtMs(milliseconds) {
  const n = Number(milliseconds);
  if (!Number.isFinite(n)) return "—";
  if (n < 1) return n.toFixed(2) + " ms";
  if (n < 1000) return n.toFixed(1) + " ms";
  return (n / 1000).toFixed(2) + " s";
}

export function parseDate(iso) {
  if (!iso) return null;
  const value = Date.parse(iso);
  return Number.isNaN(value) ? null : new Date(value);
}

function pad(value) { return String(value).padStart(2, "0"); }

export function absTime(iso, options = {}) {
  const date = parseDate(iso);
  if (!date) return "—";
  const y = date.getFullYear();
  const m = pad(date.getMonth() + 1);
  const d = pad(date.getDate());
  const hh = pad(date.getHours());
  const mm = pad(date.getMinutes());
  const ss = pad(date.getSeconds());
  return y + "-" + m + "-" + d + " " + hh + ":" + mm + (options.seconds ? ":" + ss : "");
}

export function relTime(iso) {
  const date = parseDate(iso);
  if (!date) return "—";
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 45) return "刚刚";
  if (seconds < 3600) return Math.floor(seconds / 60) + " 分钟前";
  if (seconds < 86400) return Math.floor(seconds / 3600) + " 小时前";
  if (seconds < 86400 * 30) return Math.floor(seconds / 86400) + " 天前";
  if (seconds < 86400 * 365) return Math.floor(seconds / (86400 * 30)) + " 个月前";
  return Math.floor(seconds / (86400 * 365)) + " 年前";
}

export function shortId(id, head = 8, tail = 4) {
  const text = String(id ?? "");
  if (text.length <= head + tail + 1) return text;
  return text.slice(0, head) + "…" + (tail > 0 ? text.slice(-tail) : "");
}

export function truncate(text, max = 160) {
  const value = String(text ?? "");
  if (value.length <= max) return value;
  return value.slice(0, Math.max(0, max - 1)).trimEnd() + "…";
}

export function titleCase(text) {
  return String(text ?? "").replace(/[A-Za-z]+/g, (word) => word[0].toUpperCase() + word.slice(1));
}

export function duration(seconds) {
  const n = Math.max(0, Number(seconds) || 0);
  const days = Math.floor(n / 86400);
  const hours = Math.floor((n % 86400) / 3600);
  const minutes = Math.floor((n % 3600) / 60);
  const secs = Math.floor(n % 60);
  if (days > 0) return days + " 天 " + (hours > 0 ? hours + " 小时" : "");
  if (hours > 0) return hours + " 小时 " + (minutes > 0 ? minutes + " 分" : "");
  if (minutes > 0) return minutes + " 分 " + (secs > 0 ? secs + " 秒" : "");
  return secs + " 秒";
}

/* kind/state labels stay in data-speak; the UI can localise tiers only. */
export function humanize(value) {
  return String(value ?? "").replace(/[_-]+/g, " ").trim();
}
