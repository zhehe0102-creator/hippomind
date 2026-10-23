/* ============================================================================
   HippoMind Atlas — inline icon set
   One 24x24 grid, 1.5px stroke, currentColor. Hand-drawn paths only: no icon
   font, no external sprite, no emoji. icon() builds real SVG nodes.
   ========================================================================= */
"use strict";

import { svg } from "./core/dom.js";

const GLYPHS = {
  pulse: [
    ["path", { d: "M3 12h4l2.2-5.4 3.2 10.8 2.3-7 1.3 1.6H21" }],
  ],
  archive: [
    ["rect", { x: 3, y: 4, width: 18, height: 4, rx: 0.5 }],
    ["path", { d: "M5 8v11.5h14V8" }],
    ["path", { d: "M10 12.5h4" }],
  ],
  recall: [
    ["circle", { cx: 10.5, cy: 10.5, r: 6 }],
    ["path", { d: "M15 15l5.5 5.5" }],
    ["path", { d: "M8 9h5M8 12h3.5" }],
  ],
  compose: [
    ["path", { d: "M5 3.5h8.5L19 9v11.5H5z" }],
    ["path", { d: "M13.5 3.5V9H19" }],
    ["path", { d: "M9 13h6M12 10v6" }],
  ],
  graph: [
    ["circle", { cx: 6, cy: 6, r: 2.3 }],
    ["circle", { cx: 18, cy: 7, r: 2.3 }],
    ["circle", { cx: 12, cy: 18, r: 2.3 }],
    ["path", { d: "M8.2 6.6l7.6.3" }],
    ["path", { d: "M7.4 8.1L11 15.8" }],
    ["path", { d: "M16.8 9.2L13 15.9" }],
  ],
  facts: [
    ["path", { d: "M5 6h14M5 12h8.5M5 18h6" }],
    ["path", { d: "M15.5 16.5l1.8 1.8 3.2-3.6" }],
  ],
  timeline: [
    ["path", { d: "M4 20h16" }],
    ["path", { d: "M7 20V11M12 20V6M17 20v-6" }],
  ],
  agents: [
    ["circle", { cx: 12, cy: 7.5, r: 3.2 }],
    ["path", { d: "M5 20c0-3.6 3.1-5.8 7-5.8s7 2.2 7 5.8" }],
    ["path", { d: "M4 5.5v3M20 5.5v3" }],
  ],
  health: [
    ["path", { d: "M12 3.2l7.2 2.8v5.4c0 4.5-3 7.6-7.2 8.6-4.2-1-7.2-4.1-7.2-8.6V6z" }],
    ["path", { d: "M8.6 12.2l2.2 2.2 4.4-4.6" }],
  ],
  reference: [
    ["path", { d: "M4 5.6A1.6 1.6 0 015.6 4H10v16H5.6A1.6 1.6 0 014 18.4z" }],
    ["path", { d: "M20 5.6A1.6 1.6 0 0018.4 4H14v16h4.4A1.6 1.6 0 0020 18.4z" }],
  ],
  search: [
    ["circle", { cx: 11, cy: 11, r: 6 }],
    ["path", { d: "M15.4 15.4L21 21" }],
  ],
  refresh: [
    ["path", { d: "M4.5 12a7.5 7.5 0 0112.9-5.2L20 9" }],
    ["path", { d: "M20 4.5V9h-4.5" }],
    ["path", { d: "M19.5 12a7.5 7.5 0 01-12.9 5.2L4 15" }],
    ["path", { d: "M4 19.5V15h4.5" }],
  ],
  theme: [
    ["circle", { cx: 12, cy: 12, r: 4 }],
    ["path", { d: "M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2" }],
    ["path", { d: "M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6M6.9 17.1l-1.6 1.6" }],
  ],
  copy: [
    ["rect", { x: 9, y: 9, width: 11, height: 11, rx: 1.5 }],
    ["path", { d: "M5 15H4.2A1.2 1.2 0 013 13.8V4.2A1.2 1.2 0 014.2 3h9.6A1.2 1.2 0 0115 4.2V5" }],
  ],
  check: [
    ["path", { d: "M5 12.5l4.6 4.6L19 6.8" }],
  ],
  close: [
    ["path", { d: "M6 6l12 12M18 6L6 18" }],
  ],
  "chevron-left": [
    ["path", { d: "M14.5 5.5L8 12l6.5 6.5" }],
  ],
  "chevron-right": [
    ["path", { d: "M9.5 5.5L16 12l-6.5 6.5" }],
  ],
  "arrow-up": [
    ["path", { d: "M12 19V5M5.5 11.5L12 5l6.5 6.5" }],
  ],
  "arrow-down": [
    ["path", { d: "M12 5v14M5.5 12.5L12 19l6.5-6.5" }],
  ],
  pin: [
    ["path", { d: "M8.5 3.5h7l-.9 5.7 3.4 3.3v1.7H6v-1.7l3.4-3.3z" }],
    ["path", { d: "M12 14.2V21" }],
  ],
  "feedback-up": [
    ["path", { d: "M7.5 10.2V20H4.6v-9.8z" }],
    ["path", { d: "M7.5 10.2l3.9-6.4a1.9 1.9 0 012.9 2.3l-1.2 3.2h5a1.9 1.9 0 011.9 2.3l-1.2 5.8a1.9 1.9 0 01-1.9 1.5H7.5" }],
  ],
  "feedback-down": [
    ["path", { d: "M7.5 13.8V4H4.6v9.8z" }],
    ["path", { d: "M7.5 13.8l3.9 6.4a1.9 1.9 0 002.9-2.3l-1.2-3.2h5a1.9 1.9 0 001.9-2.3l-1.2-5.8a1.9 1.9 0 00-1.9-1.5H7.5" }],
  ],
  external: [
    ["path", { d: "M14 4h6v6" }],
    ["path", { d: "M20 4l-8.5 8.5" }],
    ["path", { d: "M18 14.5V19a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h4.5" }],
  ],
  filter: [
    ["path", { d: "M3.5 5h17l-6.5 7.5V20l-4-2v-5.5z" }],
  ],
  plus: [
    ["path", { d: "M12 5v14M5 12h14" }],
  ],
  minus: [
    ["path", { d: "M5 12h14" }],
  ],
  warning: [
    ["path", { d: "M12 3.8l8.6 16.4H3.4z" }],
    ["path", { d: "M12 9.5v4.2" }],
    ["path", { d: "M12 17.2v.01" }],
  ],
  error: [
    ["circle", { cx: 12, cy: 12, r: 8.6 }],
    ["path", { d: "M9 9l6 6M15 9l-6 6" }],
  ],
  info: [
    ["circle", { cx: 12, cy: 12, r: 8.6 }],
    ["path", { d: "M12 10.8V17" }],
    ["path", { d: "M12 7.4v.01" }],
  ],
  menu: [
    ["path", { d: "M4 7h16M4 12h16M4 17h16" }],
  ],
};

const FALLBACK = GLYPHS.info;

/**
 * icon(name, {size=16, title}) -> SVGElement
 * A title turns the icon into a labelled image; otherwise it is decorative.
 */
export function icon(name, options = {}) {
  const size = Number(options.size) || 16;
  const title = options.title ? String(options.title) : "";
  const key = Object.prototype.hasOwnProperty.call(GLYPHS, name) ? name : "info";
  const node = svg("svg", {
    class: "icon icon--" + key,
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.5,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    role: title ? "img" : null,
    "aria-hidden": title ? null : "true",
    focusable: "false",
  });
  if (title) node.appendChild(svg("title", null, title));
  for (const glyph of (GLYPHS[key] || FALLBACK)) {
    node.appendChild(svg(glyph[0], glyph[1]));
  }
  return node;
}

export const ICON_NAMES = Object.freeze(Object.keys(GLYPHS));
export default icon;
