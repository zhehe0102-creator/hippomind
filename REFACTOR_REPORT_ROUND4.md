# Round 4 — Automatic Memory Core (v2)

Date: 2026-10-23
Goal: move HippoMind from a marker-driven archive to an automatic memory loop.

## What landed

### 1. Automatic capture
- `hippomind/transcripts.py`: incremental Claude Code / Codex JSONL capture,
  per-file cursors in `source_cursors`, bounded batches, source health.
- `hippomind/gemini_capture.py`: incremental Gemini/Antigravity
  `conversation_summaries.db` capture (title/preview/project/agent/step count).
- daemon capture loop every 60s; `/stats.health.capture` exposes files,
  messages, inserted, duplicates, errors.

### 2. Automatic distillation
- `hippomind/distill.py`: Claude Code print-mode extraction of
  subject/predicate/slot_key/value/confidence claims, deterministic fallback,
  evidence-linked distilled memories, idempotent `distill:<memory_id>` cursors.
- Claims are deduplicated per memory before ingest (ledger unique key).
- Same-subject distilled memories get `related_to` causal edges.
- Status-like conflicts are auto-resolved by newest `asserted_at` when
  confidence is close; other conflicts stay in the review queue.
- Claim states now converge with memory quality/lifecycle via
  `repair_claim_states`; production health shows 0 dangling claims.

### 3. Retrieval / association
- `ContextPackBuilder` adds a bounded associative expansion through shared
  claim subjects (max 8, score 0.35), so the graph participates in retrieval.
- `scripts/eval_memory.py`: repeatable 10-query smoke benchmark.
  Current baseline: term_recall 0.700, MRR 0.900, duplicate 0.025,
  p50 15.5ms, p95 34.2ms.

### 4. Markerless injection
- Codex managed hook uses `--trigger-mode always`.
- DSH plugin patch uses `triggerMode: always`.
- MCP Agent rules default to `--trigger-mode always`; `记忆：` remains a
  force-query marker.
- Health page shows `capture` and `distill` checks.

### 5. Install-and-forget
- `scripts/install-autostart.sh`: copies the package to internal-disk
  `~/Library/Application Support/HippoMind/runtime` and installs a LaunchAgent
  (external-volume TCC workaround). The ledger is already internal.
- Verified: launchd PID runs from the internal runtime; capture/distill loops
  are live after login-style start.
- `HippoMind Atlas.app` remains the manual UI entry.

## Evidence (production, 2026-10-23)
- 656 tests OK; lint gate 90/90.
- Production ledger: 260,401 memories (254,479 raw), 192 distilled memories,
  437 claims (373 active, 39 conflict), 7 relations, 0 dangling claims.
- Capture: ~59 JSONL files / 100-message cycles; Gemini summary DB integrated.
- Distillation: 16 candidates / 5-minute cycle; Claude extractor works under
  launchd after adding the node runtime to the extractor PATH.
- Autostart: `gui/501/com.hippomind.daemon` running from internal runtime.

## What still blocks a true 95
1. No real semantic embedding/reranker yet: retrieval is FTS/LIKE + character
   n-gram. Need bge/mxbai/nomic local embeddings and a reranker.
2. Capture is complete for Claude/Codex/DSH + Gemini summaries; Cursor/Pi still
   rely on MCP/rules or their own storage.
3. Auto-injection has no score-threshold relevance gate or per-injection trace.
4. Distillation throughput is ~16 records/5min; 254k raw history needs
   session-level summarization for full backfill.
5. The 10-query harness is a smoke benchmark, not a labelled 95-point eval set.
6. Complex conflicts still require review; only clear/status conflicts auto-win.

## Next
- Add local embedding provider + hybrid reranker, then re-run eval.
- Add Cursor/Pi native capture where storage permits.
- Add injection trace + relevance threshold; expose in GUI.
- Build a labelled 50–100 query eval set and gate changes on it.
