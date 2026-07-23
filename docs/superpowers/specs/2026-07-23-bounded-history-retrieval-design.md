# Bounded Historical-Idea Retrieval Design

Status: approved design; implementation pending

## Goal

Historical idea growth must not increase an agent stage's context without bound. The system must retrieve the few historical records that can materially affect a candidate, preserve enough source evidence to audit the comparison, and abstain when retrieval is incomplete.

The full history remains outside model context. Internal history retrieval answers three narrower questions:

1. Does an earlier record contain the same core proposition?
2. Is the candidate an explicit revision, recheck, or descendant of an earlier record?
3. Does the candidate repeat a known failure mechanism or experimental defect?

External prior-work search remains a separate downstream stage. An internal `complete_no_match` result means only that the current internal index found no match; it is not evidence of academic novelty.

## Invariants

| Invariant | Enforcement |
|---|---|
| Full history never enters `generate` or a bounded comparator | Run those stages in mirrors that do not contain `ledger.tsv`, the SQLite database, indexes, or unrestricted repository-search tools. |
| Canonical records are independent of search technology | Store records, verdicts, typed lineage, and provenance in SQLite; treat FTS and vector data as rebuildable projections. |
| Similarity proposes candidates but does not decide identity | Exact, lexical, and dense scores affect recall and ranking only. They cannot create lineage edges, reject an idea, or establish novelty. |
| Every semantic conclusion is evidence-addressable | Return stable record IDs, lineage IDs, source locations, matched facets, and extractive evidence spans. |
| Model input has a hard upper bound | Validate both result count and estimated input tokens before invoking an agent. |
| Incomplete retrieval cannot create a permanent verdict | Propagate explicit partial and failure states; require abstention or retry. |
| Index quality remains measurable | Retain exhaustive lexical and dense baselines as retrieval oracles and compare incremental indexes with clean rebuilds. |

Prompt instructions are not a sufficient context boundary. A stage that can search the repository can still ingest the full ledger accidentally. The orchestrator must control the files and tools visible to each stage.

## Architecture

```text
write transaction
  -> SQLite canonical records + search_projection_outbox
  -> exact lookup / FTS5 / facet embeddings / failure aggregates
  -> published index generation with a source watermark

generation
  <- bounded generation_brief.json
  -> candidate

candidate
  -> model-free exact + lexical + dense + explicit-lineage retrieval
  -> selection

selected or retrieval-triggered candidate
  -> rank fusion and lineage collapse
  -> schema- and token-validated retrieval_pack.json
  -> bounded comparator
  -> internal-history decision and history_receipt.json
  -> external prior-work search
```

### Canonical store

SQLite is the canonical structured store. It contains immutable candidate records, canonical `story_aliases`, normalized searchable facets, reviews and verdicts, typed lineage edges, artifact provenance, and the outbox used to update derived search indexes. TSV remains an import/export and audit format during migration; it is not the query interface used by agent stages.

`AWR-REBUILD-DRAFT.md` §5 remains canonical for lineage identity, historical import, transaction boundaries, and file materialization semantics. Retrieval adds two canonical structures under the same identity and transaction rules:

- `lineage_edges(parent_candidate_id, child_candidate_id, relation_type, evidence_artifact_id)` stores explicit `evolved_from`, `recheck_of`, and `supersedes` relations. Parent relations are cycle-checked, and the tuple of parent, child, and type is unique. Similarity can propose an edge but cannot write one.
- `search_projection_outbox(record_id, projection_kind, content_version, source_sequence)` drives exact, lexical, dense, and aggregate projections for ordinary writes and historical import.

The search outbox is distinct from AWR's `materialization_outbox`, which retains its existing responsibility for bridge file effects.

One effective writer owns canonical mutations. Readers use a published database snapshot or a read transaction. Every complete retrieval records the source watermark and index generation so its evidence can be reproduced.

### Rebuildable projections

The following data is derived from canonical records and may be rebuilt:

- an exact-lookup projection built from canonical `story_aliases` and normalized content hashes;
- SQLite FTS5 indexes for lexical retrieval;
- one embedding per searchable facet rather than one embedding for the whole record;
- lineage summaries used only for result collapse and presentation;
- structured failure-code counts and theme aggregates;
- benchmark and index-health statistics.

An embedding cache key includes the record ID, facet name, normalized-content hash, embedding model and revision, preprocessing version, dimensions, and distance metric. A changed key invalidates only the affected projection. Deletion or supersession removes stale searchable entries without deleting canonical history.

At the current corpus size, dense retrieval uses an exhaustive flat scan. Approximate nearest-neighbor search is introduced only when representative measurements show that exhaustive search violates an explicit latency, CPU, or memory target. Any approximate index must report `Recall@K` against the exhaustive oracle. A dedicated vector database is not part of the initial architecture.

## Stage Boundaries

### Generation input

`generate` receives only:

- the current divergence lens and generation policy;
- a bounded `generation_brief.json`;
- structured counts by theme and failure code;
- at most one confirmed evolution or recheck parent, including its explicit defect and allowed delta;
- optional bounded research context.

The brief is generated deterministically from canonical data. It contains no raw ledger dump and no free-form list whose size grows with history.

Recurring failure codes are assigned or updated when a verdict is committed. SQL aggregation replaces the routine full-ledger `meta` pass. Unmapped free-text reasons may enter an isolated, bounded batch-distillation job; its output is a rebuildable projection and cannot alter verdicts.

### Candidate retrieval

Every generated candidate receives model-free internal-history retrieval before selection or prescreen. The bounded comparator runs only for a candidate retained for prescreen or deep research, or when an exact or lineage hit must be resolved before selection. A selector-discarded candidate keeps its retrieval trace but consumes no comparator context. No candidate can receive a permanent ledger conclusion without a complete history receipt.

Retrieval has three explicit intents:

| Intent | Primary relation sought |
|---|---|
| `duplicate_search` | Same core proposition, including paraphrases |
| `evolution_search` | Parent, ancestor, descendant, or sibling with an explicit material delta |
| `failure_pattern_search` | Repeated failure mechanism, occupied combination, or experimental defect |

The candidate and historical records are projected into these facets:

- `problem_estimand`
- `claimed_delta`
- `mechanism`
- `evaluation_expected_signal`
- `setting_task`
- `entities_datasets_methods`

Verdict and rejection reason belong to the failure-pattern index, not the proposition embedding. Lineage and version are relational fields, not semantic-vector substitutes.

The candidate set is the union of:

1. exact normalized-story and alias matches;
2. FTS/BM25 results per facet;
3. exhaustive dense results per facet;
4. explicit lineage neighbors;
5. optional expansion from already confirmed neighbors.

Theme may adjust ranking but cannot be a hard filter. Initial fusion uses recorded per-channel ranks and reciprocal-rank fusion. A learned fusion rule may replace it only after a labeled benchmark demonstrates a material improvement and the rule remains versioned and reproducible.

Each index generation names a `retrieval_policy_version`. The policy fixes mandatory channels, per-channel candidate depth, optional reranker and rerank depth, final lineage count, score normalization, token limits, and expansion limits.

| Channel | `duplicate_search` | `evolution_search` | `failure_pattern_search` |
|---|---|---|---|
| Canonical exact lookup | Story hash and alias required | Story hash and alias required | Structured failure-code equality required |
| Facet FTS/BM25 | Required | Required | Required over failure fields |
| Exhaustive dense facets | Required | Required | Required over failure fields |
| Canonical lineage query | Required | Required | Required for grouping and provenance |
| Confirmed-neighbor expansion | Conditional | Conditional | Conditional |

`not_applicable` is a valid recorded channel result only where the policy table permits it. A conditional channel becomes mandatory for the final receipt once the comparator requests it. Any missing required channel makes the retrieval status `partial`.

Pack construction groups results by lineage before token measurement. Each lineage contributes its highest-matching version, the current version, and the material delta needed to distinguish them.

### Bounded comparator

The comparator receives the current candidate plus the validated retrieval pack. It classifies each retained lineage as one of:

- `same_core_idea`
- `same_lineage_revision`
- `related_component`
- `same_failure_mechanism`
- `related_failure_pattern`
- `distinct`
- `uncertain`

The duplicate and evolution intents use the first three semantic relations. The failure-pattern intent uses `same_failure_mechanism` and `related_failure_pattern`. All intents may emit `distinct` or `uncertain`.

The output includes the relation, supporting record and lineage IDs, matched facets, material differences, extractive evidence IDs, and confidence. A generic cosine threshold cannot emit these relations. Exact identity may be established through stable IDs or normalized exact aliases; every semantic relation remains a bounded comparison over retrieved evidence.

An `uncertain` result may request a bounded expansion for named record or lineage IDs. Expansion cannot expose unrestricted history and stops at `max_expansion_rounds`. Exhausting that bound preserves `uncertain`; it does not force a binary decision.

## Retrieval Pack Contract

`retrieval_pack.json` is the only historical payload visible to the comparator. It contains:

- query and candidate IDs;
- retrieval intent;
- retrieval policy version and configured candidate depths;
- retrieval status: `complete`, `partial`, `backend_failed`, or `budget_exceeded`;
- canonical source watermark;
- index generation and projection versions;
- channels executed and any channel failures;
- retained matches grouped by lineage;
- per-match record ID, source artifact and location, facet scores and ranks, material delta, and extractive evidence spans;
- omitted lineage count;
- estimated input tokens;
- a deterministic receipt ID.

Only a `complete` pack is sent to the comparator. `history_receipt.json` records the pack hash, comparator version, evidence-addressed relations, and one status from [Completion and Failure Semantics](#completion-and-failure-semantics).

The orchestrator enforces:

```text
fixed instructions
+ current candidate
+ retrieval pack
+ tool receipts
+ output reserve
+ safety margin
<= stage context limit
```

`max_matches`, `max_retrieval_tokens`, and `max_expansion_rounds` are hard limits. Preflight serializes the exact adapter invocation, including fixed instructions, tool schemas, candidate content, retrieval payload, receipts, and message wrappers. It then enforces:

```text
input_upper_bound
+ max_output_tokens
+ safety_margin
<= model_context_limit
```

`input_upper_bound` is the target tokenizer's exact count when that tokenizer is available. The fallback is `UTF-8 byte length of the serialized invocation + adapter_wrapper_allowance`; one input byte is treated as no less than one token. The adapter version fixes and tests its wrapper allowance. If neither an exact tokenizer nor a verified wrapper allowance exists, preflight fails closed. The same rule applies to `generation_brief.json`.

If a pack exceeds its budget, reduction occurs in this order:

1. retain only facets relevant to the current intent;
2. replace long reasons and reports with extractive evidence spans;
3. discard the lowest-ranked lineages;
4. return `budget_exceeded` without invoking the comparator.

The third step may discard only lineages below the policy's calibrated comparator cutoff. If fitting the pack requires dropping any lineage inside that cutoff, the result is `budget_exceeded`. Compression never removes stable IDs, source locations, policy and index versions, or the count of omitted results. Free-form summaries cannot serve as the sole evidence for rejection or lineage mutation.

## Completion and Failure Semantics

| History receipt status | Meaning | Allowed consequence |
|---|---|---|
| `complete_match` | A `complete` pack covered all mandatory channels against one source watermark, and at least one material relation survived comparison | Continue with the evidence-addressed relation; a permanent verdict still follows the normal review policy. |
| `complete_no_match` | A `complete` pack covered all mandatory channels and the comparator found no material internal relation within the calibrated retrieval boundary | Continue to external prior-work search; do not claim novelty. |
| `uncertain` | A complete pack produced an unresolved semantic relation after bounded expansion | Preserve the evidence and abstain from a permanent duplicate or internal-no-match conclusion. |
| `partial` | At least one mandatory channel or source partition was unavailable | Retry, expand, or abstain; no permanent Reject, Accept, duplicate, or novel conclusion. |
| `backend_failed` | The retrieval service could not produce a valid pack | Retry or stop the candidate; no permanent conclusion. |
| `budget_exceeded` | Evidence could not fit the configured bound after deterministic reduction | Reduce scope or stop; do not invoke the comparator. |
| `conflicting_evidence` | Retrieved records or lineage facts disagree materially | Fetch a small set of original records for explicit resolution; otherwise abstain. |

Exact and FTS retrieval may remain available when dense retrieval fails, but the history receipt remains `partial` when dense retrieval is mandatory for that policy version. `complete_no_match` is always scoped to the recorded corpus watermark, channels, depths, and comparator cutoff. A duplicate or internal-no-match statement without a complete history receipt is structurally invalid.

## Consistency and Recovery

A canonical write and its search-outbox event commit in one transaction. Projection workers claim search-outbox entries idempotently, build a new index generation, validate it, and atomically publish the generation marker. Readers never mix projection generations within one query.

A complete result requires:

- an index source watermark that covers the requested canonical snapshot;
- matching schema, tokenizer, embedding, and preprocessing versions;
- successful execution of every mandatory channel;
- a retrieval pack that passes schema and token validation.

No-op rebuilds create zero new embeddings. Appending or changing `N` facet values creates exactly `N` new embeddings. Deletions remove stale searchable entries. An incremental build must return the same exhaustive top results as a clean full rebuild for the same versions and source watermark.

## Protocol Cutover

The existing runtime protocol remains authoritative until one compatibility change switches the history path. The cutover changes these surfaces together:

- SQLite schema, historical import, stable TSV export, `lineage_edges`, and `search_projection_outbox`;
- index builder, retrieval API, pack schema validator, token preflight, and history receipts;
- `PROGRAM.md`, `roles/generate.md`, `roles/meta.md`, and the corresponding `hunt.sh` stages and mirror manifests;
- structural tests proving that generation and comparison mirrors cannot access full history;
- failure-path tests proving that partial, failed, conflicting, uncertain, and over-budget retrieval cannot create permanent conclusions.

The primary write path switches only after import parity, dual-read validation, clean projection rebuild, and retrieval-receipt replay pass against the same source snapshot. A partial prompt-only or role-only cutover is invalid.

## Evaluation

Retrieval and comparison are evaluated separately.

Three manually adjudicated query sets are required:

1. Duplicate relations: blocking duplicate, substantive overlap, and unrelated.
2. Lineage relations: direct parent, other ancestor or descendant, sibling, and unrelated.
3. Failure-pattern relations: the same failure mechanism, a related but materially different defect, and unrelated.

Each set includes explicit no-hit queries. Duplicate gains are `2` for a blocking duplicate, `1` for substantive overlap, and `0` for unrelated. Lineage gains are `3` for a direct parent, `2` for another ancestor or descendant, `1` for a sibling, and `0` for unrelated. Failure-pattern gains are `2` for the same mechanism, `1` for a related defect, and `0` for unrelated.

Train, calibration, and held-out test folds are grouped by lineage so the same relationship cannot teach and test a model. Within each fold, every query uses an as-of-time corpus containing only records committed earlier than the query. Earlier ancestors from the query's lineage may therefore appear in its index, but all queries and relationship judgments for that lineage remain in one fold. Query text excludes verdicts, rejection reasons, citations, and future revisions.

Hard negatives include:

- the same theme and vocabulary with a different estimand;
- the same intervention with a different causal claim;
- the same hypothesis in a different task or embodiment;
- a revision sibling whose delta changes the core proposition.
- the same theme and verdict with a different failure mechanism.

Judgment pools are the union of top results from exact, lexical, dense, hybrid, and reranking systems. Test judgments are independently adjudicated twice; disagreements receive a third adjudication, and agreement is reported. Unjudged pairs remain unjudged rather than becoming automatic negatives.

The benchmark compares normalized exact matching, FTS/BM25, exhaustive dense retrieval, hybrid fusion, an optional versioned reranker, and hybrid retrieval with the bounded comparator. Four arms keep errors attributable:

1. retrieval-only ranking against gold IDs;
2. comparator-only classification from oracle packs;
3. end-to-end retrieval and comparison;
4. a closed-book comparator with no historical evidence.

It reports:

- duplicate `Hit@1/3/5/10`, `MRR@10`, alert precision, and no-hit false-positive rate;
- lineage `nDCG@10`, direct-parent accuracy at 1, and ancestor `Recall@5/10`;
- failure-pattern `Recall@K` and relation precision and recall;
- comparator precision and recall by relation, false-duplicate rate, false-internal-no-match rate, and abstention accuracy;
- evidence precision, evidence recall, and unsupported-claim rate;
- results by theme, lexical-overlap bucket, relation type, and history age;
- p50 and p95 latency, model input tokens, comparator pairs, and cost per query.

Context-budget, candidate-depth, rerank-depth, and final-lineage-count sweeps select operating limits. Paired bootstrap 95% confidence intervals accompany all primary metrics and system differences. A relation with fewer than 30 independent positive and 30 independent hard-negative held-out queries remains advisory and cannot set an automated threshold.

The selected depths, metric thresholds, maximum false-duplicate and false-internal-no-match rates, latency target, and token budget are committed in the versioned retrieval policy before held-out evaluation. A learned fusion or reranking change is accepted only when the lower bound of its paired confidence interval improves the declared primary metric without crossing either error budget. Approximate retrieval remains disabled until its held-out exhaustive-oracle recall meets the policy threshold and its latency improvement meets the policy target.

No retrieval result automatically creates a permanent verdict. A policy without sealed calibration values may run only in shadow mode and emit evidence for manual inspection.

## Deliberate Exclusions

- Full-ledger reads by `generate`, `meta`, reviewers, or the comparator
- Automatic lineage creation or merging from semantic similarity
- Permanent verdicts based only on vector distance or a generated summary
- Recency decay for duplicate detection
- A dedicated vector database before measured scale or concurrency requires it
- GraphRAG or recurring whole-history map-reduce in the routine candidate path
- Model-managed forgetting, history mutation, or index publication
- Treating context compaction as a history-retrieval mechanism

Graph or hierarchical-summary systems may be evaluated later for global questions such as theme evolution across the entire archive. They are outside the correctness path for candidate duplication and lineage retrieval.

## References

- [OpenAI: Harness engineering](https://openai.com/index/harness-engineering/)
- [OpenAI Agents SDK: Sessions](https://openai.github.io/openai-agents-python/sessions/)
- [SQLite FTS5 Extension](https://www.sqlite.org/fts5.html)
- [SQLite Write-Ahead Logging](https://sqlite.org/wal.html)
- [BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of Information Retrieval Models](https://arxiv.org/abs/2104.08663)
- [MTEB: Massive Text Embedding Benchmark](https://arxiv.org/abs/2210.07316)
- [LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory](https://arxiv.org/abs/2410.10813)
- [Lost in the Middle: How Language Models Use Long Contexts](https://arxiv.org/abs/2307.03172)
