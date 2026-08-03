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
