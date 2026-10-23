/* ============================================================================
   HippoMind Atlas — tiny observable store
   Persisted keys live under one settings blob; runtime keys (status, tick) do
   not touch localStorage.
   ========================================================================= */
"use strict";

const SETTINGS_KEY = "hippomind.atlas.settings";
const DEFAULTS = Object.freeze({
  theme: "paper",
  refreshMs: 30000,
  pageSize: 50,
  sidebar: true,
});

function readSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch { return {}; }
}

function writeSettings(settings) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch { /* storage disabled */ }
}

const state = {
  settings: Object.assign({}, DEFAULTS, readSettings()),
  status: { online: false, checkedAt: 0, latencyMs: null, health: null, meta: null, error: null },
  tick: 0,
  view: "pulse",
};

const listeners = new Set();

function emit() {
  for (const listener of listeners) {
    try { listener(state); } catch (error) { console.error("[atlas] store listener failed", error); }
  }
}

export const store = {
  DEFAULTS,
  get(key) { return state[key]; },
  settings() { return state.settings; },
  setSetting(key, value) {
    state.settings = Object.assign({}, state.settings, { [key]: value });
    writeSettings(state.settings);
    emit();
  },
  patch(partial) {
    Object.assign(state, partial);
    emit();
  },
  subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  snapshot() { return state; },
};
export default store;
