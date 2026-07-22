# Development Roadmap

Status: maintained
Historical snapshot: 2026-07-12

Goal: make `ai-ideas` a calibratable, auditable, topic-independent research harness, then add safe concurrency, portable deployment, and stable product entry points.

IDs follow the original plan; `6` remains unassigned.

## Roadmap

| ID | Area | Initiative | Priority | Primary dependency |
|---|---|---|---|---|
| 0 | Harness engineering | Move deterministic control into `.sh` where practical; improve Claude and Codex adapters | P0 | None |
| 1 | Research quality | Improve autoresearch success rate (w/ sol) | P0 | Decision and observability foundations from 0 |
| 2 | Architecture | Decouple the harness from research topics and generated content | P1 | 0 |
| 3 | Storage | Evaluate and migrate `ledger.tsv` to a lightweight database | P1 | Data boundaries from 2 |
| 4 | Execution | Support safe concurrency | P2 | 2 and 3 |
| 5 | Documentation and structure | Build a user-oriented `README.md` and decide whether to restructure the repository | P1 | Boundary design from 2 |
| 7 | Delivery | Containerize the system | P2 | 2 and 5 |
| 8 | Product | Productize the workflow | P3 | Stable interfaces from 0–7 |

## A. Research Quality

### 1. Improve autoresearch success rate (w/ sol)

#### Historical baseline

- `ledger.tsv`: 209 ideas, 155 AwR, 54 Reject, and 0 SA.
- `tmp/hunt.metrics.tsv`: 96 candidates completed three-reviewer evaluation. Of 288 votes, only 6 were SA; 3 candidates received `2×SA + 1×AwR`, and none received unanimous SA.
- Of 59 measured attempts, 32 reached a verdict and 27 stopped at empty/fail.
- `tmp/sa-potential-ideas.md` is a dynamic candidate pool. Its `SA-possible` entries lack formal main-loop confirmation and do not count as SA.
- `calib/results-2026-07-12.md` (aligned criteria and honest gold-set evidence): after reconstructing complete prior work for three positive cases, the load-bearing-assumption probe recovered from 1/3 to 3/3 SA once its evidence was complete; pos-meanflow was exposed as a high-overlap direct hit occupied by MP1 and was reclassified as the negative case neg-meanflow-mp1; pos-robomme was exposed as a medium-overlap borderline case whose taxonomy was occupied by MIKASA-Robo. Both named verdict clauses were correct. The defect was the old gold set's false low-overlap claim, so the clauses remained unchanged. All 5 current gold-set cases pass.

#### Optimization metrics

| Metric | Definition |
|---|---|
| Calibration accuracy | Whether gold positive and negative cases are separated consistently |
| Candidate quality rate | Share of formal candidates receiving at least 1 SA vote |
| Near-SA conversion rate | Share of `2,2,1` or `1,2,2` candidates that become unanimous SA after revision |
| Final SA hit rate | Share of formal candidates receiving unanimous SA |
| Run completion rate | Share of attempts that reach a verdict |

Run completion rate and SA hit rate remain separate metrics. A functioning mechanism does not imply improved research quality.

#### P0: Align decisions and complete observability

- [x] Use the clear-accept standard in `brainstorming_policy.md` as the sole SA definition, removing conflicts among `rubric.md`, role prompts, and the sidecar. (2026-07-12: rubric Step 8 and Integrity gate #5 now point to the policy; the stricter "and can contend for oral/spotlight" clause was removed from review.md; incorrect references in awr-judge/trigger/README were corrected.)
- [x] Establish an embodied-domain gold set covering an ordinary method, a benchmark/new problem, removal of a load-bearing assumption, and a direct-hit negative. (Five cases, machine-readable `expect` fields, and batch scoring through `calib/run_all.sh`; a formal embodied load-bearing-assumption positive awaits the 2026 fall conference outcome, with the cross-domain pos-axiom-adam probe as its current substitute.)
- [x] Separate verdict calibration over frozen `ideas + priorwork` from end-to-end calibration with live retrieval. (Frozen: `calib/run_all.sh`; end-to-end: retrieval recall through `calib/run_e2e.sh`. Positive controls have no end-to-end run because live retrieval would correctly classify published work as self-occupying.)
- [x] Generate stable `run_id` and `candidate_id` values for every run, recording source, backend, policy version, stage timing, and exit reason. (`run_id` = start time + pid + round; `candidate_id` = `<run_id>/I<n>`; manifest + `stages.tsv`.)
- [x] Preserve per-run `ideas`, `priorwork`, three-reviewer vote vectors, complete reasons, aggregate result, and retrieval failures; keep only summaries in the ledger. (End-of-round archive at `tmp/runs/<run_id>/`: complete `tmp/round`, manifest, ledger delta, and per-stage logs.)

Acceptance: known class-A positives reproducibly receive unanimous approval, direct-hit negatives remain unanimous Reject, and every ledger conclusion can be reconstructed from its inputs and decision process.

#### P1: Improve candidate quality and near-SA conversion

- [x] Generation explores broadly; an independent selector ranks candidates by proposition strength, clear-accept ceiling, minimum falsification experiment, and executability. Novelty evidence is unavailable before submission and remains the responsibility of prior-work search and reviewers.
- [x] `roles/research.md` reports prior-work coverage facts without pre-judging the clear-accept ceiling.
- [x] Distinguish `direct-hit`, `medium-overlap`, and incomplete retrieval. Incomplete retrieval receives more search before formal classification.
- [x] Classify non-SA outcomes as `novelty-dead`, `evidence-incomplete`, `design-fixable`, or `ceiling-limited`.
- [x] Preserve revision lineage and explicit deltas; prioritize the near-SA queue over blind pool expansion.
- [x] Add only direct hits and CRITICAL findings to the permanent non-revival set; retain auditable recheck conditions for all other outcomes.

Retrieval completeness receives structural machine checks; semantic completeness remains a trusted-backend judgment. Story-once enforcement across revisions where R≠L and across paths A/B belongs to storage milestone #3: `lineages` stores immutable identity, `reentry_grants` stores path-specific eligibility evidence, and `reentry_requests` uses `UNIQUE(lineage_key)` to unify readiness, claims, and consumption. Rich revision chains preserve per-version deltas and semantic lineage merges. Until #3 is complete, Path A retains the generation self-discipline gate.

Acceptance: the share of candidates receiving at least one SA vote increases, and every near-SA candidate reaches a terminal state through added evidence, revision, reevaluation, or rejection.

#### P1: AwR re-entry architecture

Status: deferred. The experiment gate rejected implementation. The complete architecture and crash matrix remain in [`AWR-REBUILD-DRAFT.md`](AWR-REBUILD-DRAFT.md) pending stronger candidate evidence.

Experiment gate run on 2026-07-14, with no new pipeline:

- [x] Select the strongest and only readiness-labeled candidate in the pool, `2b500d736c99` (VLA autopilot), then faithfully reconstruct main-loop ideas, prior work, and three-reviewer evaluation with current tools.
- [x] Gate rule: at least 1 unanimous-SA candidate permits the thin slice; 0 defers it. **Result: the candidate never reached SA.** The source audit found that row 172's original round used Claude for every role, not agy. Its prior-work review found the nearest neighbor Fighting Copycat (2010.14876), classified overlap as low, and produced a 2,2,1 vote with accept-w-rev (near-SA). The stricter review added de Haan 1905.11979 and closed-loop causal benchmark 2504.14709, classified overlap as high, and produced a unanimous reject. The same idea moved between low and high depending on whether the seven-year-old neighboring copycat phenomenon occupied the combination "VLA + autopilot score + observation forcing." The overlap decision was highly sensitive to prior-work phrasing; the stricter brief explicitly named copycat and therefore was guided rather than fully neutral.

Conclusion: the AwR re-entry architecture, including the thin slice below, remains deferred. The candidate sits on the near-SA/reject overlap-calibration boundary and never reached true SA. The operative levers are the low/high overlap scale and candidate quality, not a re-entry pipeline. Work returns to candidate quality and calibration under P0. The thin slice and deferred set remain as architecture records; the failed gate does not activate them.

Thin slice, conditional on a future successful experiment gate and limited to research quality:

- [ ] Connect the Strongest Counterexample, distinct-neighbor, and Papers Read evidence gates from `AWR-REBUILD-DRAFT.md` §3.5 to the existing `check_judge`.
- [ ] Use Claude or Codex as the trusted judge through the existing `--ignore-user-config`, `workspace-write`, and `--strict-mcp-config` controls; accept `asserted` independence without blocking on OS-level confinement.

Deferred until evidence includes at least 1 SA candidate; full details remain in `AWR-REBUILD-DRAFT.md`: entry truth table and capability predicates, invocation bundle and provenance DAG (§3.4), atomic `ledger.good` publication receipt (§3.7), sealed-plan legacy migration (§4), exactly-once manual promotion (§3.8), and storage milestone #3 lineages/grants/requests/outbox (§5). These correctness costs become relevant only after the pipeline can produce artifacts worth committing correctly.

Acceptance: the experiment gate produces an explicit SA/no-SA result that determines whether work proceeds. If the thin slice is activated, `SA-possible` artifacts support manual review and dormant mode retains zero side effects. Formal main-loop verdict re-entry belongs to #3.

#### P2: Reduce ineffective runs

- [ ] Record structural, API/network, model, and content failures separately. Infrastructure failures must not create permanent idea conclusions.
- [ ] Retry only the failed candidate; support safe resume from valid upstream artifacts.
- [ ] Preserve candidates beyond `SHORT_MAX` and their selector scores for later reranking.
- [ ] Change one variable per configuration epoch and compare on a fixed calibration set and review budget.

Acceptance: completion rate rises and invocation cost falls for the same number of formal reviews. The count of `SA-possible` artifacts is not a success metric.

## B. Harness Engineering

### 0. Shell-first control and Claude/Codex adapters

Natural language handles research judgment. Scripts handle parameter validation, state transitions, retries, aggregation, archival, and safety boundaries.

- [ ] Inventory mechanically decidable control logic in prompts and move it into `.sh` files or shared libraries.
- [ ] Define one agent-adapter interface for input, environment variables, exit codes, timeouts, capability declarations, isolation, and artifact collection.
- [ ] Build separate Claude and Codex adapters over shared command parsing, temporary mirrors, logging, and error classification.
- [ ] Cover parsing, timeout, failure recovery, path boundaries, and artifact integrity with a fake agent and small shell probes.

Acceptance: the same stage can switch between Claude and Codex through configuration; invalid configurations fail fast; critical invariants do not depend on a model obeying natural-language instructions.

### 2. Decouple the harness from research topics and generated content

- [ ] Limit the harness to lifecycle, scheduling, locking, retries, aggregation, storage, and publication.
- [ ] Put context, brainstorming policy, rubric, role prompts, and topic resources in the research-topic package.
- [ ] Separate runtime artifacts from source configuration so state is never written back into topic definitions.
- [ ] Test the harness with a minimal synthetic-topic fixture independent of embodied-domain content.

Acceptance: adding or switching a research topic requires no harness changes; topic-prompt changes do not affect scheduling or storage tests.

### 3. Evaluate and migrate `ledger.tsv` to a lightweight database

Evaluate SQLite first. Preserve TSV import/export and do not delete the existing ledger directly.

- [ ] Define the minimal schema for ideas, runs, candidates, reviews, artifacts, invocations, and revision lineage. `lineages` stores only immutable identity and one deterministic root candidate; row-specific `origin_stable_id` exists only on each candidate; `story_aliases(canonical_hash UNIQUE)` prevents the same revised story from spanning lineages; `reentry_grants` stores path-specific eligibility evidence and rule versions with a deterministic fact key; `reentry_requests` uses `UNIQUE(lineage_key)` for readiness and claim generation; `round_slots` uses `round_id UNIQUE + CHECK(slot_kind='reentry')` so evolve, recheck, and Path B share one slot while binding state/lineage/candidate/generation/token; `materialization_outbox(candidate_id UNIQUE)` isolates effects outside the transaction. Historical import stores ledger, parent pointers, promotion and mapping inputs, and the union plan in immutable CAS before creating `import_epochs`; one transaction writes epoch done plus lineage/alias/candidate/consumed request, with no provisional lineage or planless result. Ordinary unconsumed import creates no ready grant/request. Expired claims may be reclaimed across rounds; claims bind a specific grant, revocation fences that grant atomically, and remaining grants derive request state. Committed slots are never reused after lease expiry. P1 and #3 share versioned canonical lineage plus `origin_stable_id` based on ledger instance, 1-based data-row number, and raw-row SHA; snapshot SHA is provenance only, and promotion is unique by lineage key. Recheck consumption, evolution parent pointers, sidecar origin fingerprints, tracked attestations, formally committed `promoted.tsv`, and manual mappings jointly enforce story-once. See `AWR-REBUILD-DRAFT.md` §5.
- [ ] Compare SQLite with continued TSV use across queries, concurrent writes, migration, and maintenance, then record the storage decision.
- [ ] If migration proceeds, provide one-time import, dual-read validation, and stable TSV export before switching the primary write path.
- [ ] Make writes transactional and support unique constraints, idempotent resume, and schema versions.

Acceptance: historical runs, complete votes, and revision chains are queryable; repeated execution creates no duplicate records; existing TSV workflows remain exportable.

### 4. Support safe concurrency

- [ ] Parallelize candidate-level research and review before evaluating round-level concurrency; give every task an independent working directory and log.
- [ ] Provide a global concurrency limit, backend rate limits, resource locks, cancellation, and failure retries.
- [ ] Keep aggregation and persistence idempotent to prevent duplicate votes, file overwrites, and ledger write conflicts.
- [ ] Under fixed inputs, concurrent and serial execution must produce the same candidate set and verdicts.

Acceptance: concurrent runs create no file conflicts or duplicate writes; interrupted work resumes safely; results do not depend on completion order.

## C. Delivery and Product

### 5. Build the README and decide repository structure

- [ ] P1 Phase 0 already requires correcting sidecar startup examples and failure semantics. This item covers the subsequent complete user journey and cannot delay that safety correction.
- [ ] Organize the README around purpose, prerequisites, quick start, core configuration, output locations, recovery, and failure diagnosis.
- [ ] Link detailed internals to dedicated documents instead of copying the roadmap and policy text into the README.
- [ ] Evaluate directory restructuring after the harness/topic boundary is defined; migrate only if the new boundary reduces coupling.
- [ ] If restructuring proceeds, provide compatibility for old entry points or a one-time migration guide, and update script references and documentation links together.

Acceptance: a first-time operator can run the minimal example and locate its artifacts from the README; the directory layout expresses the harness, topic, adapter, and runtime-state boundaries directly.

### 7. Containerize the system

- [ ] Pin shell and system-tool dependencies and provide a minimal reproducible image.
- [ ] Define mount boundaries for agent CLIs, credentials, repository source, and runtime artifacts.
- [ ] Provide a container smoke test covering generation, review, resume, and export paths.
- [ ] Compare artifact formats and exit semantics between host and container execution.

Acceptance: a clean environment starts the minimal flow through one entry point; credentials are absent from the image; host and container artifacts remain compatible.

### 8. Productize the workflow

Stable CLI operations and auditable run records precede any UI evaluation.

- [ ] Stabilize `init`, `run`, `status`, `resume`, `review`, and `export` operations and their exit semantics.
- [ ] Provide a versioned configuration schema, an example topic, a run registry, and a result-browsing entry point.
- [ ] Establish release versions, upgrade paths, and end-to-end acceptance.
- [ ] Decide whether to add a local UI or service entry point from observed workflows.

Acceptance: a new project can be initialized, run, resumed, audited, and exported; upgrades preserve existing configurations and run records.

## Delivery Order

1. `0 + 1`: stabilize the control plane, align decisions, and complete observability and calibration.
2. `2 + 3 + 5`: define architecture and data boundaries, decide storage, and design user documentation.
3. `4 + 7`: add concurrency and portable deployment on top of isolation and transactions.
4. `8`: form product entry points around a stable CLI, configuration, and run records.

Until decision rules, observability, and architecture boundaries are stable, ledger growth, higher concurrency, and UI work do not substitute for validating the SA success rate.
