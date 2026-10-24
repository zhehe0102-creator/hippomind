# Retrieval evaluation and regression gate (EVAL)

Status: **active** — this is the pass/fail gate for retrieval quality against the
production ledger. It replaces the 10-query smoke benchmark that measured only
term coverage and could not distinguish "the fact is reachable" from "some word
appeared somewhere".

Owned files: `scripts/eval_memory.py`, `tests/test_eval_memory.py`, this document.
Core, daemon, transcripts, distill and GUI code are not modified by the eval.

## Quick start

```bash
# Run the labelled set against the production ledger (default path).
python3 scripts/eval_memory.py

# Machine-readable report (full per-query detail + gate verdict).
python3 scripts/eval_memory.py --json

# Report metrics but always exit 0 (useful while investigating a regression).
python3 scripts/eval_memory.py --no-gate

# Evaluate an exported/custom labelled set, or a ledger snapshot.
python3 scripts/eval_memory.py --queries output/eval/labelled-queries.json
python3 scripts/eval_memory.py --ledger /path/to/hippomind.sqlite

# Retrieve more/fewer results per query (default k=8).
python3 scripts/eval_memory.py -k 5

# Export the built-in labelled set for editing.
python3 scripts/eval_memory.py --dump-queries /tmp/queries.json

# Include ledger size/claim counts in the report (adds one ~250 ms stats pass).
python3 scripts/eval_memory.py --ledger-stats --json
```

Exit codes: `0` gate passed, `1` gate failed, `2` usage/input error (bad query
file, missing ledger). A missing ledger is never created implicitly.

## What the gate measures

Each query declares expected evidence as a list of substrings (`expect`). A
retrieved memory is **relevant** when *all* expected substrings appear in its
rendered text — `content` plus the structured `metadata.context` fields the
Context Pack exposes (`goal`, `progress`, `key_decisions`, `related_files`,
`blockers`, `next_steps`, `preferences`). Matching is case-insensitive.

Including context is deliberate: a consolidated memory may keep the evidence in
`context.key_decisions` while its `content` is only a summary. A user of the
memory system receives both, so the eval scores what retrieval actually
delivers.

| metric | definition |
| --- | --- |
| `recall_at_k` | fraction of queries with at least one relevant hit in the top k |
| `mrr` | mean reciprocal rank of the first relevant hit (0 when none) |
| `duplicate_rate` | mean fraction of top-k results whose whitespace/case-normalised `content` repeats another result |
| `latency_p50_ms` / `latency_p95_ms` | per-query `ledger.search` wall time, linear-interpolated percentiles |
| `term_recall_at_k` | secondary: fraction of expected substrings present anywhere in the top k |
| `empty_rate` | secondary: fraction of queries that returned no hits |

`duplicate_rate` is computed over stored `content`, not rendered text: it
tracks duplicate flooding in the projection rather than repeated context text.

## Labelled query set

The built-in set has **46 queries** across five categories, grounded in the
production ledger/claims snapshot of 2026-10-23:

| category | queries | what it covers |
| --- | --- | --- |
| `hippomind` | 16 | system facts and design decisions (theme, storage, schema, architecture, security, roadmap) |
| `android_ops` | 16 | netproxy / Zygisk / ADB / GhostSam operational lessons |
| `project` | 8 | other project decisions (RelayDesk, Codex compaction, Orbit, StoryFoundry, remote-mic, local models) |
| `project_filtered` | 3 | queries that must respect a `project` filter (`hippomind-e2e`, `outside-of-project`, `hippomind`) |
| `english` | 3 | English-language queries over bilingual memories |

Every entry records `source_ids` (memory ids the expectation was grounded in)
so the set can be audited against the ledger. The canonical set lives in
`scripts/eval_memory.py`; `output/eval/labelled-queries.json` is a dumped copy
(the `output/` tree is gitignored, so the embedded copy is the source of truth).

Query style is keyword-oriented on purpose. The distilled rows in the ledger
store concatenated claim *values* without their subject/predicate context, so a
natural-language question whose words do not occur in the summary cannot reach
it through the current lexical retriever. Queries therefore use vocabulary that
actually exists in the target memory; a handful intentionally include the
answer token (for example the theme query) because that is how a user searches
a keyword index. This is a property of the current retriever, not of the gate.

## Gate thresholds

Defaults keep a safety margin under the measured baseline and can be overridden
per run:

| flag | default | meaning |
| --- | --- | --- |
| `--min-recall` | `0.90` | minimum `recall_at_k` |
| `--min-mrr` | `0.75` | minimum `mrr` |
| `--max-duplicate-rate` | `0.10` | maximum mean duplicate rate |
| `--max-p95-ms` | `250` | maximum p95 search latency |
| `--min-queries` | `30` | the labelled set must not shrink silently |

The gate prints `gate: PASS` or `gate: FAIL` with each failed threshold, and the
process exits non-zero on failure so it can gate a release/refactor step.

## Current baseline (2026-10-23)

Production ledger at baseline run: **260,663 memories** (254,515 raw chat),
9,807 active, **576 claims** (481 active, 57 conflict), schema v8, ~5.3k
FTS-indexed rows. The daemon keeps writing, so counts drift; treat this as the
snapshot the thresholds were calibrated against.

```
queries=46 k=8 recall@8=1.000 mrr=0.915 duplicate_rate=0.016
term_recall=1.000 empty=0.000 p50=5.1ms p95=12.5ms
gate: PASS  (min_recall@8=0.90, min_mrr=0.75,
             max_duplicate_rate=0.10, max_p95=250.0ms, min_queries=30)
```

| category | n | recall@8 | MRR | duplicate rate |
| --- | --- | --- | --- | --- |
| `hippomind` | 16 | 1.000 | 0.911 | 0.016 |
| `android_ops` | 16 | 1.000 | 0.927 | 0.016 |
| `project` | 8 | 1.000 | 0.917 | 0.016 |
| `project_filtered` | 3 | 1.000 | 1.000 | 0.000 |
| `english` | 3 | 1.000 | 0.778 | 0.042 |
| **overall** | **46** | **1.000** | **0.915** | **0.016** |

All 46 queries currently hit, but six land below rank 1, which is where MRR
headroom comes from: `hippomind-actr` (rank 3), `hippomind-vector-status` (4),
`zygisk-next-version` (3), `zygisk-root` (2), `relaydesk-effort` (3),
`english-storyfoundry` (3). A change that only reshuffles ranks is visible in
MRR before recall breaks.

For comparison, the superseded 10-query smoke harness reported term coverage
0.650 and MRR 0.800 on 2026-10-23; its numbers are not comparable to the
labelled metrics because relevance is now defined per query rather than as
"expected word appears somewhere in the result list".

Baseline artifacts (local, gitignored):

- `output/eval/baseline-2026-10-23.json` — full JSON report with per-query detail
- `output/eval/baseline-2026-10-23.txt` — human-readable report
- `output/eval/labelled-queries.json` — dumped labelled set

## Query file schema

`--queries` accepts a JSON array. `id` is optional (defaults to the query text)
but must be unique; `expect` is required and must be non-empty; `project` and
`scope` are optional filters passed to `ledger.search`.

```json
[
  {
    "id": "zygisk-next-version",
    "category": "android_ops",
    "query": "Zygisk Next 最新发布版本",
    "expect": ["v1.5.2"],
    "project": "",
    "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"]
  }
]
```

`validate_queries()` rejects malformed sets (missing/blank query, empty or
non-string expectations, duplicate ids) with exit code 2.

## Refreshing the labelled set

The set is a snapshot of what the ledger currently knows. Refresh it when the
ledger is intentionally pruned/superseded or when retrieval changes enough that
the expectations should be re-grounded:

1. Read active claims: `ledger.list_claims(state="active")` (or SQL on
   `memory_claims` joined with `memories`).
2. For each candidate claim, write a keyword query from its
   `subject` + `predicate` and take a distinctive fragment of its value as
   `expect`.
3. Verify the expectation is actually reachable: run the query and confirm at
   least one top-k memory renders the expected substring; record the memory id
   in `source_ids`.
4. Keep at least 30 queries and cover the important categories (system facts,
   operational lessons, project decisions, filtered queries, English).
5. Export with `--dump-queries`, curate the JSON, then run
   `python3 scripts/eval_memory.py --queries <file>` and re-baseline the
   thresholds with a margin under the new measured values.
6. Update the baseline table above and `DEFAULT_THRESHOLDS` in
   `scripts/eval_memory.py`.

## Limitations

- **Lexical retrieval only.** HippoMind still uses FTS5/LIKE plus heuristics;
  there is no embedding/reranker yet. A query whose vocabulary does not overlap
  the stored memory cannot retrieve it, and this eval faithfully reflects that.
- **Substring relevance is a proxy.** It proves expected evidence is reachable
  in the top k; it does not judge answer correctness, ordering quality beyond
  MRR, or whether the memory is stale.
- **Production drift.** The daemon captures/distills continuously; a query can
  start hitting a newer consolidated memory. Recall may improve without code
  changes. Record `--ledger-stats` when publishing a baseline.
- **Latency is machine-specific.** The p95 gate guards against gross
  regressions on this Mac; it is not a portable benchmark.
- **Not part of CI.** It needs the production ledger, so `tests/test_eval_memory.py`
  covers the metric math with temporary ledgers instead. Run the gate manually
  before release/refactor steps that touch retrieval, ranking, FTS projection,
  claims or consolidation.

## Related work

- `tests/test_eval_memory.py` — 19 tests for percentiles, relevance, MRR,
  duplicate rate, query validation, gate verdicts, CLI exit codes and the
  embedded set.
- `hippomind/retrieval_eval.py` + `tests/test_retrieval_eval.py` — synthetic
  40-case Chinese and 32-case English QA suites. They test retrieval semantics
  in isolation; this production eval tests the real ledger.
- `REFACTOR_REPORT_ROUND4.md` — original gap: "the 10-query harness is a smoke
  benchmark, not a labelled eval set".
