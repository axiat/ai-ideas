## Purpose

Separate early shadow calibration from production release authority so small real datasets can guide development while no-match decisions remain fail-closed until statistically supported.

## ADDED Requirements

### Requirement: Qrels preserve lineage, time, relation, and slice identity
Each qrel SHALL identify query lineage, historical lineage, as-of sequence, shared semantic relation, evidence anchors, adjudication state, and zero or more risk slices. Train, development, and test partitions SHALL be disjoint by lineage and temporal group; synthetic contract fixtures SHALL NOT qualify a production policy.

#### Scenario: Lineage leakage invalidates qualification
- **WHEN** related revisions of one lineage appear in more than one partition
- **THEN** evaluation fails before computing a release qualification

#### Scenario: Synthetic scope remains diagnostic
- **WHEN** all thresholds pass on repository synthetic fixtures
- **THEN** the result is reported as contract evidence and production qualification remains false

### Requirement: Minimum viable qrels can start shadow calibration
The `shadow-calibration-v1` readiness state SHALL require at least 30 independent blocking-positive query lineages, at least five positive examples in each of low-overlap, cross-language, and lineage-revision slices, and at least 20 adjudicated hard-negative or true-no-match query lineages. This state SHALL enable diagnostics and threshold tuning only; it SHALL NOT release production no-match authority.

#### Scenario: Small real set starts shadow work
- **WHEN** the minimum counts, evidence anchors, lineage grouping, and temporal checks pass
- **THEN** the profile enters `shadow_ready` and records sample counts and confidence intervals

#### Scenario: Shadow readiness is not production authority
- **WHEN** a policy is `shadow_ready` but lacks the production sample and metric gates
- **THEN** every clean no-hit remains `uncertain/semantic_policy_unqualified`

### Requirement: Production qualification uses explicit statistical gates
A production semantic policy SHALL compute its minimum-positive count, aggregate recall and false-negative bounds, negative count, and required bad-slice bounds exclusively from held-out qrels with `partition=test`. Training qrels MAY fit a model and development qrels MAY tune policy, but neither partition SHALL contribute a production release count or metric. The held-out test partition SHALL contain at least 300 independent blocking-positive query lineages and pass the configured one-sided 95% bounds. Production qualification SHALL also require complete provider capacity profiles and passing fault/replay evidence. Qualification SHALL bind the complete validated qrels and exact outputs across all declared partitions, the test-only metric report, the L1 or L2 policy, prompts/schemas, provider pools, capacity profiles, and corpus/evaluation identity.

#### Scenario: Training and development evidence cannot satisfy release gates
- **WHEN** train contains 300 passing positives, development contains passing negatives, and the held-out test partition has no positives
- **THEN** production qualification is false, the production aggregate abstains with denominator zero, and train/development contribute no release counts

#### Scenario: Held-out test controls production metrics
- **WHEN** the held-out test partition passes every production count and bound while train or development outputs fail
- **THEN** production metrics use only the held-out test outcomes while the evaluation identity still changes with any exact output change in any declared partition

#### Scenario: Missing bad-slice evidence vetoes release
- **WHEN** aggregate recall passes but a required slice lacks enough observations or misses its bound
- **THEN** production qualification is false and the slice records `abstain` or `failed`

#### Scenario: Complete qualification can authorize one basis
- **WHEN** all L2 production gates pass for a frozen profile while L1 remains unqualified
- **THEN** only `no_match_basis=l2_exhaustive` can use that qualification

### Requirement: Qualification invalidation is dependency local
Provider/model/reasoning/prompt/schema/capacity changes SHALL stale the affected adjudication and semantic qualification without rebuilding unrelated lexical or metadata projections. Embedding/tokenizer/metadata-profile changes SHALL stale only their dependent search generations and any qualifications that bind them.

#### Scenario: Provider change preserves lexical generation
- **WHEN** a comparator provider default changes
- **THEN** comparator capacity and semantic qualifications become stale while the active FTS generation remains unchanged

#### Scenario: Metadata failure cannot revoke flat reachability
- **WHEN** a metadata generation is stale or unavailable
- **THEN** its channel is omitted from shadow union and the flat baseline continues unchanged

### Requirement: Production no-match is vetoed by default
Absent a current matching qualification and complete execution/adjudication receipts, the runtime SHALL NOT emit or persist v2 production `complete_no_match`. A provisional clean result SHALL remain nonpermanent and SHALL retain the exact veto reason.

#### Scenario: Fresh installation is fail-closed
- **WHEN** the runtime has no production qrels qualification
- **THEN** clean L1 and L2 no-hit results cannot authorize permanent no-match

#### Scenario: Expired qualification revokes authority
- **WHEN** a bound profile becomes stale after a dependency change
- **THEN** subsequent no-hit decisions return `uncertain/semantic_policy_unqualified` until requalification
