## Purpose

Define a bounded and replayable internal-history audit whose fast path scales with asset growth and whose expensive path can prove execution coverage without confusing it with semantic correctness.

## ADDED Requirements

### Requirement: Every audit uses a frozen prior-history boundary
The host SHALL freeze a source-sequence watermark, snapshot identity, v2 staging-candidate ID set and namespace, and exclusion-policy identity before comparing a candidate. A staging ID SHALL NOT occupy or predict the legacy canonical candidate ID; activation SHALL use the existing append allocator and persist an immutable staging-to-legacy identity map. Prior-history queries SHALL read only records at or below that watermark and SHALL exclude the staged batch namespace; batch-internal duplicate detection SHALL run separately before activation.

#### Scenario: Current candidate cannot match itself
- **WHEN** a raw candidate has a host-assigned v2 staging ID in the current batch
- **THEN** its staging ID is excluded from L1 and L2 prior-history inputs while remaining present in the batch-internal comparison plan, and its later legacy ID maps to a source sequence above the watermark

#### Scenario: Concurrent append does not change the audit corpus
- **WHEN** another run activates a candidate after the watermark is frozen
- **THEN** the active audit's snapshot ID, expected asset IDs, and result remain unchanged

### Requirement: L1 preserves an unfiltered flat baseline
L1 SHALL union exact/lineage, lexical, near-duplicate, and dense-core retrieval with optional metadata families. Tags, summaries, taxonomy, and clusters SHALL be additive signals and SHALL NOT be default hard filters. Multiple revisions, duplicate query views, or repeated metadata expansions SHALL NOT multiply a lineage's family weight.

#### Scenario: Corrupt metadata cannot hide a baseline hit
- **WHEN** all tags are removed, randomized, or stale
- **THEN** any lineage reachable through the frozen flat baseline remains in the retrieval domain

#### Scenario: Revisions do not create extra votes
- **WHEN** one lineage has several visible revisions
- **THEN** each retrieval family contributes at most one normalized lineage score

### Requirement: L2 plans deterministic token-and-item bounded shards
L2 SHALL partition every expected snapshot ID into deterministic shards using the selected capacity profile's final-request token bound and item cap. An overflow, truncation, missing/duplicate/extra ID, or invalid anchor SHALL invalidate the parent result; overflow SHALL produce deterministic non-overlapping split children, and an unsplittable item SHALL exhaust locally.

#### Scenario: Safe profile enforces both bounds
- **WHEN** `safe-24k-v1` plans 550 compact records
- **THEN** no shard exceeds 12 records or its serialized evidence limit and the plan contains at least 46 map shards

#### Scenario: Invalid parent is not counted as coverage
- **WHEN** a parent shard overflows and two split children later succeed
- **THEN** only the valid children contribute observed IDs and the parent is recorded as superseded

### Requirement: Settlement is deterministic and exactly once
Attempts SHALL be at least once while each logical task has exactly one terminal settlement. A retry starts only when no valid result exists. Multiple valid normalized results SHALL settle once when equal and SHALL produce `uncertain/conflict` when they differ, independent of arrival order.

#### Scenario: Duplicate completion commits once
- **WHEN** two attempts return the same normalized item relations and anchors
- **THEN** one logical result is committed and both attempts remain in provenance

#### Scenario: Conflicting completions do not use first arrival
- **WHEN** valid attempts disagree on relation, item set, or evidence anchors
- **THEN** every arrival permutation settles to the same conflict receipt

### Requirement: Coverage, adjudication, and semantic qualification are separate gates
`coverage_complete` SHALL mean exact expected/observed ID equality with no duplicate, extra, truncation, schema, or anchor error. `adjudication_complete` SHALL mean every required comparator/detail/reducer task is valid with no unresolved conflict. Neither flag SHALL imply `semantic_policy_qualified`.

#### Scenario: Coverage alone cannot publish no-match
- **WHEN** every ID has a schema-valid `distinct` map result but semantic qualification is absent
- **THEN** final status is `uncertain` with reason `semantic_policy_unqualified`

#### Scenario: Reducer reads only exceptional cards
- **WHEN** the host has full coverage over `R` records and `K` records are hit or uncertain
- **THEN** reducer input contains only the `K` cards while the coverage receipt still commits all `R` IDs

### Requirement: Final status follows a closed priority order
The only v2 final statuses SHALL be `overlap_found`, `complete_no_match`, `uncertain`, `partial`, and `invalid`. Invalid identity/schema/anchor evidence SHALL win first; a verified hit SHALL survive execution gaps as `overlap_found/match_found_partial_coverage`; no-hit execution gaps SHALL be `partial`; unresolved semantic conflict SHALL be `uncertain`; `complete_no_match` SHALL require all three gates and no blocking, substantive, or uncertain relation.

#### Scenario: Positive survives partial coverage
- **WHEN** one adjudicated blocking hit exists and another shard is exhausted
- **THEN** final status is `overlap_found` and reason is `match_found_partial_coverage`

#### Scenario: Budget exhaustion cannot become no-match
- **WHEN** no hit exists and a leaf exhausts its attempt or split budget
- **THEN** final status is `partial` and reason is `budget_exceeded`

### Requirement: V2 receipts use one closed persisted schema
Every persisted v2 receipt SHALL bind the manifest and codec versions; run, plan, candidate, frozen snapshot, watermark, staged-batch namespace and exclusion hashes; expected/observed ID hashes and all coverage faults; canonical ordered stage pools and capability/capacity profiles; shard, task, attempt, CAS, settlement, risk-router and evidence identities; the three completion gates; `no_match_basis`; `final_status`; and `stage_reason_code`. Unknown fields and the aliases `basis`, `excluded_batch_ids_hash`, flattened provider pools, or a separate failover field SHALL be rejected. `no_match_basis` SHALL be non-null only for `complete_no_match` and SHALL equal `l1_calibrated` or `l2_exhaustive`.

The required canonical fields SHALL be:

```text
manifest_schema_version, canonical_codec_version, run_id, plan_hash,
candidate_hash, snapshot_id, snapshot_hash, history_as_of_watermark,
current_batch_id_namespace, current_batch_ids_hash, exclusion_policy_sha,
expected_asset_ids_hash, observed_asset_ids_hash, missing_ids, duplicate_ids,
extra_ids, invalid_schema, invalid_anchor, truncated,
provider_pools_ordered.{comparator,map,detail,reduce},
provider_capability_profile_hashes, capacity_profile_id,
semantic_policy_profile_id, risk_policy_version, matched_router_rule_ids,
settlement_policy_sha, shard_plan_sha, logical_task_hashes,
attempt_manifest_hashes, raw_request_output_cas_hashes,
minimum_receipt_sha, coverage_complete, adjudication_complete,
semantic_policy_qualified, no_match_basis, final_status, stage_reason_code,
evidence_anchors
```

#### Scenario: Legacy aliases cannot enter a v2 receipt
- **WHEN** a receipt contains `basis`, `excluded_batch_ids_hash`, or a flattened provider pool
- **THEN** receipt validation fails before persistence or replay

#### Scenario: Receipt references durable CAS only
- **WHEN** an attempt request or output lacks a verified CAS object and durable descriptor
- **THEN** its hash cannot enter a task settlement or completion receipt

### Requirement: Per-intent budgets are reserved and settled
The runtime SHALL enforce candidate, started-attempt, input-token, output-token, provider-usage-unit, and optional currency ceilings per intent and round. Planning SHALL reject an over-limit candidate set before launching workers; every retry, failover, split child, detail, and reduce attempt SHALL reserve from and settle against the same ledger.

#### Scenario: Candidate ceiling is atomic
- **WHEN** a round requests more guarded candidates than its intent allows
- **THEN** none of those candidates starts and the budget receipt records the rejected reservation

#### Scenario: Positive takes priority after budget exhaustion
- **WHEN** the remaining round budget cannot start all shards but a verified hit already exists
- **THEN** the result remains `overlap_found/match_found_partial_coverage` and the missing work remains visible

### Requirement: Audit traces use retained content-addressed storage
Every L2 request/output and every large rank-trace payload SHALL be compressed and content addressed before validation or settlement. The database SHALL retain raw and compressed hashes, codec/version, lengths, object descriptors, pins, minimum receipts, tombstones, and integrity state. Garbage collection SHALL preserve final evidence, write a tombstone before deleting an unpinned expired payload, and distinguish normal expiry from missing or corrupt data.

#### Scenario: Final evidence survives retention
- **WHEN** an object reaches expiry while pinned by a final overlap receipt
- **THEN** garbage collection leaves the payload and pin intact

#### Scenario: Corruption is not normal expiry
- **WHEN** an object is absent before a valid tombstone or its hash mismatches
- **THEN** verification reports an integrity fault and cannot mint completion

### Requirement: Legacy receipts remain compatibility evidence only
The runtime SHALL preserve v1 rows and exact hashes without relabeling them. Legacy statuses and relations SHALL be available only through a versioned compatibility record; ambiguous mappings SHALL become uncertain and no legacy row SHALL qualify v2 no-match authority.

#### Scenario: Old no-match is quarantined
- **WHEN** migration encounters a v1 `complete_no_match` receipt
- **THEN** the v2 compatibility record preserves its identity and marks it unqualified without creating an L1 or L2 release receipt
