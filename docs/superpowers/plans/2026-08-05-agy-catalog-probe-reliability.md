# Agy Catalog Probe Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AwR v2 startup reliable when `agy models` is slow, without weakening catalog validation or launch-time authority checks.

**Architecture:** Keep catalog probing fail-closed and extend only its bounded wait from 15 to 30 seconds. In AwR startup, derive the effective provider tuple for the base and each role, then memoize successful diagnostics by the exact `(provider, model, reasoning)` tuple for that preflight invocation. Profile creation and immediate pre-launch revalidation remain fresh operations.

**Tech Stack:** Bash, Python 3 standard library, `unittest`, shell smoke tests.

## Global Constraints

- Never invoke Claude or start any provider model workload. Tests use fake provider executables; live verification is limited to catalog introspection and `provider-command`.
- Preserve the fail-closed catalog grammar: nonzero exit, stderr, empty or noncanonical output, invalid UTF-8, duplicates, excessive output, and absent requested models still fail.
- Do not add retries. The catalog subprocess receives exactly one 30-second bound per diagnostic.
- Deduplicate only successful startup diagnostics inside one `awr_runtime_preflight` call and only by the exact provider, model, and reasoning tuple.
- A failed diagnostic is never cached. Different tuples are never coalesced.
- Keep `awr_write_role_profile` and portable launch-time revalidation fresh.
- Keep v1 behavior and non-agy command grammar unchanged.
- Do not modify or stage the main checkout's existing `ledger.tsv` changes.

---

### Task 1: Extend the bounded host catalog probe

**Files:**
- Modify: `tests/provider_model_catalog_authority_smoke.py`
- Modify: `lib/provider_adapters.py`

- [ ] Add a focused test that substitutes the bounded subprocess boundary with a probe double that returns no observation below 30 seconds and a canonical agy catalog at 30 seconds or above. Assert `_host_model_catalog_probe("agy", ...)` returns the expected catalog evidence. The test must exercise the value passed by production code, not read the constant from source.
- [ ] Run `python3 tests/provider_model_catalog_authority_smoke.py`; confirm the new test fails because production passes 15 seconds.
- [ ] Change `_HOST_CATALOG_PROBE_TIMEOUT_SECONDS` from `15` to `30`. Do not alter byte limits, output validation, or retry behavior.
- [ ] Re-run `python3 tests/provider_model_catalog_authority_smoke.py`; confirm all cases pass.
- [ ] Run `python3 tests/provider_adapters_smoke.py` if present and `python3 tests/provider_host_execution_smoke.py` if present; record exact results.
- [ ] Self-review the diff for scope, then commit with message `fix: allow slower provider catalog probes`.

### Task 2: Deduplicate identical AwR startup diagnostics

**Files:**
- Modify: `tests/portable_runtime_abi_smoke.sh`
- Modify: `awr-side.sh`

- [ ] Extend the fake agy provider so catalog invocations can be counted without treating them as model launches. Add a black-box AwR v2 smoke case with a header-only ledger and invalid `SIDE_POLL_SEC` so execution stops after preflight and ordinary validation.
- [ ] In the new smoke case, run once with all three roles inheriting the base agy tuple and assert exactly one `agy models` call. Run again with one role overriding only the model to a second catalog-listed agy model and assert exactly two calls. In both runs assert exit status 2, the expected ordinary validation error, no provider launch, and no protected-state mutation.
- [ ] Run `bash tests/portable_runtime_abi_smoke.sh`; confirm the new inherited-tuple assertion fails against the existing four diagnostics.
- [ ] In `awr_runtime_preflight`, track exact successful tuple keys for the duration of the function. Preserve base-first validation. Before each role diagnostic, skip only a tuple already validated successfully; otherwise diagnose it and record success. Use a collision-safe representation for empty and arbitrary accepted values rather than delimiter-concatenated text.
- [ ] Re-run `bash tests/portable_runtime_abi_smoke.sh`; confirm the inherited tuple produces one startup catalog call and the distinct override produces one additional call.
- [ ] Run `bash -n awr-side.sh tests/portable_runtime_abi_smoke.sh` and self-review that profile creation and launch-time checks were not changed.
- [ ] Commit with message `fix: deduplicate awr startup diagnostics`.

### Task 3: Full regression and live no-workload verification

**Files:**
- Verify only; modify production files only for defects found by the regression suite.

- [ ] Run the focused tests from Tasks 1 and 2 together.
- [ ] Run the repository's provider adapter, host execution, portable hardening, portable stage, dynamic model, fake Hunt/AwR end-to-end, product-contract, and OpenSpec checks discovered in the current test tree. Record every exact command and result.
- [ ] Run one live `agy models` catalog introspection and one matching `history_audit_cli.py provider-command` for `agy`, `gemini-3.6-flash-high`, and `high`. Do not run the sidecar against the real queue and do not launch a model request.
- [ ] Inspect `git diff` and `git status`; verify the branch contains only the design, plan, tests, and scoped implementation.
- [ ] Commit any test-only documentation updates required by the product contract; otherwise leave no uncommitted changes.
