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

A third capability is measured by its own script because it has no recall
metric: **refusal** — when the ledger cannot answer, does it return nothing, or
does it hand back a plausible-looking neighbour? `scripts/eval_refusal.py`
runs 38 questions the ledger provably cannot answer and gates on zero leakage
(measured 0/38 on the production ledger). `tests/test_knowledge_update.py`
covers the other untested capability from LongMemEval, knowledge update.

Owned files: `scripts/eval_memory.py`, `scripts/eval_refusal.py`,
`tests/test_eval_memory.py`, `tests/test_eval_refusal.py`,
`tests/test_knowledge_update.py`, this document. Core, daemon, transcripts,
distill and GUI code are not modified by the eval.

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

# Refusal: questions the ledger cannot answer must come back empty.
# Separate script because refusal has no recall denominator (see below).
python3 scripts/eval_refusal.py
python3 scripts/eval_refusal.py --gate
python3 scripts/eval_memory.py --set refusal                 # same, delegated
python3 scripts/eval_memory.py --set all --include-refusal   # three sets, one gate
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

A third set — `refusal` — exists but is **not** in `SET_ORDER`, because it has
no recall metric and would make the combined report incomparable. Run it with
`--set refusal` (delegates to `scripts/eval_refusal.py`) or fold it into
`--set all` with `--include-refusal`. See "Refusal set" below.

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
## Refusal set (scripts/eval_refusal.py)

The two labelled sets above measure **recall**: given a memory that answers the
question, can the retriever find it. Refusal measures the opposite capability,
and LongMemEval (arXiv 2410.10813) lists it as one of the five long-term-memory
abilities: **when the ledger cannot answer, does the system return nothing, or
does it hand back a plausible-looking neighbour?** HippoMind had only three
manual probes ("今天天气", "1+1") before this; there was no reproducible number.

Refusal lives in its own script, not as a third `--set` in `eval_memory.py`,
because it has **no recall denominator**: there is no `expect` to hit, only
`absent_terms` that must not exist. Folding a leak rate into `check_gate`'s
recall threshold would produce a metric that is 1.0 by construction and can
never fail.

```bash
# Report only (never changes the exit code).
python3 scripts/eval_refusal.py

# Gate: exit 1 on any leak, on a shrunken set, or on an ungrounded label.
python3 scripts/eval_refusal.py --gate

# Machine-readable, and the same thing through eval_memory.
python3 scripts/eval_refusal.py --gate --json
python3 scripts/eval_memory.py --set refusal --gate

# Fold it into the combined gate for a release run.
python3 scripts/eval_memory.py --set all --include-refusal
```

Exit codes: `0` report done / gate passed, `1` gate failed (only with
`--gate`), `2` usage or input error. A missing ledger is never created.
The ledger is opened **read-only** (`mode=ro` + `PRAGMA query_only=ON`), so
pointing `--ledger` at the production database cannot modify it.

### Three verdicts, not two

Measured on the 2026-10-25 production ledger: "今天天气怎么样" returns **5
memories**, the top one scoring **0.394** — above
`context_pack.MIN_RELIABLE_SCORE` (0.22) — with `fts=0.000` and
`coverage=0.167`. That 0.39 is entirely recency/touch/weight, i.e. evidence
unrelated to the query. **A "score below threshold means refused" rule is
therefore wrong**: an unrelated question has a score floor around 0.37.

What actually decides whether something reaches the model is the gate on the
automatic-injection path, `context_pack._passes_relevance_gate(candidate,
min_score)`: first the `min_score` floor (0.20, the same default as
`auto_context` and the daemon's `/context-pack`), then a hard requirement of
`fts > 0` **or** `coverage >= 0.6`. Each query is therefore classified into
one of three states:

| verdict | meaning | counts as |
| --- | --- | --- |
| `empty` | `ledger.search` returned nothing | **refused** |
| `noise` | rows came back, but none passes the injection gate (pack is empty) | **refused** — nothing was invented, low-score neighbours simply sit in the candidate list |
| `leak` | at least one row passes the injection gate | **failed** — an unrelated question would inject unrelated history |

`refusal_rate = (empty + noise) / n` and the gate requires it to be **1.0**,
i.e. `leak_rate <= 0.0`. `noise_rate` is reported but is **not** a threshold:
making it one would demand that the retriever return no candidates at all, which
is a different (and much more expensive) goal.

### Threshold rationale

| threshold | value | why exactly this |
| --- | --- | --- |
| `max_leak_rate` | `0.0` | A leak means an unrelated question injects unrelated history. There is no acceptable rate for that; the capability is binary. Measured 0/38 on the production ledger, repeated runs. |
| `min_queries` | `20` | HEALTH_REVIEW §6 asks for "20+ and in the gate". Set at the requirement, not at the current count (38), so a deliberate trim is allowed but a silent shrink is not. |
| `require_grounded` | `True` | Every `absent_terms` entry must have **0 recallable rows**. Without this, a mislabelled term would be reported as a retrieval leak and someone would "fix" the retriever instead of the label. |

There is deliberately **no** `min_recall`, `min_mrr`, `max_noise_rate` or
latency threshold here. Refusal has no `expect`, so recall and MRR are
undefined; noise is an accepted form of refusal; and the latency budget is
already enforced by the keyword/semantic gate on the path that actually injects.

### Composition (38 queries)

| category | n | examples |
| --- | --- | --- |
| `fiction` / `nonsense` | 4 | 火星基地的氧气配额, 月球电梯的年度维护预算, 蓝色独角兽的飞行速度 |
| `everyday` / `arithmetic` | 4 | 今天天气怎么样, 明天会下雨吗, 1+1等于几, 圆周率的第一千位 |
| `geography` / `history` / `space` | 7 | 澳大利亚的首都, 古罗马帝国灭亡年份, 国际空间站的轨道高度 |
| `physics` / `biology` / `medical` | 4 | 光速的精确数值, 人类基因组的碱基对总数, 布洛芬的成人单次剂量 |
| `personal` | 3 | 我的驾驶证号, 我的护照到期时间, 我女儿的生日 |
| `cooking` / `travel` / `pets` / `plants` / `cars` | 8 | 红烧肉要炖多久, 去冰岛看极光, 养一只柯基, 多肉多久浇水 |
| `finance` / `law` / `recommendation` | 5 | 明天股市会涨吗, 劳动法规定的年假天数, 推荐一部科幻电影 |
| `sports` / `music` / `film` / `games` / `language` | 5 | 马拉松世界纪录, 贝多芬第九交响曲首演, 奥斯卡评选规则 |

The spread is deliberate: a set that only asks about one topic would measure "we
have no corpus for this topic" rather than the ability to refuse. Two categories
are load-bearing:

* **`medical` and `personal`** — a wrong answer here has real-world
  consequences, so refusing is the only acceptable behaviour.
* **`fiction`** — these are the queries most likely to be "answered" by a
  topically similar neighbour ("火星" does occur in 6 rows of the production
  ledger, none recallable). They are the canary for the vector channel: if
  semantic similarity is ever allowed to pass the injection gate on its own, it
  will show up here first.

### Labelling rules

Each entry carries `id`, `category`, `query`, `absent_terms` and
`note`. The rules below are enforced by `tests/test_eval_refusal.py` because
violating any of them makes the metric lie rather than merely score low:

1. **`absent_terms` must have 0 rows in the recallable set.** The recallable
   predicate mirrors `ledger.search`'s defaults exactly:

   ```sql
   scope = ?                             -- the query's scope (default shared)
   AND raw_chat = 0                      -- include_raw_chat=False
   AND quality_state = 'accepted'
   AND lifecycle IN ('active','cold')    -- include_cold=True
   AND evidence_json != '[]'             -- require_evidence=True
   AND content LIKE '%<absent_term>%'    -- must be 0 rows
   ```

   This SQL **is** the executable definition of "the ledger does not have it",
   and `--audit-grounding` (on by default) re-runs it every time. Note the
   predicate is *not* "the string never appears anywhere": `天气` occurs in 7
   rows of the production ledger, all of them non-recallable. Requiring 0 rows
   in the whole table would make most realistic queries unlabelable.
2. **A term must be a topic word, at least 2 characters, and not a filler
   word.** `我的`, `多少`, `什么` occur everywhere; using one as an
   "absent" term fails the audit immediately. Enforced by
   `test_every_term_is_a_substantive_topic_word` and the 1-character rejection
   in `validate_refusal_queries`.
3. **Every query carries a `note`** recording the row count observed at
   labelling time, so a reviewer can re-derive the label.
4. **Queries are Chinese.** LongMemEval needs 500 English QA pairs against an
   English corpus; this ledger belongs to a Chinese personal assistant, so only
   the capability dimension is borrowed, not the corpus (HEALTH_REVIEW §9.2).

Verification query used for each candidate (run read-only):

```bash
sqlite3 "file:$HOME/Library/Application Support/HippoMind/hippomind.sqlite?mode=ro" "
  SELECT count(*) FROM memories
  WHERE scope='shared' AND raw_chat=0 AND quality_state='accepted'
    AND lifecycle IN ('active','cold') AND evidence_json!='[]'
    AND content LIKE '%<absent_term>%';"
```

### Drift

**`leak` is stable at 0. The `empty`/`noise` split is not.**

Sampling basis (use this as the comparison anchor when re-measuring): **18 runs,
2026-10-25, production ledger ~267.9k memories / ~12.0k active**. The split moved
between `empty=22 noise=16` and `empty=29 noise=9`, with `leak=0` in every
single run, and `max_top_score` moved between 0.377 and 0.394.

The mechanism is worth stating because it is easy to mistake for flakiness in
the gate: the ledger is live, and `ledger.search` builds its candidate pool
from FTS/LIKE, whose `coverage` denominators and candidate budgets shift as
the daemon writes. An unrelated query therefore returns 0 rows on one run and
1-6 low-score rows on the next. What does **not** move is whether any of those
rows carries lexical evidence — that is a property of the query, not of the
ledger, and it stays at zero.

Two consequences:

- The gate pins **only** `leak_rate` (0.0). Pinning `empty`/`noise` would
  make a query fail because its neighbour list grew by one, which is the ledger
  growing, not a regression.
- The baseline records a **range** for the split (`empty 22-29`,
  `noise 9-16`) for the same reason `SET_BASELINE["semantic"]` records one.

The one legitimate way for `leak` to move is that the ledger learns the fact —
which is exactly what the grounding audit reports. Treat an audit violation as a
signal to **replace the query**, never as a reason to loosen the threshold.

### Refreshing the refusal set

1. Pick a topic the ledger genuinely should not know (real-time, personal,
   external-world, fictional, or a different domain entirely).
2. Write the question as a user would ask it, in Chinese.
3. Choose the **topic word** as `absent_terms` and verify it with the SQL
   above; record the whole-table count in `note`.
4. Run `python3 scripts/eval_refusal.py --gate` and confirm `grounding: OK`.
5. Update `REFUSAL_BASELINE` in `scripts/eval_refusal.py` and the baseline
   table below with the measured `empty`/`noise`/`leak` counts.

### Baseline — 2026-10-25

Production ledger at baseline run: **267,914 memories** (259,409 raw chat),
11,986 active, 4,771 claims (2,815 active), 2,989 FTS-indexed rows.

```
set=refusal queries=38 k=8 min_score=0.20 empty=22..29 noise=9..16 leak=0
refusal_rate=1.000 leak_rate=0.000 max_top_score=0.377..0.394
p50=75ms p95=125..146ms
grounding: OK (38 queries, 39 absent_terms 全部 0 行)
gate: PASS  (max_leak_rate=0.00, min_queries=20, grounded=on)
```

Representative run (`empty=27 noise=11`):

```
set=refusal queries=38 k=8 min_score=0.20 empty=27 noise=11 leak=0
refusal_rate=1.000 noise_rate=0.289 leak_rate=0.000 max_top_score=0.387
p50=74.9ms p95=135.8ms
grounding: OK (38 queries, 39 absent_terms 全部 0 行)
gate: PASS  (max_leak_rate=0.00, min_queries=20, grounded=on)
```

**0 of 38 leaked.** The rest split between `empty` (nothing came back at all)
and `noise` (rows came back, none carried lexical evidence, none passed the
injection gate). The `noise` cases are the honest part of the number:
`ledger.search` still returns a candidate list for them (highest score 0.39),
it just never survives the relevance gate. Reporting only the `empty` count
would overstate the capability.

The queries that consistently produce candidates are the personal ones
(`我的驾驶证号是多少` returns 4-8 rows, top ~0.35, `coverage<=0.14`): every
character-level overlap comes from the pronoun `我的`, which is why the label
uses `驾驶证` as its absent term instead. They are `noise`, not `leak` —
no row has `fts > 0` or `coverage >= 0.6`.

### Scope: which read path this measures

This set measures the **automatic-injection path** — the one that decides what
gets put in front of the model without being asked. That is the path where a
wrong neighbour is actively harmful, and it is the only path with a relevance
gate.

**Explicit recall (`hippomind_recall`) is deliberately not gated.** It calls
`ledger.search` directly with `min_score=0`, so for an unanswerable query it
returns the same low-score candidates the `noise` verdict describes. That is
the intended contract, not a gap: an explicit call means "show me the candidates
and I will judge", so it returns **candidates, not assertions**. Applying the
injection gate there would silently drop the rows the caller asked for.

The two paths therefore answer different questions and are measured separately:
the gate here covers "what gets injected", and the keyword/semantic sets cover
"what can be found". A future `--mode recall` could report the explicit path's
candidate quality, but it would be a different metric (ranking quality, not
refusal), and averaging it in here would blur exactly the distinction this set
exists to make.

### Related tests

`tests/test_eval_refusal.py` — 50 tests: the three-verdict classifier
(including the 0.394-score noise case), query-set validation (1-character and
filler terms rejected), the grounding audit (quarantine rows do not count as
present), the read-only ledger (writes rejected at both the transaction and the
SQLite layer, `touch` is a no-op, source file mtime unchanged), gate verdicts,
and the CLI exit codes.

## Knowledge-update tests (tests/test_knowledge_update.py)

HEALTH_REVIEW §6 lists knowledge update as the second untested capability: write
A, then write B into the same slot, ask "what is it now" (must be B) and "what
was it before" (must be A). It is tested by
`tests/test_knowledge_update.py` rather than by a script, because it needs a
controlled ledger (a fresh write ordering and explicit validity windows), not
the production one.

The file pins **two different code paths** that are easy to conflate:

**Path 1 — claim level (same slot, competing values).** Two memories assert
different values for the same `(scope, subject, predicate, slot_key)`. On
ingest, `_insert_claims` compares `confidence * source_trust`: the winner
stays `active`, the loser's claims become `superseded`, the losing **memory**
leaves recall (`lifecycle='superseded'`) and points at the winner via
`superseded_by`. Tests: `test_query_for_the_current_value_returns_b_only`,
`test_context_pack_for_the_current_value_contains_only_b`,
`test_lower_confidence_new_value_becomes_a_conflict_not_a_silent_overwrite`,
`test_close_confidences_put_both_sides_in_conflict`.

**Path 2 — memory level (declared validity windows).** Two memories that do
**not** compete (their claim windows do not overlap), each carrying
`valid_from`/`valid_until`. `ledger.search(as_of=T)` evaluates the windows
at T, so the same query returns A at T-2d and B now. Tests:
`test_as_of_inside_the_old_window_returns_only_the_old_value`,
`test_as_of_before_both_windows_returns_nothing`,
`test_context_pack_as_of_returns_the_historical_fact`.

One subtlety worth recording, because it cost a debugging round: the window must
be written **inside the claim dict**, not only on the record.
`_normalise_claims` defaults `claim.valid_until` to `""` rather than
inheriting the record's value, so a record-level-only window makes both claims
look windowless — they then compete, A is superseded, and the test silently
becomes a path-1 test.

**The two paths are not yet connected** (HEALTH_REVIEW §3, T2's job): a
superseded claim gets no `valid_until`, and a `superseded` memory is excluded
from the `as_of` branch of `search`. So "what was it before" is answerable
today **only** on path 2 (explicit windows), not on path 1.
`KnowledgeUpdateTemporalGapTests` asserts the *consistency* of that state
rather than the current behaviour: if the claim has no `valid_until`,
`as_of` must return empty; if it has one, `as_of` must return A. The suite
therefore stays green both before and after T2 lands, and cannot let a
half-finished fix through.

## Refusal in the combined gate

`scripts/eval_memory.py` gains two opt-in flags. **Neither changes the existing
output format or exit codes** — `tests/test_eval_memory.py` pins those, and the
refusal set is deliberately absent from `SET_ORDER`:

* `--set refusal` delegates to `scripts/eval_refusal.py` (same thresholds,
  same verdicts, same exit codes). It forwards `--ledger`, `--limit`,
  `--min-score`, `--queries`, `--json` and `--min-queries`, and turns on
  the refusal gate unless `--no-gate` is passed. Note the flag naming: in
  `eval_memory.py` gating is the **default** (there is only `--no-gate`),
  whereas `eval_refusal.py` has an explicit `--gate` so a plain run is a
  report. Delegating rather than reimplementing keeps one definition of
  "refused".
* `--set all --include-refusal` appends the refusal set to the combined gate.
  Failures are prefixed `refusal:` like the other sets, and the report gains an
  `order` key listing all three. Without the flag, `--set all` emits exactly
  the bytes it emitted before.

```bash
python3 scripts/eval_memory.py --set refusal                 # refusal only
python3 scripts/eval_memory.py --set all                     # unchanged
python3 scripts/eval_memory.py --set all --include-refusal   # three sets
```

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
- **Refusal proves the injection gate, not every read path.** The refusal set
  measures the automatic-injection relevance gate. An explicit
  `hippomind_recall` MCP call bypasses it by design (`min_score=0`) and
  returns the same low-score candidates; that path is not measured here.
- **Knowledge update is only half-answerable today.** "What was it before" works
  for memories that declare `valid_from`/`valid_until` windows, but not for
  claim-level supersession, because a superseded claim gets no `valid_until`
  yet (HEALTH_REVIEW §3). `tests/test_knowledge_update.py` asserts the
  *consistency* of that state so a half-finished fix cannot pass.
- **Not part of CI.** It needs the production ledger, so `tests/test_eval_memory.py`
  covers the metric math, both query sets' labels and the gate combinations with
  temporary ledgers instead, and `tests/test_eval_refusal.py` does the same for
  the refusal classifier, grounding audit and read-only ledger. Run the gates
  manually before release/refactor steps that touch retrieval, ranking, FTS
  projection, claims or consolidation.

## Related work

- `tests/test_eval_memory.py` — 39 tests for percentiles, relevance, MRR,
  duplicate rate, query validation, gate verdicts, CLI exit codes, the embedded
  sets, the semantic label rules and `--set` combinations.
- `scripts/eval_refusal.py` + `tests/test_eval_refusal.py` — the refusal set,
  its three-verdict classifier and the read-only ledger (see "Refusal set").
- `tests/test_knowledge_update.py` — 16 tests for claim-level and memory-level
  knowledge update, plus the temporal-gap consistency guard (see
  "Knowledge-update tests").
- `hippomind/retrieval_eval.py` + `tests/test_retrieval_eval.py` — synthetic
  40-case Chinese and 32-case English QA suites. They test retrieval semantics
  in isolation; this production eval tests the real ledger.
- `REFACTOR_REPORT_ROUND4.md` — original gap: "the 10-query harness is a smoke
  benchmark, not a labelled eval set".
- `docs/SEMANTIC.md` — the semantic retrieval work this set exists to measure.

## Benchmark fixture gate (2026-11-14)

`scripts/eval_benchmark.py` 把“检索是否有效”变成 release gate 的一步
（`scripts/release_check.sh` 第 8 步，CI 的 release-gate job 也会跑）。
它不依赖生产账本：fixture 里同时给出 memories 与 queries，脚本在临时目录建账本、
一次 ingest 后按类别评测。

- fixture：`tests/fixtures/retrieval/benchmark_zh_v1.json`
  （64 memories / 48 queries；fact_recall 12、knowledge_update 8、temporal 8、
  continuity 8、abstention 12）。这是自建、按 LongMemEval 能力维度对齐的回归集，
  不是官方分数、不能横向比较。
- 指标：recall@8、MRR、duplicate_rate、update_hit（新值命中且旧值不在 top-k）、
  abstain_accuracy（自动注入门控下必须空包）、p95 延迟、per-category recall。
- 阈值：`tests/fixtures/retrieval/benchmark_baseline.json`（recall 0.95 / MRR 0.8722 /
  update_hit 0.90 / abstain 0.90 / per-category 0.90 / p95 50ms）。
  阈值 = 实测值 − margin，只允许显式 `--update-baseline` 重写并记录 measured。
- 官方 LongMemEval 支持：`--format longmemeval --data <file>` 适配可子串验证的子集；
  仓库不内置官方数据（许可/体积），不可验证的题会报错并给出 schema，不猜答案。

``bash
PYTHONPATH=$PWD /usr/bin/python3 scripts/eval_benchmark.py --json
# 默认自建 fixture：48 queries，gate PASS；--format longmemeval --data <file> 跑官方子集
``
