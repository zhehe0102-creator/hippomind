# Architecture and Design Rationale

## Product boundary

HippoMind is a local, single-user memory hub for coding agents. It is not a cloud database, a remote authorization service, or an instruction source. The durable record is a SQLite/WAL event ledger; a Context Pack is a bounded, untrusted view derived from that record for one request.

## Request path

```text
normal user request
        |
        +-- injection state off --> no injection (collection may continue)
        |
        +-- injection state on  --> native hook / DSH plugin / managed rule
        |                          (auto-context, trigger-mode always)
        |                          |
        |                          v
        |                 localhost daemon or local CLI
        |                          |
        |                          v
        |             scope + evidence filtering, FTS5 + optional vector/entity, rank, MMR
        |                          |
        |                          v
        |     <= 8 items, <= 16000 transport characters, 6000-token budget
        |                          |
        |                          v
        |             escaped, nonce-wrapped untrusted reference data
        |
        +-- explicit "记忆：" query --> same pipeline, but the query is forced
                                         (trigger-mode anywhere; still parsed
                                         before runtime paths, and never fires
                                         inside quoted lines/fenced code)
```

Injection is **on by default per agent** for the installed integrations, so an
ordinary request prefetches the minimal evidence context without a marker. The
`记忆：` marker is not a global gate any more: it only forces the query for that
turn, and it remains parsed before any runtime path is created, so quoted or
fenced occurrences never trigger a lookup accidentally. A disabled agent still
collects evidence but receives an empty Context Pack until it is enabled again.

A short follow-up (继续/同上, a bare acknowledgement, or an otherwise content-free
request under 16 characters) is anchored to the most recent real user message in
the same session, bounded to the last two messages and a 180-minute window,
before retrieval. This is a retrieval-only rewrite: non-follow-up queries take
the identical path and produce byte-identical packs, and a short request that
still carries its own content words (跑测试/部署/回滚) is deliberately *not*
anchored. Queries with content words keep a strict original-token overlap guard,
and `as_of` historical slices never anchor because a session window comes from
now. Disable per process with `HIPPOMIND_QUERY_CONTEXT=0` or per request with
`options.query_context=false`; the anchor query itself is a bounded read-only
SELECT over the last 30 user raw-chat rows (p95 under 1 ms in the fixture).

## Storage and lifecycle

`ledger_events` is append-only evidence. `memories` is a mutable retrieval projection. `memory_fts` is an incremental FTS5 index. Claims use `(subject, predicate, slot_key)` to mark superseded facts inactive; relation rows preserve causal parents until the active chain can be archived together. Structured facts include valid time and asserted time; claim identity is versioned (v3) so an explicit source event id distinguishes repeated wording, while replay of the same event stays idempotent. Procedural records keep trigger patterns, preconditions, an ordered pipeline, verification assertions, and a calibration score without executing stored commands.

Raw chat is evidence only. Watchers admit user and assistant messages, filter tool/system/developer content, redact secrets, constrain size, retain raw data for a bounded period, and archive cold material as private compressed JSONL. Context Pack excludes raw chat by default.

## Retrieval contract

The retriever filters by scope, task, project, source, time, lifecycle, and evidence before scoring candidates. Keyword and FTS5 signals are combined with freshness, importance, utility, confidence, and touch count. The system keeps the latest high-confidence structured state first, groups overlapping task records, then applies MMR to reserve remaining space for distinct relevant history. An empty result contains debug reasons and source evidence instead of unrelated filler.

## Integration contract

Known runtimes have a ranked integration path: Codex uses a trusted UserPromptSubmit hook; DeepSeek Harness uses a Cordis plugin; MCP-aware agents use registered stdio profiles; managed rules are a fallback. All use the same `on`, `off`, `toggle`, and `status` injection state. Discovery is report-only for unknown agents: no unknown configuration is modified or injected into. Tokens are scoped, stored separately at mode 0600, and never embedded in rules or logs.

MCP resources are an additional explicit control surface. The active-context template, `hippomind://projects/{name}/active-context`, can be exposed by a host as a picker or search result. This follows MCP's application-driven resource model and complements, rather than replaces, the request-level marker.

## Operational model

The daemon binds only to `127.0.0.1`, validates ledger ownership, enforces token and ACL boundaries, reconstructs watcher cursors after failures, and uses bounded retry/backoff for privacy permissions. All authenticated write verbs are downgraded in `--read-only` mode, and a read-only daemon additionally opens the ledger through `mode=ro` plus `PRAGMA query_only=ON`, skips schema migration/WAL maintenance/raw purge, and starts no background write loop (watcher, discovery auto-install, capture, distill, vector refresh, sleep consolidation, metrics trend); only pure-read index builds keep running. `/health` is the only unauthenticated HTTP endpoint. SQLite uses WAL, busy timeouts, batched writes, incremental FTS updates, pagination, caches, and checkpoints. The legacy matplotlib GUI is optional and samples graph edges incrementally so it cannot become a daemon dependency. The primary GUI is the same-origin Atlas web console served at `/ui`; its read model lives in `hippomind/gui_api.py` behind `/api/v1/*`, requires the local admin token, is bounded and read-only except audited pin/feedback operations, and the static asset server only exposes files inside `hippomind/web/` with CSP/frame-ancestors headers.

## Portability and release

Data defaults are platform-native: macOS uses `~/Library/Application Support/HippoMind`, Linux uses `$XDG_STATE_HOME/hippomind` or `~/.local/state/hippomind`, and Windows uses `%APPDATA%/HippoMind`. `HIPPOMIND_HOME` overrides all of them. Portable export carries only structured events, excluding raw/tool/system/developer/environment/command/approval records, watcher cursors, tokens, credentials, and daemon state. See `PORTABILITY.md` and `RELEASE.md` for exact transfer and publication procedures.
