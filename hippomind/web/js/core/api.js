/* ============================================================================
   HippoMind Atlas — transport
   Same-origin JSON client for the daemon that served this page. The token is
   entered once by the operator and kept in this browser only.
   ========================================================================= */
"use strict";

export const TOKEN_KEY = "hippomind.atlas.token";

export class ApiError extends Error {
  constructor(status, detail, path = "") {
    super(status ? status + " · " + detail : detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.path = path;
  }
}

let token = "";
try { token = localStorage.getItem(TOKEN_KEY) || ""; } catch { token = ""; }

function broadcastUnauthorized(path) {
  window.dispatchEvent(new CustomEvent("hippomind:unauthorized", { detail: { path } }));
}

function buildUrl(path, params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined) continue;
    query.append(key, String(value));
  }
  const suffix = query.toString();
  if (!suffix) return path;
  return path + (path.includes("?") ? "&" : "?") + suffix;
}

async function request(method, path, { body, params, signal, headers } = {}) {
  const options = {
    method,
    headers: Object.assign(
      { "Accept": "application/json" },
      token ? { "X-HippoMind-Token": token } : {},
      method === "POST" ? { "Content-Type": "application/json" } : {},
      headers || {}
    ),
    signal,
  };
  if (body !== undefined) options.body = JSON.stringify(body);
  let response;
  try {
    response = await fetch(buildUrl(path, params), options);
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    throw new ApiError(0, "无法连接 daemon，请确认 hippomindd 正在运行", path);
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
  }
  if (!response.ok) {
    const detail = (data && (data.error || data.detail)) || response.statusText || "请求失败";
    if (response.status === 401) broadcastUnauthorized(path);
    throw new ApiError(response.status, detail, path);
  }
  return data;
}

export const api = {
  getToken() { return token; },
  setToken(value) {
    token = String(value || "").trim();
    try { token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY); } catch { /* storage disabled */ }
    return token;
  },
  clearToken() { this.setToken(""); },
  hasToken() { return Boolean(token); },
  get(path, params, options = {}) { return request("GET", path, Object.assign({}, options, { params })); },
  post(path, body, options = {}) { return request("POST", path, Object.assign({}, options, { body })); },
  async text(path, options = {}) {
    const response = await fetch(path, {
      headers: token ? { "X-HippoMind-Token": token } : {},
      signal: options.signal,
    });
    const body = await response.text();
    if (!response.ok) throw new ApiError(response.status, body || response.statusText, path);
    return body;
  },
  url(path) { return path; },
};

export default api;
