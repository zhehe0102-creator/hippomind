/* ============================================================================
   HippoMind Atlas — claim entry
   A small, explicit write path: one fact with subject/predicate/slot/value and
   mandatory evidence, posted through the daemon's audited /ingest endpoint.
   This is the only way the console can create memory; it is admin-token only.
   ========================================================================= */
"use strict";

import { el } from "../core/dom.js";
import { api } from "../core/api.js";
import { drawer, textField, textareaField, toast } from "../components.js";
import { icon } from "../icons.js";

export function openClaimForm(options = {}) {
  const onCreated = typeof options.onCreated === "function" ? options.onCreated : null;
  const state = {
    subject: "",
    predicate: "",
    slot: "",
    value: "",
    content: "",
    evidence: "atlas-gui:manual",
    confidence: "0.8",
  };
  let submitting = false;

  const subject = textField({
    label: "subject · 主语", value: state.subject, placeholder: "例如 HippoMind",
    onInput: (event, value) => { state.subject = value; },
  });
  const predicate = textField({
    label: "predicate · 谓词", value: state.predicate, placeholder: "例如 uses",
    onInput: (event, value) => { state.predicate = value; },
  });
  const slot = textField({
    label: "slot_key · 槽位", value: state.slot, placeholder: "例如 database",
    onInput: (event, value) => { state.slot = value; },
  });
  const value = textField({
    label: "value · 值", value: state.value, placeholder: "例如 SQLite",
    onInput: (event, next) => { state.value = next; },
  });
  const evidence = textField({
    label: "evidence · 证据（必填）", value: state.evidence,
    placeholder: "例如 atlas-gui:manual 或 codex:session-1",
    onInput: (event, next) => { state.evidence = next; },
  });
  const confidence = textField({
    label: "confidence · 置信度 (0–1)", value: state.confidence, monospace: true,
    onInput: (event, next) => { state.confidence = next; },
  });
  const content = textareaField({
    label: "content · 记忆正文（可选，留空自动生成）", value: state.content,
    placeholder: "例如：HippoMind 的账本使用 SQLite + WAL",
    onInput: (event, next) => { state.content = next; },
  });

  const note = el("p", { class: "vb-note" },
    "写入会同时产生一条账本事件与一条事实投影，并立即出现在事实/图谱视图。",
    "证据是必填的；source_agent 固定为 atlas-gui，scope 为 shared。");

  async function commit() {
    if (submitting) return;
    const record = {
      subject: state.subject.trim(),
      predicate: state.predicate.trim(),
      slot_key: state.slot.trim(),
      value: state.value.trim(),
    };
    if (!record.subject || !record.predicate || !record.slot_key || !record.value) {
      toast("subject / predicate / slot_key / value 都不能为空", { tone: "warn" });
      return;
    }
    const proof = state.evidence.trim();
    if (!proof) {
      toast("evidence 是必填项：没有证据的推断不会进入账本", { tone: "warn" });
      return;
    }
    let score = Number(state.confidence);
    if (!Number.isFinite(score)) score = 0.8;
    score = Math.max(0, Math.min(1, score));
    const text = state.content.trim() ||
      (record.subject + " " + record.predicate + " " + record.slot_key + " = " + record.value);
    submitting = true;
    try {
      const result = await api.post("/ingest", {
        records: [{
          content: text,
          kind: "fact",
          source_agent: "atlas-gui",
          scope: "shared",
          evidence: [proof],
          timestamp: new Date().toISOString(),
          confidence: score,
          claims: [record],
        }],
      });
      const accepted = Number(result && result.accepted) || 0;
      const inserted = Number(result && result.inserted) || 0;
      toast("事实已写入：" + accepted + " 条受理 / " + inserted + " 条新增", { tone: "ok", ms: 2600 });
      drawer.close();
      if (onCreated) onCreated(result);
    } catch (error) {
      const detail = error && (error.detail || error.message) ? (error.detail || error.message) : "未知错误";
      toast("写入失败：" + detail, { tone: "danger", ms: 4200 });
    } finally {
      submitting = false;
    }
  }

  const form = el("div", { class: "vb-claim-form" },
    el("div", { class: "vb-form-grid" }, subject, predicate, slot, value, evidence, confidence),
    content,
    note);

  drawer.open({
    title: "记录一条事实",
    eyebrow: "WRITE",
    subtitle: "管理员写操作：写入一条带证据的 claim",
    content: form,
    actions: [
      { label: "取消", onClick: () => drawer.close() },
      { label: "写入事实", primary: true, onClick: commit },
    ],
  });
}

export default { openClaimForm };
