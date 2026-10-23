/* ============================================================================
   HippoMind Atlas — connection gate
   The only place a local admin token enters the browser. initConnect() stores
   the success callback; openConnect()/closeConnect() own the overlay.
   Validation: save the pasted token, call /api/v1/meta (falling back to
   /stats while an older daemon is still being upgraded), then hand control
   back to app.js through onConnected.
   ========================================================================= */
"use strict";

import { el, svg } from "./core/dom.js";
import { api } from "./core/api.js";
import { icon } from "./icons.js";
import { toast, copyButton } from "./components.js";

const TOKEN_PATH = "~/Library/Application Support/HippoMind/hippomindd.token";
const TOKEN_COMMAND = "cat \"$HOME/Library/Application Support/HippoMind/hippomindd.token\"";
const ALT_NOTE = "Linux: ~/.local/state/hippomind/hippomindd.token · Windows: %APPDATA%/HippoMind/hippomindd.token";

let onConnectedHandler = null;
let overlay = null;

export function initConnect(options = {}) {
  onConnectedHandler = typeof options.onConnected === "function" ? options.onConnected : null;
}

function brandMark() {
  const node = svg("svg", {
    class: "connect-mark",
    viewBox: "0 0 40 40",
    "aria-hidden": "true",
    focusable: "false",
  });
  const group = svg("g", {
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.5,
    "stroke-linecap": "round",
  });
  group.appendChild(svg("path", { d: "M20 33.5c-8.6 0-14-6.2-14-14S11.4 5.5 20 5.5s14 6.2 14 14" }));
  group.appendChild(svg("path", { d: "M20 28.6c-5 0-8.6-4-8.6-9.1S14.9 10.4 20 10.4s8.6 4 8.6 9.1" }));
  group.appendChild(svg("path", { d: "M20 23.6c-2.4 0-4.1-1.9-4.1-4.2s1.7-4.2 4.1-4.2 4.1 1.9 4.1 4.2" }));
  node.appendChild(group);
  node.appendChild(svg("circle", { class: "brand-dot", cx: 31.6, cy: 10, r: 2.6 }));
  return node;
}

function stepNode(index, title, ...children) {
  return el("div", { class: "connect-step" },
    el("span", { class: "connect-step-index" }, index),
    el("div", { class: "connect-step-body" },
      el("div", { class: "connect-step-title" }, title),
      ...children));
}

function trapTab(event) {
  if (!overlay) return;
  const nodes = Array.from(overlay.card.querySelectorAll(
    "button:not([disabled]), input:not([disabled]), a[href]"
  )).filter((node) => node.getClientRects().length > 0);
  if (!nodes.length) { event.preventDefault(); return; }
  const first = nodes[0];
  const last = nodes[nodes.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function onOverlayKey(event) {
  if (!overlay) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeConnect();
    return;
  }
  if (event.key === "Tab") trapTab(event);
}

function setError(message) {
  if (!overlay) return;
  overlay.error.hidden = !message;
  overlay.errorText.textContent = message || "";
}

async function verifyToken() {
  try {
    await api.get("/api/v1/meta");
    return;
  } catch (error) {
    if (error && Number(error.status) === 404) {
      await api.get("/stats");
      return;
    }
    throw error;
  }
}

async function submit() {
  if (!overlay || overlay.busy) return;
  const value = overlay.input.value.trim();
  if (!value) {
    setError("请先粘贴 daemon 令牌（43 位字符）。");
    overlay.input.focus();
    return;
  }
  const previous = api.getToken();
  overlay.busy = true;
  overlay.submitButton.disabled = true;
  overlay.submitLabel.textContent = "正在验证…";
  setError("");
  api.setToken(value);
  try {
    await verifyToken();
    if (!overlay) return;
    closeConnect();
    toast("已连接本地档案馆", { tone: "ok", ms: 1800 });
    if (onConnectedHandler) {
      try {
        await onConnectedHandler();
      } catch (error) {
        console.error("[atlas] onConnected callback failed", error);
      }
    }
  } catch (error) {
    api.setToken(previous || "");
    if (!overlay) return;
    overlay.busy = false;
    overlay.submitButton.disabled = false;
    overlay.submitLabel.textContent = "连接并验证";
    const status = error && Number(error.status);
    if (status === 401) {
      setError("令牌无效（401）。请确认粘贴的是完整令牌；daemon 重启后若更换了令牌，需要重新读取文件。");
    } else if (status === 403) {
      setError("令牌权限不足（403）：本界面需要本机管理员令牌。");
    } else if (status === 404) {
      setError("daemon 尚未提供验证接口（404）。请升级 hippomind 后重试。");
    } else if (status === 0) {
      setError("无法连接 daemon（网络错误）。请先在终端运行 hippomind start 或 hippomind serve。");
    } else {
      setError("验证失败：" + ((error && (error.detail || error.message)) || "未知错误"));
    }
    overlay.input.focus();
    overlay.input.select();
  }
}

export function openConnect() {
  const root = document.getElementById("connect-root");
  if (!root) return;
  if (overlay) {
    overlay.input.focus();
    return;
  }
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const titleId = "atlas-connect-title";
  const errorText = el("span", { class: "connect-error-text" });
  const errorBox = el("div", {
    class: "connect-error",
    role: "alert",
    "aria-live": "assertive",
    hidden: true,
  }, icon("error", { size: 15 }), errorText);
  const input = el("input", {
    class: "atlas-input connect-input",
    type: "password",
    placeholder: "粘贴令牌，例如 fR7v…（43 位字符）",
    autocomplete: "off",
    spellcheck: "false",
    "aria-label": "daemon 管理员令牌",
  });
  const revealButton = el("button", { class: "atlas-btn", type: "button" }, "显示");
  revealButton.addEventListener("click", () => {
    const reveal = input.type === "password";
    input.type = reveal ? "text" : "password";
    revealButton.textContent = reveal ? "隐藏" : "显示";
    input.focus();
  });
  const submitLabel = el("span", {}, "连接并验证");
  const formId = "atlas-connect-form";
  const submitButton = el("button", {
    class: "atlas-btn atlas-btn--primary",
    type: "submit",
    form: formId,
  }, icon("check", { size: 13 }), submitLabel);
  const closeButton = el("button", {
    class: "atlas-icon-btn",
    type: "button",
    "aria-label": "关闭连接面板",
    title: "关闭 (Esc)",
    on: { click: closeConnect },
  }, icon("close", { size: 16 }));

  const pathRow = el("div", { class: "connect-path" },
    el("code", {}, TOKEN_PATH),
    copyButton(TOKEN_PATH, { label: "复制路径" }));
  const commandRow = el("div", { class: "connect-path" },
    el("code", {}, TOKEN_COMMAND),
    copyButton(TOKEN_COMMAND, { label: "复制命令" }));
  const form = el("form", {
    class: "connect-input-row",
    id: formId,
    on: { submit: (event) => { event.preventDefault(); submit(); } },
  }, input, revealButton);

  const card = el("div", {
    class: "connect-card",
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": titleId,
  },
    el("header", { class: "connect-head" },
      brandMark(),
      el("div", { class: "connect-heading" },
        el("div", { class: "connect-eyebrow" }, "LOCAL ACCESS"),
        el("h2", { class: "connect-title", id: titleId }, "连接本机档案馆"),
        el("p", { class: "connect-subtitle" }, "HippoMind daemon · 本机管理员令牌")),
      closeButton),
    el("div", { class: "connect-body" },
      el("p", { class: "connect-intro" },
        "本页面由本机 HippoMind daemon 直接提供。读取账本需要本机管理员令牌；令牌只保存在当前浏览器的 localStorage 中，不写入页面、日志或 HTML。"),
      stepNode("01", "读取令牌文件", pathRow,
        el("p", { class: "connect-step-text" },
          "令牌是一个 43 位字符的随机串，文件权限应为 0600。也可在终端执行下面的命令查看："),
        commandRow,
        el("p", { class: "connect-alt" }, ALT_NOTE)),
      stepNode("02", "粘贴并验证", form,
        el("p", { class: "connect-step-text" },
          "粘贴后点击「连接并验证」。界面会先向 daemon 校验令牌，成功后才开始读取账本。")),
      errorBox,
      el("p", { class: "connect-note" },
        api.hasToken()
          ? "当前浏览器已保存一个令牌；重新粘贴会覆盖它。"
          : "当前浏览器尚未保存令牌。令牌不会离开这台机器。")),
    el("footer", { class: "connect-foot" },
      el("span", { class: "connect-foot-note" },
        "daemon 只监听 loopback；令牌仅在本机浏览器内使用。"),
      el("button", { class: "atlas-btn", type: "button", on: { click: closeConnect } }, "稍后再说"),
      submitButton));

  const scrim = el("div", { class: "connect-scrim" }, card);
  scrim.addEventListener("click", (event) => {
    if (event.target === scrim) closeConnect();
  });
  document.addEventListener("keydown", onOverlayKey, true);
  root.appendChild(scrim);
  overlay = {
    root, scrim, card, trigger, input, error: errorBox, errorText,
    submitButton, submitLabel, busy: false,
  };
  input.focus();
}

export function closeConnect() {
  if (!overlay) return;
  const current = overlay;
  overlay = null;
  document.removeEventListener("keydown", onOverlayKey, true);
  if (current.scrim.parentNode) current.scrim.parentNode.removeChild(current.scrim);
  if (current.trigger && document.contains(current.trigger) && typeof current.trigger.focus === "function") {
    current.trigger.focus();
  }
}

export default { initConnect, openConnect, closeConnect };
