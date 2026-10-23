/* ============================================================================
   HippoMind Atlas — hand-drawn SVG charts
   No chart library, no canvas, no external fonts: just geometry and the
   semantic palette. Every chart is decorative-friendly but carries an
   accessible label; stacked bars are keyboard reachable bucket by bucket.
   ========================================================================= */
"use strict";

import { el, svg, clear } from "./core/dom.js";
import { fmtInt } from "./core/format.js";

const TONES = ["neutral", "accent", "ok", "warn", "danger", "link"];
const SERIES_TONES = ["accent", "link", "ok", "warn", "danger", "neutral"];

function toneOf(value, fallback = "accent") {
  return TONES.indexOf(value) >= 0 ? value : fallback;
}
function toneClass(value) {
  return "tone-" + toneOf(value, "accent");
}
function numeric(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}
function seriesTone(index) {
  return SERIES_TONES[index % SERIES_TONES.length];
}
function seriesToneOf(series, item, index) {
  if (item && item.tone && TONES.indexOf(item.tone) >= 0) return item.tone;
  return seriesTone(index);
}
function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}
function niceCeil(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const magnitude = Math.pow(10, exponent);
  const fraction = value / magnitude;
  let nice;
  if (fraction <= 1) nice = 1;
  else if (fraction <= 2) nice = 2;
  else if (fraction <= 5) nice = 5;
  else nice = 10;
  return nice * magnitude;
}
function shortNumber(value) {
  const number = Math.max(0, numeric(value));
  if (number >= 10000) return Math.round(number / 1000) + "k";
  if (number >= 1000) return (number / 1000).toFixed(number % 1000 === 0 ? 0 : 1) + "k";
  return String(Math.round(number));
}

/* -------------------------------------------------------------- sparkline -- */

export function sparkline(options = {}) {
  const values = (Array.isArray(options.values) ? options.values : []).map(numeric);
  const width = Math.max(40, Number(options.width) || 220);
  const height = Math.max(20, Number(options.height) || 44);
  const pad = 2;
  const node = svg("svg", {
    class: "chart-spark " + toneClass(toneOf(options.tone, "accent")),
    viewBox: "0 0 " + width + " " + height,
    width,
    height,
    role: "img",
    "aria-label": options.label || "趋势折线",
    preserveAspectRatio: "none",
  });
  node.appendChild(svg("title", null, options.title || options.label || "趋势"));
  node.appendChild(svg("line", {
    class: "chart-baseline",
    x1: pad, y1: height - pad, x2: width - pad, y2: height - pad,
  }));
  if (values.length) {
    const max = Math.max.apply(null, values);
    const min = Math.min.apply(null, values);
    const span = max - min || 1;
    const innerHeight = Math.max(4, height - pad * 2 - 4);
    const points = values.map((value, index) => {
      const x = values.length === 1 ? width / 2 : pad + (index / (values.length - 1)) * (width - pad * 2);
      const y = height - pad - ((value - min) / span) * innerHeight;
      return [x, Math.max(pad, Math.min(height - pad, y))];
    });
    let line = "";
    points.forEach((point, index) => {
      line += (index === 0 ? "M" : "L") + point[0].toFixed(2) + " " + point[1].toFixed(2) + " ";
    });
    if (options.fill !== false) {
      node.appendChild(svg("path", {
        class: "chart-area",
        d: line + "L" + (width - pad).toFixed(2) + " " + (height - pad) + " L" + pad + " " + (height - pad) + " Z",
      }));
    }
    node.appendChild(svg("path", { class: "chart-line", d: line.trim() }));
    const last = points[points.length - 1];
    node.appendChild(svg("circle", { class: "chart-dot", cx: last[0], cy: last[1], r: 1.8 }));
  }
  return node;
}

/* ---------------------------------------------------------------- barList -- */

export function barList(options = {}) {
  const items = (Array.isArray(options.items) ? options.items : []).map((item) => ({
    label: item && item.label !== undefined ? String(item.label) : "",
    value: numeric(item && item.value),
    tone: item && item.tone ? item.tone : options.tone,
    mono: item ? item.mono : options.mono,
  }));
  const total = Number(options.total) > 0
    ? Number(options.total)
    : items.reduce((sum, item) => sum + item.value, 0);
  const maxBars = Math.max(1, Number(options.maxBars) || 10);
  const list = el("div", { class: "chart-barlist" });
  if (!items.length) {
    list.appendChild(el("div", { class: "chart-barlist-empty" }, options.empty || "暂无数据"));
    return list;
  }
  const shown = items.slice(0, maxBars);
  const maxValue = Math.max.apply(null, shown.map((item) => item.value).concat([1]));
  shown.forEach((item, index) => {
    const ratio = clamp(item.value / maxValue, 0, 1);
    const pct = total > 0 ? item.value / total : 0;
    const row = el("div", { class: "chart-bar-row" });
    row.appendChild(el("span", {
      class: "chart-bar-label" + (item.mono || options.mono ? " chart-bar-label--mono" : ""),
      title: item.label,
    }, item.label || "—"));
    const track = el("div", { class: "chart-bar-track" });
    const fill = el("span", { class: "chart-bar-fill " + toneClass(toneOf(item.tone, "accent")) });
    fill.style.width = (ratio * 100).toFixed(2) + "%";
    track.appendChild(fill);
    row.appendChild(track);
    const value = el("span", { class: "chart-bar-value" },
      options.compact ? shortNumber(item.value) : fmtInt(item.value));
    if (options.showPct !== false) {
      value.appendChild(el("span", { class: "chart-bar-pct" },
        (pct * 100).toFixed(pct >= 0.1 || pct === 0 ? 0 : 1) + "%"));
    }
    row.appendChild(value);
    list.appendChild(row);
  });
  if (items.length > shown.length) {
    list.appendChild(el("div", { class: "chart-barlist-empty" },
      "+" + fmtInt(items.length - shown.length) + " 项未显示"));
  }
  return list;
}

/* ------------------------------------------------------------------ donut -- */

export function donut(options = {}) {
  const items = (Array.isArray(options.items) ? options.items : []).map((item) => ({
    label: item && item.label !== undefined ? String(item.label) : "",
    value: numeric(item && item.value),
    tone: toneOf(item && item.tone, "neutral"),
  }));
  const size = Math.max(80, Number(options.size) || 160);
  const total = items.reduce((sum, item) => sum + item.value, 0);
  const wrapper = el("div", {
    class: "chart-donut",
    role: "img",
    "aria-label": options.label || "构成环形图",
    style: { width: size + "px", height: size + "px" },
  });
  const node = svg("svg", {
    viewBox: "0 0 120 120",
    width: size,
    height: size,
    role: "presentation",
    "aria-hidden": "true",
  });
  const radius = 52;
  const circumference = 2 * Math.PI * radius;
  node.appendChild(svg("circle", { class: "chart-donut-track", cx: 60, cy: 60, r: radius }));
  if (total > 0) {
    const gap = Math.min(2.4, circumference / Math.max(4, items.length) * 0.12);
    let offset = 0;
    for (const item of items) {
      const fraction = item.value / total;
      if (fraction <= 0) continue;
      const length = Math.max(0.5, fraction * circumference - gap);
      node.appendChild(svg("circle", {
        class: "chart-donut-seg " + toneClass(item.tone),
        cx: 60, cy: 60, r: radius,
        transform: "rotate(-90 60 60)",
        "stroke-dasharray": length.toFixed(2) + " " + circumference.toFixed(2),
        "stroke-dashoffset": (-offset).toFixed(2),
      }));
      offset += fraction * circumference;
    }
  }
  const centerValue = options.centerValue !== undefined && options.centerValue !== null
    ? String(options.centerValue)
    : fmtInt(total);
  node.appendChild(svg("text", {
    class: "chart-donut-center-value", x: 60, y: options.centerLabel ? 58 : 62,
    "text-anchor": "middle",
  }, centerValue));
  if (options.centerLabel) {
    node.appendChild(svg("text", {
      class: "chart-donut-center-label", x: 60, y: 73, "text-anchor": "middle",
    }, String(options.centerLabel)));
  }
  wrapper.appendChild(node);
  return wrapper;
}

/* ------------------------------------------------------------ stackedBars -- */

export function stackedBars(options = {}) {
  const buckets = Array.isArray(options.buckets) ? options.buckets : [];
  const series = (Array.isArray(options.series) ? options.series : []).map((item, index) => ({
    id: item && item.id !== undefined ? String(item.id) : String(index),
    label: item && item.label !== undefined ? String(item.label) : String(index),
    tone: item && item.tone ? item.tone : seriesTone(index),
  }));
  const width = Math.max(240, Number(options.width) || 900);
  const height = Math.max(120, Number(options.height) || 240);
  const padding = { top: 14, right: 10, bottom: 26, left: 40 };
  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;
  const wrapper = el("div", {
    class: "chart-stacked",
    role: "group",
    "aria-label": options.label || "时间堆叠柱状图",
  });
  const node = svg("svg", {
    viewBox: "0 0 " + width + " " + height,
    width,
    height,
    role: "presentation",
    "aria-hidden": "true",
  });
  wrapper.appendChild(node);

  const totals = buckets.map((bucket) => {
    if (bucket && bucket.total !== undefined && bucket.total !== null) return Math.max(0, numeric(bucket.total));
    return series.reduce((sum, item) => sum + numeric(bucket && bucket.parts ? bucket.parts[item.id] : 0), 0);
  });
  const maxTotal = Math.max.apply(null, totals.concat([1]));
  const scaleMax = niceCeil(maxTotal);
  const gridCount = 4;
  for (let index = 0; index <= gridCount; index += 1) {
    const value = (scaleMax * index) / gridCount;
    const y = padding.top + innerHeight - (value / scaleMax) * innerHeight;
    node.appendChild(svg("line", {
      class: "chart-grid-line",
      x1: padding.left, y1: y, x2: width - padding.right, y2: y,
    }));
    node.appendChild(svg("text", {
      class: "chart-axis-label", x: padding.left - 6, y: y + 3, "text-anchor": "end",
    }, shortNumber(value)));
  }

  const count = Math.max(1, buckets.length);
  const step = innerWidth / count;
  const barWidth = Math.max(2, Math.min(26, step * 0.62));
  const labelEvery = Math.max(1, Math.ceil(count / Math.max(4, Math.floor(width / 110))));
  const tooltip = el("div", { class: "chart-tooltip", "aria-hidden": "true" });

  const selected = options.selected === undefined || options.selected === null
    ? null
    : String(options.selected);

  function tooltipRows(bucket) {
    clear(tooltip);
    tooltip.appendChild(el("div", { class: "chart-tooltip-title" },
      String(bucket.label !== undefined ? bucket.label : (bucket.key || ""))));
    tooltip.appendChild(el("div", { class: "chart-tooltip-total" },
      fmtInt(totals[buckets.indexOf(bucket)]), " 条"));
    series.forEach((item) => {
      const value = numeric(bucket.parts ? bucket.parts[item.id] : 0);
      if (value <= 0) return;
      const swatch = el("span", { class: "chart-tooltip-swatch " + toneClass(item.tone) });
      tooltip.appendChild(el("div", { class: "chart-tooltip-row" },
        el("span", {}, swatch, item.label),
        el("b", {}, fmtInt(value))));
    });
  }

  function positionTooltip(bucket) {
    const rect = wrapper.getBoundingClientRect();
    if (!rect.width) { tooltip.style.left = "0px"; tooltip.style.top = "0px"; return; }
    const scale = rect.width / width;
    const index = buckets.indexOf(bucket);
    let left = (padding.left + step * (index + 0.5)) * scale + 10;
    if (left + 180 > rect.width) left = Math.max(2, left - 200);
    tooltip.style.left = left.toFixed(0) + "px";
    tooltip.style.top = (padding.top * scale).toFixed(0) + "px";
  }

  function show(bucket) {
    tooltipRows(bucket);
    tooltip.classList.add("is-visible");
    positionTooltip(bucket);
  }
  function hide() {
    tooltip.classList.remove("is-visible");
  }

  buckets.forEach((bucket, bucketIndex) => {
    const safeBucket = bucket || {};
    const cx = padding.left + step * (bucketIndex + 0.5);
    const group = svg("g", {
      class: "chart-bucket" + (selected !== null && (selected === String(safeBucket.key) || selected === String(bucketIndex)) ? " is-selected" : ""),
      tabindex: "0",
      role: "button",
      "aria-label": String(safeBucket.label !== undefined ? safeBucket.label : (safeBucket.key || "")) +
        "：" + fmtInt(totals[bucketIndex]) + " 条",
    });
    let bottom = padding.top + innerHeight;
    for (const item of series) {
      const value = numeric(safeBucket.parts ? safeBucket.parts[item.id] : 0);
      if (value <= 0) continue;
      const barHeight = (value / scaleMax) * innerHeight;
      group.appendChild(svg("rect", {
        class: "chart-bar " + toneClass(item.tone),
        x: (cx - barWidth / 2).toFixed(2),
        y: (bottom - barHeight).toFixed(2),
        width: barWidth.toFixed(2),
        height: Math.max(0.6, barHeight).toFixed(2),
      }));
      bottom -= barHeight;
    }
    group.appendChild(svg("rect", {
      class: "chart-hit",
      x: (cx - step / 2 + 1).toFixed(2),
      y: padding.top,
      width: Math.max(1, step - 2).toFixed(2),
      height: innerHeight,
    }));
    if (bucketIndex % labelEvery === 0) {
      group.appendChild(svg("text", {
        class: "chart-axis-label", x: cx.toFixed(2), y: height - 8, "text-anchor": "middle",
      }, String(safeBucket.label !== undefined ? safeBucket.label : (safeBucket.key || ""))));
    }
    if (typeof options.onSelect === "function") {
      group.addEventListener("click", (event) => options.onSelect(safeBucket, bucketIndex, event));
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          options.onSelect(safeBucket, bucketIndex, event);
        }
      });
    }
    group.addEventListener("mouseenter", () => show(safeBucket));
    group.addEventListener("mousemove", () => show(safeBucket));
    group.addEventListener("mouseleave", hide);
    group.addEventListener("focus", () => show(safeBucket));
    group.addEventListener("blur", hide);
    node.appendChild(group);
  });

  wrapper.appendChild(tooltip);
  return wrapper;
}

/* ----------------------------------------------------------------- legend -- */

export function legend(options = {}) {
  const items = Array.isArray(options.items) ? options.items : [];
  const node = el("div", {
    class: "chart-legend",
    role: "group",
    "aria-label": options.label || "图例",
  });
  for (const item of items) {
    if (!item) continue;
    const active = item.active !== false;
    const swatch = el("span", {
      class: "chart-swatch " + toneClass(toneOf(item.tone, "accent")),
    });
    const button = el("button", {
      class: "chart-legend-item",
      type: "button",
      dataset: { active: active ? "true" : "false" },
      "aria-pressed": active ? "true" : "false",
      title: item.label !== undefined ? String(item.label) : String(item.id),
    }, swatch, el("span", { class: "chart-legend-label" },
      item.label !== undefined ? String(item.label) : String(item.id)));
    button.addEventListener("click", () => {
      const next = button.dataset.active !== "true";
      button.dataset.active = next ? "true" : "false";
      button.setAttribute("aria-pressed", next ? "true" : "false");
      if (typeof options.onToggle === "function") options.onToggle(item.id, next);
    });
    node.appendChild(button);
  }
  return node;
}

export default { sparkline, barList, donut, stackedBars, legend };
