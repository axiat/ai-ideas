# Scalable History Runtime Implementer Contract

Status: approved for implementation on 2026-08-03  
Design manifest: `ba90e154d8101068d7d372014208b3642dec74bc87574f36f90a300adccbcf7f`  
OpenSpec change: `openspec/changes/scalable-history-runtime/`

## P0 Boundary

- Keep SQLite candidates, lineages, import epochs, and ledger projections unchanged. Add v2 as a sibling ABI; select it only with `HISTORY_RUNTIME_ABI=v2`. V1 rejects `HUNT_*` and `AWR_*` provider controls instead of ignoring them.
- Preserve every legacy receipt byte identity. A legacy `complete_match` or `complete_no_match` is compatibility evidence only and never qualifies v2 authority.
- Each compatibility row binds legacy receipt ID, exact old JSON SHA, old pack-publication ID, old status/relation tokens, migration ID, and `compatibility_state=unqualified|ambiguous`. It cannot join a v2 qualification or final-status path.
- Keep production `complete_no_match` closed until execution, adjudication, and semantic qualification are all current for the selected `no_match_basis`.
- Never pass a full ledger or durable database to an agent. Every agent sees one disposable mirror and declared bounded artifacts.
- Never register or fall back to Claude. Offline tests launch local fake providers only.

## Provider Contract

| Surface | Providers | Default model behavior | Reasoning override |
|---|---|---|---|
| Hunt | `codex`, `kimi`, `grok` | Omit model to use and freeze the CLI's current default | Codex and Grok use verified provider grammar; Kimi rejects an explicit reasoning value |
| AwR | Hunt set plus `opencode`, `agy` | Same | OpenCode uses `--variant`; agy uses `--effort` |

The canonical field `provider_pools_ordered.{comparator,map,detail,reduce}` is both allowlist and failover order. Flattened aliases and a separate failover field are rejected. Pool order, resolved-default capability, capacity, prompt/schema, risk, or settlement changes produce a new plan and logical task identity. Actual provider/model/reasoning belongs only to attempt provenance.

Local CLI argument grammar is verified; model availability, effective override, capacity, and price are not. Target v2 commands are:

```bash
rtk env HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=codex HUNT_MODEL=gpt-5.6-sol HUNT_REASONING_EFFORT=xhigh ./hunt.sh
rtk env HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=kimi HUNT_MODEL=kimi-code/k3 ./hunt.sh
rtk env HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=grok HUNT_MODEL=grok-4.5 HUNT_REASONING_EFFORT=high ./hunt.sh
rtk env HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=opencode AWR_MODEL=openai/gpt-5.6-sol AWR_REASONING_EFFORT=high ./awr-side.sh
rtk env HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=agy AWR_MODEL=gemini-3.6-flash-high AWR_REASONING_EFFORT=high ./awr-side.sh
```

Omitting `*_MODEL` and `*_REASONING_EFFORT` preserves the selected CLI's current configuration. A default marker permits diagnostic/shadow execution only. Hard-complete planning requires a probe to bind an effective model and reasoning identity, or an immutable equivalent identity that also binds effective context, token bound, serializer, usage source, and CLI revision. Unsupported, ineffective, or drifted explicit overrides fail before launch.

## Identity and Corpus Contract

- Canonical bytes are UTF-8, NFC, duplicate-key rejecting, recursively key-sorted, newline-terminated JSON. Compound hashes use domain-separated length-prefixed bytes.
- Freeze `history_as_of_watermark`, snapshot ID/hash, v2 `staging_candidate_id` values, `current_batch_ids_hash`, `current_batch_id_namespace=history-v2-staging-v1`, and `exclusion_policy_sha` before comparison.
- A staging ID never occupies or predicts the legacy `candidates.candidate_id`. The existing append path assigns the legacy ID during activation; an immutable activation map binds staging ID, legacy candidate ID, source sequence, raw artifact SHA, pair-plan/result hashes, and activation receipt.
- Prior-history predicates are `source_sequence <= watermark` plus the v2 staging exclusion namespace. Replay proves every activated legacy ID maps to the batch and has a source sequence above the watermark. Batch-internal duplicate detection is a separate plan before activation.
- Candidate content identity excludes provider, model, reasoning, CLI, and attempt data.

Direction identity is run-scoped and host-owned: `(run_id,batch_id,direction_id,contract_sha,validator_version,artifact_sha)`. Metadata may project contract-scoped soft annotations but cannot own direction state or create a global accepted concept.

## Shared Semantic ABI

```text
semantic_relation = blocking_duplicate | substantive_overlap |
                    related_only | distinct | uncertain
lineage_relation  = same_revision | evolved_from | recheck_of |
                    supersedes | none
final_status      = overlap_found | complete_no_match | uncertain |
                    partial | invalid
```

Status priority is fixed:

1. invalid identity/schema/anchor;
2. verified hit, retaining `match_found_partial_coverage` when execution is incomplete;
3. no-hit execution or budget gap as `partial`;
4. unresolved semantic conflict as `uncertain`;
5. clean unqualified no-hit as `uncertain/semantic_policy_unqualified`;
6. qualified clean no-hit as `complete_no_match`.

Reason codes never promote status. Coverage never proves semantic correctness.

## Closed Receipt ABI

Every persisted v2 receipt is a closed `history-audit-receipt-v2` object. It requires:

```text
manifest_schema_version        canonical_codec_version
run_id                         plan_hash
candidate_hash                 snapshot_id / snapshot_hash
history_as_of_watermark        current_batch_id_namespace
current_batch_ids_hash         exclusion_policy_sha
expected_asset_ids_hash        observed_asset_ids_hash
missing_ids / duplicate_ids / extra_ids
invalid_schema / invalid_anchor / truncated
provider_pools_ordered.{comparator,map,detail,reduce}
provider_capability_profile_hashes
capacity_profile_id            semantic_policy_profile_id
risk_policy_version            matched_router_rule_ids
settlement_policy_sha          shard_plan_sha
logical_task_hashes            attempt_manifest_hashes
raw_request_output_cas_hashes  minimum_receipt_sha
coverage_complete              adjudication_complete
semantic_policy_qualified      no_match_basis
final_status                   stage_reason_code
evidence_anchors
```

`no_match_basis` is `null` except for `complete_no_match`, where it is exactly `l1_calibrated` or `l2_exhaustive`. The aliases `basis`, `excluded_batch_ids_hash`, flattened provider pools, and unknown fields are rejected. A receipt cannot refer to an attempt payload until its CAS object and durable descriptor exist.

## L1, Metadata, and L2

- M0 L1 preserves exact/lineage, FTS, near-duplicate, and dense-core reachability. Deduplicate identical query views; collapse revisions and expansions to one vote per lineage per family.
- 2A synopsis, concepts, free tags, and clusters are versioned derived data. They are shadow-only additive union and never direction, activation, or default SQL filters.
- L2 plans from the same frozen snapshot. `safe-24k-v1` uses at most 12 compact records and a validated final-request token bound per map shard; 550 records require at least 46 shards.
- Every hard-complete stage pool validates every member, then uses `B_pool=min(B_p)` and `B_target=floor(utilization*B_pool)` for packing and final-render recount. One unbudgetable member invalidates the pool; runtime overflow cannot retroactively reinterpret a parent plan.
- Each map output covers every assigned ID exactly once. Overflow, truncation, missing/duplicate/extra ID, stale snapshot, or invalid anchor invalidates the parent. Overflow splits deterministically; one-item overflow exhausts.
- Host coverage commits all expected IDs. Detail/reduce reads only blocking, substantive, and uncertain cards. Equal valid attempts settle once; divergent valid attempts settle to `conflict`, independent of arrival order.
- Logical tasks use `planned | claimed | settling | settled | superseded | exhausted`. Superseded and exhausted parents never contribute coverage; exactly one terminal settlement references every valid attempt. Recovery reclaims only expired claims and resumes only unsettled tasks.

## Budget, Cost, and CAS

- Enforce per-intent, per-round and per-candidate limits for candidates, started attempts, input tokens, output tokens, provider usage units, and optional currency micros.
- Reserve before launch and settle every started attempt, including retry, failover, split, detail, reduce, and billable cancellation. Unknown price remains unknown.
- Report realized `L1 + p(escalation) * L2` calls, tokens, usage units, and latency per intent. Monetary cost exists only with a named verified source.
- Store every L2 request/output in compressed CAS before validation or settlement. Descriptors bind raw content SHA, compressed-byte SHA, codec/version, raw/compressed lengths, creation/expiry, and integrity state. Pin final evidence; preserve minimum receipts permanently; tombstone before deletion; treat unexplained absence or hash mismatch as an integrity fault.

## Semantic Release Gate

- `shadow-calibration-v1` starts at 30 independent blocking-positive lineages, at least five positives in each of low-overlap, cross-language, and lineage-revision slices, plus 20 adjudicated hard negatives or true no-matches.
- Shadow readiness enables diagnostics only. Production qualification requires at least 300 independent blocking-positive query lineages, configured one-sided 95% recall/FNR gates, required bad-slice bounds, exact profile/corpus/evaluation/report bindings and expiry, provider capacity evidence, and passing fault/replay evidence.
- Provider/prompt/capacity changes stale adjudication and qualification, not FTS. Metadata changes stale only metadata-dependent generations. Missing qualification keeps no-match nonpermanent.
- The router receipt binds its ordered rule-table version and every matched rule ID. Routing may select L2 or shadow work but cannot bypass reservation, coverage, adjudication, qualification, or status derivation.

## Completion Gates

- Every behavior begins with an observed failing test and ends with focused plus regression green evidence.
- OpenSpec strict validation, Python tests, shell ABI tests, shell syntax, `git diff --check`, migration restart, fault injection, CAS recovery, and no-Claude static scans pass.
- An independent test agent and an independent whole-branch audit both pass after all fixes.
- Runtime completion does not claim real-provider capacity, production qrels qualification, monetary price accuracy, or production no-match authority unless those external evidence artifacts actually exist.
