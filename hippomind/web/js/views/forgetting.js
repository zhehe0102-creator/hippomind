/* ============================================================================
   HippoMind Atlas — 11 forgetting 遗忘
   Why did THIS memory lose weight, and can it still be recalled?

   Data: GET /api/v1/forgetting?days=7&limit=20

   This view is an audit, not a dashboard.  hippomind/forgetting.py opens with
   three properties it refuses to give up, and a screen that dropped them would
   be worse than no screen at all -- a tidy zero reads as "nothing was
   forgotten":

     1. zero events is a finding, not a zero: the cold population and its
        unexplained remainder are shown next to the event count, always;
     2. recoverability is conditional: a cold row has no memory_fts entry, so
        search() only reaches it through the LIKE rescue channel, which runs
        only when the hot index left the candidate budget unfilled;
     3. a row archived with a zero weight drop was evicted by capacity/age/
        retention, not decayed -- reporting it beside a decay is a category
        error.

   Those three live in principleRows(), which DERIVES each one from the numbers
   in the response instead of printing a static disclaimer: change the response
   and the verdict changes with it.  tests/test_gui_forgetting.py drives that
   function under Node with real report shapes and pins the verdicts.
   ========================================================================= */
"use strict";

import { el, clear } from "../core/dom.js";
import { api } from "../core/api.js";
import { fmtInt, fmtPct, absTime, relTime, shortId } from "../core/format.js";
import {
  badge, card, metric, meter, statRow, emptyState, errorState, skeleton,
  segmented, toggleField, copyButton, rule,
} from "../components.js";
import { barList } from "../charts.js";
import { openInspector } from "../components/inspector.js";

export const meta = {
  id: "forgetting",
  index: "11",
  title: "遗忘",
  eyebrow: "FORGETTING",
  subtitle: "为什么这条记忆被降权：原因、降幅与还能不能召回",
};

const DAY_CHOICES = [7, 30, 90, 365];
const DEFAULT_DAYS = 7;
const LIMIT_CHOICES = [10, 20, 50];
const DEFAULT_LIMIT = 20;

/* Mirrors forgetting.GONE_KIND.  top_drops carries this placeholder kind for a
   memory that no longer exists in the projection, and it is the only signal the
   wire format gives for "the row is gone, only the audit event is left".  The
   test file pins the literal against the Python constant: renaming it there
   must not silently turn a deleted memory into a 404 drawer. */
const GONE_KIND = "(记忆已不在账本)";

/* Mirrors forgetting.COVERAGE_STATE_LABELS / ARCHIVE_REASONS /
   AGE_FIELD_BY_REASON.  Duplicated because the browser cannot import the Python
   module; tests/test_gui_forgetting.py asserts each pair stays equal. */
const COVERAGE_LABELS = {
  empty: "无事可解释",
  none: "无法解释",
  partial: "部分可解释",
  complete: "完全可解释",
};

/* The rule that actually removed a row from the working set answers "why is it
   gone"; earlier weight_floor events only describe how it drifted first.  Same
   two-pass order as forgetting.decisive_age(). */
const ARCHIVE_REASONS = ["raw_retention", "age_limit"];

const AGE_FIELD_BY_REASON = {
  weight_floor: "age_days",
  raw_retention: "row_age_days",
  age_limit: "row_age_days",
  expired: "",
  capacity_overflow: "",
};

const REASON_TONES = {
  expired: "warn",
  raw_retention: "neutral",
  weight_floor: "danger",
  age_limit: "neutral",
  capacity_overflow: "accent",
  unknown: "danger",
};

let live = null;

function isAbort(error) {
  return error && error.name === "AbortError";
}

function numberOr(value, fallback) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

/* ------------------------------------------------------------ pure logic -- */

/** Tone for a coverage state.  "none" is the loud one: the ledger HAS forgotten
    things and the write side kept no reason for any of them. */
export function coverageTone(state) {
  if (state === "none") return "danger";
  if (state === "partial") return "warn";
  if (state === "complete") return "ok";
  return "neutral";
}

export function coverageLabel(state) {
  return COVERAGE_LABELS[state] || "覆盖未知";
}

/** The age that decided a row, and which field it came from.
    age === null means no reason was age-driven; the caller then prints n/a
    rather than a number that decided nothing. */
export function decisiveAge(row) {
  const reasons = Array.isArray(row && row.reasons) ? row.reasons.map(String) : [];
  const ageDriven = reasons.filter((reason) => AGE_FIELD_BY_REASON[reason]);
  const archiveDriven = ageDriven.filter((reason) => ARCHIVE_REASONS.indexOf(reason) >= 0);
  const picked = archiveDriven.length ? archiveDriven[0] : ageDriven[0];
  if (!picked) return { age: null, field: "" };
  const field = AGE_FIELD_BY_REASON[picked];
  const value = row[field];
  const age = value === null || value === undefined ? null : Number(value);
  return { age: Number.isFinite(age) ? age : null, field };
}

/** Weight LOSS as a plain magnitude, never signed: the column is 降幅, so a
    minus sign would read as a weight increase.  Mirrors forgetting._fmt_drop. */
export function fmtDrop(value) {
  if (value === null || value === undefined) return "n/a";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(3) : "n/a";
}

export function fmtWeight(value) {
  if (value === null || value === undefined) return "n/a";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(3) : "n/a";
}

function fmtAge(value) {
  if (value === null || value === undefined) return "n/a";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(1) : "n/a";
}

/**
 * The three properties the report refuses to give up, derived from the numbers
 * actually returned.  This is the heart of the view: the same response that
 * shows "0 条事件" must also say what that zero does and does not mean.
 *
 * tone is never decorative -- it states whether THIS response can carry the
 * claim it superficially looks like it carries.
 */
export function principleRows(report) {
  const source = report || {};
  const events = source.events || {};
  const coverage = source.coverage || {};
  const recall = source.recoverability || {};
  const total = numberOr(events.total, 0);
  const malformed = numberOr(events.malformed, 0);
  const cold = numberOr(coverage.cold_total_now, 0);
  const explained = numberOr(coverage.explained_cold, 0);
  const unexplained = numberOr(coverage.unexplained_cold, 0);
  const reachable = numberOr(recall.predicate_reachable, 0);
  const coldNow = numberOr(recall.cold_now, 0);
  const unchanged = numberOr(source.unchanged_weight, 0);
  const involved = numberOr(recall.memories, 0);

  const rows = [];

  /* 1. Zero events is a finding, not a zero. */
  let zeroTone = "ok";
  let zeroReading = "本窗口有事件，覆盖率按下方判定解读。";
  if (cold > 0 && total === 0) {
    zeroTone = "danger";
    zeroReading = "遗忘确实发生过，但写入侧没有留下原因：" +
      "现在读到的是一个空白，不是一个零。";
  } else if (cold > 0 && explained === 0) {
    zeroTone = "danger";
    zeroReading = "有事件，但没有一条能对上当前 cold 记忆：窗口与归档时间不重叠，" +
      "这条报告不能解释任何一条现存 cold。";
  } else if (cold === 0 && total === 0) {
    zeroTone = "neutral";
    zeroReading = "账本里没有 cold 记忆，也没有事件：无事可解释。" +
      "这是「没有可解释的对象」，不是一个「遗忘为零」的结论。";
  } else if (cold > 0 && unexplained > 0) {
    zeroTone = "warn";
    zeroReading = "只有部分 cold 有解释；其余是窗口外归档，或写入侧从未记录。";
  }
  rows.push({
    id: "zero-is-not-zero",
    tone: zeroTone,
    title: "「0 条事件」不等于「没遗忘」",
    evidence: "本窗口 " + fmtInt(total) + " 条 decay 事件（无法解析 " + fmtInt(malformed) +
      " 条）；账本当前 " + fmtInt(cold) + " 条 cold 记忆，其中 " + fmtInt(explained) +
      " 条有解释、" + fmtInt(unexplained) + " 条没有。",
    reading: zeroReading,
  });

  /* 2. Recoverability is conditional. */
  let recallTone = "neutral";
  let recallReading = "本窗口没有涉及任何记忆，可召回性无从判定。";
  if (involved > 0) {
    if (coldNow > 0) {
      recallTone = "warn";
      recallReading = "这 " + fmtInt(coldNow) + " 条没有任何 SQL 条件阻断，" +
        "但它们已不在热索引里：只有没有 active 行命中同一查询时，才会走 LIKE 救援通道被取回。" +
        "「在账本里」不等于「一次查询就能取回」。";
    } else if (reachable > 0) {
      recallTone = "ok";
      recallReading = "本窗口涉及的记忆都还在 active，没有 SQL 条件阻断召回。";
    } else {
      recallTone = "warn";
      recallReading = "本窗口涉及的记忆没有一条能通过默认召回判据，阻断原因见下方。";
    }
  }
  rows.push({
    id: "recall-is-conditional",
    tone: recallTone,
    title: "可召回是条件性的",
    evidence: "涉及 " + fmtInt(involved) + " 条；无 SQL 阻断 " + fmtInt(reachable) +
      " 条，其中已是 cold " + fmtInt(coldNow) + " 条；需 raw 开关 " +
      fmtInt(numberOr(recall.needs_raw_flag, 0)) + " 条；被拒绝 " +
      fmtInt(numberOr(recall.blocked, 0)) + " 条；记忆行已删除 " +
      fmtInt(numberOr(recall.gone, 0)) + " 条。",
    reading: recallReading,
  });

  /* 3. Zero drop is eviction, not decay. */
  rows.push({
    id: "zero-drop-is-eviction",
    tone: unchanged > 0 ? "warn" : "neutral",
    title: "降幅为 0 的归档不是衰减",
    evidence: unchanged > 0
      ? fmtInt(unchanged) + " 条记忆的权重没有下降（或不可比较）却被归档。"
      : "本窗口没有「权重未降却被归档」的行。",
    reading: unchanged > 0
      ? "它们是容量挤出（capacity_overflow）、保留期满（raw_retention）、" +
        "超过冷置年龄（age_limit）或有效期到期（expired）——决定它们下线的是容量与年龄，" +
        "不是指数衰减。把它们和衰减混在一起数，会把「装不下」误报成「不重要」。"
      : "本窗口的归档都有权重下降作为依据。",
  });

  return rows;
}

/* --------------------------------------------------------------- sections -- */

function verdictSection(report) {
  const verdict = report.verdict || {};
  const state = String(verdict.coverage_state || "empty");
  const tone = coverageTone(state);
  const coverage = report.coverage || {};
  const events = report.events || {};
  const window_ = report.window || {};

  const main = el("div", { class: "vb-fg-verdict-main" },
    el("div", { class: "eyebrow" }, "COVERAGE VERDICT"),
    el("div", { class: "vb-fg-verdict-word", dataset: { tone } }, coverageLabel(state)),
    el("p", { class: "vb-fg-verdict-headline" }, verdict.headline || "（daemon 未给出结论）"),
    el("div", { class: "vb-fg-verdict-meta" },
      el("span", {}, "窗口 " + fmtInt(report.window_days) + " 天"),
      el("span", { class: "muted", title: window_.start || "" },
        String(window_.start || "—").slice(0, 16).replace("T", " ")),
      el("span", { class: "muted" }, "→ " + String(window_.end || "—").slice(0, 16).replace("T", " ")),
      el("span", {}, "生成于 " + relTime(report.generated_at)),
      el("span", { class: "muted", title: absTime(report.generated_at, { seconds: true }) },
        absTime(report.generated_at, { seconds: true }))));

  const caveats = Array.isArray(verdict.caveats) ? verdict.caveats : [];
  const caveatList = el("div", { class: "vb-fg-caveats" },
    el("div", { class: "eyebrow" }, "本报告不作以下声称"));
  if (caveats.length) {
    for (const caveat of caveats) {
      caveatList.appendChild(el("p", { class: "vb-fg-caveat" },
        el("span", { class: "vb-fg-caveat-mark" }, "·"), el("span", {}, caveat)));
    }
  } else {
    caveatList.appendChild(el("p", { class: "vb-fg-caveat" }, "daemon 未列出不确定性。"));
  }

  const side = el("div", { class: "vb-fg-verdict-side" },
    metric({ label: "可否解释", value: verdict.explainable ? "可以" : "不可以",
      small: true, tone: verdict.explainable ? "ok" : "danger",
      hint: "只对「有事件的那部分」作证" }),
    metric({ label: "COLD 记忆", value: fmtInt(coverage.cold_total_now), small: true,
      hint: "账本当前 cold 行数，不是本窗口归档数" }),
    metric({ label: "本窗口事件", value: fmtInt(events.total), small: true,
      hint: events.truncated ? "已触达读取上限，结果不完整" : "decay 事件条数" }));

  return el("section", { class: "vb-fg-verdict", dataset: { tone }, "aria-label": "覆盖判定" },
    main, side, caveatList);
}

function principlesSection(report) {
  const host = el("div", { class: "vb-fg-principles" });
  for (const row of principleRows(report)) {
    const label = row.tone === "danger" ? "必须这样读"
      : row.tone === "warn" ? "有保留"
        : row.tone === "ok" ? "成立" : "不适用";
    host.appendChild(el("article", { class: "vb-fg-principle", dataset: { tone: row.tone } },
      el("div", { class: "vb-fg-principle-head" },
        el("span", { class: "vb-fg-principle-dot tone-" + row.tone }),
        el("h3", { class: "vb-fg-principle-title" }, row.title),
        badge(label, { tone: row.tone })),
      el("p", { class: "vb-fg-principle-evidence" }, row.evidence),
      el("p", { class: "vb-fg-principle-reading" }, row.reading)));
  }
  return card({
    eyebrow: "THREE PROPERTIES",
    title: "这份报告不作弊的三条底线",
    actions: badge("源自 hippomind/forgetting.py", { tone: "neutral" }),
    body: el("div", {},
      el("p", { class: "vb-note" },
        "遗忘审计拒绝放弃的三条性质。每一条都从下面这份响应里的数字推出，" +
        "不是一句固定免责声明——数字变了，结论会跟着变。"),
      host),
  });
}

function coverageSection(report) {
  const coverage = report.coverage || {};
  const events = report.events || {};
  const cold = numberOr(coverage.cold_total_now, 0);
  const explained = numberOr(coverage.explained_cold, 0);
  const threshold = fmtPct(numberOr(coverage.significant_drop, 0), 0);
  const reasonsSeen = Array.isArray(coverage.reasons_seen) ? coverage.reasons_seen : [];
  const missing = coverage.missing_age_field || {};
  const missingKeys = Object.keys(missing);
  const tables = Array.isArray(events.tables) && events.tables.length
    ? events.tables : ["ledger_events"];

  const body = el("div", { class: "vb-fg-coverage" },
    el("div", { class: "vb-fg-coverage-meters" },
      meter({ label: "当前 cold 中「有解释」的比例", value: explained, max: Math.max(1, cold),
        tone: coverageTone(coverage.state),
        valueText: fmtInt(explained) + " / " + fmtInt(cold) }),
      meter({ label: "本窗口读取上限使用情况", value: numberOr(events.scanned, 0),
        max: Math.max(1, numberOr(events.budget, 1)),
        tone: events.truncated ? "danger" : "accent",
        valueText: fmtInt(numberOr(events.scanned, 0)) + " / " +
          fmtInt(numberOr(events.budget, 0)) })),
    statRow([
      { label: "解释范围", value: "本窗口内降幅 ≥ " + threshold + " 或 lifecycle 变化的行" },
      { label: "低于阈值", value: "缓慢漂移按设计不落事件——不落事件不等于没衰减" },
      { label: "decay 事件（全部时间）", value: fmtInt(coverage.decay_events_all_time) +
        (coverage.decay_events_available ? "" : "（账本里从来没有）") },
      { label: "读取的表", value: tables.join(" + ") },
      { label: "本窗口触发过的规则", value: reasonsSeen.length ? reasonsSeen.join("、") : "无" },
      { label: "涉及记忆", value: fmtInt(events.memories) + " 条（多次 tick 已合并为一行）" },
    ]));

  if (events.truncated) {
    body.appendChild(el("p", { class: "vb-fg-warn" },
      "读取上限 " + fmtInt(events.budget) + " 条已用满：本报告只覆盖最近这些事件，总数可能更大。" +
      "缩小回看天数可看到完整窗口。"));
  }
  if (missingKeys.length) {
    const parts = missingKeys.map((field) => fmtInt(missing[field]) + " 条缺 " + field);
    body.appendChild(el("p", { class: "vb-fg-warn" },
      "写入侧版本较旧，部分事件缺少决策年龄：" + parts.join("；") +
      "（age_days = 距上次加权，row_age_days = 距创建，两者不可互相替代）。"));
  }
  if (numberOr(events.malformed, 0) > 0) {
    body.appendChild(el("p", { class: "vb-fg-warn" },
      fmtInt(events.malformed) + " 条 decay 事件无法解析（metadata 缺 memory_id 或不是 JSON）。" +
      "它们仍计入分母：会缩小的分母是审计开始说谎的方式。"));
  }

  return card({ eyebrow: "COVERAGE", title: "这条报告能解释多少", body });
}

function reasonsSection(report) {
  const byReason = Array.isArray(report.by_reason) ? report.by_reason : [];
  if (!byReason.length) {
    return card({
      eyebrow: "REASONS",
      title: "按原因分布",
      body: el("div", {},
        el("p", { class: "vb-note" },
          "本窗口没有任何 decay 事件，所以没有原因可列。这不是「没有遗忘」：" +
          "见上方覆盖判定与 cold 记忆总数。"),
        emptyState({ plain: true, icon: "warning", title: "原因不可查",
          hint: "事件由 lifecycle.maintain 写入；写入侧落地后本表才会出现分布。" })),
    });
  }
  const items = byReason.map((item) => ({
    label: item.label + "（" + item.reason + "）",
    value: numberOr(item.events, 0),
    tone: REASON_TONES[item.reason] || "neutral",
  }));
  const rules = el("dl", { class: "vb-fg-rules" });
  for (const item of byReason) {
    rules.appendChild(el("div", { class: "vb-fg-rule-row", dataset: { reason: item.reason } },
      el("dt", {},
        badge(item.reason, { tone: REASON_TONES[item.reason] || "neutral" }),
        el("span", { class: "vb-fg-rule-label" }, item.label)),
      el("dd", {},
        el("span", { class: "vb-fg-rule-text" }, item.rule),
        el("span", { class: "vb-fg-rule-stats" },
          fmtInt(item.events) + " 事件 · " + fmtInt(item.memories) + " 记忆 · " +
          fmtInt(item.archived) + " 归档 · 累计降幅 " + fmtDrop(item.weight_drop)))));
  }
  return card({
    eyebrow: "REASONS",
    title: "按原因分布",
    actions: badge(fmtInt(byReason.length) + " 种", { tone: "neutral" }),
    body: el("div", {},
      barList({ items, maxBars: 12, showPct: true }),
      el("p", { class: "vb-note" },
        "原因取自事件 metadata.reason，是写入侧当时的判定，不是本视图的推断。" +
        "「未标注/契约外原因」不会被折叠进相邻原因——写入侧发明的原因必须自己显形。"),
      rules),
  });
}

function dropsTable(rows, context) {
  const table = el("table", { class: "atlas-table atlas-table--dense vb-fg-drops" });
  const headRow = el("tr");
  const headers = [["记忆", null], ["kind", null], ["agent", null],
    ["降幅", "right"], ["权重", "right"], ["原因", null],
    ["决定性年龄（天）", "right"], ["最后事件", null]];
  for (const header of headers) {
    headRow.appendChild(el("th", {
      scope: "col", class: header[1] === "right" ? "is-num" : null,
    }, header[0]));
  }
  table.appendChild(el("thead", {}, headRow));

  const body = el("tbody");
  rows.forEach((row, index) => {
    const gone = row.kind === GONE_KIND;
    const decided = decisiveAge(row);
    const drop = row.drop === null || row.drop === undefined ? null : Number(row.drop);
    const zeroDrop = drop === null || drop === 0;
    const tr = el("tr", { dataset: { rowKey: String(row.memory_id || index) } });

    tr.appendChild(el("td", {},
      el("code", { class: "vb-fg-mid", title: String(row.memory_id || "") },
        shortId(row.memory_id, 10, 4))));
    tr.appendChild(el("td", {}, gone
      ? badge("已删除", { tone: "danger" })
      : document.createTextNode(String(row.kind || "—"))));
    tr.appendChild(el("td", {}, document.createTextNode(String(row.agent || "—"))));
    tr.appendChild(el("td", { class: "is-num" },
      el("span", {
        class: zeroDrop ? "vb-fg-zero-drop" : null,
        title: zeroDrop
          ? "权重未下降：容量/年龄挤出，不是衰减"
          : "窗口内累计权重损失",
      }, fmtDrop(row.drop))));
    tr.appendChild(el("td", { class: "is-num" },
      document.createTextNode(fmtWeight(row.weight_before) + " → " + fmtWeight(row.weight_after))));

    const reasonsCell = el("td");
    for (const reason of (Array.isArray(row.reasons) ? row.reasons : [])) {
      reasonsCell.appendChild(badge(reason, { tone: REASON_TONES[reason] || "neutral" }));
    }
    tr.appendChild(reasonsCell);

    tr.appendChild(el("td", { class: "is-num" },
      el("span", { title: decided.field ? "取自 " + decided.field : "该原因与年龄无关" },
        fmtAge(decided.age))));
    tr.appendChild(el("td", {},
      el("span", { class: "muted", title: absTime(row.last_at, { seconds: true }) },
        relTime(row.last_at))));

    /* A gone row has no detail endpoint: the audit event outlived the memory.
       Opening the inspector would only produce a 404 drawer. */
    if (!gone) {
      tr.classList.add("is-clickable");
      tr.setAttribute("tabindex", "0");
      const open = () => openInspector(row.memory_id, {
        signal: context.signal,
        onChanged: () => { void reload(context, { silent: true }); },
      });
      tr.addEventListener("click", (event) => {
        if (event.target instanceof Element && event.target.closest("button, a")) return;
        open();
      });
      tr.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
      });
    }
    body.appendChild(tr);
  });
  table.appendChild(body);
  return el("div", { class: "atlas-table-wrap" }, table);
}

function topDropsSection(report, context) {
  const rows = Array.isArray(report.top_drops) ? report.top_drops : [];
  const unchanged = numberOr(report.unchanged_weight, 0);

  const body = el("div", {},
    el("p", { class: "vb-note" },
      "按窗口内累计降幅排序；同一条记忆的多次 tick 已合并成一行，" +
      "权重取首尾跨度而不是把每次降幅相加。点击行打开详情抽屉。"));
  if (rows.length) {
    body.appendChild(dropsTable(rows, context));
  } else {
    body.appendChild(emptyState({
      plain: true, icon: "archive", title: "本窗口没有可列出的降权记忆",
      hint: "窗口内没有降幅事件。若账本里确有 cold 记忆，那是「没有解释」，不是「没有遗忘」。",
    }));
  }
  if (unchanged > 0) {
    body.appendChild(el("p", { class: "vb-note vb-fg-zero-note" },
      "另有 " + fmtInt(unchanged) + " 条记忆权重没有下降（或不可比较）却被归档：" +
      "它们是容量挤出、保留期满、超过冷置年龄或有效期到期，降幅为 0 所以排在末尾。" +
      "这类行不是在衰减，别和上面的降幅混读。"));
  }
  body.appendChild(el("p", { class: "vb-note" },
    "决定性年龄取「实际让这行下线的那条规则」的输入：weight_floor 用距上次加权，" +
    "age_limit / raw_retention 用距创建；expired 与 capacity_overflow 与年龄无关，显示 n/a。"));

  return card({
    eyebrow: "TOP DROPS",
    title: "降权最狠的前 " + fmtInt(report.limit) + " 条",
    actions: badge(fmtInt(rows.length) + " 行", { tone: "neutral" }),
    body,
  });
}

function recallSection(report) {
  const recall = report.recoverability || {};
  const blockers = recall.blockers || {};
  const examples = Array.isArray(recall.examples) ? recall.examples : [];
  const blockerKeys = Object.keys(blockers).sort((a, b) =>
    numberOr(blockers[b], 0) - numberOr(blockers[a], 0));

  const body = el("div", { class: "vb-fg-recall" },
    statRow([
      { label: "查询条件", value: "scope=" + (recall.scope || "shared") +
        " · require_evidence=True · as_of=现在" +
        (recall.include_raw_chat ? " · include_raw_chat=True" : "") },
      { label: "涉及的记忆", value: fmtInt(recall.memories) + " 条" },
      { label: "无 SQL 条件阻断", value: fmtInt(recall.predicate_reachable) + " 条" },
      { label: "其中现在是 cold", value: fmtInt(recall.cold_now) + " 条 ← 可召回是条件性的" },
      { label: "需再加 raw 开关", value: fmtInt(recall.needs_raw_flag) + " 条" },
      { label: "被 SQL 条件拒绝", value: fmtInt(recall.blocked) + " 条" },
      { label: "记忆行已删除", value: fmtInt(recall.gone) + " 条（只剩审计事件）" },
    ]));

  if (blockerKeys.length) {
    const list = el("div", { class: "vb-fg-blockers" },
      el("div", { class: "eyebrow" }, "阻断召回的 SQL 条件"));
    for (const key of blockerKeys) {
      list.appendChild(el("div", { class: "vb-fg-blocker" },
        el("code", { class: "vb-fg-blocker-code" }, key),
        el("span", { class: "vb-fg-blocker-count" }, fmtInt(blockers[key]))));
    }
    body.appendChild(list);
  }
  if (examples.length) {
    const list = el("div", { class: "vb-fg-blocker-examples" },
      el("div", { class: "eyebrow" }, "示例（最多 5 条）"));
    for (const example of examples) {
      list.appendChild(el("div", { class: "vb-fg-blocker-example" },
        el("code", { class: "vb-fg-mid" }, shortId(example.memory_id, 12, 4)),
        el("span", { class: "muted" },
          (Array.isArray(example.blockers) ? example.blockers : []).join(" · "))));
    }
    body.appendChild(list);
  }

  body.appendChild(rule());
  body.appendChild(el("div", { class: "vb-fg-recall-explain" },
    el("p", { class: "vb-note" },
      "降权不等于删除：cold 行的内容仍在账本里。" +
      "但「在账本里」不等于「一次查询就能取回」，实测的召回路径是："),
    el("ol", { class: "vb-fg-recall-steps" },
      el("li", {}, "cold 行已从 memory_fts 删除（维护的预期行为：冷投影不占热索引）；"),
      el("li", {}, "search() 只有在热索引没喂饱候选时，才走有界的 LIKE 救援通道；"),
      el("li", {}, "因此只有当没有 active 行命中同一查询时，cold 行才会被取回。")),
    el("p", { class: "vb-note" },
      "同一个查询若已被 active 行满足，cold 行会排不进来——这是候选生成的容量约束，" +
      "不是删除。要确保取回，用更具体的关键词，或按 memory_id 直查。")));

  return card({
    eyebrow: "RECOVERABILITY",
    title: "还能不能召回",
    actions: badge(numberOr(recall.cold_now, 0) ? "条件性可达" : "见下",
      { tone: numberOr(recall.cold_now, 0) ? "warn" : "neutral" }),
    body,
  });
}

function distributionSection(report) {
  const byAgent = Array.isArray(report.by_agent) ? report.by_agent : [];
  const byKind = Array.isArray(report.by_kind) ? report.by_kind : [];
  const archive = report.archive || {};
  const targets = Array.isArray(archive.by_target) ? archive.by_target : [];

  function tableFor(rows, keyName, title) {
    const block = el("div", { class: "vb-fg-dist-block" },
      el("div", { class: "eyebrow" }, title));
    if (!rows.length) {
      block.appendChild(el("p", { class: "vb-note" }, "本窗口无数据。"));
      return block;
    }
    const table = el("table", { class: "atlas-table atlas-table--dense" });
    table.appendChild(el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, keyName),
      el("th", { scope: "col", class: "is-num" }, "事件"),
      el("th", { scope: "col", class: "is-num" }, "记忆"),
      el("th", { scope: "col", class: "is-num" }, "归档"))));
    const body = el("tbody");
    for (const row of rows) {
      body.appendChild(el("tr", {},
        el("td", {}, String(row[keyName] || "—")),
        el("td", { class: "is-num" }, fmtInt(row.events)),
        el("td", { class: "is-num" }, fmtInt(row.memories)),
        el("td", { class: "is-num" }, fmtInt(row.archived))));
    }
    table.appendChild(body);
    block.appendChild(el("div", { class: "atlas-table-wrap" }, table));
    return block;
  }

  const archiveBlock = el("div", { class: "vb-fg-dist-block" },
    el("div", { class: "eyebrow" }, "归档去向（lifecycle 变化目标）"),
    el("p", { class: "vb-note" }, "归档事件合计 " + fmtInt(archive.total) + " 条"));
  if (targets.length) {
    const chips = el("div", { class: "vb-fg-targets" });
    for (const item of targets) {
      chips.appendChild(el("span", { class: "vb-fg-target" },
        badge("→ " + item.to_lifecycle, { tone: "neutral" }),
        el("span", { class: "vb-fg-target-count" }, fmtInt(item.events))));
    }
    archiveBlock.appendChild(chips);
  } else {
    archiveBlock.appendChild(el("p", { class: "vb-note" }, "本窗口没有归档事件。"));
  }

  return card({
    eyebrow: "DISTRIBUTION",
    title: "谁的记忆被遗忘",
    body: el("div", { class: "vb-fg-dist" },
      tableFor(byAgent, "agent", "按 agent"),
      tableFor(byKind, "kind", "按 kind"),
      archiveBlock),
  });
}

function reproduceSection(report) {
  const ledger = String(report.ledger || "");
  const rawFlag = Boolean((report.recoverability || {}).include_raw_chat);
  const cliCommand = "python3 scripts/forgetting_report.py --days " +
    fmtInt(report.window_days) + " --limit " + fmtInt(report.limit) +
    (rawFlag ? " --include-raw-chat" : "") + " --json";
  const eventCommand = 'sqlite3 "file:' + ledger + '?mode=ro" ' +
    '"SELECT occurred_at,source_agent,metadata_json FROM ledger_events ' +
    "WHERE event_type='decay' ORDER BY occurred_at DESC LIMIT 20;\"";
  const coldCommand = 'sqlite3 "file:' + ledger + '?mode=ro" ' +
    '"SELECT lifecycle,COUNT(*) FROM memories GROUP BY 1;"';

  function commandRow(label, command) {
    return el("div", { class: "vb-fg-cmd" },
      el("code", { class: "vb-fg-cmd-text", title: command }, command),
      copyButton(command, { label: label }));
  }

  return card({
    eyebrow: "REPRODUCE",
    title: "复现方式（只读）",
    body: el("div", { class: "vb-fg-repro" },
      el("p", { class: "vb-note" },
        "本视图只是 forgetting_report() 的渲染层。下面的命令会给出同一份数字——" +
        "如果这里看到的和 CLI 不一致，那是 bug，不是两种口径。"),
      commandRow("复制 CLI", cliCommand),
      commandRow("复制事件查询", eventCommand),
      commandRow("复制 cold 计数", coldCommand),
      el("p", { class: "vb-note" },
        "账本 " + (ledger || "—") +
        "（只读打开：mode=ro + PRAGMA query_only——测量生产不会改动生产）")),
  });
}

/* ------------------------------------------------------------------ paint -- */

function cacheNote(cache) {
  if (!cache || !cache.hit) {
    return "本次现测 · " + Math.round(numberOr(cache && cache.elapsed_ms, 0)) + " ms";
  }
  return "缓存命中 · " + Math.round(numberOr(cache.age_s, 0)) + " 秒前测量";
}

function toolbar(report, context) {
  const days = numberOr(report.window_days, DEFAULT_DAYS);
  const limit = numberOr(report.limit, DEFAULT_LIMIT);
  const rawOn = Boolean((report.recoverability || {}).include_raw_chat);
  const cache = report.cache || {};

  return el("div", { class: "vb-fg-toolbar" },
    el("div", { class: "vb-fg-toolbar-main" },
      el("div", { class: "vb-fg-control" },
        el("span", { class: "atlas-field-label" }, "回看天数"),
        segmented({
          label: "回看天数",
          value: String(days),
          options: DAY_CHOICES.map((value) => ({ value: String(value), label: value + " 天" })),
          onChange: (value) => apply(context, { days: Number(value) }),
        })),
      el("div", { class: "vb-fg-control" },
        el("span", { class: "atlas-field-label" }, "列出条数"),
        segmented({
          label: "降权最狠列出多少条",
          value: String(limit),
          options: LIMIT_CHOICES.map((value) => ({ value: String(value), label: String(value) })),
          onChange: (value) => apply(context, { limit: Number(value) }),
        })),
      el("div", { class: "vb-fg-control" },
        el("span", { class: "atlas-field-label" }, "原始会话"),
        toggleField({
          label: "计入 raw_chat",
          checked: rawOn,
          onChange: (checked) => apply(context, { raw: checked }),
        }))),
    el("div", { class: "vb-fg-toolbar-meta" },
      badge(cacheNote(cache), { tone: cache.hit ? "neutral" : "accent" }),
      el("span", { class: "vb-note" },
        "报告按 30 秒缓存：它描述的是按天计的衰减，不是秒级指标。")));
}

function paint(root, report, context) {
  clear(root);
  root.appendChild(toolbar(report, context));
  root.appendChild(el("div", { class: "vb-fg-body" },
    verdictSection(report),
    principlesSection(report),
    coverageSection(report),
    reasonsSection(report),
    topDropsSection(report, context),
    recallSection(report),
    distributionSection(report),
    reproduceSection(report)));
}

/* ------------------------------------------------------------- navigation -- */

/** Push the chosen window into the hash so the screen stays linkable, the same
    way recall.js does.  Defaults are omitted to keep URLs short. */
function apply(context, patch) {
  const current = readState(context);
  const next = {
    days: patch.days === undefined ? current.days : patch.days,
    limit: patch.limit === undefined ? current.limit : patch.limit,
    includeRawChat: patch.raw === undefined ? current.includeRawChat : patch.raw,
  };
  const params = {};
  if (next.days !== DEFAULT_DAYS) params.days = String(next.days);
  if (next.limit !== DEFAULT_LIMIT) params.limit = String(next.limit);
  if (next.includeRawChat) params.raw = "1";
  context.setQuery(params);
}

export function readState(context) {
  const days = Number(context.params.get("days"));
  const limit = Number(context.params.get("limit"));
  return {
    days: DAY_CHOICES.indexOf(days) >= 0 ? days : DEFAULT_DAYS,
    limit: LIMIT_CHOICES.indexOf(limit) >= 0 ? limit : DEFAULT_LIMIT,
    includeRawChat: context.params.get("raw") === "1",
  };
}

function request(state, context) {
  const params = { days: state.days, limit: state.limit };
  if (state.includeRawChat) params.include_raw_chat = "1";
  return api.get("/api/v1/forgetting", params, { signal: context.signal });
}

async function reload(context, options = {}) {
  const instance = live;
  if (!instance || instance.context !== context || instance.destroyed) return;
  instance.state = readState(context);
  if (!options.silent) {
    clear(instance.root);
    instance.root.appendChild(skeleton(6));
  }
  instance.root.classList.add("is-busy");
  try {
    const report = await request(instance.state, context);
    if (instance.destroyed || context.signal.aborted) return;
    instance.report = report;
    paint(instance.root, report, context);
    instance.painted = true;
  } catch (error) {
    if (isAbort(error) || instance.destroyed) return;
    if (instance.painted && options.silent) {
      context.toast("遗忘审计刷新失败：" +
        String((error && (error.detail || error.message)) || "未知错误"), { tone: "warn" });
      return;
    }
    clear(instance.root);
    instance.root.appendChild(errorState({
      title: "遗忘审计加载失败",
      error,
      onRetry: () => { void reload(context); },
    }));
  } finally {
    instance.root.classList.remove("is-busy");
  }
}

/* ------------------------------------------------------------------- entry -- */

export async function render(context) {
  const root = el("div", { class: "vb-view vb-forgetting" });
  const instance = {
    context, root, state: readState(context), report: null,
    painted: false, destroyed: false,
  };
  live = instance;
  context.onCleanup(() => {
    instance.destroyed = true;
    if (live === instance) live = null;
  });
  root.appendChild(skeleton(6));
  await reload(context);
  return root;
}

export async function refresh(context) {
  if (!live || live.context !== context) return;
  await reload(context, { silent: live.painted });
}
