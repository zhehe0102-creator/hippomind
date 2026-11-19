/* ============================================================================
   HippoMind Atlas — 01 pulse 脉搏
   The ledger's vital signs: scale, composition, growth and health signals.
   Data: GET /api/v1/overview?days=30
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import {
  fmtInt, fmtCompact, fmtBytes, fmtPct, fmtMs, relTime, absTime, duration,
  truncate,
} from "../core/format.js";
import {
  badge, card, metric, emptyState, errorState, skeleton, copyButton, meter,
  statRow,
} from "../components.js";
import { sparkline, barList } from "../charts.js";
import { icon } from "../icons.js";

export const meta = {
  id: "pulse",
  index: "01",
  title: "脉搏",
  eyebrow: "PULSE",
  subtitle: "账本的实时体征：规模、构成、增长与健康信号",
};

const OVERVIEW_DAYS = 30;
const TONES = ["accent", "link", "ok", "warn", "neutral", "danger"];
const WAL_WARN_BYTES = 64 * 1024 * 1024;

let current = { root: null, painted: false };

function toneAt(index) {
  return TONES[Math.abs(index) % TONES.length];
}

function severityTone(severity) {
  if (severity === "error") return "danger";
  if (severity === "warn") return "warn";
  return "ok";
}

function dot(tone) {
  return el("span", { class: "va-dot tone-" + tone });
}

function sortedPairs(record, limit) {
  return Object.entries(record || {})
    .filter(([, value]) => Number(value) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, limit)
    .map(([label, value], index) => ({ label, value: Number(value), tone: toneAt(index) }));
}

function apiBase() {
  return window.location.origin + "/api/v1";
}

/* ------------------------------------------------------------------ hero -- */

function heroSection(data) {
  const server = data.server || {};
  const growth = data.growth || {};
  const ledger = data.ledger || {};
  const buckets = Array.isArray(growth.buckets) ? growth.buckets : [];
  const values = buckets.map((bucket) => Number(bucket.memories) || 0);
  const lastWeek = values.slice(-7).reduce((sum, value) => sum + value, 0);
  const priorWeek = values.slice(-14, -7).reduce((sum, value) => sum + value, 0);
  const delta = priorWeek > 0 ? (lastWeek - priorWeek) / priorWeek : null;
  const days = Number(growth.days) || OVERVIEW_DAYS;

  const lead = el("div", { class: "va-pulse-lead" },
    el("div", { class: "eyebrow" }, "ACTIVE MEMORIES · 当前"),
    metric({
      label: "活跃结构记忆",
      value: fmtInt(ledger.active),
      hero: true,
      delta,
      hint: "账本共 " + fmtInt(ledger.memories) + " 条 · raw 证据 " + fmtInt(ledger.raw_chat) + " 条不参与统计",
    }),
    el("div", { class: "va-pulse-spark" },
      el("div", { class: "va-pulse-spark-head" },
        el("span", { class: "eyebrow" }, "每日入档"),
        el("span", { class: "va-pulse-spark-total" },
          el("b", {}, fmtInt(growth.total_memories)),
          el("span", { class: "muted" }, " 条 / 近 " + fmtInt(days) + " 天"))),
      sparkline({
        values,
        width: 560,
        height: 62,
        tone: "accent",
        label: "近 " + fmtInt(days) + " 天每日入档走势",
        title: "每日结构化记忆入档数",
      })));

  const figures = el("div", { class: "va-pulse-figures" },
    metric({
      label: "EVENTS · 事件",
      value: fmtCompact(ledger.events),
      hint: "近 " + fmtInt(days) + " 天 +" + fmtCompact(growth.total_events) + " 条",
    }),
    metric({
      label: "MEMORY TOTAL · 总量",
      value: fmtCompact(ledger.memories),
      hint: "active " + fmtInt(ledger.active) + " · superseded " + fmtInt(ledger.superseded),
    }),
    metric({
      label: "UPTIME · 运行",
      value: duration(server.uptime_s),
      small: true,
      hint: "v" + (server.server_version || "?") + " · pid " + fmtInt(server.pid),
    }));

  return el("section", { class: "va-pulse-hero", "aria-label": "脉搏头条" }, lead, figures);
}

/* ---------------------------------------------------------- status strip -- */

function statusStrip(data, context) {
  const server = data.server || {};
  const watcher = data.watcher || {};
  const items = [];

  function item(label, detail, tone, titleText) {
    items.push(el("button", {
      class: "va-status-item",
      type: "button",
      title: titleText || (label + " · " + detail),
      on: { click: () => context.navigate("/health") },
    },
      dot(tone),
      el("span", { class: "va-status-label" }, label),
      el("span", { class: "va-status-detail" }, detail)));
  }

  item("daemon", "在线 · v" + (server.server_version || "?"), "ok",
    "daemon 运行中（pid " + fmtInt(server.pid) + "）");
  item(
    "watcher",
    watcher.available ? "轮询 " + fmtInt(watcher.poll_interval) + "s" : "不可用",
    watcher.available ? "ok" : "warn",
    watcher.available ? "归档 watcher 正在轮询" : "归档 watcher 未启动",
  );
  if (Number(data.wal_bytes) > WAL_WARN_BYTES) {
    item("WAL", fmtBytes(data.wal_bytes), "warn", "WAL 侧车超过 64 MiB 阈值");
  }
  const flags = Array.isArray(data.health_flags) ? data.health_flags : [];
  for (const flag of flags) {
    if (!flag || !flag.id) continue;
    const word = flag.severity === "error" ? "错误" : flag.severity === "warn" ? "警告" : "通过";
    item(flag.label || flag.id, word, severityTone(flag.severity), flag.detail || "");
  }
  return el("div", { class: "va-status-strip", role: "group", "aria-label": "daemon 与健康状态" }, ...items);
}

/* ------------------------------------------------------------ automation -- */

function numberOrZero(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

/* /api/v1/health reports the injection trace as a rendered detail string
   ("最近 N 次注入 · M 次实际注入 · 最近 <ts>") because it is the only endpoint
   that summarises the trace, and this task may not add one. Parse it strictly;
   when the wording changes the count degrades to "—" and the raw detail is
   still shown verbatim, so a stale parser never invents a number. */
function parseInjectionDetail(detail) {
  const text = String(detail || "");
  const count = text.match(/最近\s*(\d+)\s*次注入/);
  const injected = text.match(/(\d+)\s*次实际注入/);
  const lastAt = text.match(/(\d{4}-\d{2}-\d{2}T[0-9:.+-]+)/);
  return {
    count: count ? Number(count[1]) : null,
    injected: injected ? Number(injected[1]) : null,
    lastAt: lastAt ? lastAt[1] : "",
  };
}

/* "The loop has not run yet" and "the loop ran and found nothing" are
   different facts, and only one of them is a problem. The daemon publishes an
   empty object until a stage completes its first round, so an empty stage is
   rendered as unknown (—) rather than as a confident zero. */
function stageState(raw) {
  if (!raw || typeof raw !== "object" || !Object.keys(raw).length) return "pending";
  if (String(raw.error || "")) return "error";
  return "ran";
}

/* The v2 loop runs unattended, so the pulse page answers "is it actually
   working?" from two payloads it already needs: /overview carries the daemon's
   own capture/distill counters, /health carries the injection trace check.
   They are fetched together so the strip is one snapshot, not two. */
function automationStrip(data, health, context) {
  const daemonHealth = data.daemon_health && typeof data.daemon_health === "object"
    ? data.daemon_health : {};
  const capture = daemonHealth.capture && typeof daemonHealth.capture === "object"
    ? daemonHealth.capture : {};
  const distill = daemonHealth.distill && typeof daemonHealth.distill === "object"
    ? daemonHealth.distill : {};
  const checks = health && Array.isArray(health.checks) ? health.checks : [];
  const injectionCheck = checks.find((check) => check && check.id === "injection") || null;
  const injection = injectionCheck ? parseInjectionDetail(injectionCheck.detail) : null;
  const injectionCount = injection && injection.count !== null ? injection.count : null;
  const injectionInjected = injection && injection.injected !== null ? injection.injected : null;
  const injectionAt = injection ? injection.lastAt : "";

  const captureState = stageState(capture);
  const distillState = stageState(distill);
  const captureError = String(capture.error || "");
  const distillError = String(distill.error || "");
  const captureFiles = numberOrZero(capture.files);
  const captureMessages = numberOrZero(capture.messages);
  const captureInserted = numberOrZero(capture.inserted);
  const distillCandidates = numberOrZero(distill.candidates);
  const distillClaims = numberOrZero(distill.claims);
  const distillDistilled = numberOrZero(distill.distilled);

  const stages = [
    {
      id: "capture",
      eyebrow: "CAPTURE",
      label: "采集",
      tone: captureState === "error" ? "warn" : captureState === "ran" ? "ok" : "neutral",
      primary: captureState === "ran" ? fmtInt(captureFiles) : "—",
      unit: captureState === "ran" ? "个文件" : "",
      detail: captureState === "error" ? captureError
        : captureState === "ran"
          ? (fmtInt(captureMessages) + " 条消息 · 新增 " + fmtInt(captureInserted) + " 条")
          : "等待首轮采集（每 60 秒）",
      title: captureState === "error"
        ? "采集上一轮报错：" + captureError
        : captureState === "ran"
          ? "已扫描 " + fmtInt(captureFiles) + " 个会话文件，读取 " + fmtInt(captureMessages)
            + " 条消息，新增 " + fmtInt(captureInserted) + " 条记忆"
          : "daemon 尚未完成首轮采集；采集循环启动后每 60 秒运行一次",
      stage: "capture",
    },
    {
      id: "distill",
      eyebrow: "DISTILL",
      label: "提炼",
      tone: distillState === "error" ? "warn" : distillState === "ran" ? "ok" : "neutral",
      primary: distillState === "ran" ? fmtInt(distillCandidates) : "—",
      unit: distillState === "ran" ? "个候选" : "",
      detail: distillState === "error" ? distillError
        : distillState === "ran"
          ? (fmtInt(distillClaims) + " 条 claims · 提炼 " + fmtInt(distillDistilled) + " 条")
          : "等待首轮提炼（每 5 分钟）",
      title: distillState === "error"
        ? "提炼上一轮报错：" + distillError
        : distillState === "ran"
          ? "本轮 " + fmtInt(distillCandidates) + " 个候选，产出 " + fmtInt(distillClaims)
            + " 条 claims，落库 " + fmtInt(distillDistilled) + " 条"
          : "daemon 尚未完成首轮提炼；提炼循环启动后每 5 分钟运行一次",
      stage: "distill",
    },
    {
      id: "injection",
      eyebrow: "INJECTION",
      label: "注入",
      tone: injectionCheck && injectionCheck.severity !== "ok" ? "warn"
        : (injectionCheck ? "ok" : "neutral"),
      primary: injectionCount === null ? "—" : fmtInt(injectionCount),
      unit: injectionCount === null ? "" : "次注入",
      detail: injectionAt
        ? ((injectionInjected === null ? "—" : fmtInt(injectionInjected)) + " 次实际注入 · " + relTime(injectionAt))
        : (injectionCheck ? "尚无注入记录" : "daemon 未报告注入检查项"),
      title: injectionCheck && injectionCheck.detail
        ? injectionCheck.detail
        : (injectionAt ? "最近注入于 " + absTime(injectionAt, { seconds: true }) : "尚未记录自动注入"),
      stage: "injection",
    },
  ];

  const cells = stages.map((stage) => el("button", {
    class: "va-auto-cell",
    type: "button",
    dataset: { tone: stage.tone, stage: stage.id },
    title: stage.title + " · 点击查看体检",
    on: { click: () => context.navigate("/health") },
  },
    el("span", { class: "va-auto-cell-head" },
      dot(stage.tone),
      el("span", { class: "va-auto-cell-eyebrow" }, stage.eyebrow),
      el("span", { class: "va-auto-cell-label" }, stage.label)),
    el("span", { class: "va-auto-cell-figure" },
      el("b", {}, stage.primary),
      el("span", { class: "va-auto-cell-unit" }, stage.unit)),
    el("span", { class: "va-auto-cell-detail" }, truncate(stage.detail, 74))));

  return el("div", { class: "va-auto-strip", role: "group", "aria-label": "自动记忆环路" },
    el("div", { class: "va-auto-strip-head" },
      el("span", { class: "eyebrow" }, "AUTOMATION"),
      el("span", { class: "va-auto-strip-note" }, "采集 → 提炼 → 注入，由 daemon 后台自动运行")),
    el("div", { class: "va-auto-strip-cells" }, ...cells));
}


/* ------------------------------------------------------- retrieval channels -- */

/* 三路检索通道的真实状态。数据全部来自本视图已经在拿的两个 payload：
   /health 的 fts 检查项（词法），/overview 的 daemon_health（向量 + 实体）。
   不新增端点，也不重复查询。

   这里最容易做错的一件事：把「已关闭」渲染成「坏了」。entity_index 默认是
   {"state":"disabled"}，因为 HIPPOMIND_ENTITY_CHANNEL 默认关闭——那是一个
   可开启的功能，不是一个故障。两者在界面上必须能一眼分开，所以 state 映射
   到三种语气：ready=ok / disabled=neutral / unavailable|error=warn。 */
const CHANNEL_STATE_TONE = {
  ready: "ok",
  disabled: "neutral",
  unavailable: "warn",
  error: "warn",
  warn: "warn",
  stopped: "neutral",
  pending: "neutral",
};

const CHANNEL_STATE_LABEL = {
  ready: "运行中",
  disabled: "已关闭",
  unavailable: "不可用",
  error: "报错",
  warn: "有告警",
  stopped: "已停止",
  pending: "等待首轮",
};

/* The three channels do not share a field name for their health: the index
   loops publish `state`, the FTS check publishes `status`.  Reading only one
   of them made a healthy lexical channel render as "等待首轮". */
function channelState(raw) {
  if (!raw || typeof raw !== "object" || !Object.keys(raw).length) return "pending";
  const state = String(raw.state || "").trim();
  if (state) return state;
  const status = String(raw.status || "").trim();
  if (status) return status === "ok" ? "ready" : "warn";
  return raw.error ? "error" : "pending";
}

function fmtChannelCount(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? fmtInt(numeric) : "—";
}

/* 每路通道的规模说明。字段名来自各索引自己的 stats()，这里只做展示映射，
   缺失一律显示「—」而不是 0：没报告不等于零。 */
function channelDetail(id, raw, fts, ledger) {
  const state = channelState(raw);
  if (id === "lexical") {
    if (!fts || typeof fts !== "object") return "daemon 未报告 FTS 检查项";
    const rows = fmtChannelCount(fts.fts_rows);
    const unindexed = fmtChannelCount(fts.unindexed);
    const stale = fmtChannelCount(fts.stale);
    const p95 = Number(ledger.search_p95_ms);
    return "FTS " + rows + " 行 · 未索引 " + unindexed + " · 陈旧 " + stale +
      (Number.isFinite(p95) ? " · 检索 p95 " + fmtMs(p95) : "");
  }
  if (state === "disabled") {
    return id === "entity"
      ? "已关闭：HIPPOMIND_ENTITY_CHANNEL=1 可开启（暖态构建约 30 ms）"
      : "已关闭：需要 daemon 以 --watch 启动且已安装嵌入模型";
  }
  if (state === "unavailable") {
    return id === "entity"
      ? "不可用：账本里还没有可建索引的 claim"
      : "不可用：未安装嵌入模型（semantic_onnx 未就绪）";
  }
  if (state === "warn" && id === "lexical") {
    return "FTS 检查报 " + String(fts && fts.status || "warn") +
      " · 未索引 " + fmtChannelCount(fts && fts.unindexed) +
      " · 陈旧 " + fmtChannelCount(fts && fts.stale);
  }
  if (state === "error") return "报错：" + String(raw.error || "未知错误").slice(0, 120);
  if (id === "vector") {
    const docs = fmtChannelCount(raw.docs);
    const dim = raw.dim ? String(raw.dim) + " 维" : "";
    const model = String(raw.model || "");
    return docs + " docs" + (dim ? " · " + dim : "") + (model ? " · " + model : "") +
      (raw.built_at ? " · 构建于 " + relTime(raw.built_at) : "");
  }
  const entities = fmtChannelCount(raw.entities);
  const claims = fmtChannelCount(raw.claims);
  return entities + " 实体 · " + claims + " claims" +
    (raw.built_at ? " · 构建于 " + relTime(raw.built_at) : "");
}

function channelScale(id, raw, fts) {
  if (id === "lexical") {
    return fts && Number.isFinite(Number(fts.fts_rows)) ? fmtInt(fts.fts_rows) : "—";
  }
  const state = channelState(raw);
  if (state !== "ready") return "—";
  const key = id === "vector" ? raw.docs : raw.entities;
  return Number.isFinite(Number(key)) ? fmtInt(key) : "—";
}

function retrievalChannels(data, health) {
  const daemonHealth = data.daemon_health && typeof data.daemon_health === "object"
    ? data.daemon_health : {};
  const checks = health && Array.isArray(health.checks) ? health.checks : [];
  const ftsCheck = checks.find((check) => check && check.id === "fts") || null;
  const fts = (health && health.fts) || (ftsCheck ? ftsCheck.fts : null)
    || (data.ledger && data.ledger.fts) || null;
  const ledger = data.ledger || {};
  const vector = daemonHealth.vector_index && typeof daemonHealth.vector_index === "object"
    ? daemonHealth.vector_index : {};
  const entity = daemonHealth.entity_index && typeof daemonHealth.entity_index === "object"
    ? daemonHealth.entity_index : {};

  const channels = [
    { id: "lexical", eyebrow: "LEXICAL", label: "词法", raw: fts || {}, fts },
    { id: "vector", eyebrow: "VECTOR", label: "向量", raw: vector, fts },
    { id: "entity", eyebrow: "ENTITY", label: "实体", raw: entity, fts },
  ];

  const cells = channels.map((channel) => {
    const state = channelState(channel.raw);
    const tone = CHANNEL_STATE_TONE[state] || "neutral";
    return el("div", { class: "va-channel", dataset: { tone, channel: channel.id } },
      el("div", { class: "va-channel-head" },
        dot(tone),
        el("span", { class: "va-channel-eyebrow" }, channel.eyebrow),
        el("span", { class: "va-channel-label" }, channel.label),
        badge(CHANNEL_STATE_LABEL[state] || state, { tone })),
      el("div", { class: "va-channel-figure" },
        el("b", {}, channelScale(channel.id, channel.raw, channel.fts)),
        el("span", { class: "va-channel-unit" },
          channel.id === "entity" ? "实体" : "索引项")),
      el("p", { class: "va-channel-detail" },
        channelDetail(channel.id, channel.raw, channel.fts, ledger)));
  });

  const readyCount = channels.filter((c) => channelState(c.raw) === "ready").length;
  return card({
    eyebrow: "RETRIEVAL CHANNELS",
    title: "检索通道",
    actions: badge(fmtInt(readyCount) + " / 3 运行中",
      { tone: readyCount === 3 ? "ok" : readyCount ? "warn" : "neutral" }),
    body: el("div", {},
      el("div", { class: "va-channels" }, ...cells),
      el("p", { class: "va-note" },
        "召回按三路并行打分：词法（FTS + LIKE）、向量（语义近邻）、实体（claim 主语倒排）。",
        "某一路「已关闭」只是它没有参与打分，另外两路照常工作——",
        "「已关闭」不等于「不可用」，后者才是需要处理的故障。")),
  });
}


/* ---------------------------------------------------------- injection A/B -- */

/* A/B 进度条。这个区块存在的理由：用户开了实验之后，界面上没有任何地方告诉他
   「还要等多久」。30 条样本的门槛在 20% 对照比例下意味着约 150 次提问——不说
   出来，实验看起来就是永远没有结论。

   两个分母必须分开讲，混在一起这类面板就开始撒谎：
     * min_samples(30) 是「任何结论」的门槛；
     * 两臂对照还额外要求 min(treatment_n, control_n) >= 30，
       所以默认比例下**对照组**才是瓶颈，不是总数。

   进度数字来自 /api/v1/injection-ab 的快速路径（trace 尾部计数，实测 3.8 ms）。
   真实的可评估样本要跑 injection_quality，实测 6.6 s，所以它是显式按钮。 */

function armProgress(arm, label, tone) {
  const have = Number(arm && arm.have) || 0;
  const need = Math.max(1, Number(arm && arm.need) || 1);
  const shortfall = Math.max(0, Number(arm && arm.shortfall) || 0);
  return el("div", { class: "va-ab-arm", dataset: { tone } },
    meter({
      label: label,
      value: have,
      max: need,
      tone,
      valueText: fmtInt(have) + " / " + fmtInt(need) +
        (shortfall ? "（还差 " + fmtInt(shortfall) + "）" : "（已达标）"),
    }));
}

function injectionAbCard(ab, context) {
  if (!ab || typeof ab !== "object") {
    return card({
      eyebrow: "INJECTION A/B",
      title: "注入实验",
      body: emptyState({
        plain: true,
        icon: "info",
        title: "daemon 未返回实验状态",
        hint: "本视图需要 /api/v1/injection-ab；请确认 daemon 已更新到当前版本。",
      }),
    });
  }
  const config = ab.config || {};
  const progress = ab.progress || {};
  const traces = ab.traces || {};
  const enabled = Boolean(config.enabled);
  const rate = Number(config.rate);
  const binding = String(progress.binding_arm || "");
  const bindingShort = Number(progress.binding_shortfall) || 0;
  const promptsNeeded = Number(progress.prompts_needed);

  const tone = !enabled ? "neutral" : bindingShort > 0 ? "warn" : "ok";
  const headline = !enabled
    ? "实验未开启：所有提问都走注入臂，没有对照组可比"
    : bindingShort > 0
      ? "还差 " + fmtInt(bindingShort) + " 条" +
        (binding === "control" ? "对照" : "注入") + "样本才能下结论"
      : "两臂样本都达标，可以跑一次指标计算看结论";

  const body = el("div", { class: "va-ab" },
    el("div", { class: "va-ab-head" },
      dot(tone),
      el("div", { class: "va-ab-headline" }, headline),
      badge(enabled ? "实验进行中" : "已关闭", { tone })),
    statRow([
      { label: "对照比例",
        value: Number.isFinite(rate) ? fmtPct(rate) : "—" },
      { label: "每 100 次提问的对照数",
        value: config.expected_control_per_100_prompts === null ||
          config.expected_control_per_100_prompts === undefined
          ? "—" : String(config.expected_control_per_100_prompts) },
      { label: "结论门槛",
        value: fmtInt(ab.min_samples) + " 条 / 臂" },
      { label: "达标所需提问",
        value: Number.isFinite(promptsNeeded) ? fmtInt(promptsNeeded) + " 次" : "—" },
      { label: "已记录 trace",
        value: fmtInt(traces.read) + " 条" +
          (traces.truncated ? "（只读了文件尾部）" : "") +
          (Number(traces.malformed) ? " · 无法解析 " + fmtInt(traces.malformed) : "") },
      { label: "最近一次",
        value: traces.last_at ? relTime(traces.last_at) : "尚无记录" },
    ]),
    el("div", { class: "va-ab-arms" },
      armProgress(progress.treatment, "注入臂（treatment）", "accent"),
      armProgress(progress.control, "对照臂（control · 不注入）", "link")));

  const footnote = el("p", { class: "va-note" },
    "「已记录 trace」是可评估样本的【上界】，不是它本身：" +
    "一条 trace 只有在其时间窗内",
    "存在可读的 assistant 回复时才可评估。真实样本数与两臂指标要点下面的按钮现算（约数秒）。");
  body.appendChild(footnote);

  const result = el("div", { class: "va-ab-metrics" });
  const button = el("button", {
    class: "atlas-btn atlas-btn--ghost atlas-btn--sm",
    type: "button",
  }, icon("pulse", { size: 13 }), el("span", {}, "计算两臂指标"));
  button.addEventListener("click", async () => {
    button.disabled = true;
    result.replaceChildren(el("p", { class: "va-note" },
      "正在计算（要读 raw 回复，实测数秒）…"));
    try {
      const data = await api.get("/api/v1/injection-ab",
        { metrics: "1", limit: 500 }, { signal: context.signal });
      const arms = data.arms || {};
      const verdict = data.verdict || {};
      const nodes = [];
      if (data.metrics_error) {
        nodes.push(el("p", { class: "va-note tone-warn" },
          "指标计算失败：" + String(data.metrics_error)));
      } else {
        nodes.push(el("div", { class: "va-ab-verdict" },
          badge(verdict.conclusive ? "可下结论" : "尚不可下结论",
            { tone: verdict.conclusive ? "ok" : "warn" }),
          el("span", { class: "va-ab-verdict-text" }, verdict.headline || "")));
        nodes.push(statRow([
          { label: "可评估样本",
            value: fmtInt(data.evaluable) + " 条（门槛 " + fmtInt(ab.min_samples) + "）" },
          { label: "注入臂 n / 复用率",
            value: fmtInt(arms.treatment_n) + " · " +
              (arms.treatment_citation_rate === null ? "—" :
                fmtPct(arms.treatment_citation_rate)) },
          { label: "对照臂 n / 复用率",
            value: fmtInt(arms.control_n) + " · " +
              (arms.control_citation_rate === null ? "—" :
                fmtPct(arms.control_citation_rate)) },
          { label: "差值",
            value: arms.difference === null || arms.difference === undefined
              ? "—" : String(arms.difference) },
          { label: "耗时",
            value: Math.round(Number(data.metrics_elapsed_ms) || 0) + " ms" },
        ]));
        const caveats = Array.isArray(verdict.caveats) ? verdict.caveats : [];
        if (caveats.length) {
          nodes.push(el("ul", { class: "va-ab-caveats" },
            ...caveats.map((line) => el("li", {}, line))));
        }
      }
      result.replaceChildren(...nodes);
    } catch (error) {
      if (error && error.name === "AbortError") return;
      result.replaceChildren(el("p", { class: "va-note tone-danger" },
        "指标计算失败：" + String((error && (error.detail || error.message)) || "未知错误")));
    } finally {
      button.disabled = false;
    }
  });
  body.appendChild(el("div", { class: "va-ab-actions" }, button));
  body.appendChild(result);

  return card({
    eyebrow: "INJECTION A/B",
    title: "注入实验",
    actions: enabled
      ? badge("对照 " + (Number.isFinite(rate) ? fmtPct(rate) : "—"), { tone: "accent" })
      : badge("未开启", { tone: "neutral" }),
    body,
  });
}

/* ------------------------------------------------------------ composition -- */

function compositionCard(data) {
  const ledger = data.ledger || {};
  const total = Number(ledger.memories) || 0;
  const raw = Number(ledger.raw_chat) || 0;
  const structured = Math.max(0, total - raw);
  const items = sortedPairs(ledger.kinds, 9);
  const body = el("div", { class: "va-composition" },
    barList({ items, total: total || undefined, maxBars: 9, label: "kind 构成" }),
    el("p", { class: "va-note" },
      "结构记忆 " + fmtInt(structured) + " 条，raw 证据 " + fmtInt(raw) + " 条（" +
      fmtPct(total > 0 ? raw / total : 0) + "）。raw/工具输出默认不进入召回，",
      "口径上「结构记忆」= 全部记忆 − raw 证据。"));
  return card({
    eyebrow: "COMPOSITION",
    title: "记忆构成",
    body,
    className: "va-card--pulse",
  });
}

/* ---------------------------------------------------------------- sources -- */

function sourcesCard(data) {
  const ledger = data.ledger || {};
  const items = sortedPairs(ledger.agents, 8);
  const body = el("div", { class: "va-sources" },
    barList({ items, total: Number(ledger.memories) || undefined, mono: true, maxBars: 8, label: "Agent 来源分布" }),
    el("p", { class: "va-note" },
      fmtInt(ledger.trusted_agents) + " 个 Agent 有信任基线 · " +
      fmtInt(ledger.cursors) + " 个采集游标 · " +
      fmtInt(ledger.agent_credentials) + " 个有效凭据"));
  return card({
    eyebrow: "PROVENANCE",
    title: "来源分布",
    body,
  });
}

/* ------------------------------------------------------------ recent slips -- */

function slip(item, index, context) {
  const stamp = item.updated_at || item.created_at || "";
  const metaRow = el("span", { class: "va-slip-meta" },
    badge(item.kind || "unknown", { tone: "neutral" }),
    el("span", { class: "va-slip-agent" }, item.source_agent || "unknown"),
    el("span", { class: "va-slip-time", title: absTime(stamp) }, relTime(stamp)),
    item.pinned ? el("span", { class: "va-slip-mark" }, icon("pin", { size: 11 }), "置顶") : null,
    item.has_claims ? el("span", { class: "va-slip-mark" }, "claim") : null);
  return el("button", {
    class: "va-slip",
    type: "button",
    title: "打开详情：" + (item.content || "").slice(0, 60),
    on: { click: () => context.navigate("/archive?focus=" + encodeURIComponent(String(item.memory_id || ""))) },
  },
    el("span", { class: "va-slip-index" }, String(index + 1).padStart(2, "0")),
    el("span", { class: "va-slip-main" },
      metaRow,
      el("span", { class: "va-slip-content" }, truncate(item.content || "", 260))));
}

function recentCard(data, context) {
  const recent = Array.isArray(data.recent) ? data.recent : [];
  const body = el("div", { class: "va-slips" });
  if (!recent.length) {
    body.appendChild(emptyState({
      title: "还没有最近入档",
      hint: "daemon 采集到第一条结构化记忆后，这里会列出最近的 6 条。",
      action: { label: "查看端点", icon: "agents", onClick: () => context.navigate("/agents") },
    }));
  } else {
    recent.slice(0, 6).forEach((item, index) => body.appendChild(slip(item, index, context)));
  }
  return card({
    eyebrow: "RECENT ACCESSIONS",
    title: "最近入档",
    actions: { label: "全部记忆", icon: "archive", ghost: true, onClick: () => context.navigate("/archive") },
    body,
  });
}

/* --------------------------------------------------------------- activity -- */

function activityCard(activity, context) {
  const items = Array.isArray(activity && activity.items) ? activity.items : [];
  const list = el("div", { class: "va-activity" });
  if (!items.length) {
    list.appendChild(emptyState({
      title: "还没有结构化事件",
      hint: "watcher 采集到的结构化事件会出现在这里；raw/tool/system 事件不会展示。",
      action: { label: "查看端点", icon: "agents", onClick: () => context.navigate("/agents") },
    }));
  } else {
    for (const item of items.slice(0, 8)) {
      list.appendChild(el("div", { class: "va-activity-row" },
        el("span", { class: "va-activity-time", title: absTime(item.occurred_at) }, relTime(item.occurred_at)),
        badge(item.event_type || "event", { tone: "neutral" }),
        el("span", { class: "va-activity-agent" }, item.source_agent || "—"),
        el("span", { class: "va-activity-content" }, truncate(item.content_preview || "", 140))));
    }
  }
  const counts = activity && activity.counts ? activity.counts : {};
  const summary = Object.keys(counts).slice(0, 4)
    .map((key) => key + " " + fmtInt(counts[key])).join(" · ");
  return card({
    eyebrow: "ACTIVITY",
    title: "最近活动",
    actions: { label: "查看年轮", icon: "timeline", ghost: true, onClick: () => context.navigate("/timeline") },
    body: el("div", {}, list,
      summary ? el("div", { class: "va-activity-summary" }, summary) : null),
  });
}

/* ------------------------------------------------------------ health flags -- */

function healthCard(data, context) {
  const flags = Array.isArray(data.health_flags) ? data.health_flags : [];
  const list = el("div", { class: "va-flags" });
  if (!flags.length) {
    list.appendChild(emptyState({
      title: "未报告健康信号",
      hint: "daemon 本次没有返回健康 flags。打开体检页可查看完整检查项。",
      plain: true,
      action: { label: "打开体检", icon: "health", onClick: () => context.navigate("/health") },
    }));
  } else {
    for (const flag of flags) {
      const tone = severityTone(flag.severity);
      list.appendChild(el("button", {
        class: "va-flag",
        type: "button",
        title: (flag.label || flag.id) + " · " + (flag.detail || ""),
        on: { click: () => context.navigate("/health") },
      },
        dot(tone),
        el("span", { class: "va-flag-label" }, flag.label || flag.id || "check"),
        badge(flag.severity === "error" ? "错误" : flag.severity === "warn" ? "警告" : "通过", { tone }),
        el("span", { class: "va-flag-detail" }, truncate(flag.detail || "", 120))));
    }
  }
  return card({
    eyebrow: "HEALTH FLAGS",
    title: "体检摘要",
    actions: { label: "打开体检", icon: "health", ghost: true, onClick: () => context.navigate("/health") },
    body: list,
  });
}

/* --------------------------------------------------------------- shortcuts -- */

function shortcutCard(context) {
  const base = apiBase();
  const actions = [
    el("button", { class: "atlas-btn atlas-btn--block", type: "button", on: { click: () => context.navigate("/recall") } },
      icon("recall", { size: 14 }), el("span", {}, "检索记忆")),
    el("button", { class: "atlas-btn atlas-btn--block", type: "button", on: { click: () => context.navigate("/compose") } },
      icon("compose", { size: 14 }), el("span", {}, "组装上下文")),
    el("button", { class: "atlas-btn atlas-btn--block", type: "button", on: { click: () => context.navigate("/archive") } },
      icon("archive", { size: 14 }), el("span", {}, "浏览记忆库")),
  ];
  const copyRow = el("div", { class: "va-copy-row" },
    el("code", { class: "va-api-base", title: base }, base));
  copyRow.appendChild(copyButton(base, { label: "复制 API 地址" }));
  return card({
    eyebrow: "SHORTCUTS",
    title: "快捷动作",
    body: el("div", { class: "va-quick-actions" }, ...actions,
      el("div", { class: "va-quick-sep" }),
      el("div", { class: "va-note" }, "API 基地址（同源，需管理员令牌头 X-HippoMind-Token）"),
      copyRow),
  });
}

/* ------------------------------------------------------------------- empty -- */

function emptyLedgerCard(context) {
  return card({
    body: emptyState({
      icon: "archive",
      title: "账本里还没有结构化记忆",
      hint: "daemon 已连接，但账本为空。可以先去端点页确认 Agent 接入，或用 CLI 写入第一条记忆。",
      action: [
        { label: "查看端点", icon: "agents", primary: true, onClick: () => context.navigate("/agents") },
        { label: "浏览记忆库", icon: "archive", onClick: () => context.navigate("/archive") },
      ],
    }),
  });
}

/* ------------------------------------------------------------------- paint -- */

function paint(root, data, activity, health, ab, context) {
  clear(root);
  const ledger = data.ledger || {};
  root.appendChild(heroSection(data));

  const strip = statusStrip(data, context);
  if (strip.children.length) root.appendChild(strip);

  root.appendChild(automationStrip(data, health, context));
  root.appendChild(retrievalChannels(data, health));
  root.appendChild(injectionAbCard(ab, context));

  if (!Number(ledger.memories)) root.appendChild(emptyLedgerCard(context));

  root.appendChild(el("div", { class: "atlas-grid atlas-grid--2" },
    compositionCard(data),
    sourcesCard(data)));

  root.appendChild(el("div", { class: "va-pulse-lower" },
    el("div", { class: "va-pulse-main" },
      recentCard(data, context),
      activityCard(activity, context)),
    el("div", { class: "va-pulse-side" },
      healthCard(data, context),
      shortcutCard(context))));

  current.painted = true;
}

/* ------------------------------------------------------------------- entry -- */

/* The automation strip needs the health checks for the injection trace. It is
   an enhancement, never a reason for the first screen to fail: a failed
   /health leaves the strip in its "not reported" state. */
async function fetchHealth(context) {
  try {
    return await api.get("/api/v1/health", null, { signal: context.signal });
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    console.warn("[atlas] pulse automation health unavailable", error);
    return null;
  }
}

/* The automation strip reads overview.daemon_health and health.checks; taking
   both in one parallel batch keeps the three stages describing the same
   moment instead of two snapshots a minute apart. */
/* The A/B progress payload is sub-4 ms on the daemon (trace tail counting),
   so it belongs in the first-paint batch.  Its DEEP metrics cost 6.6 s and
   stay behind the card's explicit button instead. */
async function fetchInjectionAb(context) {
  try {
    return await api.get("/api/v1/injection-ab", null, { signal: context.signal });
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    console.warn("[atlas] pulse injection-ab unavailable", error);
    return null;
  }
}

async function loadSnapshot(context) {
  const [overview, health, ab] = await Promise.all([
    api.get("/api/v1/overview", { days: OVERVIEW_DAYS }, { signal: context.signal }),
    fetchHealth(context),
    fetchInjectionAb(context),
  ]);
  return { overview, health, ab };
}

export async function render(context) {
  const root = el("div", { class: "va-view va-pulse" });
  current = { root, painted: false };
  root.appendChild(skeleton(4));
  try {
    const snapshot = await loadSnapshot(context);
    if (context.signal.aborted) return root;
    const data = snapshot.overview;
    let activity = { items: [], counts: {} };
    try {
      activity = await api.get("/api/v1/events", { limit: 8 }, { signal: context.signal });
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      console.warn("[atlas] pulse activity unavailable", error);
    }
    if (context.signal.aborted) return root;
    paint(root, data || {}, activity || {}, snapshot.health, snapshot.ab,
      context);
  } catch (error) {
    if (error && error.name === "AbortError") return root;
    clear(root);
    root.appendChild(errorState({
      title: "脉搏加载失败",
      error,
      onRetry: () => context.refresh(),
    }));
  }
  return root;
}

export async function refresh(context) {
  const root = current.root;
  if (!root || !root.isConnected) return;
  try {
    const snapshot = await loadSnapshot(context);
    if (context.signal.aborted) return;
    let activity = { items: [], counts: {} };
    try {
      activity = await api.get("/api/v1/events", { limit: 8 }, { signal: context.signal });
    } catch (error) {
      if (error && error.name === "AbortError") return;
      console.warn("[atlas] pulse activity unavailable", error);
    }
    if (context.signal.aborted) return;
    paint(root, snapshot.overview || {}, activity || { items: [], counts: {} },
      snapshot.health, snapshot.ab, context);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (current.painted) context.toast("脉搏刷新失败：" + String((error && (error.detail || error.message)) || "未知错误"), { tone: "warn" });
    else {
      clear(root);
      root.appendChild(errorState({ title: "脉搏加载失败", error, onRetry: () => context.refresh() }));
    }
  }
}

