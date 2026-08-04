# Scalable History Runtime Independent Code Audit

## Verdict

**PASS**

Audited implementation HEAD: `100f252a4dd5ab5e3f4978ca06507c5e22f4e117`

Comparison base: `8a22ab1442dbb89e7400877235bae0732d562591`

Review role: independent whole-branch code auditor.

The audit covered the complete change from the comparison base through the fixed implementation HEAD, including affected pre-existing call paths. It found no caller-forged authority, cross-run evidence stitching, stale-evidence reuse, unbounded model context, legacy-schema authority promotion, or automatic Claude execution path.

### Final launch-authority delta

The implementation in `c70f687ca3d96295e562054db2c53932c49937df..100f252a4dd5ab5e3f4978ca06507c5e22f4e117` closes the previously identified launch-time TOCTOU window. This delta is **PASS**.

- The direct stdout runner retains entry revalidation to reject already-drifted OpenCode or agy model authority before schema processing, state creation, and input copying.
- After `_copy_inputs()`, command rendering, and `_provider_environment()`, the runner invokes the same revalidation helper again. The next executable statement is `subprocess.Popen`.
- The second check revalidates the OpenCode pure configuration, OpenCode and agy model-catalog revision and SHA, effective model, and resolver-issued intent. Catalog or configuration drift during input copying or environment preparation cannot reach workload launch.
- Both checks preserve `ProviderResolutionError` as the exception cause and map it uniformly to `PortableAgentError("provider_model_authority_changed")`. The upper `run_stage` boundary retains the same code when mapping to `PortableStageError`; no new authority or fallback path is introduced.
- The upper `_load_launch_intent()` check remains an early-failure layer. Codex, Kimi, and Grok retain the existing command-intent and executable-identity checks. The v1 ABI, provider grammar, and shell compatibility are unchanged.

## Audit scope and evidence

### Bounded history input

- The v2 generation, history-comparison, and review paths in `hunt.sh` declare only role-required files to `portable_stage.py`. SQLite, the complete `ledger.tsv`, Git metadata, and unrelated candidate state are reserved and cannot enter the portable mirror.
- Generation reads a bounded generation brief and policy, with optional research context and direction contract. History comparison reads a bounded retrieval pack. Review reads one candidate, prior-work evidence, the review contract, and an optional verified history summary.
- Portable host input, each declared input, model stdout, and each projected output have fixed byte caps. The output file set is closed. Extra files, symlinks, hard links, boundary-crossing paths, and changed inputs are rejected.
- V1 ledger and SQLite operations remain host-side. AwR v2 derives one task from one physical ledger row before entering a portable role and never passes the ledger snapshot to the agent.

### Provider portability, route authority, and registry ABI

- The fixed registry byte SHA-256 is `07954f11103e6474dad0cfbaf9978ae331a75c9bf33b614d9763dec24c7c5a30`. The loader verifies the raw bytes, closed fields, provider order, surface allowlists, executable names, and reasoning subsets.
- Hunt accepts exactly `codex|kimi|grok`. AwR additionally accepts `opencode|agy`. There is no provider fallback and no Claude registry entry.
- OpenCode and agy routes undergo NFKC normalization, case folding, repeated percent decoding, and slash normalization before Claude-family tokens and dynamic route markers are rejected. Their effective models must be exact members of bounded host catalogs. An omitted OpenCode model additionally requires a pure configuration probe; an omitted agy model fails closed.
- Model-catalog revision, catalog SHA, and effective model enter the command profile and are re-probed before launch. Resolver-issued intents use a private issuance table and immutable snapshot; a constructed or modified dataclass cannot obtain execution authority.
- General portable command intents are explicitly `provider_validation=unverified`, `authority=shadow-only`, and `hard_complete_eligible=false`. Provider or model spelling cannot mint hard-complete authority.

### Portable mirror and request attestation

- The mirror copies only manifest-declared, SHA-matching, stable regular single-link files. Source roots, directory components, and files use no-follow traversal.
- The request binding covers stage, seat, serialized prompt, role SHA, declared-input names and SHAs, and response schema. The serialized prompt has a separate SHA.
- The response must echo both bindings exactly in a closed envelope. Validation precedes artifact projection and completion publication. The completion then binds the preflight, model envelope, and output descriptors.
- Provider executable identity, role, inputs, prompt, profile, and preflight are revalidated at launch and publication boundaries. Expired or replaced execution objects cannot reuse an existing prepared stage.
- After declared inputs are copied and the provider environment is built, the direct runner revalidates multi-backend model authority immediately before `subprocess.Popen`. Entry and upper-layer checks provide earlier failure but do not replace this launch-edge check.

### Canonical identities, CAS, and recovery

- V2 canonical JSON uses UTF-8, NFC, recursive key sorting, compact encoding, and one LF. It rejects duplicate keys, floats, control characters, non-NFC input, and non-canonical types. Compound identities use domain-separated, uint64 length-prefixed framing.
- Plan, candidate, snapshot, staging batch, logical task, attempt, receipt, router, and qualification identities bind their closed inputs. Candidate identity excludes provider, model, and agent versions, avoiding runtime coupling in content identity.
- Before descriptor persistence, CAS performs zlib compression, temporary-file fsync, atomic publication, and read-back verification. Descriptors bind raw and compressed SHAs, codec, lengths, relative path, retention policy, and integrity state.
- CAS objects referenced by terminal receipts are pinned. Garbage collection handles only expired, unpinned objects, persists a verifiable tombstone first, then deletes the payload. Missing data without a tombstone, hash conflicts, symlinks, hard links, and sparse files are integrity faults.
- Additive migrations preserve old migration SHAs and v1 receipt identities. Legacy `complete_no_match` enters compatibility evidence only and cannot join a v2 qualification. Tests verify real old-database upgrade, restart idempotence, foreign-key integrity, and atomic failure for malformed migrations.

### L1/L2, budgets, router, and release gate

- L1 preserves flat reachability for exact/lineage, FTS, near-duplicate, and dense-core retrieval. Metadata is an additive shadow union, not a default hard filter. Family voting is deduplicated by lineage.
- L2 plans deterministic shards from the same frozen snapshot and enforces both item caps and final serialized token bounds. `safe-24k-v1` allows at most 12 items per shard; 550 items require at least 46 shards. Overflow, missing, duplicate, or extra IDs, truncation, and schema or anchor errors cannot count as complete coverage.
- A hard-complete pool uses the minimum capacity of every member. Candidate, started-attempt, input-token, output-token, usage-unit, and optional currency budgets are reserved before launch and settled across retry, failover, split, detail, reduce, failure, and cancellation paths.
- Router authority derives from a durable host source chain and binds the rule table, matched rule IDs, plan and dependency heads, and budget authority. Private helpers, caller-supplied booleans, valid fragments stitched across A/B runs, and dependency drift cannot mint dispatch or receipt authority.
- Final status uses the fixed priority: invalid, verified positive, partial, conflict, unqualified no-hit, qualified no-hit. A positive survives partial coverage. Budget exhaustion or a clean but unqualified result cannot become `complete_no_match`.
- Production qualification counts and metrics use held-out `partition=test` only, while evaluation identity binds every qrel and exact output across train, development, and test. Synthetic or shadow evidence, self-reported capacity, forged qualification, expired roots, and dependency-head drift cannot publish production authority.
- At the fixed HEAD, `history/production-evidence-roots-v1.json` has empty fault, replay, and semantic-evaluation root lists. The repository therefore has no production `complete_no_match` authority; public and internal persistence paths retain the veto.

### Hunt, AwR, and v1 integration

- Provider-neutral portable internal stages require `HISTORY_RUNTIME_ABI=v2`; v1 remains the compatibility default. V1 rejects v2 provider variables, while v2 rejects contained or legacy side-command mixing.
- Hunt generation, history comparison, and review seats support base-provider and per-seat model/reasoning overrides. AwR researcher, prior-work, and judge roles support base-provider and per-role overrides. A provider switch does not inherit another provider's model or reasoning values.
- Hunt selector, prescreen, external prior-work, and report assembly retain their external process boundaries and do not enter the v2 internal-provider registry. AwR v1 custom `SIDE_CMD` remains an explicit compatibility interface, not an automatic fallback.

## Verification results

- 412 Python cases covering provider, portable execution, canonical identity, planning, budgets, L1, semantic evaluation, receipts, router authority, and migrations passed.
- CAS foundation, run with the repository test import convention: 13/13 passed.
- CLI, store, runtime, and P0 lifecycle: 134/134 passed.
- `PYTHONPATH=tests python3 tests/provider_model_catalog_authority_smoke.py`: 9/9 passed at the final delta.
- The related provider-adapter, host-capability, portable-hardening, dynamic-output, and portable-stage-runtime unittest batch: 69/69 passed.
- Independent race reproductions changed the agy catalog and the OpenCode pure-config default during `_copy_inputs()`. Both returned `provider_model_authority_changed`, preserved a `ProviderResolutionError` cause, and recorded zero mock `subprocess.Popen` calls.
- `bash tests/portable_runtime_abi_smoke.sh` and `bash tests/portable_hunt_awr_e2e_smoke.sh` passed, including v1 generation/runtime compatibility.
- `python3 tests/verify_product_contract.py all` returned `ok: all`; relevant shell syntax checks also passed.
- `rg -nP '[\p{Han}]' docs/reviews/scalable-history-runtime-independent-code-audit.md` returned no matches.
- `openspec validate scalable-history-runtime --strict` passed.
- All verification used local fake providers or host-only logic. No real model workload and no Claude invocation occurred.

## Non-claim boundaries

- This audit does not establish local account entitlement, real-provider availability, price, throughput, context capacity, or future CLI behavior.
- Portable provider output at the fixed HEAD remains shadow authority. Fake-provider tests and grammar or catalog checks are not hard-complete capability certificates.
- This report does not claim completed production semantic qualification. The repository intentionally carries no real qrel, capacity, fault, or replay root evidence; production no-match must remain fail-closed while that evidence is absent.
- Explicit v1 custom commands and Hunt external stages remain compatibility boundaries. This audit establishes only that v2 provider controls do not select them implicitly or use them as fallbacks.
