/* ============================================================================
   HippoMind Atlas — hash router
   #/view?k=v keeps every screen linkable without a server rewrite rule.
   ========================================================================= */
"use strict";

export function parse(hash = window.location.hash) {
  const raw = String(hash || "").replace(/^#/, "");
  const [pathPart, queryPart] = raw.split("?");
  const path = "/" + pathPart.replace(/^\/+/, "");
  const id = path === "/" ? "" : path.slice(1).split("/")[0];
  return { id, path, params: new URLSearchParams(queryPart || "") };
}

export function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) value.forEach((entry) => search.append(key, String(entry)));
    else search.append(key, String(value));
  }
  const text = search.toString();
  return text ? "?" + text : "";
}

export function href(path, params) {
  const clean = String(path || "/").replace(/^#/, "");
  const [base, existing] = clean.split("?");
  if (existing && !params) return "#" + clean;
  return "#" + base.replace(/^\/+/, "/") + query(params);
}

export function navigate(path, options = {}) {
  const target = String(path || "/").replace(/^#/, "");
  const normalized = "#" + (target.startsWith("/") ? target : "/" + target);
  if (window.location.hash === normalized) return false;
  if (options.replace) {
    const url = window.location.pathname + window.location.search + normalized;
    window.history.replaceState(null, "", url);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else {
    window.location.hash = normalized;
  }
  return true;
}

export function start(handler) {
  const onHash = () => handler(parse());
  window.addEventListener("hashchange", onHash);
  if (!window.location.hash || window.location.hash === "#") {
    navigate("/pulse", { replace: true });
  }
  onHash();
  return () => window.removeEventListener("hashchange", onHash);
}

export const router = { parse, query, href, navigate, start };
export default router;
