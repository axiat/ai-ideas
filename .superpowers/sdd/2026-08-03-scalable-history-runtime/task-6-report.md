# Task 6 Report

- Base: `9d4a104dc62b84a934815c0f16f92d5c43c7926f`
- Commit: `74f665d` (`feat: add semantic shadow release router`)
- Scope: qrels validation, shadow readiness, production qualification and veto, immutable qualification lookup/invalidation, deterministic routing, and event-derived cost summaries.

Verification:

- Task 6 smoke: 16 passed.
- V2 Python regressions: 154 passed across contract, migration, provider, plan, L1, metadata, store, and runtime suites.
- V1 shell runtime regression: exit 0.
- `openspec validate scalable-history-runtime --strict`: valid.
- `git diff --check`, Python compilation, JSON parsing, and scoped no-Claude scan: clean.

Repository fixtures remain `diagnostic_synthetic`; they can reach `shadow_ready` but cannot produce production qualification. No real provider capacity, production qrels, replay/fault evidence, or verified price evidence was added. Production `complete_no_match` therefore remains unavailable unless an exact live, non-invalidated qualification is persisted for the current basis and dependencies.

## Review fix round 1 checkpoint

- Production `complete_no_match` is fail-closed for both L1 and L2 with stable reason `production_runtime_authority_unavailable`. Direct SQL, public writer, private authorization, and current-authority replay cannot mint a receipt or authorization. Historical authorization replay is archival-only.
- Semantic qualifications are recomputed from raw evaluator inputs and persisted under connection-local issuance guards. Qrels bind independent lineage relations and one cross-role lineage partition map.
- Migration ledger writes bind the exact component, version, SHA, and timestamp; updates and deletes are immutable. Startup compares every managed `audit_*` object against a pristine reference schema, replays migration probes, and checks audit foreign keys under `BEGIN IMMEDIATE`.
- Durable attempt launch and terminal cost facts are append-only and host-guarded. Summaries read the database by `run_id`, recompute attempt identity and kind, retain unknown billing/latency as incomplete, quarantine pre-ledger attempts, and reject caller-constructed event lists.

Checkpoint verification: evaluation 30 passed; runtime 45 passed; CAS 11 passed; migration 11 passed; store 13 passed; Python compilation and `git diff --check` clean. Candidate route/slice facts and timestamp-derived latency remain in the next Task 6 checkpoint; expected and slice summaries stay explicitly unavailable until those facts exist.

## Review fix round 2 checkpoint

- Selected candidate cohorts, raw router facts, matched rules, routes, `call_l1_model`, and risk slices are immutable host-issued facts. Risk policy identity is bound to the validated runtime plan; the fixed critical-slice policy rejects caller-expanded slice vocabularies. Summary replay recomputes every route decision and fact hash after database reopen.
- L2 dispatch is a separate immutable fact bound to the exact run, candidate, route fact, and validated L2 plan. Plan persistence and attempt launch fail closed without route and dispatch authority. A frozen two-candidate cohort with one dispatched candidate retains the zero-attempt candidate in the denominator: candidate count 2, dispatch count 1, escalation rate 0.5, and expected calls per candidate 0.5.
- Direct-L2 expected cost uses `0 + escalation_rate * L2_per_escalation`. Any candidate requiring L1 reports `durable_l1_attempt_facts_unavailable`; legacy plans without route facts report `candidate_route_facts_unavailable`. Pre-ledger attempts remain quarantined rather than backfilled.
- Realized cost is grouped by intent, provider, and overlapping risk slice. Retry, failover, split-child, failed, cancelled, token, usage-unit, and optional currency facts retain their durable attempt bindings. Detail and reduce counters remain present in the schema with `producer_unavailable`; current runtime does not claim producer coverage for those kinds.
- Queue latency is derived from durable task-ready or predecessor-terminal time to attempt launch. Run latency is derived from launch to terminal settlement. Injected timestamps are checked against those durable boundaries; legacy unknown latency is omitted from totals.

Checkpoint verification: 199 Python tests passed across evaluation, runtime, contract, migration, provider, plan, L1, metadata, and store suites; the 80-test shell runtime suite passed; strict OpenSpec validation passed; Python compilation and `git diff --check` passed. Production `complete_no_match` remains fail-closed with `production_runtime_authority_unavailable`.

## Review fix round 3 checkpoint

- Candidate route issuance now precedes the entire L2 lifecycle. Any existing L2 plan, budget reservation, or attempt for the run rejects the public route writer. Idempotent plan persistence verifies existing route material without reopening issuance; a legacy plan cannot acquire route facts retroactively.
- The route cohort equals the complete frozen batch member set. Omitting any frozen candidate rejects plan persistence, so zero-attempt and non-dispatched candidates remain in the denominator without a separate selected-cohort claim.
- L2 dispatch consumes a one-shot connection-local issuance held only around insertion of one new exact plan in the same transaction. Public dispatch calls and existing plans cannot activate the guard or retrofit attempt authority. Existing plan replay verifies the durable dispatch and never issues a replacement.
- Raw router and risk-slice inputs carry an immutable `host_issued_shadow` observation boundary with `production_authority=0`. Caller-supplied `release_qualified=true` is rejected because current durable production runtime authority is unavailable. Summaries expose the shadow boundary explicitly; it cannot authorize production `complete_no_match`.
- Databases upgraded from pre-boundary route and dispatch facts receive no fabricated boundary. Their route summaries report `candidate_route_observation_boundary_unavailable`, and old dispatch facts cannot launch new attempts.

Checkpoint verification: 205 Python tests passed across evaluation, runtime, contract, migration, provider, plan, L1, metadata, and store suites; the 80-test shell runtime suite passed; strict OpenSpec validation passed; Python compilation and `git diff --check` passed. Production `complete_no_match` remains fail-closed with `production_runtime_authority_unavailable`.
