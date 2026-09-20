## 1. Registry and surface eligibility

- [x] 1.1 Add `claude` to `history/provider-adapters-v1.json` with executable `claude`, grammar `claude-portable-v1`, and reasoning values `low|medium|high|xhigh|max`. Set `registry_revision` to `2026-08-07`. Include `claude` on both `hunt` and `awr` surfaces.
- [x] 1.2 Update `lib/provider_adapters.py` closed provider tuples/surfaces and recompute `_PROVIDER_REGISTRY_V1_SHA256` from the tracked registry bytes.
- [x] 1.3 Delete the registry/executable blanket `_FORBIDDEN` Claude scan while retaining multi-backend Anthropic-alias and dynamic-route denylists and Grok compatibility cells.

## 2. Command grammar and structured transport

- [x] 2.1 Render Claude argv in `_render_command_fields` / `render_command` with bare print, JSON output, inline schema, empty tools, permission bypass, optional model/effort, and `--add-dir` mirror.
- [x] 2.2 Parse Claude outer stdout in `lib/portable_agent.py` and accept only `is_error=false`, `subtype=success`, object `structured_output`; reject text-only `result`.
- [x] 2.3 Add Claude transport instructions in `lib/portable_stage.py` parallel to Agy, requiring structured final output without fences or mirror writes.

## 3. External worker and shell wiring

- [x] 3.1 Add `claude-worker.sh` for `AGENT_CMD` / `FRONT_CMD` / `BACK_CMD` / panel / side external stages with `CLAUDE_MODEL`, `CLAUDE_REASONING_EFFORT`, absolute work root, and unattended flags.
- [x] 3.2 Confirm `hunt.sh` / `awr-side.sh` resolve `claude` through existing provider-command preflight without hard-coded allowlists that omit it.
- [x] 3.3 Update product-contract scanners so registry membership includes Claude, implicit shell fallbacks remain rejected, and explicit worker/provider selection is allowed.

## 4. Offline tests and fakes

- [x] 4.1 Extend `tests/fake_portable_stage_provider.py` with Claude outer success/error envelopes and structured_output modes.
- [x] 4.2 Update `tests/provider_adapters_smoke.py`, `provider_model_catalog_authority_smoke.py`, `provider_portable_hardening_smoke.py`, `portable_stage_runtime_smoke.py`, `provider_host_capability_evidence_smoke.py`, `runtime_policy_smoke.py`, and `verify_product_contract.py` for first-class Claude.
- [x] 4.3 Replace `test_registry_and_resolved_commands_have_no_claude_path` and Hunt/AwR acceptance tests with positive Claude registration and negative implicit-fallback coverage.
- [x] 4.4 Run the offline provider/portable suites and product-contract gate to green.

## 5. Operator documentation

- [x] 5.1 Document Claude in `README.md`, `docs/backends.md`, `docs/getting-started.md`, and `CONTRIBUTING.md` with one verified spelling example and the structured-transport boundary.
- [x] 5.2 Delete unsupported-Claude / never-invoke-Claude operator claims outside historical review archives and completed plan ledgers that record past constraints.
- [x] 5.3 Align OpenSpec change validation (`openspec validate claude-first-class-provider --strict`) with the implemented registry and docs.

## 6. Independent verification

- [x] 6.1 Run independent offline test agent over the full provider/portable/product-contract surface; repair every blocking failure.
- [x] 6.2 Run independent whole-branch code audit for grammar closure, transport fail-closed behavior, no implicit fallback, docs/registry parity, and absence of text fallback; repair every blocking finding.
- [x] 6.3 Optional: one bounded live Claude `awr-judge` smoke with explicit model and effort after offline green; record completion hashes or the diagnosed skip reason.
