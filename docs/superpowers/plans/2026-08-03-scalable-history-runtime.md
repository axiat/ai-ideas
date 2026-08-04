# Scalable History Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to implement this plan task by
> task. Use `superpowers:test-driven-development` for every behavior and
> `superpowers:verification-before-completion` before any completion claim.

**Goal:** Add a provider-neutral, bounded, replayable v2 history-audit runtime
with M0 L1 retrieval, additive metadata shadow, guarded exhaustive L2,
per-intent budget/cost accounting, semantic release gates, and explicit Hunt
and AwR provider controls while preserving the v1 SQLite/ledger ABI.

**Architecture:** V2 is a sibling runtime selected explicitly with
`HISTORY_RUNTIME_ABI=v2`. A canonical codec and additive SQLite component
migrations own identity and durable state. Narrow provider adapters run normal
local CLIs in disposable mirrors; host code owns snapshots, capacity, budgets,
settlement, validation, CAS, receipts, status, and release authority. L1 keeps
an unfiltered flat baseline, metadata is additive shadow, and L2 scans one
frozen snapshot through deterministic token-and-item shards. Production
`complete_no_match` stays vetoed without current real qrels and provider
capacity evidence.

**Tech Stack:** Bash 3.2, Python 3.9+ standard library, SQLite, canonical
UTF-8 JSON, zlib CAS, TSV/JSONL fixtures, `unittest`, deterministic local fake
providers, OpenSpec, and Git.

## Authoritative Inputs

- OpenSpec: `openspec/changes/scalable-history-runtime/`
- P0 contract:
  `docs/superpowers/specs/2026-08-03-scalable-history-runtime-implementer-contract.md`
- Frozen accepted design under ignored
  `tmp/asset-growth-strategies/`, manifest
  `ba90e154d8101068d7d372014208b3642dec74bc87574f36f90a300adccbcf7f`
- Planning audits under ignored `tmp/implementation-planning/`

If a task brief conflicts with OpenSpec or the P0 contract, the contract wins
and the task stops for controller resolution.

## Global Constraints

- Work only in
  `/Users/qinningxu/code/ai-ideas/.worktrees/multi-provider-runtime` on branch
  `design/multi-provider-runtime`. Preserve the main checkout's existing
  `ledger.tsv` change byte-for-byte.
- Every shell command starts with `rtk`. All edits use `apply_patch`; formatting
  tools may perform mechanical rewrites.
- Never invoke a real provider during implementation or tests. Never invoke
  Claude directly or indirectly. Provider tests use only local fake
  executables and static CLI grammar fixtures.
- Preserve v1 candidate, lineage, import epoch, history receipt, runtime round,
  search projection, and ledger projection behavior. New v2 tables and files
  are additive.
- `HISTORY_RUNTIME_ABI` defaults to `v1`. No policy-file change may silently
  select v2. No task may enable production v2 `complete_no_match` from
  synthetic fixtures or legacy receipts.
- Provider/model/reasoning/CLI data never enters candidate or corpus identity.
  It enters capability profiles, plans, attempts, usage, and receipts only.
- Tags, summaries, concepts, and clusters are optional derived data. No query,
  direction gate, activation gate, or SQL predicate may require them.
- Capacity checks use the final serialized request and both token and item
  limits. Missing exact count or validated upper bound is `unbudgetable`.
- Status derivation follows the P0 priority. Verified positive evidence wins
  over partial coverage; execution gaps and semantic uncertainty never become
  no-match.
- Use one focused behavioral RED, confirm the intended failure, implement the
  smallest behavior, then run focused GREEN and the listed regressions before
  each commit. Missing-module, typo, broken fixture, or mock-call-count failures
  are not acceptable RED evidence.
- All tracked prose is English, minimal, bounded, and dense. No generated
  reports or ignored design artifacts enter commits.

---

### Task 1: Canonical V2 Identity, Component Migrations, and Legacy Quarantine

**Files:**

- Create: `lib/history_contract_v2.py`
- Create: `lib/history_audit_store.py`
- Create: `lib/history_cas.py`
- Create: `tests/history_contract_v2_smoke.py`
- Create: `tests/history_audit_migration_smoke.py`
- Create: `tests/history_audit_cas_foundation_smoke.py`
- Create: `tests/fixtures/history-v2-codec-vectors.json`
- Modify: `lib/history_store.py` only to expose a connection-safe v2 migration
  hook; do not alter existing DDL or call it from v1 startup

**Interfaces:**

```python
class ContractV2Error(ValueError):
    pass


def canonical_bytes(value):
    """Return NFC-normalized, sorted, compact UTF-8 JSON plus one LF."""


def parse_json_bytes(raw, *, allowed_fields=None):
    """Reject invalid UTF-8, duplicate keys, non-NFC text, and unknown fields."""


def framed_sha256(domain, *parts):
    """Hash a domain and uint64-be length-prefixed byte parts."""


def ordered_set_sha256(domain, values):
    """Validate unique strings, sort them, and hash canonical set bytes."""


def plan_sha256(manifest):
    """Bind the complete ordered v2 plan manifest."""


def logical_task_key(plan_sha, stage, candidate_id, input_id):
    """Return a domain-separated stable logical task identity."""


def attempt_id(task_key, ordinal, provenance):
    """Bind one attempt ordinal and actual execution provenance."""


def validate_receipt(value):
    """Validate one closed history-audit-receipt-v2 object and aliases."""
```

`history_audit_store.init_schema(conn)` applies an ordered migration list in
one transaction per component and records exact migration SHA-256 values in
`audit_schema_migrations(component,version,migration_sha256,applied_at)` with
`PRIMARY KEY(component,version)`. `schema_meta` and `PRAGMA user_version`, if
read, are checked compatibility mirrors and never v2 migration authority. The
hook creates closed, prefixed v2 tables without
changing any v1 table:

```text
audit_run_manifests        audit_snapshots
audit_batch_staging        audit_batch_pairs
audit_activation_maps      audit_direction_contracts
audit_direction_checks     audit_legacy_receipts
audit_provider_profiles    audit_capacity_profiles
audit_shard_plans          audit_logical_tasks
audit_task_attempts        audit_task_settlements
audit_budget_events        audit_receipts
audit_cas_objects          audit_cas_pins
audit_cas_tombstones       audit_metadata_profiles
audit_annotations          audit_metadata_outbox
audit_semantic_qualifications
```

`audit_receipts` stores only the closed `history-audit-receipt-v2` fields from
the P0 contract. It rejects `basis`, `excluded_batch_ids_hash`, flattened
provider pools, unknown fields, and a non-null `no_match_basis` unless status is
`complete_no_match`. Closed status/relation values use `CHECK` constraints. Immutable fact tables
reject update/delete. Mutable claim and outbox rows change only through fenced
compare-and-set helpers. `quarantine_legacy_receipts(conn)` inserts exact v1
receipt IDs, exact old JSON hashes, pack-publication IDs, old status/relation
tokens, and migration IDs with `compatibility_state='unqualified'` or
`'ambiguous'`; it never inserts v2 receipts or qualifications.

Foundation CAS interfaces are available before task execution:

```python
def put_object(conn, root, raw, retention_profile, *, pin_reason=None):
    """Compress, fsync, publish, describe, and optionally pin one CAS object."""


def verify_object(conn, root, object_id):
    """Verify raw/compressed hashes, codec, lengths, descriptor, and payload."""


def write_minimum_receipt(conn, receipt):
    """Persist the permanent closed receipt after all referenced CAS descriptors exist."""
```

- [ ] **Step 1: Write the codec RED tests against current canonical behavior**

`tests/history_contract_v2_smoke.py` first calls the existing
`history_runtime.canonical_bytes` through a small test adapter and proves the
missing v2 behavior rather than importing a nonexistent module:

```text
test_nfc_equivalent_values_have_identical_canonical_bytes
test_duplicate_json_keys_are_rejected
test_ordered_pool_order_changes_plan_sha
test_id_set_order_does_not_change_set_sha
test_provider_attempt_changes_attempt_id_not_logical_task_key
test_committed_vectors_match_literal_bytes_and_hashes
test_receipt_requires_canonical_fields_and_rejects_legacy_aliases
test_no_match_basis_is_closed_and_status_dependent
```

Run:

```bash
rtk python3 tests/history_contract_v2_smoke.py
```

Expected RED: NFC/duplicate-key or domain-framing assertions fail while the
test module and fixture load successfully.

- [ ] **Step 2: Implement the canonical codec and vectors**

Implement the interfaces above. Reject floats, bytes, sets, non-string object
keys, control characters, invalid SHA values, duplicate IDs, and integer values
outside signed 64-bit range. Preserve array order. The fixture stores literal
canonical UTF-8 text and expected hashes for nested provider pools, non-ASCII
candidate text, null/default markers, and an ordered ID set.

- [ ] **Step 3: Run codec GREEN and regression**

```bash
rtk python3 tests/history_contract_v2_smoke.py
rtk python3 tests/history_budget_smoke.py
rtk python3 tests/history_runtime_smoke.py
```

- [ ] **Step 4: Write migration and quarantine RED tests**

`tests/history_audit_migration_smoke.py` covers:

```text
test_empty_database_applies_each_component_once
test_repeated_init_is_byte_idempotent
test_migration_sha_drift_fails_closed
test_interrupted_component_rolls_back_all_ddl
test_current_populated_database_keeps_v1_schema_and_rows
test_legacy_complete_no_match_is_quarantined_not_promoted
test_unknown_legacy_status_is_preserved_as_invalid_compatibility
test_claim_compare_and_set_rejects_stale_fence
test_immutable_fact_tables_reject_update_and_delete
```

The test builds a minimal valid v1 database through existing store helpers,
then requests v2 migration. Expected RED is absence of the migration ledger or
incorrect legacy promotion, not a missing import.

- [ ] **Step 5: Implement component migrations and quarantine**

Use `BEGIN IMMEDIATE`, exact migration source hashes, foreign keys, and
idempotent inserts. Never add a v2 hook to the existing v1 `connect()` default;
the v2 CLI calls the hook explicitly. Store the legacy payload/hash and reason
without translating old relation/status into a new negative.

Direction rows bind `(run_id,batch_id,direction_id,contract_sha,
validator_version,artifact_sha)` and remain host-owned. Activation rows bind
staging ID, legacy candidate ID assigned by the existing append path, source
sequence, raw artifact SHA, pair-plan/result hashes, and activation receipt.

- [ ] **Step 6: Write and implement CAS-before-settlement RED tests**

`tests/history_audit_cas_foundation_smoke.py` covers:

```text
test_descriptor_binds_raw_and_compressed_hash_codec_and_lengths
test_equal_raw_payloads_deduplicate
test_final_evidence_pin_is_durable
test_minimum_receipt_rejects_missing_cas_descriptor
test_crash_after_cas_publish_before_receipt_can_recover_descriptor
```

Run the file before implementation and confirm the failure is descriptor/order
semantics. Implement atomic publish and verification; garbage collection and
tombstones remain Task 5.

- [ ] **Step 7: Run focused and v1 regression GREEN**

```bash
rtk python3 tests/history_audit_migration_smoke.py
rtk python3 tests/history_audit_cas_foundation_smoke.py
rtk python3 tests/history_store_smoke.py
rtk python3 tests/history_projection_smoke.py
rtk python3 tests/history_runtime_smoke.py
rtk bash tests/history_runtime_smoke.sh
```

- [ ] **Step 8: Commit Task 1**

```bash
rtk git add lib/history_contract_v2.py lib/history_audit_store.py lib/history_cas.py lib/history_store.py tests/history_contract_v2_smoke.py tests/history_audit_migration_smoke.py tests/history_audit_cas_foundation_smoke.py tests/fixtures/history-v2-codec-vectors.json
rtk git commit -m "feat: add history v2 identity and migration core"
```

---

### Task 2: Provider Registry, Portable Mirrors, Capacity, and Budget Planning

**Files:**

- Create: `lib/provider_adapters.py`
- Create: `lib/portable_agent.py`
- Create: `lib/history_audit_plan.py`
- Create: `history/provider-adapters-v1.json`
- Create: `history/capacity-profiles-v1.json`
- Create: `history/l2-budget-v1.json`
- Create: `tests/fake_portable_agent.py`
- Create: `tests/provider_adapters_smoke.py`
- Create: `tests/history_audit_plan_smoke.py`
- Modify: `tests/verify_product_contract.py`

**Provider command grammar:**

```text
codex:    codex [-m MODEL] [-c model_reasoning_effort=EFFORT]
          -c approval_policy=never exec -s workspace-write
          --skip-git-repo-check --ephemeral PROMPT
kimi:     kimi --auto --output-format text [-m MODEL] -p PROMPT
grok:     grok --always-approve --no-memory --no-subagents
          --output-format plain --cwd MIRROR [-m MODEL]
          [--reasoning-effort EFFORT] -p PROMPT
opencode: opencode run --pure --auto --dir MIRROR [-m MODEL]
          [--variant EFFORT] PROMPT
agy:      agy --dangerously-skip-permissions --disable-slash-commands
          --output-format text --add-dir MIRROR [--model MODEL]
          [--effort EFFORT] --print PROMPT
```

Kimi has no reasoning flag in CLI `0.31.1`; an explicit Kimi reasoning value
is rejected. Hunt allows `codex|kimi|grok`. AwR allows those plus
`opencode|agy`. Omitted reasoning uses the CLI default. Codex, Kimi, and Grok
also retain omitted model defaults. OpenCode omission requires a pure host
configuration probe; the resolved backend-qualified safe model enters the
profile and is passed with explicit `-m`. Every OpenCode/agy model must exactly
match the bounded local CLI catalog; model and catalog identity are re-probed
before launch. Agy requires an explicit model. The registry reasoning grammar is a conservative
verified subset: Codex `high|xhigh`, Grok `high`, OpenCode `high`, agy
`low|medium|high`, and no Kimi value. Provider-default markers remain
diagnostic/shadow-only until a probe binds effective model/reasoning or an
immutable equivalent capacity identity. No registry key, executable basename,
alias, fallback, or normalized multi-backend model route selects Claude.

**Interfaces:**

```python
def load_registry(path):
    """Return a closed provider registry with surface eligibility."""


def resolve_provider(registry, surface, provider, model=None, reasoning=None,
                     executable_lookup=None, version_probe=None):
    """Return a frozen no-launch capability and command grammar."""


def render_command(capability, mirror, prompt, schema_path=None):
    """Return closed argv and a minimal environment delta."""


def run_portable_attempt(capability, *, inputs, output_contract, prompt,
                         state_root, timeout_seconds):
    """Run one disposable mirror and import one validated output."""


def build_plan(snapshot, candidate, provider_pools, capabilities,
               capacity_profile, budget_policy, intent, records):
    """Preflight budgets and return deterministic token/item shards."""
```

Capacity profiles bind provider/default marker or model, reasoning marker,
prompt/schema, serializer, counter/bound, effective context, max output,
evidence limit, item cap, and expiry. `safe-24k-v1` has total 24K, evidence 12K,
output 3K, item cap 12, and only accepts an exact counter or validated upper
bound plus an auditable effective default identity. Real provider entries begin
`unbudgetable`; the fake profile is the only test hard-complete profile.

Budget policy defines per-intent and per-candidate ceilings for candidates,
started attempts, input/output tokens, provider usage units, and optional
currency micros. Reservation is an append-only event; settlement cannot erase
a reservation. Missing price omits currency instead of writing zero.

- [ ] **Step 1: Write provider-resolution RED tests**

Create a zero-behavior `lib/provider_adapters.py` containing only its module
docstring so imports succeed. Add tests:

```text
test_hunt_accepts_exactly_codex_kimi_grok
test_awr_adds_opencode_and_agy
test_omitted_model_and_reasoning_emit_no_override_flags
test_default_marker_is_shadow_only_without_effective_capacity_identity
test_default_model_reasoning_or_cli_drift_stales_capability
test_ignored_override_fails_before_launch
test_each_explicit_override_uses_verified_cli_spelling
test_kimi_reasoning_and_unknown_provider_fail_before_launch
test_registry_and_resolved_commands_have_no_claude_path
test_pool_failover_cannot_escape_declared_order
```

Run:

```bash
rtk python3 tests/provider_adapters_smoke.py
```

Expected RED: supported-provider/default-resolution assertions fail after a
successful import; no fake subprocess starts.

- [ ] **Step 2: Implement registry, resolver, and portable mirror**

The fake agent supports output success, extra file, symlink, hardlink,
oversize, malformed JSON, nonzero, timeout, and undeclared-read probes. Mirror
creation copies only manifest-declared regular files, resolves no symlinks,
omits `.git`, database, and ledger, writes owner-only state, starts a new
process group, kills the group on timeout, and imports only a no-follow
single-link regular target after schema and SHA validation.

- [ ] **Step 3: Run provider and mirror GREEN**

```bash
rtk python3 tests/provider_adapters_smoke.py
rtk python3 tests/verify_product_contract.py runtime
```

- [ ] **Step 4: Write capacity/plan/budget RED tests**

```text
test_plan_uses_final_serialized_request_and_rejects_unbudgetable_provider
test_safe_profile_550_records_has_at_least_46_shards
test_item_and_token_limits_both_apply
test_secondary_pool_capacity_constrains_primary_shard_before_launch
test_record_order_does_not_change_shard_membership_or_plan_sha
test_pool_order_capability_capacity_and_settlement_change_plan_sha
test_attempt_provider_does_not_change_logical_task_key
test_candidate_gate_rejects_whole_set_before_fake_launch
test_retry_split_detail_reduce_share_one_intent_budget
test_unknown_price_is_unknown_not_zero
test_usage_unverified_retains_worst_case_reservation
```

The fake counter counts tokens from the final canonical serialized request,
including schema and item wrappers. Expected RED is an incorrect plan or
budget result, not a missing fixture.

- [ ] **Step 5: Implement planning and budget events**

Packing sorts records by `(serialized_weight descending, item_id ascending)`,
places each into the admissible shard with the smallest
`(token_sum,item_count,shard_id)`, then renders and recounts every final
request. The pool bound is the minimum hard bound across the selected stage
pool: `B_pool=min(B_p)` and `B_target=floor(utilization*B_pool)`. The same
target governs final-render recount and failover; a runtime overflow never
reinterprets the parent plan. Plan-time candidate reservation is atomic.

- [ ] **Step 6: Run focused and baseline GREEN**

```bash
rtk python3 tests/history_audit_plan_smoke.py
rtk python3 tests/history_contract_v2_smoke.py
rtk python3 tests/history_budget_smoke.py
rtk python3 tests/history_store_smoke.py
rtk python3 tests/verify_product_contract.py runtime
```

- [ ] **Step 7: Commit Task 2**

```bash
rtk git add lib/provider_adapters.py lib/portable_agent.py lib/history_audit_plan.py history/provider-adapters-v1.json history/capacity-profiles-v1.json history/l2-budget-v1.json tests/fake_portable_agent.py tests/provider_adapters_smoke.py tests/history_audit_plan_smoke.py tests/verify_product_contract.py
rtk git commit -m "feat: add provider neutral audit planning"
```

---

### Task 3: Frozen Corpus, M0 L1 Fairness, and Shared Receipt Status

**Files:**

- Create: `lib/history_audit.py`
- Create: `history/history-audit-policy-v2.json`
- Create: `tests/history_audit_l1_smoke.py`
- Modify: `lib/history_audit_store.py`
- Modify: `lib/history_projection.py` only through new public read helpers;
  keep `history-projection-v4` bytes unchanged

**Interfaces:**

```python
SEMANTIC_RELATIONS = (...)
LINEAGE_RELATIONS = (...)
FINAL_STATUSES = (...)


def freeze_snapshot(conn, *, run_id, batch_id, current_batch_ids):
    """Persist the source-sequence watermark, exclusion set, and asset root."""


def stage_raw_batch(conn, *, snapshot, raw_candidates, direction_receipt):
    """Assign v2 staging IDs and freeze raw/canonical artifact hashes."""


def plan_batch_pairs(staged_batch):
    """Return deterministic batch-internal exact and semantic pairs."""


def fair_family_fusion(channel_rankings, lineage_by_candidate):
    """Deduplicate views/revisions and return one score per lineage/family."""


def build_l1_receipt(snapshot, retrieval, adjudication, qualification):
    """Build a closed v2 L1 receipt without legacy permanence."""


def derive_final_status(*, identity_valid, verified_hits, coverage_complete,
                        adjudication_complete, semantic_policy_qualified,
                        unresolved_conflict, exhausted_reason, no_match_basis):
    """Return final_status and stage_reason_code in fixed priority order."""
```

Snapshot rows use the current integer `source_sequence` boundary, not time.
The asset root hashes sorted corpus candidate IDs visible at the watermark.
Current-batch IDs are host-assigned v2 staging IDs, not provider-local `I<n>`
labels and not predicted legacy `candidates.candidate_id` values. Accepted
activation calls the existing append allocator and records an immutable map
from staging ID to legacy candidate ID/source sequence; replay proves the
legacy sequence is above the frozen watermark.
L1 reads existing exact/FTS/hash-dense indexes through the frozen predicate and
records that hash-dense is a lexical approximation, not semantic qualification.

- [ ] **Step 1: Write frozen-boundary and status RED tests**

Create a docstring-only `lib/history_audit.py` first, then add:

```text
test_snapshot_uses_source_sequence_and_excludes_current_batch_ids
test_concurrent_append_after_watermark_does_not_change_asset_root
test_batch_internal_pairs_exist_before_activation
test_provider_local_ids_are_never_used_as_corpus_exclusions
test_staging_id_never_occupies_or_predicts_legacy_candidate_id
test_activation_map_binds_existing_append_allocator_identity
test_crash_before_and_after_activation_recovers_idempotently
test_status_derivation_is_priority_ordered
test_verified_hit_with_missing_leaf_is_overlap_partial_coverage
test_clean_coverage_without_qualification_is_uncertain
test_complete_no_match_requires_basis_and_all_three_gates
test_closed_receipt_rejects_basis_and_excluded_batch_ids_hash_aliases
```

Run:

```bash
rtk python3 tests/history_audit_l1_smoke.py
```

Expected RED: current queries include post-watermark/current-batch records or
the status table derives a legacy/misordered result.

- [ ] **Step 2: Implement snapshot, staging, and status**

Activation is a separate transaction after direction and batch-pair results.
No staging failure mutates `candidates`, `lineages`, or projection outboxes.
Accepted activation binds the direction check, pair-plan/result hashes, raw
artifact, append-assigned legacy ID/source sequence, and projection outboxes in
one idempotent activation receipt. Receipt schema requires the exact P0 closed
fields, including `current_batch_id_namespace`, `history_as_of_watermark`,
`current_batch_ids_hash`, `exclusion_policy_sha`, all three gate flags,
`no_match_basis`, `final_status`, `stage_reason_code`, CAS, and evidence hashes.

- [ ] **Step 3: Write L1 fairness RED tests**

```text
test_identical_query_views_vote_once
test_multiple_revisions_vote_once_per_lineage_and_family
test_mandatory_candidates_bypass_routine_cutoff_but_semantic_hits_do_not
test_missing_mandatory_comparison_is_partial_without_hit
test_verified_mandatory_hit_survives_missing_semantic_work
test_flat_family_results_are_stable_without_metadata
```

Expected RED is duplicate score inflation or incomplete receipt coverage.

- [ ] **Step 4: Implement M0 fair fusion and L1 receipt**

Reuse existing exact/FTS/hash-dense data but compute v2 family scores outside
the v1 publication path. Exact/confirmed typed relations are the only automatic
positive sources. Normalized exact and authoritative alias/declared parent
evidence enter mandatory comparison. Semantic rank never becomes mandatory by
score alone.

- [ ] **Step 5: Run focused and retrieval regression GREEN**

```bash
rtk python3 tests/history_audit_l1_smoke.py
rtk python3 tests/history_projection_smoke.py
rtk python3 tests/history_retrieval_smoke.py
rtk python3 tests/history_retrieval_adversarial.py
rtk python3 tests/history_runtime_smoke.py
```

- [ ] **Step 6: Commit Task 3**

```bash
rtk git add lib/history_audit.py lib/history_audit_store.py lib/history_projection.py history/history-audit-policy-v2.json tests/history_audit_l1_smoke.py
rtk git commit -m "feat: add frozen l1 history audit"
```

---

### Task 4: Versioned 2A Metadata Shadow

**Files:**

- Create: `lib/history_metadata.py`
- Create: `history/metadata-policy-v1.json`
- Create: `tests/history_metadata_shadow_smoke.py`
- Modify: `lib/history_audit_store.py`
- Modify: `lib/history_audit.py`

**Interfaces:**

```python
def register_profile(conn, profile):
    """Register immutable synopsis/concept/tag producer identity."""


def enqueue_candidate(conn, candidate_id, content_sha, profile_id):
    """Append one metadata-generation outbox fact idempotently."""


def publish_annotations(conn, claim, annotations):
    """Publish append-only versioned annotations and settle the outbox."""


def shadow_rank(conn, query_annotations, snapshot, profile_ids):
    """Return one best metadata rank per visible lineage."""


def union_shadow(flat_rankings, metadata_rankings):
    """Add metadata candidates without removing or reranking flat reachability."""
```

Annotations cover bounded synopsis, controlled concepts, free tags, and soft
cluster memberships with source hash, producer/prompt/profile, created time,
and stale state. Unknown/empty values are valid. Direction evidence may be
referenced by contract hash but never creates a global accepted concept.

- [ ] **Step 1: Write metadata RED tests**

```text
test_annotations_are_append_only_and_bind_source_and_profile
test_unknown_and_missing_annotations_do_not_block_candidate
test_shadow_union_never_removes_flat_result
test_deleted_randomized_and_stale_tags_preserve_flat_recall
test_many_tags_versions_and_revisions_vote_once_per_lineage
test_metadata_profile_change_stales_only_metadata_generation
test_direction_assignment_does_not_become_global_concept
test_outbox_claim_rejects_stale_fence_and_recovers_expired_claim
```

Run:

```bash
rtk python3 tests/history_metadata_shadow_smoke.py
```

Expected RED: flat IDs disappear, metadata votes multiply, or profile
invalidation touches unrelated projection identity.

- [ ] **Step 2: Implement schema helpers, outbox, and shadow union**

No metadata call runs inside candidate activation. Failed enrichment leaves a
pending/failed derived state and omits that channel from the query manifest.
Flat scores and ordering are retained; metadata contributes a separately
auditable candidate union for later comparison.

- [ ] **Step 3: Run focused and projection regression GREEN**

```bash
rtk python3 tests/history_metadata_shadow_smoke.py
rtk python3 tests/history_audit_l1_smoke.py
rtk python3 tests/history_projection_smoke.py
rtk python3 tests/history_store_smoke.py
```

- [ ] **Step 4: Commit Task 4**

```bash
rtk git add lib/history_metadata.py lib/history_audit.py lib/history_audit_store.py history/metadata-policy-v1.json tests/history_metadata_shadow_smoke.py
rtk git commit -m "feat: add metadata shadow retrieval"
```

---

### Task 5: Deterministic L2 Execution, CAS Retention, Settlement, and Recovery

**Files:**

- Modify: `lib/history_cas.py`
- Create: `lib/history_execution.py`
- Create: `tests/history_audit_runtime_smoke.py`
- Create: `tests/history_audit_store_smoke.py`
- Modify: `lib/history_audit_plan.py`
- Modify: `lib/history_audit_store.py`
- Modify: `lib/history_audit.py`

**Interfaces:**

```python
def collect_garbage(conn, root, now, grace_seconds):
    """Tombstone then delete eligible unpinned objects idempotently."""


def claim_task(conn, task_key, worker_id, lease_seconds, expected_fence):
    """Acquire or renew one fenced logical task claim."""


def record_attempt(conn, task_key, capability, usage_reservation):
    """Append a started attempt before process launch."""


def validate_map_output(task, raw_output, snapshot):
    """Require exact item IDs, relation schema, anchors, and truncated=false."""


def settle_task(conn, task_key, valid_attempts):
    """Commit equal output once or deterministic conflict."""


def split_task(conn, parent_key):
    """Supersede an invalid parent with deterministic .0/.1 children."""


def build_coverage_receipt(plan, settlements, semantic_qualification):
    """Derive coverage, adjudication, exceptional cards, status, and reason."""


def recover_run(conn, plan_sha):
    """Requeue only unclaimed/unsettled valid tasks and preserve settlements."""
```

Task 1 already provides zlib CAS write/descriptor/verify, raw/compressed hashes,
codec/version, lengths, atomic publish, minimum receipts, and evidence pins.
This task adds retention and task integration. A normal GC
tombstone records object identity and reason before payload deletion; missing
without a tombstone or hash mismatch is an integrity error.

L2 map output has exactly one row/object per assigned item with shared semantic
relation, independent lineage relation, extractive anchors, and
`truncated=false`. `blocking_duplicate`, `substantive_overlap`, and
`uncertain` become exceptional cards. Host coverage uses every expected ID;
detail/reduce uses exceptional cards only and collapses lineage revisions by
maximum severity and evidence union.

Logical task states are closed to `planned`, `claimed`, `settling`, `settled`,
`superseded`, and `exhausted`. Superseded/exhausted parents contribute no
coverage. Exactly one terminal settlement references every valid attempt;
recovery reclaims only expired claims and resumes only unsettled tasks.

- [ ] **Step 1: Write CAS lifecycle RED tests**

```text
test_equal_raw_objects_deduplicate_after_compression
test_final_evidence_pin_blocks_expiry
test_gc_writes_tombstone_before_payload_delete
test_missing_without_tombstone_is_integrity_fault
test_compressed_or_raw_hash_mismatch_is_integrity_fault
test_minimum_receipt_verifies_after_normal_raw_expiry
test_crash_after_object_publish_before_db_descriptor_recovers_safely
test_crash_after_tombstone_before_delete_resumes_idempotently
```

Run:

```bash
rtk python3 tests/history_audit_store_smoke.py
```

Expected RED: deletion precedes a tombstone, pins are ignored, or corruption is
reported as normal expiry.

- [ ] **Step 2: Implement CAS and retention**

Filesystem paths are derived only from validated object IDs under one bounded
root. No symlink, hardlink, sparse, or special object is accepted. Minimum
receipts remain in SQLite and refer to tombstones after normal expiry.

- [ ] **Step 3: Write task/settlement/fault RED tests**

`tests/history_audit_runtime_smoke.py` uses an in-memory provider script driven
by task ID and covers:

```text
test_map_requires_exact_manifest_ids_and_frozen_anchors
test_timeout_then_success_commits_one_logical_result
test_429_or_5xx_fails_over_in_declared_pool_order
test_equal_duplicate_completions_are_arrival_order_independent
test_conflicting_valid_completions_are_arrival_order_independent
test_overflow_supersedes_parent_and_splits_not_retries
test_single_item_overflow_exhausts_without_empty_children
test_missing_duplicate_extra_and_truncated_outputs_never_cover_parent
test_reducer_receives_only_hit_and_uncertain_cards
test_lineage_uses_maximum_relation_severity_without_extra_votes
test_exhausted_leaf_is_partial_unless_verified_hit_exists
test_crash_after_cas_before_settlement_resumes_only_unsettled_task
test_budget_covers_retry_failover_split_detail_and_reduce
```

Expected RED is wrong status, coverage, settlement, or split DAG after valid
fake execution; assertions do not inspect only mock call counts.

- [ ] **Step 4: Implement L2 execution and recovery**

Each attempt CAS-writes request/output and durable descriptors before it
validates usage or offers a
normalized result for settlement. Retry limit is two attempts per logical
task. Overflow/truncation/item-set errors split immediately; syntax/schema may
retry the same payload once. A one-item poison task exhausts. Recovery trusts
only durable terminal settlement rows whose CAS references verify.

- [ ] **Step 5: Run focused and runtime regression GREEN**

```bash
rtk python3 tests/history_audit_store_smoke.py
rtk python3 tests/history_audit_runtime_smoke.py
rtk python3 tests/history_audit_plan_smoke.py
rtk python3 tests/history_audit_l1_smoke.py
rtk python3 tests/history_runtime_smoke.py
rtk bash tests/history_runtime_smoke.sh
```

- [ ] **Step 6: Commit Task 5**

```bash
rtk git add lib/history_cas.py lib/history_execution.py lib/history_audit.py lib/history_audit_plan.py lib/history_audit_store.py tests/history_audit_runtime_smoke.py tests/history_audit_store_smoke.py
rtk git commit -m "feat: add deterministic l2 audit runtime"
```

---

### Task 6: Qrels Shadow Readiness, Production Veto, Router, and Cost Counters

**Files:**

- Create: `lib/history_audit_eval_v2.py`
- Create: `history/risk-policy-v1.json`
- Create: `history/semantic-release-policy-v1.json`
- Create: `tests/history_audit_eval_smoke.py`
- Create: `tests/fixtures/history-audit-qrels-shadow.jsonl`
- Modify: `lib/history_audit.py`
- Modify: `lib/history_audit_store.py`

**Interfaces:**

```python
def validate_qrels(rows, partitions, *, scope):
    """Reject lineage/temporal leakage, bad relations, anchors, or scope."""


def evaluate_shadow_readiness(qrels, outputs, policy):
    """Return not_ready or shadow_ready with counts and intervals."""


def evaluate_production_qualification(qrels, outputs, policy, evidence):
    """Return a bound qualification or a precise veto list."""


def invalidate_qualification(qualification, changed_dependencies):
    """Stale only qualifications/search generations that bind the change."""


def route_candidate(facts, ordered_rules):
    """Return routine/guarded/exhaustive and all matched rule IDs."""


def summarize_realized_cost(attempts, candidates):
    """Report per-intent L1, escalation, L2, calls, tokens, usage, latency."""
```

Use one-sided 95% Wilson bounds with a fixed z value recorded in policy. Shadow
readiness requires 30 independent positive query lineages, at least five in
each of low-overlap, cross-language, and lineage-revision slices, plus 20
adjudicated hard-negative/no-match query lineages. Production requires at least
300 independent positives plus the accepted aggregate and bad-slice gates,
valid provider capacity evidence, and fault/replay evidence. Repository
synthetic scope can never produce `production_qualified=true`.

The ordered router includes at least:

```text
retriever_uncalibrated
finalist_or_sa
mandatory_channel_failed
comparator_uncertain
bad_slice_membership
index_profile_recently_changed
permanent_no_match_without_release_gate
```

If L2 is already required before L1 comparator, skip the L1 model call and
retain only model-external diagnostics. Router rules never override budget or
release status.

- [ ] **Step 1: Write qrels and release RED tests**

```text
test_qrels_reject_lineage_and_temporal_partition_leakage
test_qrels_require_shared_relation_and_evidence_anchors
test_shadow_ready_requires_30_positives_critical_slices_and_20_negatives
test_shadow_ready_never_qualifies_production
test_synthetic_scope_never_qualifies_production
test_production_requires_300_independent_positives_and_all_evidence_gates
test_missing_bad_slice_is_abstain_and_veto
test_qualification_binds_no_match_basis_and_dependency_hashes
test_provider_change_stales_adjudication_not_fts
test_metadata_change_does_not_stale_flat_generation
```

Run:

```bash
rtk python3 tests/history_audit_eval_smoke.py
```

Expected RED: leakage or synthetic scope is accepted, or small shadow data
grants production authority.

- [ ] **Step 2: Implement qrels validation and qualification**

Keep the repository qrels fixture diagnostic-only. Store the metric numerator,
denominator, bound, policy hash, corpus/evaluation hash, dependency hashes, and
veto list. Qualification lookup requires an exact current dependency match.

- [ ] **Step 3: Write router and realized-cost RED tests**

```text
test_uncalibrated_and_permanent_no_match_route_to_l2
test_finalist_uncertain_bad_slice_and_profile_change_record_rule_ids
test_rule_order_is_deterministic_and_model_free
test_router_cannot_override_candidate_or_attempt_budget
test_cost_counts_failed_retry_split_detail_reduce_and_billable_cancel
test_expected_cost_reports_l1_plus_escalation_rate_times_l2_per_intent
test_unknown_price_omits_currency_total_but_keeps_usage
```

- [ ] **Step 4: Implement router and cost summaries**

Cost aggregation derives from append-only attempt and budget events, not
mutable candidate columns. Report calls, input/output/cache tokens, provider
usage units, queue/run latency, escalation rate, and optional verified currency
by intent and slice.

- [ ] **Step 5: Run focused and all v2 GREEN**

```bash
rtk python3 tests/history_audit_eval_smoke.py
rtk python3 tests/history_contract_v2_smoke.py
rtk python3 tests/history_audit_migration_smoke.py
rtk python3 tests/provider_adapters_smoke.py
rtk python3 tests/history_audit_plan_smoke.py
rtk python3 tests/history_audit_l1_smoke.py
rtk python3 tests/history_metadata_shadow_smoke.py
rtk python3 tests/history_audit_store_smoke.py
rtk python3 tests/history_audit_runtime_smoke.py
```

- [ ] **Step 6: Commit Task 6**

```bash
rtk git add lib/history_audit_eval_v2.py lib/history_audit.py lib/history_audit_store.py history/risk-policy-v1.json history/semantic-release-policy-v1.json tests/history_audit_eval_smoke.py tests/fixtures/history-audit-qrels-shadow.jsonl
rtk git commit -m "feat: add semantic shadow release router"
```

---

### Task 7: V2 CLI, Hunt/AwR Portable Integration, Examples, and Compatibility

**Files:**

- Create: `lib/history_audit_cli.py`
- Create: `lib/portable_stage.py`
- Create: `tests/portable_runtime_abi_smoke.sh`
- Modify: `hunt.sh`
- Modify: `awr-side.sh`
- Modify: `lib/resolve_cmd.sh` only if required for v2 closed argv; keep v1
  command-string semantics unchanged
- Modify: `tests/runtime_abi_smoke.sh`
- Modify: `tests/generation_contract_smoke.sh`
- Modify: `tests/verify_product_contract.py`
- Modify: `docs/backends.md`
- Modify: `docs/architecture.md`
- Modify: `CONTRIBUTING.md`
- Modify: `README.md`

**CLI:**

```text
rtk python3 lib/history_audit_cli.py init --db PATH --cas-root PATH
rtk python3 lib/history_audit_cli.py provider-command \
  --surface hunt|awr --provider ID [--model MODEL] [--reasoning VALUE]
rtk python3 lib/history_audit_cli.py plan --db PATH --candidate PATH \
  --intent INTENT --output PATH
rtk python3 lib/history_audit_cli.py run --plan PATH --state PATH
rtk python3 lib/history_audit_cli.py resume --plan PATH --state PATH
rtk python3 lib/history_audit_cli.py verify --receipt PATH
rtk python3 lib/history_audit_cli.py evaluate --qrels PATH --outputs PATH
```

`provider-command` prints canonical JSON and never starts a provider. `plan`
cannot create hard-complete work with unbudgetable profiles. `verify` replays
identity, CAS, coverage, adjudication, qualification, budget, and status.

Hunt controls:

```text
HISTORY_RUNTIME_ABI=v1|v2
HUNT_PROVIDER=codex|kimi|grok
HUNT_MODEL=
HUNT_REASONING_EFFORT=
HUNT_REVIEW_PROVIDER_<N>=
HUNT_REVIEW_MODEL_<N>=
HUNT_REVIEW_REASONING_EFFORT_<N>=
```

AwR controls:

```text
AWR_PROVIDER=codex|kimi|grok|opencode|agy
AWR_MODEL=
AWR_REASONING_EFFORT=
AWR_RESEARCH_PROVIDER/MODEL/REASONING_EFFORT
AWR_PRIORWORK_PROVIDER/MODEL/REASONING_EFFORT
AWR_JUDGE_PROVIDER/MODEL/REASONING_EFFORT
```

V1 continues to use `AGENT_CMD`, `CONTAINED_*`, and `SIDE_*`. V2 rejects
mixing provider controls with legacy command overrides. V2 portable stages use
the existing canonical stage prompts and host output projection/validation but
do not claim v1 Codex containment. Every v2 completion receipt records
`execution_boundary=portable-mirror-v1`.

- [ ] **Step 1: Write no-launch CLI and configuration RED tests**

```text
test_provider_command_prints_closed_json_without_launch
test_default_v1_ignores_unset_v2_provider_controls
test_v1_rejects_set_hunt_or_awr_provider_controls
test_v2_defaults_to_codex_provider_default_markers
test_v2_rejects_mixed_legacy_and_provider_controls
test_hunt_rejects_opencode_and_agy
test_awr_accepts_opencode_and_agy
test_role_overrides_do_not_leak_between_seats_or_roles
test_every_documented_model_reasoning_example_matches_resolver_argv
test_no_runtime_or_documentation_default_mentions_claude
```

Run:

```bash
rtk bash tests/portable_runtime_abi_smoke.sh
```

Expected RED: the new variables/CLI are absent or wrong while all fake paths
remain local.

- [ ] **Step 2: Implement CLI and portable stage**

The portable stage stages the same bounded role/input artifacts as v1, adds a
closed response schema to the prompt, captures one final JSON envelope, uses
the existing host projection validators for generation/review artifacts, and
writes v2 preflight/completion receipts. It does not read v1 Codex auth,
capability registry, proxy, or sandbox profile.

- [ ] **Step 3: Wire Hunt and AwR v2 controls**

Validate ABI/provider configuration at startup before history mutation or
queue scanning. V2 `hunt.sh` uses portable generation/comparison/review only;
legacy selector/prescreen/research/report command controls remain explicit
until their v2 adapters are selected. AwR uses portable mirrors for all three
roles. Process-group timeout and artifact checks are shared.

- [ ] **Step 4: Document locally verified CLI grammar and evidence boundary**

Include these target v2 commands. Local help verifies argument grammar;
OpenCode/agy catalog probes verify exact local model spelling without claiming
account entitlement, capacity, or price:

```bash
rtk env HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=codex HUNT_MODEL=gpt-5.6-sol HUNT_REASONING_EFFORT=xhigh ./hunt.sh
rtk env HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=kimi HUNT_MODEL=kimi-code/k3 ./hunt.sh
rtk env HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=grok HUNT_MODEL=grok-4.5 HUNT_REASONING_EFFORT=high ./hunt.sh
rtk env HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=opencode AWR_MODEL=openai/gpt-5.6-sol AWR_REASONING_EFFORT=high ./awr-side.sh
rtk env HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=agy AWR_MODEL=gemini-3.6-flash-high AWR_REASONING_EFFORT=high ./awr-side.sh
```

Also document that omitted values use CLI defaults, Kimi has no explicit
reasoning control in the inspected CLI, portable mirrors are not OS-level
containment, real capacity profiles are absent, and production no-match stays
vetoed.

- [ ] **Step 5: Run focused shell and product GREEN**

```bash
rtk bash -n hunt.sh awr-side.sh tests/portable_runtime_abi_smoke.sh tests/runtime_abi_smoke.sh tests/generation_contract_smoke.sh
rtk bash tests/portable_runtime_abi_smoke.sh
rtk bash tests/runtime_abi_smoke.sh
rtk bash tests/generation_contract_smoke.sh
rtk python3 tests/verify_product_contract.py runtime
rtk python3 tests/verify_product_contract.py fixtures
```

- [ ] **Step 6: Run complete offline regression before commit**

```bash
rtk python3 tests/history_contract_v2_smoke.py
rtk python3 tests/history_audit_migration_smoke.py
rtk python3 tests/provider_adapters_smoke.py
rtk python3 tests/history_audit_plan_smoke.py
rtk python3 tests/history_audit_l1_smoke.py
rtk python3 tests/history_metadata_shadow_smoke.py
rtk python3 tests/history_audit_store_smoke.py
rtk python3 tests/history_audit_runtime_smoke.py
rtk python3 tests/history_audit_eval_smoke.py
rtk python3 tests/history_store_smoke.py
rtk python3 tests/history_projection_smoke.py
rtk python3 tests/history_budget_smoke.py
rtk python3 tests/history_retrieval_smoke.py
rtk python3 tests/history_retrieval_adversarial.py
rtk python3 tests/history_runtime_smoke.py
rtk bash tests/history_runtime_smoke.sh
rtk bash tests/calibration_abi_smoke.sh
rtk python3 tests/verify_product_contract.py all
```

- [ ] **Step 7: Commit Task 7**

```bash
rtk git add lib/history_audit_cli.py lib/portable_stage.py lib/resolve_cmd.sh hunt.sh awr-side.sh tests/portable_runtime_abi_smoke.sh tests/runtime_abi_smoke.sh tests/generation_contract_smoke.sh tests/verify_product_contract.py docs/backends.md docs/architecture.md CONTRIBUTING.md README.md
rtk git commit -m "feat: add portable hunt and awr providers"
```

---

### Task 8: Contract Alignment, Independent Test, Independent Audit, and Final Evidence

**Files:**

- Modify: `openspec/changes/scalable-history-runtime/tasks.md`
- Modify:
  `docs/superpowers/specs/2026-08-03-scalable-history-runtime-implementer-contract.md`
  only if implementation names changed without changing requirements
- Create: `docs/reviews/scalable-history-runtime-independent-test.md`
- Create: `docs/reviews/scalable-history-runtime-code-audit.md`
- Create: `docs/reviews/scalable-history-runtime-final-evidence.md`

- [ ] **Step 1: Run static contract alignment**

```bash
rtk openspec validate scalable-history-runtime --strict
rtk rg -n "complete_match|excluded_batch_ids_hash|\"basis\"" lib/history_*v2.py lib/history_audit*.py history/*v1.json tests/history_audit* || true
rtk rg -n -i "claude" lib/provider_adapters.py lib/portable_agent.py lib/portable_stage.py history/provider-adapters-v1.json hunt.sh awr-side.sh docs/backends.md README.md
rtk git diff --check
```

The first scan may find explicit legacy-migration test fixtures only. The
second scan must find no provider/default/fallback path; historical prose that
documents an explicit legacy opt-in is outside v2 and must remain clearly
scoped.

- [ ] **Step 2: Run the fresh complete verification matrix**

Run every Task 7 Step 6 command again from a clean shell, followed by:

```bash
rtk python3 tests/verify_history_retrieval_benchmark.py
rtk python3 tests/runtime_policy_smoke.py
rtk bash litwatch_test.sh
rtk git status --short
rtk git diff --stat HEAD~7..HEAD
```

`tests/history_retrieval_round2.py` is also run. If its only failure remains the
pre-existing 531-versus-538 committed-ledger characterization, record the exact
failure separately and do not weaken or skip the assertion. Any other failure
blocks completion.

- [ ] **Step 3: Obtain independent test and repair until PASS**

Give a fresh test agent the OpenSpec change, P0 contract, final diff, and exact
verification commands. The agent must run tests itself, inspect fake-provider
process boundaries, exercise migration interruption, budget exhaustion,
split/recovery, CAS corruption, qrels leakage, and v1 compatibility, then write
`docs/reviews/scalable-history-runtime-independent-test.md`. A controller does
not write its own PASS report. Every FAIL returns to the responsible
implementer and reviewer loop, then the independent test reruns from a fresh
worktree state.

- [ ] **Step 4: Obtain independent whole-branch audit and repair until PASS**

Give a separate fresh audit agent the same contracts plus a review package for
the complete branch. It checks provider neutrality, no-Claude reachability,
identity boundaries, legacy no-match quarantine, current-batch exclusion,
metadata non-filtering, settlement order independence, positive-first status,
budget coverage, CAS lifecycle, qualification invalidation, shell integration,
and documentation truth. It writes
`docs/reviews/scalable-history-runtime-code-audit.md`. All blocking findings are
fixed by an implementer and re-audited until PASS.

- [ ] **Step 5: Record final evidence without lifting external vetoes**

`docs/reviews/scalable-history-runtime-final-evidence.md` records commit IDs,
OpenSpec validation, exact test counts/commands, independent report hashes,
known pre-existing baseline drift, and these explicit non-claims:

```text
real provider capacity qualified = false
production qrels qualified       = false
production complete_no_match     = VETO
currency price accuracy          = unclaimed
portable mirror OS containment   = unclaimed
```

Update all completed OpenSpec task checkboxes only after corresponding fresh
evidence exists.

- [ ] **Step 6: Commit final evidence**

```bash
rtk git add openspec/changes/scalable-history-runtime/tasks.md docs/superpowers/specs/2026-08-03-scalable-history-runtime-implementer-contract.md docs/reviews/scalable-history-runtime-independent-test.md docs/reviews/scalable-history-runtime-code-audit.md docs/reviews/scalable-history-runtime-final-evidence.md
rtk git commit -m "docs: record scalable history runtime evidence"
```
