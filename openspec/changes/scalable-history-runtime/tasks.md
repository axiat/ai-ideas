## 1. Contract and Migration Foundation

- [x] 1.1 Add canonical v2 codec and cross-language identity test vectors for manifests, ordered pools, ID sets, plans, tasks, and attempts.
- [x] 1.2 Add the closed receipt schema, idempotent component migrations, staging-to-legacy activation identity, fenced compare-and-set helpers, direction ownership, and legacy receipt quarantine without modifying v1 rows.
- [x] 1.3 Add compressed CAS write/descriptor/verify, permanent minimum-receipt, deduplication, integrity, and final-evidence pin primitives before any L2 task can settle.
- [x] 1.4 Add the tracked one-page implementer contract and keep it aligned with the OpenSpec requirements.

## 2. Provider-Neutral Execution and Capacity

- [x] 2.1 Add the provider registry and offline probes for Hunt Codex/Kimi/Grok and AwR Codex/Kimi/Grok/OpenCode/agy, with no Claude entry or fallback.
- [x] 2.2 Add effective-default capability identity and provider-specific model/reasoning override validation, plus a no-launch command diagnostic and diagnostic-only default marker.
- [x] 2.3 Add final-request serialization, all-pool `B_pool=min(B_p)` exact-or-bounded token preflight, versioned capacity profiles, ordered stage-pool plan identity, and deterministic token-and-item shard planning.
- [x] 2.4 Add per-intent candidate/attempt/token/usage/currency reservation and settlement with append-only attempt counters.

## 3. M0 L1 and Frozen Corpus Boundary

- [x] 3.1 Add v2 run/snapshot/batch staging with host staging IDs, source-sequence watermark, staged-batch exclusion hash/namespace, batch-internal duplicate plan, and immutable activation mapping to legacy IDs assigned by the existing append path.
- [x] 3.2 Add flat L1 family fairness, mandatory queue coverage, bounded comparator planning, and v2 L1 receipts while preserving v1 behavior.
- [x] 3.3 Add closed semantic/lineage relation values and priority-ordered final status/reason derivation.

## 4. Additive Metadata Shadow

- [x] 4.1 Add versioned synopsis/concept/free-tag profiles, append-only annotations, provenance, stale state, and metadata outbox/generation records.
- [x] 4.2 Add the metadata retrieval family as a shadow-only union with one lineage vote and corruption/staleness regression tests proving flat reachability.

## 5. Minimal Exhaustive L2 Runtime

- [x] 5.1 Add CAS-backed durable logical tasks and attempts, ordered failover, bounded retry, deterministic overflow split, equal-result settlement, and conflict settlement.
- [x] 5.2 Add strict map ID/anchor/snapshot validation, host coverage over all expected IDs, exceptional-card detail/reduce, and positive-first final receipt derivation.
- [x] 5.3 Add the closed task-state/supersession model and interruption recovery that resumes only unsettled tasks and never counts invalid, exhausted, or superseded parents as coverage.

## 6. CAS Retention and Recovery

- [x] 6.1 Add grace-period tombstones and garbage collection over the foundation CAS primitives while preserving final-evidence pins and permanent minimum receipts.
- [x] 6.2 Add crash/recovery tests for CAS-before-settlement, tombstone/delete interruption, expiry, missing, corrupt, and pinned objects.

## 7. Qrels, Router, and Cost Calibration

- [x] 7.1 Add lineage-temporal qrels validation, `shadow-calibration-v1` readiness, production qualification records, statistical reports, and synthetic-scope veto.
- [x] 7.2 Add dependency-local qualification invalidation and the ordered deterministic router for uncalibrated, finalist, disputed, bad-slice, and permanent-no-match cases.
- [x] 7.3 Add per-intent realized `L1 + p(escalation) * L2` counters and reports without inventing currency prices.

## 8. Hunt and AwR Integration

- [x] 8.1 Add explicit `HISTORY_RUNTIME_ABI=v2` Hunt portable-mirror provider/model/reasoning controls while retaining v1 command compatibility and defaulting migration runs to v1.
- [x] 8.2 Add AwR provider/model/reasoning controls and role overrides for the full registered AwR provider set.
- [x] 8.3 Add one locally verified CLI-grammar example per provider with explicit v2 selection and local-catalog/account/capacity boundaries, plus offline end-to-end fake-provider tests, shell syntax gates, and product-contract checks.
- [x] 8.4 Requalify the binding-covered Grok final-response fence, reducer-joined unique-terminal-fence transport, and closed six-cell compatibility environment with a live Grok `awr-judge` smoke on final code commit `bd148e1`. The no-retry smoke recorded completion `e7ac65b9a94d0cdf5ca1cb3d4a70c728e7be23dd47544a0afb44de802a2b1665`, model envelope `b3c44fb1cc44418c30c380811789fede5820cc38cd0d79eed85accfe527dda2a`, and projected judge `93fe96fcafac3d4a541d6e39861d1666c47f9299fefd2d297b77f932f3b57fa8`.
- [x] 8.5 Requalify binding-covered non-Grok raw canonical stdout instructions with a live agy `awr-judge` smoke on final code commit `bd148e1` after implementing request binding, descriptor/no-follow declared-file integrity, bounded ignored `.tmp` scratch, process-group quiescence, cleanup-before-import, and legacy descriptor/no-follow extra-file enumeration. The no-retry smoke recorded completion `172ad814a6d0179d1b748abf5f294b0e945063af225303ba09d944fe0305d8d6`, model envelope `7bd619c6f65a9728a435be855d9dc8aa3f7c94eb3ec4d6b58db990fc7180b3d6`, and projected judge `08c10f0738f5012d45ddf36f4cd73f20444dddcfd1a75f0fbf022128dfbf9b1a`.
- [ ] 8.6 Run structured-transport offline tests and one final-revision live agy qualification with an explicit catalog model.

## 9. Independent Verification

- [x] 9.1 Run the full offline regression suite, OpenSpec strict validation, migration/recovery/fault injection, shell syntax checks, and `git diff --check`.
- [x] 9.2 Obtain an independent test report and an independent whole-branch code audit, repair every blocking finding, and rerun both until they pass.
- [x] 9.3 Confirm that production `complete_no_match` remains vetoed without real qrels and provider-capacity evidence, then record the final evidence boundary.
