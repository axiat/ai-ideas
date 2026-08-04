# Scalable History Runtime Independent Test

Status: **PASS**

Audited implementation HEAD: `100f252a4dd5ab5e3f4978ca06507c5e22f4e117`

Independent verdict: **PASS**

Immediate predecessor: `c70f687ca3d96295e562054db2c53932c49937df`

Full-suite baseline: `bb7078cb7a2ebca0ee0a0d997e949898edbb4e40`

Test date: `2026-08-03`

Execution boundary: local offline/fake-provider verification; no model workload and no Claude invocation.

## Verification matrix

| Area | Result |
|---|---|
| History foundation, authority, receipts, and cost | Baseline: 234 unittest executions passed; commit diff contains no affected file |
| History L1, metadata shadow, audit runtime/CLI, evaluation, and router | Baseline: 287 unittest executions passed; commit diff contains no affected file |
| Legacy history, retrieval, stages, store, witness, and runtime policy | Baseline: 356 unittest executions plus 2 script-style smokes passed; final diff contains no affected file |
| Provider adapters, model authority, portable hardening, and portable stage runtime | Final HEAD: 78 unittest executions passed |
| Shell ABI and fake end-to-end gates | Final HEAD: portable runtime ABI, runtime ABI, and portable Hunt/AwR fake E2E passed |
| Product contract, OpenSpec strict validation, Python compile, shell syntax | Final HEAD: all passed |
| Supplemental retrieval benchmark and round-two suite | 66 unittest executions passed |
| Litwatch | 14 passed, 0 failed, 1 skipped |
| Additional provider fail-closed attacks | 5/5 passed |

The baseline's 953 direct-script unittest executions include tests rediscovered through imported suites; the number is an execution count, not a unique-method count. The final candidate changes only `lib/portable_agent.py` and `tests/provider_model_catalog_authority_smoke.py` relative to its immediate predecessor; the affected Python group was rerun as 78/78 at the audited implementation HEAD.

## Commands and exact results

### Final implementation launch-authority delta

```sh
git show --stat --oneline --no-renames 100f252
git diff --no-ext-diff --unified=100 c70f687..100f252 -- \
  lib tests history hunt.sh awr-side.sh
python3 tests/provider_model_catalog_authority_smoke.py
```

The diff contains 59 insertions and 6 deletions across `lib/portable_agent.py` and `tests/provider_model_catalog_authority_smoke.py`. `run_portable_stdout_attempt` retains its entry revalidation and adds another revalidation after input copying, rendering, and environment construction, immediately before `subprocess.Popen`. The direct test file ran 9 tests, all passed.

A separate inline driver reproduced both timing cases with an issued fake `agy` intent:

```sh
python3 - <<'PY'
import pathlib
import tempfile
from unittest import mock
from lib import history_contract_v2, portable_agent, portable_stage, provider_adapters

root = pathlib.Path.cwd()
registry = provider_adapters.load_registry(root / "history/provider-adapters-v1.json")
fake = root / "tests/fake_portable_stage_provider.py"

def catalog(*models):
    material = {
        "schema_version": "provider-model-catalog-v1",
        "provider": "agy",
        "models": sorted(models),
    }
    return {
        **material,
        "probe_revision": "fixture-model-catalog-v1",
        "catalog_sha256": history_contract_v2.framed_sha256(
            "provider-model-catalog-v1",
            history_contract_v2.canonical_bytes(material),
        ),
    }

def issue():
    return provider_adapters._resolve_command_intent_for_test(
        registry,
        "awr",
        "agy",
        model="gemini-safe",
        executable_lookup=lambda _: str(fake),
        model_catalog_probe=lambda *_: catalog("gemini-safe"),
    )

def invoke(intent, state):
    portable_agent.run_portable_stdout_attempt(
        intent,
        inputs=[],
        prompt="PROMPT",
        response_schema=portable_stage._response_schema("awr-research"),
        state_root=state,
        timeout_seconds=1,
    )

# Authority drift exists before the runner call.
with tempfile.TemporaryDirectory() as directory:
    state = pathlib.Path(directory) / "state"
    with mock.patch.object(
        provider_adapters,
        "_host_model_catalog_probe",
        return_value=catalog("gemini-safe", "new-model"),
    ) as probe, mock.patch.object(
        provider_adapters,
        "render_command",
        side_effect=AssertionError("renderer reached"),
    ) as render, mock.patch.object(
        portable_agent.subprocess,
        "Popen",
        side_effect=AssertionError("workload reached"),
    ) as popen:
        try:
            invoke(issue(), state)
        except portable_agent.PortableAgentError as exc:
            assert exc.code == "provider_model_authority_changed"
        else:
            raise AssertionError("pre-call catalog drift was accepted")
        assert probe.call_count == 1
        assert render.call_count == 0
        assert popen.call_count == 0
        assert not state.exists()

# Authority drifts while inputs are copied.
with tempfile.TemporaryDirectory() as directory:
    state = pathlib.Path(directory) / "state"
    current = {"catalog": catalog("gemini-safe")}
    def drift_during_copy(*_args, **_kwargs):
        current["catalog"] = catalog("gemini-safe", "new-model")
        return set()
    with mock.patch.object(
        provider_adapters,
        "_host_model_catalog_probe",
        side_effect=lambda *_: current["catalog"],
    ) as probe, mock.patch.object(
        portable_agent,
        "_copy_inputs",
        side_effect=drift_during_copy,
    ) as copy_inputs, mock.patch.object(
        provider_adapters,
        "render_command",
        return_value=([str(fake), "PROMPT"], {}),
    ) as render, mock.patch.object(
        portable_agent.subprocess,
        "Popen",
        side_effect=AssertionError("workload reached"),
    ) as popen:
        try:
            invoke(issue(), state)
        except portable_agent.PortableAgentError as exc:
            assert exc.code == "provider_model_authority_changed"
        else:
            raise AssertionError("copy-time catalog drift was accepted")
        assert probe.call_count == 2
        assert copy_inputs.call_count == 1
        assert render.call_count == 1
        assert popen.call_count == 0
        assert state.is_dir()
        assert not any(state.glob("attempt-*"))

print("PASS: pre-call drift blocks renderer/Popen/state-root; copy-time drift blocks Popen on second revalidation")
PY
```

Result: `PASS: pre-call drift blocks renderer/Popen/state-root; copy-time drift blocks Popen on second revalidation`.

### Direct Python smoke suites

The following direct-script groups were run with `python3` for every listed file:

```sh
for test_file in \
  tests/direction_contract_smoke.py \
  tests/history_contract_v2_smoke.py \
  tests/history_audit_migration_smoke.py \
  tests/history_audit_plan_smoke.py \
  tests/history_audit_plan_authority_smoke.py \
  tests/history_audit_store_smoke.py \
  tests/history_audit_cas_foundation_smoke.py \
  tests/history_candidate_budget_authority_smoke.py \
  tests/history_receipt_authority_smoke.py \
  tests/history_verified_usage_authority_smoke.py \
  tests/history_l1_cost_authority_smoke.py
do
  python3 "$test_file"
done
```

Result: 234 executions, 234 passed, 0 failed.

```sh
for test_file in \
  tests/history_audit_l1_smoke.py \
  tests/history_metadata_shadow_smoke.py \
  tests/history_audit_runtime_smoke.py \
  tests/history_audit_cli_smoke.py \
  tests/history_audit_cli_lifecycle_smoke.py \
  tests/history_audit_cli_p0_lifecycle_smoke.py \
  tests/history_audit_host_cli_smoke.py \
  tests/history_audit_eval_smoke.py \
  tests/history_router_source_authority_smoke.py
do
  python3 "$test_file"
done
```

Result: 287 executions, 287 passed, 0 failed.

```sh
for test_file in \
  tests/history_budget_smoke.py \
  tests/history_projection_smoke.py \
  tests/history_retrieval_smoke.py \
  tests/history_retrieval_adversarial.py \
  tests/history_runtime_smoke.py \
  tests/history_stage_proxy_smoke.py \
  tests/history_stage_smoke.py \
  tests/history_store_smoke.py \
  tests/history_witness_smoke.py \
  tests/ledger_evidence_smoke.py \
  tests/runtime_policy_smoke.py
do
  python3 "$test_file"
done
```

Result: 356 unittest executions passed; `ledger_evidence_smoke.py` reported `ok: ledger evidence smoke`; `runtime_policy_smoke.py` reported `ok: runtime policy smoke`.

```sh
for test_file in \
  tests/provider_adapters_smoke.py \
  tests/provider_host_capability_evidence_smoke.py \
  tests/provider_model_catalog_authority_smoke.py \
  tests/provider_portable_hardening_smoke.py \
  tests/portable_stage_runtime_smoke.py \
  tests/portable_dynamic_output_smoke.py
do
  python3 "$test_file"
done
```

Final-HEAD result: 78 executions, 78 passed, 0 failed. Per-file counts were 28, 9, 9, 20, 11, and 1 respectively.

### Shell ABI and fake end-to-end gates

```sh
bash tests/portable_runtime_abi_smoke.sh
bash tests/runtime_abi_smoke.sh
bash tests/portable_hunt_awr_e2e_smoke.sh
```

Results:

- `portable_runtime_abi_smoke.sh`: all v1/v2 provider-control and fallback cases passed; `ok: portable runtime ABI smoke`.
- `runtime_abi_smoke.sh`: AwR fake flow and legacy ABI passed; `ok: runtime ABI smoke (default)`.
- `portable_hunt_awr_e2e_smoke.sh`: Hunt v2 portable, AwR v2 portable isolation, and v1 generation/runtime passed; `ok: portable Hunt/AwR e2e smoke`.

The unaffected `history_runtime_smoke.sh`, `history_mirror_smoke.sh`, `calibration_abi_smoke.sh`, and `generation_contract_smoke.sh` results remain baseline evidence and were not required for the final two-file delta.

### Product, specification, and static gates

```sh
python3 tests/verify_product_contract.py all
openspec validate scalable-history-runtime --strict
python3 - <<'PY'
from pathlib import Path
paths = sorted(Path("lib").glob("*.py")) + sorted(Path("tests").glob("*.py"))
for path in paths:
    compile(path.read_text(), str(path), "exec")
print(f"ok: compiled {len(paths)} Python sources")
PY
for script_file in $(rg --files -g '*.sh'); do /bin/bash -n "$script_file"; done
git diff --check
```

Exact results:

- Product contract: `ok: all`.
- OpenSpec: `Change 'scalable-history-runtime' is valid`.
- Python static compile: `ok: compiled 71 Python sources`.
- Bash syntax: `ok: bash syntax (23 files)`.
- `git diff --check`: exit 0.

### Supplemental verification

```sh
python3 tests/verify_history_retrieval_benchmark.py
python3 tests/history_retrieval_round2.py
bash litwatch_test.sh
```

Results:

- Sealed synthetic retrieval benchmark: 40 tests passed; `PASS: synthetic history-retrieval benchmark matches its sealed contract`.
- Retrieval round two: 26 tests passed.
- Litwatch: `litwatch tests: 14 ok, 0 fail, 1 skip`.

### Provider fail-closed attacks

A temporary fake `opencode`/`agy` fixture invoked `python3 -B lib/history_audit_cli.py provider-command` with the following provider/model states. A hardcoded temporary marker recorded every fake executable invocation, avoiding dependence on environment variables scrubbed by host probing.

| Attack | Observed result |
|---|---|
| OpenCode with `--model openrouter/auto` | Exit 2; rejected before any probe or workload invocation |
| agy with `--model claude-sonnet-4-6` | Exit 2; rejected before any probe or workload invocation |
| agy with omitted model | Exit 2; rejected before any probe or workload invocation |
| OpenCode with `--model openai/not-listed` | Only `models --pure` probe ran; absent catalog membership rejected before workload |
| OpenCode omitted model with fake config default `openrouter/auto` | Only `--pure debug config` ran; rejected before catalog or workload invocation |

Result: `PASS provider black-box fail-closed attacks`.

## Static policy scans

```sh
rg -n 'complete_match|excluded_batch_ids_hash|"basis"' \
  lib/history_*v2.py lib/history_audit*.py history/*v1.json tests/history_audit* || true
rg -n -i 'claude' \
  lib/provider_adapters.py lib/portable_agent.py lib/portable_stage.py \
  history/provider-adapters-v1.json hunt.sh awr-side.sh docs/backends.md README.md || true
```

The history scan found only the migration/quarantine compatibility list and alias-rejection tests. The provider scan found denylist/reserved-route enforcement, documentation of forbidden Claude/Anthropic routes, and the explicit v1 opt-in path. No Claude route appeared in the v2 provider/default/fallback path. The full product-contract gate also passed.

## Skips and evidence boundary

The only skipped check was Litwatch T11: `skip T11 smoke: network unavailable or rate-limited`. Simulated HTTP 429/503 retry cases passed.

This run establishes local contract, migration, recovery, provider-routing, portable-runtime, v1 compatibility, fake Hunt/AwR end-to-end, and static-policy behavior at the fixed commit. It does not qualify real provider capacity, external model behavior, production qrels, currency-price accuracy, or operating-system containment of the portable mirror. Production `complete_no_match` remains a veto condition under the tested contract.

## Execution notes

One shell-syntax wrapper attempt used zsh's special `path` variable and removed `bash` from command lookup. The corrected `/bin/bash` run passed all 23 shell files. One initial black-box observer stored its marker path in an environment variable that host probing correctly scrubbed; the hardcoded temporary-path rerun passed. Neither harness issue invoked a model workload or changed product files.
