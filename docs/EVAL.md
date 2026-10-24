# Retrieval evaluation and regression gate (EVAL)

Status: **active** — this is the pass/fail gate for retrieval quality against the
production ledger. It replaces the 10-query smoke benchmark that measured only
term coverage and could not distinguish "the fact is reachable" from "some word
appeared somewhere".

It runs **two** labelled sets. The original keyword set is a regression detector
(recall@8 1.000 — it cannot see a semantic change). The semantic set asks the
questions the keyword set cannot: the same facts, worded differently. That set
reports **recall@8 ≈ 0.24**, which is the honest size of the current gap and the
number semantic work should move.

Owned files: `scripts/eval_memory.py`, `tests/test_eval_memory.py`, this document.
Core, daemon, transcripts, distill and GUI code are not modified by the eval.

## Quick start

```bash
# Run the keyword set against the production ledger (default path, default set).
python3 scripts/eval_memory.py

# Run the paraphrase set (measures the real cross-vocabulary gap).
python3 scripts/eval_memory.py --set semantic

# Run both, with a combined gate that needs both to pass.
python3 scripts/eval_memory.py --set all

# Machine-readable report (full per-query detail + gate verdict).
python3 scripts/eval_memory.py --set all --json

# Report metrics but always exit 0 (useful while investigating a regression).
python3 scripts/eval_memory.py --no-gate

# Evaluate an exported/custom labelled set, or a ledger snapshot.
python3 scripts/eval_memory.py --queries output/eval/labelled-queries.json
python3 scripts/eval_memory.py --ledger /path/to/hippomind.sqlite

# Retrieve more/fewer results per query (default k=8).
python3 scripts/eval_memory.py -k 5

# Export the built-in labelled set for editing (--set all writes both, keyed by
# set name; a single set writes a plain array).
python3 scripts/eval_memory.py --dump-queries /tmp/queries.json
python3 scripts/eval_memory.py --set all --dump-queries /tmp/all.json

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

## Labelled query sets

There are **two** built-in labelled sets, selected with `--set`. They measure
two different capabilities and are reported separately on purpose.

| `--set` | queries | what it measures | role |
| --- | --- | --- | --- |
| `keyword` (default) | 46 | Can the retriever find a memory when the query uses vocabulary that literally exists in it? | **Regression detector** — near-saturated at recall 1.000 |
| `semantic` | 53 | Can the retriever find a memory when the query is worded *differently* from the stored text? | **Capability measure** — the real gap is visible here |
| `all` | 99 | Both, with a combined gate | Full picture |

`--set all` prints both sets, prints a `combined` summary, and passes only if
**both** gates pass; failures are prefixed with the set name so a passing set
cannot mask a failing one.

### keyword set (46 queries)

Grounded in the production ledger/claims snapshot of 2026-10-23:

| category | queries | what it covers |
| --- | --- | --- |
| `hippomind` | 16 | system facts and design decisions (theme, storage, schema, architecture, security, roadmap) |
| `android_ops` | 16 | netproxy / Zygisk / ADB / GhostSam operational lessons |
| `project` | 8 | other project decisions (RelayDesk, Codex compaction, Orbit, StoryFoundry, remote-mic, local models) |
| `project_filtered` | 3 | queries that must respect a `project` filter (`hippomind-e2e`, `outside-of-project`, `hippomind`) |
| `english` | 3 | English-language queries over bilingual memories |

Query style is keyword-oriented **by construction**. The distilled rows in the
ledger store concatenated claim *values* without their subject/predicate
context, so a natural-language question whose words do not occur in the summary
cannot reach it through a lexical retriever. Queries therefore use vocabulary
that actually exists in the target memory; a handful intentionally include the
answer token (for example the theme query) because that is how a user searches
a keyword index. This is a property of the current retriever, not of the gate —
and it is exactly why the set is near-saturated and cannot be used to judge a
semantic change.

## Semantic query set

### Why it exists

The keyword set reports **recall@8 = 1.000**. That number is true and useless
for deciding whether retrieval *understands* a request: every query already
shares vocabulary with its target, so a purely lexical retriever scores
perfectly. Any semantic improvement — embeddings, query expansion, a reranker —
can only show up there as "same or worse", which makes the set incapable of
answering the question it is being asked.

The semantic set closes that hole by asking the opposite question: *given a
memory, can a differently-worded request still reach it?* Every query is a
natural paraphrase that deliberately avoids the target's wording.

The honest answer, measured on the 2026-10-24 production ledger:

```
keyword  (46 queries)  recall@8 1.000  mrr 0.866
semantic (53 queries)  recall@8 0.207-0.283  mrr 0.179-0.261
```

**Roughly 3 in 4 paraphrased questions cannot reach the memory that answers
them.** That is the real gap the keyword set was hiding, and it is reported as
a range because the ledger is live (see "Drift" below).

### Composition

| category | queries | what it covers |
| --- | --- | --- |
| `hippomind` | 16 | memory-system design facts asked in user language (safety, token budget, storage, cross-agent, benchmark, recall defects) |
| `android_ops` | 14 | Zygisk / ADB / GhostSam operational lessons asked as symptoms ("手机连上了但是列表里没有设备") |
| `project` | 11 | model quantization, app crashes, context windows, per-session model choice, novel tool progress |
| `ops` | 7 | autostart, external-disk placement, prefix caching, tool registration, build/test safety |
| `english` | 5 | English paraphrases over bilingual memories |

The distribution is deliberately weighted towards `hippomind` and
`android_ops` because those are where the ledger actually holds distilled
facts; the other categories keep the set from over-fitting one project.

### Labelling rules

Every entry carries `id`, `category`, `query`, `expect`, `source_ids`,
`note` and `expect_hits`. Four rules are enforced by
`tests/test_eval_memory.py`, because violating any of them makes the metric
lie rather than merely score low:

1. **`expect` must literally exist in the target memory.** Verify it with SQL
   against the production ledger *before* writing the query. An invented
   expectation makes the query unhittable forever, and the resulting "miss" is
   a labelling bug that looks exactly like a retrieval failure. This is the
   trap that produced `KeepAlive` and `superseded` in the first draft — two
   plausible words that occur nowhere in the ledger.
2. **`expect` must NOT appear in the query.** If it does, the query is a
   keyword query wearing a disguise and measures nothing new. Tested by
   `test_every_query_is_a_paraphrase_not_a_keyword_echo`.
3. **`source_ids` must record the active+accepted memories the expectation
   was grounded in**, so a reviewer can re-derive the label later.
4. **`expect_hits` must be ≥ 1.** It records how many ledger rows contained
   the expectation at labelling time; `0` means the label was never grounded.
   A very large value (say > 100) means the expectation is not distinctive and
   the query will be easy for the wrong reason.

Verification query used for each candidate (run read-only):

```bash
sqlite3 "file:$HOME/Library/Application Support/HippoMind/hippomind.sqlite?mode=ro" "
  SELECT memory_id FROM memories
  WHERE raw_chat=0 AND lifecycle='active' AND quality_state='accepted'
    AND content LIKE '%<expect>%';"
```

Matching at eval time is case-insensitive over rendered text (content plus
`metadata.context`), so the check is a lower bound; the labelling pass used the
same rendered-text definition as `render_hit()` to avoid context-only surprises.

### Drift

The semantic set runs against the **live production ledger**, which the daemon
keeps writing to. Over 34 consecutive samples the hit count moved between 11 and
15 of 53 (recall 0.207-0.283, MRR 0.179-0.261) with no code change: new
consolidated memories enter the top-k and displace older hits. Two consequences:

- `SET_BASELINE["semantic"]` records a **range**, not a point.
- The gate floor (`min_recall 0.17`, `min_mrr 0.14`) sits **below the low end**
  of the observed range, so production drift alone cannot fail the gate. Only a
  real regression can. When the gap genuinely closes, raise the floor — that is
  the ratchet.
- The duplicate-rate ceiling is deliberately loose (`0.35` vs `0.10` for the
  keyword set). A miss on this set usually means the retriever fell back to one
  broad memory repeated across several ranks, so duplicate rate tracks recall
  inversely and moves with the ledger (observed 0.007-0.163). It is a
  gross-regression guard, not a quality bar.

The keyword set is stable in comparison (1.000 / 0.865-0.867 across runs) because
its targets are long-lived facts that new memories rarely displace.

### Refreshing the semantic set

Repeat when the ledger is pruned, when memories are superseded in bulk, or when
retrieval changes enough that the expectations should be re-grounded:

1. Pick candidate facts from active+accepted memories
   (`ledger.list_claims(state="active")` or SQL on `memories`).
2. Write the query **as a user would ask it**, without reusing the target's
   vocabulary.
3. Choose a distinctive `expect` fragment of the target memory and verify it
   with the SQL above. Record the row count as `expect_hits`.
4. Record the grounding memory ids in `source_ids`.
5. Run `python3 scripts/eval_memory.py --set semantic` and re-baseline
   `SET_BASELINE["semantic"]` plus `SEMANTIC_THRESHOLDS` with a margin below
   the measured range.
6. Update this document's numbers.

`--dump-queries` exports the active set (`--set all` writes an object keyed by
set name; a single set writes a plain array).

## Gate thresholds

Each set has its **own** thresholds. Defaults keep a safety margin under that
set's measured baseline and can be overridden per run:

| flag | `keyword` default | `semantic` default | meaning |
| --- | --- | --- | --- |
| `--min-recall` | `0.90` | `0.17` | minimum `recall_at_k` |
| `--min-mrr` | `0.75` | `0.14` | minimum `mrr` |
| `--max-duplicate-rate` | `0.10` | `0.35` | maximum mean duplicate rate |
| `--max-p95-ms` | `250` | `400` | maximum p95 search latency |
| `--min-queries` | `30` | `40` | the labelled set must not shrink silently |

The semantic numbers are **not** a quality target; they are a ratchet floor set
just under the currently measured gap. Passing them means "the gap did not get
worse", not "retrieval is good". Raising them is the point of any semantic work.

With `--set all`, both sets are evaluated and the combined gate passes only if
every set passes; failures are prefixed with the set name.

`--mode pack` raises the p95 ceiling to at least 600 ms for both sets: pack mode
adds grouping, MMR, budget rendering and the relevance gate on top of search, so
it is legitimately slower, and the real constraint is the host injection timeout
(DSH 700 ms, Codex hook 12 s).

The gate prints `gate: PASS` or `gate: FAIL` with each failed threshold, and the
process exits non-zero on failure so it can gate a release/refactor step.

## Current baselines

The daemon keeps writing, so counts drift; treat these as the snapshots the
thresholds were calibrated against.

### keyword set — 2026-10-23

Production ledger at baseline run: **260,663 memories** (254,515 raw chat),
9,807 active, **576 claims** (481 active, 57 conflict), schema v8, ~5.3k
FTS-indexed rows.

```
set=keyword queries=46 k=8 recall@8=1.000 mrr=0.867 duplicate_rate=0.016
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

All 46 queries hit, but six land below rank 1, which is where MRR headroom comes
from: `hippomind-actr` (rank 3), `hippomind-vector-status` (4),
`zygisk-next-version` (3), `zygisk-root` (2), `relaydesk-effort` (3),
`english-storyfoundry` (3). A change that only reshuffles ranks is visible in
MRR before recall breaks.

### semantic set — 2026-10-24

Measured on a production ledger of ~266.6k memories (1,633 active+accepted
structured rows). **The headline is the gap, not the gate:**

```
set=keyword  queries=46 k=8 recall@8=1.000 mrr=0.866
set=semantic queries=53 k=8 recall@8=0.207-0.283 mrr=0.179-0.261
gate: PASS  (both sets, each against its own thresholds)
```

| category | n | recall@8 | MRR |
| --- | --- | --- | --- |
| `hippomind` | 16 | 0.125 | 0.094 |
| `android_ops` | 14 | 0.214 | 0.214 |
| `project` | 11 | 0.455 | 0.455 |
| `ops` | 7 | 0.286 | 0.214 |
| `english` | 5 | 0.400 | 0.267 |
| **overall** | **53** | **~0.24** | **~0.21** |

Category numbers are a single representative run; per-query results are not
stable (see "Drift"). Two observations that matter more than the totals:

- **`hippomind` is the worst category (0.125)** even though it is the
  best-covered topic in the ledger. Its memories are dense comma-joined claim
  values ("纸本；SQLite；mac-mini；6000；…"), so a user question about *one* of
  those facts has almost no lexical surface to match against.
- **`project` is the best (0.455)** because project memories are prose
  summaries that repeat their own keywords in full sentences.
- **12 of 53 queries always hit, 4 are flaky, 37 never hit.** The flaky ones
  (`s-swapalgo`, `s-terminalbounce`, `s-uimoved`, `s-uptime`) land on
  ranks 8-12 and flip in and out of the top-8 window as new memories arrive.

For comparison, the superseded 10-query smoke harness reported term coverage
0.650 and MRR 0.800 on 2026-10-23; its numbers are not comparable to the
labelled metrics because relevance is now defined per query rather than as
"expected word appears somewhere in the result list".

Baseline artifacts (local, gitignored):

- `output/eval/baseline-2026-10-23.json` — keyword-set JSON report with per-query detail
- `output/eval/baseline-2026-10-23.txt` — human-readable report
- `output/eval/labelled-queries.json` — dumped keyword set
## Query file schema

`--queries` accepts a JSON array. `id` is optional (defaults to the query text)
but must be unique; `expect` is required and must be non-empty; `project` and
`scope` are optional filters passed to `ledger.search`; `source_ids` and
`expect_hits` are optional annotation metadata carried through for auditing.

```json
[
  {
    "id": "zygisk-next-version",
    "category": "android_ops",
    "query": "Zygisk Next 最新发布版本",
    "expect": ["v1.5.2"],
    "project": "",
    "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"],
    "expect_hits": 2
  }
]
```

`validate_queries()` rejects malformed sets (missing/blank query, empty or
non-string expectations, duplicate ids, non-integer or negative `expect_hits`)
with exit code 2.

`--queries` overrides the built-in set for a single `--set keyword|semantic` run
and cannot be combined with `--set all` (ambiguous: which set would the file
replace?); that combination exits 2.

## Refreshing the keyword set

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

For the semantic set, follow "Refreshing the semantic set" above instead.

## Limitations

- **Lexical retrieval only.** HippoMind still uses FTS5/LIKE plus heuristics;
  there is no embedding/reranker yet. A query whose vocabulary does not overlap
  the stored memory cannot retrieve it, and **both** sets faithfully reflect
  that — the semantic set exists precisely to make it visible.
- **Substring relevance is a proxy.** It proves expected evidence is reachable
  in the top k; it does not judge answer correctness, ordering quality beyond
  MRR, or whether the memory is stale.
- **The semantic set measures recall, not understanding.** A hit proves the
  right memory surfaced; it says nothing about whether the answer was used well.
  It also cannot distinguish "the retriever cannot bridge the vocabulary" from
  "the fact is one of 40 candidates and rank 9". Both look like a miss.
- **Per-query stability is low on the semantic set.** 4 of 53 queries flip in
  and out of the top 8 between runs on the live ledger. Do not read a
  single-query delta as a signal; compare aggregates and re-run.
- **Production drift.** The daemon captures/distills continuously; a query can
  start hitting a newer consolidated memory. Recall may improve without code
  changes. Record `--ledger-stats` when publishing a baseline.
- **Latency is machine-specific.** The p95 gate guards against gross
  regressions on this Mac; it is not a portable benchmark.
- **Not part of CI.** It needs the production ledger, so `tests/test_eval_memory.py`
  covers the metric math, both query sets' labels and the gate combinations with
  temporary ledgers instead. Run the gate manually before release/refactor steps
  that touch retrieval, ranking, FTS projection, claims or consolidation.

## Related work

- `tests/test_eval_memory.py` — 39 tests for percentiles, relevance, MRR,
  duplicate rate, query validation, gate verdicts, CLI exit codes, the embedded
  sets, the semantic label rules and `--set` combinations.
- `hippomind/retrieval_eval.py` + `tests/test_retrieval_eval.py` — synthetic
  40-case Chinese and 32-case English QA suites. They test retrieval semantics
  in isolation; this production eval tests the real ledger.
- `REFACTOR_REPORT_ROUND4.md` — original gap: "the 10-query harness is a smoke
  benchmark, not a labelled eval set".
- `docs/SEMANTIC.md` — the semantic retrieval work this set exists to measure.
