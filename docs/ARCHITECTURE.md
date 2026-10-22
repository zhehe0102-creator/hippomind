# Architecture and Design Rationale

## Product boundary

HippoMind is a local, single-user memory hub for coding agents. It is not a cloud database, a remote authorization service, or an instruction source. The durable record is a SQLite/WAL event ledger; a Context Pack is a bounded, untrusted view derived from that record for one request.

## Request path

```text
normal user request
        |
        +-- no 记忆： marker --> no ledger read, no daemon start, no injection
        |
        +-- marker present --> native hook / DSH plugin / managed rule
                                  |
                                  v
                         localhost daemon or local CLI
                                  |
                                  v
                     scope + evidence filtering, FTS5, rank, MMR
                                  |
                                  v
                 <= 8 items, <= 16000 transport characters, 6000-token budget
                                  |
                                  v
                    nonce-wrapped untrusted reference data
```

The explicit marker is deliberately the first gate. It is parsed before creating runtime paths or contacting the daemon. Markers inside quoted lines, fenced code blocks, inline code, or without text after the colon do not trigger. This preserves a simple user interaction while preventing accidental history injection in ordinary work.

## Storage and lifecycle

`ledger_events` is append-only evidence. `memories` is a mutable retrieval projection. `memory_fts` is an incremental FTS5 index. Claims use `(subject, predicate, slot_key)` to mark superseded facts inactive; relation rows preserve causal parents until the active chain can be archived together. Structured facts include valid time and asserted time. Procedural records keep trigger patterns, preconditions, an ordered pipeline, verification assertions, and a calibration score without executing stored commands.

Raw chat is evidence only. Watchers admit user and assistant messages, filter tool/system/developer content, redact secrets, constrain size, retain raw data for a bounded period, and archive cold material as private compressed JSONL. Context Pack excludes raw chat by default.

## Retrieval contract

The retriever filters by scope, task, project, source, time, lifecycle, and evidence before scoring candidates. Keyword and FTS5 signals are combined with freshness, importance, utility, confidence, and touch count. The system keeps the latest high-confidence structured state first, groups overlapping task records, then applies MMR to reserve remaining space for distinct relevant history. An empty result contains debug reasons and source evidence instead of unrelated filler.

## Integration contract

Known runtimes have a ranked integration path: Codex uses a trusted UserPromptSubmit hook; DeepSeek Harness uses a Cordis plugin; MCP-aware agents use registered stdio profiles; managed rules are a fallback. All use the same `on`, `off`, `toggle`, and `status` injection state. Discovery is report-only for unknown agents: no unknown configuration is modified or injected into. Tokens are scoped, stored separately at mode 0600, and never embedded in rules or logs.

MCP resources are an additional explicit control surface. The active-context template, `hippomind://projects/{name}/active-context`, can be exposed by a host as a picker or search result. This follows MCP's application-driven resource model and complements, rather than replaces, the request-level marker.

## Operational model

The daemon binds only to `127.0.0.1`, validates ledger ownership, enforces token and ACL boundaries, reconstructs watcher cursors after failures, and uses bounded retry/backoff for privacy permissions. SQLite uses WAL, busy timeouts, batched writes, incremental FTS updates, pagination, caches, and checkpoints. The legacy matplotlib GUI is optional and samples graph edges incrementally so it cannot become a daemon dependency. The primary GUI is the same-origin Atlas web console served at `/ui`; its read model lives in `hippomind/gui_api.py` behind `/api/v1/*`, requires the local admin token, is bounded and read-only except audited pin/feedback operations, and the static asset server only exposes files inside `hippomind/web/` with CSP/frame-ancestors headers.

## Portability and release

Data defaults are platform-native: macOS uses `~/Library/Application Support/HippoMind`, Linux uses `$XDG_STATE_HOME/hippomind` or `~/.local/state/hippomind`, and Windows uses `%APPDATA%/HippoMind`. `HIPPOMIND_HOME` overrides all of them. Portable export carries only structured events, excluding raw/tool/system/developer/environment/command/approval records, watcher cursors, tokens, credentials, and daemon state. See `PORTABILITY.md` and `RELEASE.md` for exact transfer and publication procedures.
