## Why

The current history runtime bounds model context but cannot prove a scalable no-match decision: production packs repeatedly exceed their budget, the execution ABI is tied to one audited Codex profile, and legacy receipts mix retrieval failures with final business status. The runtime needs a provider-neutral, replayable L0/L1/L2 path that preserves the SQLite core while keeping production `complete_no_match` closed until real semantic evidence qualifies it.

## What Changes

- Add a versioned audit ABI with canonical identity, frozen history watermarks, current-batch exclusion, shared relation/status semantics, deterministic task settlement, coverage/adjudication receipts, and content-addressed trace storage.
- Add a portable-mirror provider layer. Hunt supports Codex, Kimi, and Grok; AwR additionally supports OpenCode and agy. Codex, Kimi, and Grok preserve current model defaults, OpenCode resolves and pins a safe host probe, and agy requires an explicit catalog model and supported reasoning setting.
- Add token-and-item capacity profiles, per-intent round/candidate budget gates, and append-only usage counters covering retries, failover, split children, detail, and reduce work.
- Add the M0 flat retrieval baseline, additive versioned metadata shadow, a minimal exhaustive sharded L2 audit, and a deterministic risk router. Metadata, tags, summaries, and clusters never become default exclusion filters.
- Add lineage-temporal qrels shadow evaluation and release qualifications. Production `complete_no_match` remains vetoed without a valid execution receipt, complete adjudication, and a currently qualified semantic policy.
- Preserve the legacy v1 runtime and old receipts behind an explicit compatibility boundary. Legacy `complete_match` and `complete_no_match` rows are never relabeled as qualified v2 evidence.
- **BREAKING**: selecting the new v2/portable path uses provider/model/reasoning configuration instead of the Codex-only contained-command ABI. The legacy v1 path remains available during migration.

## Capabilities

### New Capabilities

- `provider-neutral-execution`: Provider selection, default resolution, explicit model/reasoning overrides, portable mirrors, ordered pools, capacity preflight, and usage accounting.
- `scalable-history-audit`: Frozen-snapshot L1/L2 planning, deterministic shards and settlement, unified receipts/status, budget gates, CAS retention, metadata shadow, and risk routing.
- `semantic-release-gate`: Lineage-temporal qrels, shadow qualification, dependency-local invalidation, and the fail-closed production no-match authority.

### Modified Capabilities

None.

## Impact

- New v2 modules and schemas under `lib/`, with additive configuration under `history/` and deterministic offline tests under `tests/`.
- `hunt.sh` and `awr-side.sh` gain explicit provider/model/reasoning controls while retaining legacy command overrides and v1 execution.
- Existing SQLite candidate, lineage, ledger projection, and v1 receipt tables remain authoritative for legacy runs; v2 execution evidence is stored in separate versioned tables and CAS objects.
- No real provider is invoked by implementation tests. Claude is neither a registered provider nor an allowed fallback.
