/**
 * Deterministic out-of-process harness for the dsh-hippomind Cordis plugin.
 *
 * Usage: node dsh_plugin_harness.mjs <pluginDir> <agentConfig> <mode>
 *
 * It imports the real lib/index.js with a mock Cordis ctx, drives the same
 * event shapes the DSH host produces (agent/pre-step, session/event, effect
 * dispose), and prints one JSON summary on stdout. Python tests point the
 * agent config at a real hippomindd and assert on the summary plus the
 * ledger contents.
 */

import { pathToFileURL } from "node:url"
import { join } from "node:path"

const [pluginDir, agentConfig, mode] = process.argv.slice(2)
if (!pluginDir || !agentConfig || !mode) {
  console.error("usage: node dsh_plugin_harness.mjs <pluginDir> <agentConfig> <mode>")
  process.exit(2)
}

const logs = []
const handlers = new Map()
const effects = []
const ctx = {
  logger: {
    info: (...args) => logs.push(["info", args.map(String).join(" ")]),
    warn: (...args) => logs.push(["warn", args.map(String).join(" ")]),
  },
  on(event, handler) {
    if (!handlers.has(event)) handlers.set(event, [])
    handlers.get(event).push(handler)
  },
  effect(fn) { effects.push(fn) },
  get() { return undefined },
}

const plugin = await import(pathToFileURL(join(pluginDir, "lib", "index.js")).href)
const fastConfig = {
  agentConfig,
  requestTimeoutMs: Number(process.env.HARNESS_TIMEOUT_MS ?? 900),
  // The production plugin clamps idle detection to at least 1 second.
  // Keep the harness explicit so fast CI runners cannot finish before a sweep.
  writeBackIdleMs: Math.max(1000, Number(process.env.HARNESS_IDLE_MS ?? 1000)),
  sweepIntervalMs: Number(process.env.HARNESS_SWEEP_MS ?? 100),
}

function makeSession(overrides = {}) {
  return {
    id: overrides.id ?? "session-harness",
    header: overrides.header ?? {},
    events: [],
    surface: { nodes: new Set() },
    cwd: overrides.cwd,
    ...overrides.extra,
  }
}

function makeAgent(session, overrides = {}) {
  return { session, options: overrides.options ?? {} }
}

async function preStep(agent, messages) {
  const list = handlers.get("agent/pre-step") ?? []
  let handler = list[0]
  const next = async () => ({ kind: "enter", messages })
  return handler({ agent, signal: undefined }, next)
}

function sessionEvent(session, event) {
  for (const handler of handlers.get("session/event") ?? []) handler(session, event)
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const summary = { mode, logs, checks: {} }

try {
  plugin.apply(ctx, fastConfig)

  if (mode === "inject" || mode === "full") {
    // 1. 正常顶层请求：注入有证据的最小上下文（untrusted 包裹、消息尾追加）
    const session = makeSession()
    const userText = "记忆：继续检查 daemon API 的并发限制"
    const decision = await preStep(makeAgent(session), [
      { role: "user", source: { kind: "user" }, content: [{ type: "text", text: userText }] },
    ])
    const injection = decision.messages[decision.messages.length - 1]
    summary.checks.injected = Boolean(
      decision.kind === "enter" && decision.messages.length === 2
      && injection?.source?.plugin === "hippomind"
      && injection.content[0].text.includes("<MEMORY_DATA_UNTRUSTED>"),
    )
    summary.checks.injectionKeepsUserMessage = decision.messages[0].content[0].text === userText
    summary.checks.injectionBounded = injection.content[0].text.length <= 16000
    summary.injectionText = injection.content[0].text.slice(0, 400)

    // A normal request with no explicit marker must not perform an injection.
    const unmarked = await preStep(makeAgent(session), [
      { role: "user", source: { kind: "user" }, content: [{ type: "text", text: "普通请求，不使用记忆" }] },
    ])
    summary.checks.unmarkedSkipped = unmarked.messages.length === 1

    // 2. 子代理不注入
    const subagentSession = makeSession({ header: { origin: "subagent", parentSession: "root" } })
    const subDecision = await preStep(makeAgent(subagentSession), [
      { role: "user", source: { kind: "user" }, content: [{ type: "text", text: "记忆：子代理任务" }] },
    ])
    summary.checks.subagentSkipped = subDecision.messages.length === 1

    // 3. 已有 hippomind 注入的消息不重复注入
    const already = await preStep(makeAgent(session), [
      { role: "user", source: { kind: "user" }, content: [{ type: "text", text: userText }] },
      { role: "user", source: { kind: "plugin", plugin: "hippomind" }, content: [{ type: "text", text: "old" }] },
    ])
    summary.checks.duplicateSkipped = already.messages.length === 2

    // 4. reject 决策原样返回
    const rejected = await (handlers.get("agent/pre-step")[0])(
      { agent: makeAgent(session), signal: undefined },
      async () => ({ kind: "reject", reason: "aborted" }),
    )
    summary.checks.rejectPassthrough = rejected.kind === "reject"
  }

  if (mode === "writeback" || mode === "full") {
    // 会话事件采集 → dispose 冲洗结构化写回
    const session = makeSession({ id: "session-writeback", cwd: "/tmp/demo/hippomind-e2e" })
    sessionEvent(session, { type: "user/message", seq: 1, data: { source: { kind: "user" }, content: [{ type: "text", text: "部署 Redis 集群并验证故障转移" }] } })
    sessionEvent(session, { type: "assistant/message", seq: 2, data: { content: [{ type: "text", text: "先在 staging 部署三主三从。" }] } })
    sessionEvent(session, { type: "tool/call", seq: 3, data: { name: "bash" } })
    sessionEvent(session, { type: "assistant/message", seq: 4, data: { content: [{ type: "text", text: "决定采用 sentinel 方案，故障转移验证通过。" }] } })
    summary.checks.trackedEvents = true
  }

  if (mode === "idle") {
    const session = makeSession({ id: "session-idle", cwd: "/tmp/demo/idle-project" })
    sessionEvent(session, { type: "user/message", seq: 1, data: { source: { kind: "user" }, content: [{ type: "text", text: "整理本周的发布纪要" }] } })
    sessionEvent(session, { type: "assistant/message", seq: 2, data: { content: [{ type: "text", text: "已整理完成：共三项发布。" }] } })
    await sleep(fastConfig.writeBackIdleMs + fastConfig.sweepIntervalMs + 250)
    // 超过 writeBackIdleMs + sweepIntervalMs，避免不同 CI runner 的时序抖动
    summary.checks.idleWaited = true
  }

  if (mode === "downgrade") {
    // daemon 不可达：决策原样返回、不抛异常、耗时受限
    const started = Date.now()
    const decision = await preStep(makeAgent(makeSession()), [
      { role: "user", source: { kind: "user" }, content: [{ type: "text", text: "记忆：降级检查" }] },
    ])
    summary.checks.downgradePassthrough = decision.messages.length === 1
    summary.checks.downgradeFastMs = Date.now() - started
    summary.checks.noThrown = true
  }

  if (mode !== "idle") {
    // dispose：触发全部 pending 会话的最终写回
    for (const dispose of effects) dispose()
    summary.checks.disposed = true
  }

  // 给后台异步 ingest 留出完成时间
  await sleep(Number(process.env.HARNESS_SETTLE_MS ?? 700))
  summary.ok = true
} catch (error) {
  summary.ok = false
  summary.error = String(error && error.stack ? error.stack : error)
}

console.log(JSON.stringify(summary))
