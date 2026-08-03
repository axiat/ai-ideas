## Context

The accepted architecture is the frozen nine-document design identified by manifest `ba90e154d8101068d7d372014208b3642dec74bc87574f36f90a300adccbcf7f`. The live code is a v1 SQLite/FTS/hash-dense runtime with bounded packs, Codex-specific contained stages, overloaded receipt status, and large traces embedded in SQLite. Canonical candidates, lineages, and ledger projections are already protected by transactional and replay contracts. They must remain byte-compatible while v2 is introduced.

The implementation runs on a local host with disposable mirrors. Portability and provider choice are favored over the v1 Codex-specific OS containment, but all canonical identity, database writes, output validation, budget decisions, and receipts remain host-owned. Offline tests use local fake providers and never invoke a real model.

## Goals / Non-Goals

**Goals:**

- Deliver the v2 common contracts first: canonical identity, frozen corpus boundaries, final status/reason, provider capability and capacity, deterministic settlement, receipts, budget accounting, and CAS retention.
- Deliver M0 flat L1, additive 2A metadata shadow, minimal exhaustive L2, and a deterministic guarded router on those contracts.
- Expose provider/model/reasoning selection for Hunt and AwR while preserving each CLI's current defaults when overrides are omitted.
- Preserve v1 operation and make v2 an explicit, replayable opt-in until its production release gate is qualified.

**Non-Goals:**

- ANN, cluster-tree routing, learned risk prediction, semantic hedging by default, live provider calibration, price discovery, or a production SLA.
- Rewriting immutable candidate, lineage, import-epoch, or ledger-projection identity.
- Treating internal no-match as academic novelty.
- Granting production no-match authority from synthetic tests, legacy receipts, metadata coverage, or L2 ID coverage alone.

## Decisions

### 1. Add a sibling v2 runtime instead of mutating v1 in place

New v2 tables, receipts, and entry points sit beside the existing history runtime. `HISTORY_RUNTIME_ABI=v1|v2` selects the path explicitly; v1 remains the default until guarded integration evidence passes. A compatibility table binds each imported legacy receipt to its exact source hash and marks it unqualified.

This avoids an in-place status rename that could turn an old `complete_no_match` into new authority. Reverting to the pre-database flow was rejected because it reintroduces unbounded history reasoning and loses the current transactional projection core.

### 2. Use one canonical codec for every v2 identity

Canonical JSON is UTF-8, NFC-normalized, closed-schema, duplicate-key rejecting, recursively key-sorted, and terminated by one newline. Identity composition uses domain-separated length-prefixed byte fields; ordered arrays remain ordered, while ID sets are sorted before encoding. Fixed test vectors cover non-ASCII text, null, booleans, nested objects, and ordered provider tuples.

`plan_hash` binds the snapshot, canonical `provider_pools_ordered.{comparator,map,detail,reduce}`, resolved capability profiles, capacity profile, prompt/schema, risk policy, and settlement policy. Flattened pools and a separate failover field are invalid. A logical task binds plan, stage, candidate, and deterministic shard input. Attempt identity adds ordinal plus actual provider/model/reasoning provenance. Candidate content identity excludes all provider facts.

The persisted `history-audit-receipt-v2` schema is closed. It binds manifest/codec versions, run/plan/candidate, snapshot/watermark and staged-batch exclusion identities, expected/observed ID roots and fault counts, ordered pools and profile hashes, shard/task/attempt/CAS/settlement/risk/evidence identities, all three completion gates, `no_match_basis`, `final_status`, and `stage_reason_code`. Legacy aliases are rejected, and attempt hashes enter a receipt only after their CAS descriptors are durable.

Plain `json.dumps()` hashes were rejected because implicit Unicode, null/default, and nested ordering conventions are not a cross-language ABI.

### 3. Keep provider adapters narrow and portable

A provider registry declares surface eligibility, executable/argv grammar, model and reasoning override grammar, capability probe, serializer revision, token counter or bound, usage parser, and artifact protocol. The runtime preserves omitted CLI defaults but freezes an effective capability identity. A bare provider-default marker is diagnostic/shadow-only; hard-complete work requires effective model/reasoning or an immutable equivalent identity bound to context, token bound, serializer, usage source, and CLI revision. It never guesses model names, capacity, price, or support for reasoning.

Portable attempts receive a disposable mirror populated from an explicit input manifest. The process runs with its normal local authentication/configuration and writes one declared artifact. The host validates the target's type, size, schema, prompt attestation, and request identity before importing it. The canonical database, full ledger, `.git`, and unrelated state are not mirrored.

Adapting the existing contained Codex registry for every provider was rejected because its proxy, auth, CLI-version, sandbox, and Responses-wire assumptions are provider-specific. Arbitrary command strings remain a legacy compatibility surface, not a v2 provider identity.

### 4. Separate execution state from business status

The final status vocabulary is closed to `overlap_found`, `complete_no_match`, `uncertain`, `partial`, and `invalid`. Execution details such as budget exhaustion, conflict, truncation, and provisional no-match are reason codes. Relation is split into a shared semantic relation and an independent lineage relation.

Status derivation has one fixed priority:

1. invalid identity, schema, or anchor;
2. verified blocking/substantive hit, with partial-coverage reason when necessary;
3. incomplete execution or budget;
4. unresolved semantic uncertainty/conflict;
5. clean but unqualified no-hit;
6. qualified complete no-match.

The reducer cannot override this derivation. This preserves a verified positive even when later work fails and prevents an execution gap from becoming a negative conclusion.

### 5. Build L1, 2A, and L2 as independent producers of typed evidence

M0 L1 freezes the prior-history predicate, deduplicates query views, collapses revisions per lineage and retrieval family, and preserves exact/FTS/hash-dense reachability. Before activation, raw candidates receive v2 staging IDs in a separate namespace. The existing append allocator assigns legacy canonical candidate IDs only during accepted activation, and an immutable map binds both identities, source sequence, raw artifact, pair-plan/result, and activation receipt. The activation transaction binds the direction check and required projection/outbox writes; rejection and pre-activation crashes leave canonical candidates unchanged. Direction contracts and per-candidate checks are host-owned run facts. Metadata may project direction evidence under its contract hash but cannot own direction state or create a global accepted concept. The first implementation improves fairness and receipts without claiming that hash-dense is a true semantic embedding.

2A adds versioned synopsis, controlled concept, free-tag, and provenance records through an append-only outbox. Its retrieval family is unioned after flat retrieval, receives one vote per lineage, and can be removed without reducing flat reachability. It starts in shadow and cannot gate direction validation or candidate activation.

L2 exports compact records from the same frozen snapshot. The planner applies final-render token and item caps, creates deterministic shards, and records the expected ID root. Map validates exactly one relation per input ID. Full records are loaded only for hit/uncertain detail; a bounded tree reducer sees only exceptional cards. Host coverage always commits the full expected/observed ID sets.

Serially feeding the entire ledger to one agent was rejected because history growth again becomes context growth. Using metadata folders as hard routing was rejected because stale or wrong annotations would make canonical assets unreachable.

### 6. Make attempts durable and settlement deterministic

Logical tasks use fenced compare-and-set claims. Every launched attempt is append-only and writes raw request/output to CAS before result validation. Retry starts only without a valid result. Overflow, truncation, or item-set mismatch invalidates the parent and creates two deterministic children; a one-item overflow exhausts. Equal valid completions settle once; divergent completions settle to conflict independent of arrival order.

First-valid-wins was rejected because provider latency would change semantic results and make replay nondeterministic.

### 7. Reserve budgets before work and settle every attempt

Budget policy is keyed by intent and round. It covers candidate count, started attempts, input/output tokens, provider usage units, and optional currency micros. Every hard-complete stage pool validates every member and plans against `B_pool=min(B_p)` plus its utilization target; final-render recount uses the same bound. Planning reserves worst-case resources using frozen capacity profiles; each launch consumes a reservation; completion settles actual usage when verified. Missing usage retains the reservation and marks usage unverified. Retry, failover, split, detail, reduce, and billable cancellation share the same ledger.

The guarded default has explicit per-candidate and per-round L2 ceilings. Exhaustion produces partial coverage unless a verified hit already exists. Unknown price is not represented as zero and does not block non-monetary accounting.

### 8. Store large traces in compressed CAS with permanent minimum receipts

CAS object descriptors bind raw content hash, compressed-byte hash, codec/version, lengths, creation/expiry, and integrity state. Every L2 request/output is published with a durable descriptor before validation or settlement. Final evidence is pinned. Garbage collection marks a grace-period tombstone before deletion and never removes the minimum receipt. A missing object without a valid tombstone or a hash mismatch is an integrity fault.

Moving every legacy embedded trace into CAS was rejected as expensive and unnecessary. Legacy blobs remain v1 evidence; only new v2 attempts write CAS.

### 9. Distinguish shadow readiness from production qualification

`shadow-calibration-v1` starts with 30 independent positive lineages, explicit minimum critical-slice coverage, and 20 adjudicated negatives/no-matches. It enables metric computation and router observation but never no-match authority. Production qualification retains the accepted approximately 300-independent-positive lineage gate, one-sided confidence bounds, bad-slice checks, provider capacity evidence, and replay/fault evidence.

Qualification binds all semantic dependencies and invalidates locally. Provider/prompt/capacity changes do not rebuild FTS; metadata failure does not hide flat results. A deterministic ordered rule table routes uncalibrated, finalist, disputed, bad-slice, or permanent-no-match cases to L2. The router records rule IDs and cannot override budget or release gates.

### 10. Integrate providers without hiding legacy behavior

Hunt gains `HUNT_PROVIDER`, `HUNT_MODEL`, `HUNT_REASONING_EFFORT`, and role-specific review overrides. AwR gains `AWR_PROVIDER`, `AWR_MODEL`, `AWR_REASONING_EFFORT`, and role-specific overrides. These controls are valid only with `HISTORY_RUNTIME_ABI=v2`; v1 rejects them rather than silently ignoring migration intent. The v2 resolver generates closed argv/environment manifests and readable `--print-provider-command` diagnostics without starting a model. Existing `AGENT_CMD`, `CONTAINED_*`, and `SIDE_*` remain v1 compatibility controls.

Default provider is Codex. Omitted model and reasoning use the CLI's current configuration; documentation includes one verified spelling example per provider and labels unsupported reasoning controls explicitly. No registry entry or fallback names Claude.

## Risks / Trade-offs

- [Portable mirrors inherit local CLI credentials and configuration] -> Mirror only declared inputs, validate all returned artifacts on the host, record the exact executable/profile, and describe the boundary as portability rather than containment.
- [A new v2 schema increases migration surface] -> Use a component migration ledger, idempotent transactions, closed enums, compatibility hashes, and restart tests; never rewrite v1 rows.
- [L2 can be expensive even at 550 records] -> Apply per-intent reservations, guarded routing, token-and-item shards, positive-first status, and measured cost counters.
- [Small shadow qrels can overfit] -> Keep shadow readiness separate from authority and require independent lineage/time splits plus production confidence gates.
- [Provider default resolution can drift] -> Resolve at planning time, freeze capability hashes, and make profile changes produce a new plan.
- [Metadata enrichment can become an accidental dependency] -> Keep it asynchronous, additive, removable, versioned, and covered by corruption tests.
- [CAS retention can remove data needed for replay] -> Pin final evidence, keep minimum receipts permanently, tombstone before delete, and fail on unexplained absence.

## Migration Plan

1. Add codec vectors, the closed receipt schema, component migrations, staging-to-legacy activation identity, CAS write/descriptor/minimum-receipt primitives, and legacy compatibility records without changing v1 behavior.
2. Add provider probes/resolution, capacity profiles, plan identity, budget ledgers, and diagnostic CLI; run only fake providers.
3. Add M0 L1 fairness, activation mapping, and frozen-corpus receipts in shadow beside v1.
4. Add L2 map/detail/reduce execution and deterministic settlement using the already-durable CAS primitives.
5. Add CAS tombstone/GC, restart recovery, and fault injection.
6. Add 2A metadata outbox/generation as an additive shadow family.
7. Add qrels shadow readiness, semantic qualification records, cost summaries, and the deterministic router. Keep production no-match vetoed.
8. Add explicit v2 portable integration to Hunt and AwR with v1 still the default, then run offline end-to-end compatibility tests.
9. Enable guarded v2 by operator selection only. Rollback selects v1 and its existing projection generation; v2 tables and CAS remain append-only for audit.
10. A future default flip requires independent runtime evidence and is outside this change. Production `complete_no_match` remains closed unless a current qualification is present at decision time.
