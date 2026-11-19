/* ============================================================================
   HippoMind Atlas — entity graph canvas
   A deliberately small force-directed renderer: no library, no animation loop
   left running. Layout is solved in bounded synchronous ticks, then drawn only
   on interaction or resize. Nodes are claim subjects; edges are memory
   relations. Everything is text/colour from the current theme tokens.
   ========================================================================= */
"use strict";

import { el } from "../core/dom.js";

function readTheme() {
  const style = getComputedStyle(document.documentElement);
  const pick = (name, fallback) => (style.getPropertyValue(name) || fallback).trim();
  return {
    paper: pick("--surface", "#FBF8F0"),
    ink: pick("--ink", "#1A1916"),
    ink3: pick("--ink-3", "#7C7466"),
    rule: pick("--rule", "#D9D1BF"),
    accent: pick("--accent", "#C4471E"),
    link: pick("--link", "#1E43B2"),
    ok: pick("--ok", "#2E6A4D"),
    warn: pick("--warn", "#9A640F"),
    danger: pick("--danger", "#9E2626"),
    ink2: pick("--ink-2", "#4D483D"),
  };
}

/* Exported so tests can drive the two tier functions directly instead of
   pattern-matching their source.  A regex on `return [7, 3]` proves the text
   exists; calling relationDash("same_task") proves it returns a dash. */
export function relationTone(type, colors) {
  const value = String(type || "");
  if (value.indexOf("contradict") >= 0 || value.indexOf("supersed") >= 0) return colors.danger;
  if (value.indexOf("cause") >= 0) return colors.link;
  if (value.indexOf("support") >= 0 || value.indexOf("confirm") >= 0) return colors.ok;
  /* Structural tiers added with the relation vocabulary (2026-10-25).
     same_subject(0.9) is "about the same thing"; same_task(0.7) is the
     weakest link in the graph.  Both used to fall through to ink3, which
     drew three different evidence strengths as one colour. */
  if (value.indexOf("same_subject") >= 0) return colors.ink2;
  return colors.ink3;
}

/* Dash pattern per relation tier.  Empty array = solid, which keeps every
   pre-existing relation drawn exactly as before.  same_subject(0.9) and
   same_task(0.7) are two different evidence strengths that used to share
   one colour; they are now separable without relying on hue. */
export function relationDash(type) {
  const value = String(type || "");
  if (value.indexOf("same_subject") >= 0) return [7, 3];
  if (value.indexOf("same_task") >= 0) return [2, 3];
  return [];
}

/* Weight each relation carries in associative expansion, mirrored from
   ledger/graph.py RELATION_WEIGHTS.  Shown in the legend so the reader
   learns how much the relation actually counts, not just its name. */
export const RELATION_WEIGHT_HINTS = [
  { type: "same_subject", weight: 0.9, label: "同主语" },
  { type: "same_task", weight: 0.7, label: "同任务" },
  { type: "related_to", weight: 1.0, label: "其他关系" },
];

export function createGraphCanvas(options = {}) {
  const host = el("div", { class: "vb-graph-canvas" });
  const canvas = el("canvas", {
    class: "vb-graph-canvas-el",
    tabindex: "0",
    role: "img",
    "aria-label": options.label || "实体关系图",
  });
  const tooltip = el("div", { class: "vb-graph-tip", hidden: true });
  host.appendChild(canvas);
  host.appendChild(tooltip);
  const ctx = canvas.getContext("2d");
  const onSelect = typeof options.onSelect === "function" ? options.onSelect : () => {};
  const onHover = typeof options.onHover === "function" ? options.onHover : () => {};

  let nodes = [];
  let edges = [];
  let byName = new Map();
  let neighbours = new Map();
  let width = 0;
  let height = 0;
  let dpr = 1;
  let transform = { x: 0, y: 0, k: 1 };
  let selected = null;
  let hovered = null;
  let dragging = null;
  let moved = false;
  let destroyed = false;

  function resize() {
    const box = host.getBoundingClientRect();
    width = Math.max(240, Math.round(box.width || host.clientWidth || 640));
    height = Math.max(320, Math.round(box.height || host.clientHeight || 520));
    dpr = Math.min(2.5, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    draw();
  }

  function layout() {
    const cx = width / 2;
    const cy = height / 2;
    const count = Math.max(1, nodes.length);
    const radius = Math.min(width, height) * 0.34;
    nodes.forEach((node, index) => {
      const angle = (index / count) * Math.PI * 2 + (index % 3) * 0.13;
      node.x = cx + Math.cos(angle) * radius * (0.7 + ((index * 37) % 30) / 100);
      node.y = cy + Math.sin(angle) * radius * (0.7 + ((index * 53) % 30) / 100);
      node.vx = 0;
      node.vy = 0;
      node.radius = Math.max(4.5, Math.min(17, 4.5 + Math.sqrt(Number(node.claims) || 1) * 2.2));
    });
    const edgePairs = edges.map((edge) => {
      const from = byName.get(edge.from) || byName.get(String(edge.from).toLowerCase());
      const to = byName.get(edge.to) || byName.get(String(edge.to).toLowerCase());
      return from && to ? { from, to, confidence: Number(edge.confidence) || 0.5, type: edge.relation_type } : null;
    }).filter(Boolean);
    for (let step = 0; step < 240; step += 1) {
      for (let i = 0; i < nodes.length; i += 1) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j += 1) {
          const b = nodes[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let distance = Math.sqrt(dx * dx + dy * dy) || 0.01;
          if (distance < 0.1) { dx = 0.1; distance = 0.1; }
          const force = Math.min(7, 2600 / (distance * distance));
          const ux = dx / distance;
          const uy = dy / distance;
          a.vx -= ux * force;
          a.vy -= uy * force;
          b.vx += ux * force;
          b.vy += uy * force;
        }
      }
      for (const edge of edgePairs) {
        const dx = edge.to.x - edge.from.x;
        const dy = edge.to.y - edge.from.y;
        const distance = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const target = 92 + (1 - edge.confidence) * 50;
        const force = (distance - target) * 0.018;
        const ux = dx / distance;
        const uy = dy / distance;
        edge.from.vx += ux * force;
        edge.from.vy += uy * force;
        edge.to.vx -= ux * force;
        edge.to.vy -= uy * force;
      }
      for (const node of nodes) {
        node.vx += (cx - node.x) * 0.0035;
        node.vy += (cy - node.y) * 0.0035;
        node.vx *= 0.82;
        node.vy *= 0.82;
        const speed = Math.sqrt(node.vx * node.vx + node.vy * node.vy);
        if (speed > 9) { node.vx = (node.vx / speed) * 9; node.vy = (node.vy / speed) * 9; }
        node.x += node.vx;
        node.y += node.vy;
      }
    }
    neighbours = new Map();
    for (const node of nodes) neighbours.set(node.subject, new Set());
    for (const edge of edgePairs) {
      neighbours.get(edge.from.subject).add(edge.to.subject);
      neighbours.get(edge.to.subject).add(edge.from.subject);
    }
    fit();
  }

  function fit() {
    if (!nodes.length) { transform = { x: 0, y: 0, k: 1 }; return; }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const node of nodes) {
      minX = Math.min(minX, node.x - node.radius);
      maxX = Math.max(maxX, node.x + node.radius);
      minY = Math.min(minY, node.y - node.radius);
      maxY = Math.max(maxY, node.y + node.radius);
    }
    const pad = 44;
    const k = Math.max(0.35, Math.min(1.7,
      Math.min((width - pad * 2) / Math.max(1, maxX - minX), (height - pad * 2) / Math.max(1, maxY - minY))));
    transform = {
      k,
      x: width / 2 - ((minX + maxX) / 2) * k,
      y: height / 2 - ((minY + maxY) / 2) * k,
    };
    draw();
  }

  function screenToWorld(clientX, clientY) {
    const box = canvas.getBoundingClientRect();
    return {
      x: (clientX - box.left - transform.x) / transform.k,
      y: (clientY - box.top - transform.y) / transform.k,
    };
  }

  function nodeAt(clientX, clientY) {
    const point = screenToWorld(clientX, clientY);
    let best = null;
    let bestDistance = Infinity;
    for (const node of nodes) {
      const dx = node.x - point.x;
      const dy = node.y - point.y;
      const distance = Math.sqrt(dx * dx + dy * dy);
      const hit = Math.max(node.radius + 5, 10 / transform.k);
      if (distance <= hit && distance < bestDistance) { best = node; bestDistance = distance; }
    }
    return best;
  }

  function draw() {
    if (destroyed || !ctx || width === 0) return;
    const colors = readTheme();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.save();
    ctx.translate(transform.x, transform.y);
    ctx.scale(transform.k, transform.k);

    for (const edge of edges) {
      const from = byName.get(edge.from) || byName.get(String(edge.from).toLowerCase());
      const to = byName.get(edge.to) || byName.get(String(edge.to).toLowerCase());
      if (!from || !to) continue;
      const active = !hovered || hovered === from.subject || hovered === to.subject ||
        (neighbours.get(hovered) || new Set()).has(from.subject) ||
        (neighbours.get(hovered) || new Set()).has(to.subject);
      ctx.strokeStyle = relationTone(edge.relation_type, colors);
      ctx.globalAlpha = active ? 0.28 + (Number(edge.confidence) || 0.5) * 0.42 : 0.08;
      ctx.lineWidth = (0.6 + (Number(edge.confidence) || 0.5) * 1.2) / transform.k;
      /* Dashes are in device space, so they must scale with the viewport
         transform or a zoomed-in graph would show one enormous dash. */
      const dash = relationDash(edge.relation_type);
      ctx.setLineDash(dash.length
        ? dash.map((step) => step / transform.k)
        : []);
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(to.x, to.y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.globalAlpha = 1;

    const labelled = new Set();
    const byClaims = nodes.slice().sort((a, b) => (Number(b.claims) || 0) - (Number(a.claims) || 0));
    for (const node of byClaims.slice(0, 28)) labelled.add(node.subject);
    if (hovered) labelled.add(hovered);
    if (selected) labelled.add(selected);

    for (const node of nodes) {
      const isSelected = selected === node.subject;
      const isHovered = hovered === node.subject;
      const conflict = Number(node.conflicts) > 0;
      ctx.beginPath();
      ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
      ctx.fillStyle = isSelected ? colors.accent : (isHovered ? colors.ink : colors.paper);
      ctx.fill();
      ctx.lineWidth = (conflict ? 2 : 1.2) / transform.k;
      ctx.strokeStyle = conflict ? colors.accent : (isSelected ? colors.accent : colors.ink);
      ctx.stroke();
      if (labelled.has(node.subject) || isHovered) {
        const label = String(node.subject || "");
        ctx.font = (isSelected || isHovered ? "600 " : "") + (11 / transform.k) + "px 'Songti SC', Georgia, serif";
        ctx.fillStyle = isSelected ? colors.accent : colors.ink;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        const text = label.length > 22 ? label.slice(0, 21) + "…" : label;
        ctx.fillText(text, node.x, node.y + node.radius + 4 / transform.k);
      }
    }
    ctx.restore();
  }

  function showTooltip(node, clientX, clientY) {
    if (!node) { tooltip.hidden = true; return; }
    const box = host.getBoundingClientRect();
    tooltip.hidden = false;
    tooltip.textContent = node.subject + " · " + node.claims + " claims · " +
      node.memories + " 记忆" + (Number(node.conflicts) ? " · " + node.conflicts + " 冲突" : "");
    const x = Math.min(box.width - 12, Math.max(12, clientX - box.left));
    const y = Math.min(box.height - 12, Math.max(12, clientY - box.top));
    tooltip.style.left = x + "px";
    tooltip.style.top = y + "px";
  }

  function setSelected(subject) {
    selected = subject || null;
    draw();
  }

  function setData(payload) {
    nodes = (payload && Array.isArray(payload.nodes) ? payload.nodes : []).map((node) => Object.assign({}, node));
    edges = (payload && Array.isArray(payload.edges) ? payload.edges : []).map((edge) => Object.assign({}, edge));
    byName = new Map();
    for (const node of nodes) {
      byName.set(node.subject, node);
      byName.set(String(node.subject).toLowerCase(), node);
    }
    selected = null;
    hovered = null;
    resize();
    if (nodes.length) layout();
    draw();
  }

  function pointerDown(event) {
    if (event.button !== 0) return;
    dragging = { x: event.clientX, y: event.clientY, tx: transform.x, ty: transform.y };
    moved = false;
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("is-dragging");
  }

  function pointerMove(event) {
    if (dragging) {
      const dx = event.clientX - dragging.x;
      const dy = event.clientY - dragging.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      transform.x = dragging.tx + dx;
      transform.y = dragging.ty + dy;
      draw();
      return;
    }
    const node = nodeAt(event.clientX, event.clientY);
    const subject = node ? node.subject : null;
    if (subject !== hovered) {
      hovered = subject;
      onHover(node);
      showTooltip(node, event.clientX, event.clientY);
      draw();
    } else if (node) {
      showTooltip(node, event.clientX, event.clientY);
    }
    canvas.style.cursor = node ? "pointer" : "grab";
  }

  function pointerUp(event) {
    if (!dragging) return;
    const wasMoved = moved;
    dragging = null;
    canvas.classList.remove("is-dragging");
    try { canvas.releasePointerCapture(event.pointerId); } catch (error) { /* already released */ }
    if (wasMoved) return;
    const node = nodeAt(event.clientX, event.clientY);
    selected = node ? node.subject : null;
    onSelect(node);
    draw();
  }

  function wheel(event) {
    event.preventDefault();
    const box = canvas.getBoundingClientRect();
    const point = screenToWorld(event.clientX, event.clientY);
    const factor = event.deltaY < 0 ? 1.12 : 0.89;
    const next = Math.max(0.25, Math.min(4, transform.k * factor));
    transform.k = next;
    transform.x = event.clientX - box.left - point.x * next;
    transform.y = event.clientY - box.top - point.y * next;
    draw();
  }

  canvas.addEventListener("pointerdown", pointerDown);
  canvas.addEventListener("pointermove", pointerMove);
  canvas.addEventListener("pointerup", pointerUp);
  canvas.addEventListener("pointerleave", () => { hovered = null; showTooltip(null); draw(); });
  canvas.addEventListener("wheel", wheel, { passive: false });
  canvas.addEventListener("dblclick", (event) => { event.preventDefault(); fit(); });
  canvas.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { selected = null; onSelect(null); draw(); }
    if (event.key === "0") { event.preventDefault(); fit(); }
  });

  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(() => resize()) : null;
  if (observer) observer.observe(host);
  const themeObserver = typeof MutationObserver === "function"
    ? new MutationObserver(() => draw()) : null;
  if (themeObserver) themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  return {
    element: host,
    setData,
    setSelected,
    fit,
    zoom(factor) {
      transform.k = Math.max(0.25, Math.min(4, transform.k * factor));
      draw();
    },
    destroy() {
      destroyed = true;
      if (observer) observer.disconnect();
      if (themeObserver) themeObserver.disconnect();
    },
  };
}
