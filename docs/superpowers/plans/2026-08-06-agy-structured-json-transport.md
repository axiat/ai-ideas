# Agy Structured JSON Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace AwR v2's ambiguous Agy text transport with version-gated structured JSON while retaining strict schema, attestation, mirror, and publication checks.

**Architecture:** A host-owned version probe admits Agy 1.1.8 or newer. `prepare_stage()` freezes one exact response schema; the Agy command passes that schema inline to `--json-schema`, and `portable_agent` accepts only a successful provider outer object whose type-exact `json_schema` echo and `structured_output` match the frozen request. The host validates and canonicalizes only the extracted structured value; no text fallback or `response` recovery exists.

**Tech Stack:** Python 3 standard library, Bash, `unittest`, fake CLI executables, OpenSpec.

## Global Constraints

- Never invoke Claude directly or indirectly through a CLI, model route, hook, plugin, fallback, subagent, or test.
- Preserve the uncommitted `ledger.tsv` in the main checkout; execute the plan in an isolated worktree created with `superpowers:using-git-worktrees`.
- AwR v2 SHALL require Agy 1.1.8 or newer and SHALL fail before workload launch for missing, malformed, older, timed-out, non-zero, or stderr-producing version probes.
- Agy SHALL retain an explicit catalog-verified non-Claude model and optional validated effort; provider/model failover remains controlled only by the declared pool.
- Agy SHALL use `--output-format json --json-schema <inline-schema>` with no text fallback.
- The inline schema SHALL be the frozen canonical schema bytes with only the codec-owned terminal LF removed; `strip()` and `rstrip()` are forbidden.
- Provider request, command argv, schema echo, inner validation, preflight, and completion SHALL refer to the same frozen response schema.
- Complete provider stdout SHALL remain under 128 KiB. The outer JSON may contain finite floating-point metadata but SHALL reject invalid UTF-8, duplicate keys, non-finite constants, non-object roots, non-success status, absent/non-object `structured_output`, and absent/type-different `json_schema`.
- Only `structured_output` SHALL enter strict model-value, closed-schema, NFC, no-float, and exact-attestation validation. `response`, usage, duration, and conversation metadata SHALL never supply artifact bytes.
- Only host-canonical UTF-8 JSON with one terminal LF SHALL be hashed, imported, projected, and named by completion receipts.
- Codex, Kimi, and OpenCode SHALL retain raw canonical stdout; Grok SHALL retain its current provider-owned JSON plus exact terminal-fence decoder.
- Existing mirror integrity, bounded `.tmp`, process-group quiescence, cleanup-before-import, and no-brain-state-recovery checks remain active.
- Automated tests SHALL use fake providers. One bounded real Agy `awr-judge` call is permitted only after offline tests and independent review pass; it SHALL pin `gemini-3.6-flash-high` with effort `high`, run without retry, and stop if any automatic Claude path is found.

## File Map

- `lib/provider_adapters.py`: Agy minimum-version probe, command grammar, diagnostic schema placeholder, and launch revalidation.
- `lib/portable_stage.py`: frozen response-schema authority and binding-covered Agy transport instruction.
- `lib/portable_agent.py`: Agy provider-outer decoding and host canonicalization.
- `history/provider-adapters-v1.json`: tracked `agy-portable-v2` grammar identity.
- `tests/fake_portable_stage_provider.py`: offline Agy version, outer transport, corruption, and three-stage fixtures.
- `tests/provider_adapters_smoke.py`, `tests/history_audit_cli_smoke.py`, `tests/provider_model_catalog_authority_smoke.py`: resolver, argv, catalog, and version gates.
- `tests/provider_portable_hardening_smoke.py`, `tests/portable_stage_runtime_smoke.py`: schema freezing and strict transport behavior.
- `tests/portable_runtime_abi_smoke.sh`, `tests/portable_hunt_awr_e2e_smoke.sh`: subprocess and all-Agy AwR integration.
- `docs/backends.md`, `README.md`, `docs/getting-started.md`: current operator contract.
- `openspec/changes/scalable-history-runtime/{proposal.md,design.md,tasks.md}` and `specs/provider-neutral-execution/spec.md`: normative product contract and qualification state.

---

### Task 1: Gate Agy on structured-output-capable CLI versions

**Files:**

- Modify: `lib/provider_adapters.py:90-110, 440-690, 983-1150`
- Modify: `tests/fake_portable_stage_provider.py:390-430`
- Modify: `tests/provider_adapters_smoke.py`
- Modify: `tests/provider_model_catalog_authority_smoke.py:95-275`
- Modify: `tests/history_audit_cli_smoke.py:20-55`
- Modify: `tests/portable_runtime_abi_smoke.sh:35-70`
- Modify: `tests/portable_hunt_awr_e2e_smoke.sh:40-75`
- Modify: Agy intent helpers in `tests/provider_host_capability_evidence_smoke.py`, `tests/provider_portable_hardening_smoke.py`, `tests/portable_stage_runtime_smoke.py`, and `tests/history_runtime_smoke.py`

**Interfaces:**

- Produces: `_parse_agy_cli_revision(raw: bytes) -> str`, accepting only canonical `MAJOR.MINOR.PATCH\n` at or above `1.1.8`.
- Produces: `_host_agy_version_probe(provider: str, executable_path: str) -> bytes | None`, using the existing bounded diagnostic runner; the resolver passes those bytes through `_parse_agy_cli_revision`.
- Changes: `_resolve_command_intent(..., version_probe)` and `_resolve_command_intent_for_test(..., version_probe)` require a successful Agy version result before the model-catalog probe.
- Consumes: `revalidate_command_intent_for_launch(intent)` reruns the host Agy version gate before catalog revalidation.

- [ ] **Step 1: Write RED tests for exact version grammar**

Add table-driven assertions in `tests/provider_model_catalog_authority_smoke.py`:

```python
accepted = (b"1.1.8\n", b"1.1.10\n", b"2.0.0\n")
rejected = (
    b"1.1.7\n", b"01.1.8\n", b"1.01.8\n", b"1.1.08\n",
    b"1.1.8\r\n", b"v1.1.8\n", b"1.1.8-beta\n",
    b"1.1.8+build\n", b"1.1.8\nextra\n", b"\xff\n",
)
for raw in accepted:
    self.assertEqual(provider_adapters._parse_agy_cli_revision(raw), raw[:-1].decode("ascii"))
for raw in rejected:
    with self.assertRaises(provider_adapters.ProviderResolutionError):
        provider_adapters._parse_agy_cli_revision(raw)
```

Add bounded-probe cases for timeout, non-zero exit, nonempty stderr, output over 32 KiB, and exact `1.1.8` success. Assert that unsupported versions fail before `model_catalog_probe`, `render_command`, or provider workload invocation.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
rtk python3 tests/provider_model_catalog_authority_smoke.py
rtk python3 tests/provider_adapters_smoke.py
```

Expected: new tests fail because the Agy version parser/probe and resolver gate do not exist.

- [ ] **Step 3: Implement the bounded version parser and probe**

Add:

```python
_AGY_STRUCTURED_JSON_MIN_VERSION = (1, 1, 8)
_AGY_VERSION_PATTERN = re.compile(
    rb"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\n"
)

def _parse_agy_cli_revision(raw):
    if type(raw) is not bytes:
        raise ProviderResolutionError("agy CLI version is unavailable")
    match = _AGY_VERSION_PATTERN.fullmatch(raw)
    if match is None:
        raise ProviderResolutionError("agy CLI version is unavailable")
    version = tuple(int(part) for part in match.groups())
    if version < _AGY_STRUCTURED_JSON_MIN_VERSION:
        raise ProviderResolutionError("agy structured output is unsupported")
    return raw[:-1].decode("ascii")
```

`_host_agy_version_probe` runs `[executable_path, "--version"]` in a temporary directory with the current five-second/32 KiB bounded probe, requires `returncode == 0` and empty stderr, and returns `None` on any failure. Agy intent resolution requires a callable probe and a valid revision before catalog lookup. Production resolution injects the host probe; test resolution requires an explicit fake probe. Launch revalidation calls the host probe again before catalog lookup.

- [ ] **Step 4: Teach every fake Agy executable the version command**

Before model-catalog and workload branches, add the exact behavior:

```sh
if [ "$1" = "--version" ]; then
  printf '%s\n' '1.1.10'
  exit 0
fi
```

The Python fixture uses the equivalent `sys.stdout.write("1.1.10\n")`. Keep version probes out of catalog-call counters and provider workload markers. Add `version_probe=lambda *_: b"1.1.10\n"` to test-only Agy intent helpers; Codex, Kimi, Grok, and OpenCode must not call it.

- [ ] **Step 5: Run GREEN tests and subprocess regressions**

Run:

```bash
rtk python3 tests/provider_model_catalog_authority_smoke.py
rtk python3 tests/provider_adapters_smoke.py
rtk python3 tests/history_audit_cli_smoke.py
rtk bash tests/portable_runtime_abi_smoke.sh
```

Expected: every suite passes; Agy catalog counts remain unchanged because version diagnostics are counted separately.

- [ ] **Step 6: Commit Task 1**

```bash
rtk git add lib/provider_adapters.py tests/fake_portable_stage_provider.py \
  tests/provider_adapters_smoke.py tests/provider_model_catalog_authority_smoke.py \
  tests/history_audit_cli_smoke.py tests/portable_runtime_abi_smoke.sh \
  tests/portable_hunt_awr_e2e_smoke.sh \
  tests/provider_host_capability_evidence_smoke.py \
  tests/provider_portable_hardening_smoke.py \
  tests/portable_stage_runtime_smoke.py tests/history_runtime_smoke.py
rtk git commit -m "fix: gate Agy structured output version"
```

---

### Task 2: Freeze one response schema across preflight and launch

**Files:**

- Modify: `lib/portable_stage.py:170-225, 530-710, 958-1005`
- Modify: `tests/portable_stage_runtime_smoke.py`
- Modify: `tests/provider_portable_hardening_smoke.py`

**Interfaces:**

- Changes: `_issue_prepared(..., response_schema_raw: bytes)` stores the exact canonical schema in the opaque private authority.
- Produces: `_frozen_response_schema(prepared, private) -> dict`, verifying the private bytes, preflight/output-contract SHA, and current stage schema before launch.
- Consumes: `run_stage()` passes the returned frozen object to `run_portable_stdout_attempt()` and never independently regenerates the launch schema.

- [ ] **Step 1: Write the schema-drift RED test**

Prepare a valid stage, then replace `_response_schema` before `run_stage()`:

```python
prepared, _, _ = self._prepare(root, stage="awr-research")
changed = copy.deepcopy(portable_stage._response_schema("awr-research"))
changed["properties"]["artifacts"]["maxItems"] += 1
with mock.patch.object(portable_stage, "_response_schema", return_value=changed), \
     mock.patch.object(portable_agent, "run_portable_stdout_attempt") as workload:
    with self.assertRaises(portable_stage.PortableStageError) as caught:
        portable_stage.run_stage(prepared, timeout_seconds=2)
self.assertEqual(caught.exception.code, "response_schema_changed")
workload.assert_not_called()
```

Add a positive capture test proving that the exact frozen object appears in the provider request and is the object passed to `run_portable_stdout_attempt()`.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
rtk python3 tests/portable_stage_runtime_smoke.py
```

Expected: the drift case reaches the mocked workload because `run_stage()` currently regenerates the schema without comparing it to preflight.

- [ ] **Step 3: Implement the opaque frozen-schema authority**

At prepare time compute `response_schema_raw = _canonical_bytes(schema)` once. Store those bytes in `_PREPARED_STAGES` through `_issue_prepared`, and continue hashing the same bytes into `output_contract` and preflight.

Add:

```python
def _frozen_response_schema(prepared, private):
    raw = private["response_schema_raw"]
    expected_sha = prepared["output_contract"]["response_schema_sha256"]
    current = _canonical_bytes(_response_schema(prepared["stage"]))
    if _sha(raw) != expected_sha or current != raw:
        raise PortableStageError("response_schema_changed")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableStageError("invalid_prepared_stage") from exc
    return value
```

Call it after preflight verification and before launch-intent revalidation or provider rendering. Pass only this returned object into the portable agent.

- [ ] **Step 4: Run GREEN and portable hardening tests**

Run:

```bash
rtk python3 tests/portable_stage_runtime_smoke.py
rtk python3 tests/provider_portable_hardening_smoke.py
```

Expected: schema drift fails before provider rendering; normal stages retain identical request, import, projection, and receipt hashes.

- [ ] **Step 5: Commit Task 2**

```bash
rtk git add lib/portable_stage.py tests/portable_stage_runtime_smoke.py \
  tests/provider_portable_hardening_smoke.py
rtk git commit -m "fix: freeze portable response schema"
```

---

### Task 3: Implement Agy structured JSON transport

**Files:**

- Modify: `history/provider-adapters-v1.json`
- Modify: `lib/provider_adapters.py:100-110, 1150-1260, 1790-1895`
- Modify: `lib/portable_stage.py:380-450, 980-1005`
- Modify: `lib/portable_agent.py:600-725, 1317-1420`
- Modify: `tests/fake_portable_stage_provider.py`
- Modify: `tests/provider_adapters_smoke.py:220-325`
- Modify: `tests/history_audit_cli_smoke.py:90-280`
- Modify: `tests/provider_model_catalog_authority_smoke.py`
- Modify: `tests/provider_host_capability_evidence_smoke.py`
- Modify: `tests/provider_portable_hardening_smoke.py`
- Modify: `tests/portable_stage_runtime_smoke.py`
- Modify: `tests/portable_hunt_awr_e2e_smoke.sh:640-810`

**Interfaces:**

- Changes: `_render_command_fields(..., response_schema_argument: str | None = None)` requires an Agy schema argument.
- Changes: `render_command(capability, mirror, prompt, schema_path=None, *, response_schema=None)` preserves the legacy `schema_path` rejection and requires a real dictionary for runnable Agy commands.
- Produces: `_parse_agy_transport(raw: bytes, response_schema: dict) -> tuple[dict, bytes]`.
- Changes: `_parse_provider_stdout(provider: str, raw: bytes, response_schema: dict) -> tuple[dict, bytes]`.
- Consumes: `run_portable_stdout_attempt()` gives the same frozen schema to command rendering, outer schema comparison, and inner validation.

- [ ] **Step 1: Write provider-command RED tests**

Change only the Agy expected argv to:

```python
[
    "<EXECUTABLE>", "--dangerously-skip-permissions",
    "--disable-slash-commands", "--output-format", "json",
    "--add-dir", "<MIRROR>", "--model", "gemini-3.6-flash-high",
    "--effort", "high", "--json-schema", "RESPONSE_SCHEMA",
    "--print", "<PROMPT>",
]
```

Add a runtime-render test with a small dictionary schema and assert the argv contains its sorted compact JSON without a terminal LF. Assert runnable Agy rendering without `response_schema` fails before launch. Retain byte-exact expected argv for every other provider and retain rejection of non-`None` legacy `schema_path`.

- [ ] **Step 2: Extend the fake provider and write transport RED tests**

Dispatch JSON wrappers by executable name, not by `--output-format json`, because Grok and Agy now share that flag. Add:

```python
def _flag_value(arguments, flag):
    index = arguments.index(flag)
    return arguments[index + 1]

def _agy_json_requested(arguments):
    return pathlib.Path(sys.argv[0]).name == "agy" and (
        _flag_value(arguments, "--output-format") == "json"
    )
```

Build one outer object containing `status="SUCCESS"`, the parsed inline schema as `json_schema`, raw-spliced inner JSON as `structured_output`, deliberately noisy/fenced text in `response`, and finite floating-point duration/usage metadata. Raw splicing preserves duplicate-key and non-NFC inner corruption fixtures.

Add these tests in `tests/provider_portable_hardening_smoke.py`:

```text
test_agy_structured_transport_imports_only_structured_output
test_agy_structured_transport_rejects_invalid_outer
test_agy_structured_transport_requires_success_status
test_agy_structured_transport_requires_object_payload
test_agy_structured_transport_requires_type_exact_schema_echo
test_agy_structured_transport_rejects_non_strict_inner_values
test_agy_structured_transport_preserves_closed_schema_validation
```

The success case asserts canonical imported bytes, matching `model_envelope_sha256`, valid projection, and absence of noisy `response` text. Rejection cases cover malformed/invalid UTF-8 outer JSON, outer array, duplicate outer keys, missing/failure status, missing/null/array payload, missing/mutated schema, `1` versus `1.0`, `1` versus `true`, inner duplicate keys, float, non-NFC, surrogate, closed-schema errors, and attestation drift. Every failure asserts no import, projection, or completion.

- [ ] **Step 3: Run the transport tests and verify RED**

Run:

```bash
rtk python3 tests/provider_adapters_smoke.py
rtk python3 tests/history_audit_cli_smoke.py
rtk python3 tests/provider_portable_hardening_smoke.py
```

Expected: command tests still see text mode; Agy outer success is rejected by the raw canonical decoder.

- [ ] **Step 4: Implement the command grammar and tracked identity**

Set `registry_revision` to `2026-08-06` and Agy grammar to `agy-portable-v2`. Add a fixed diagnostic placeholder `RESPONSE_SCHEMA`. Runnable rendering canonicalizes the validated response-schema dictionary, verifies the encoded result ends in exactly one LF, removes only `raw[-1:]`, decodes it as UTF-8, and passes the remaining compact JSON to `--json-schema`.

Render Agy as:

```python
argv += [
    "--dangerously-skip-permissions", "--disable-slash-commands",
    "--output-format", "json", "--add-dir", mirror,
    "--model", model,
]
if reasoning is not None:
    argv += ["--effort", reasoning]
argv += ["--json-schema", response_schema_argument, "--print", prompt]
```

Recompute and replace the byte-exact registry constant:

```bash
rtk shasum -a 256 history/provider-adapters-v1.json
```

Do not change the registry schema name or loosen its exact-byte check.

- [ ] **Step 5: Bind the Agy structured-result instruction**

Select this Agy-specific stdout instruction before request-binding computation:

```text
Return exactly one JSON object matching response_schema as the structured final result. The Agy CLI owns the outer stdout JSON; only a status=SUCCESS structured_output member is eligible for import. Do not put Markdown fences or narration inside the structured value.
```

Codex/Kimi/OpenCode retain the raw instruction; Grok retains the exact terminal-fence instruction. Update request-binding tests and fake expected instruction constants.

- [ ] **Step 6: Implement strict Agy outer decoding**

Add:

```python
def _parse_agy_transport(raw, response_schema):
    outer = _parse_strict_json(raw, reject_floats=False, require_nfc=False)
    if type(outer) is not dict or outer.get("status") != "SUCCESS":
        raise PortableAgentError("malformed_output")
    if type(outer.get("structured_output")) is not dict:
        raise PortableAgentError("malformed_output")
    try:
        observed_schema = _canonical_json_bytes(outer.get("json_schema"))
        expected_schema = _canonical_json_bytes(response_schema)
        model_raw = _canonical_json_bytes(outer["structured_output"])
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PortableAgentError("malformed_output") from exc
    if observed_schema != expected_schema:
        raise PortableAgentError("malformed_output")
    value = _parse_strict_model_json(model_raw)
    return value, model_raw
```

Canonical byte comparison makes schema echo type-exact. Update `_parse_provider_stdout` so only Agy takes this path; raw providers and Grok remain unchanged. In `run_portable_stdout_attempt`, pass `response_schema` to both `render_command` and `_parse_provider_stdout`, then retain existing closed-schema and exact-attestation checks before import.

- [ ] **Step 7: Convert all-Agy fixtures and run GREEN**

Make every Agy mirror/tmp/race fixture emit the provider outer around its existing inner envelope. Update `agy-portable-audit` to verify JSON mode, one inline schema, the Agy-specific request instruction, and all three research/prior-work/judge projections and receipts.

Run:

```bash
rtk python3 tests/provider_adapters_smoke.py
rtk python3 tests/history_audit_cli_smoke.py
rtk python3 tests/provider_model_catalog_authority_smoke.py
rtk python3 tests/provider_host_capability_evidence_smoke.py
rtk python3 tests/provider_portable_hardening_smoke.py
rtk python3 tests/portable_stage_runtime_smoke.py
rtk bash tests/portable_hunt_awr_e2e_smoke.sh
rtk bash tests/portable_runtime_abi_smoke.sh
```

Expected: every command exits zero; the fake all-Agy scan completes all three portable stages without text parsing.

- [ ] **Step 8: Commit Task 3**

```bash
rtk git add history/provider-adapters-v1.json lib/provider_adapters.py \
  lib/portable_stage.py lib/portable_agent.py \
  tests/fake_portable_stage_provider.py tests/provider_adapters_smoke.py \
  tests/history_audit_cli_smoke.py tests/provider_model_catalog_authority_smoke.py \
  tests/provider_host_capability_evidence_smoke.py \
  tests/provider_portable_hardening_smoke.py \
  tests/portable_stage_runtime_smoke.py tests/portable_hunt_awr_e2e_smoke.sh
rtk git commit -m "fix: use Agy structured JSON transport"
```

---

### Task 4: Synchronize OpenSpec and operator documentation

**Files:**

- Modify: `openspec/changes/scalable-history-runtime/specs/provider-neutral-execution/spec.md`
- Modify: `openspec/changes/scalable-history-runtime/design.md`
- Modify: `openspec/changes/scalable-history-runtime/proposal.md`
- Modify: `openspec/changes/scalable-history-runtime/tasks.md`
- Modify: `docs/backends.md`
- Modify: `README.md`
- Modify: `docs/getting-started.md`
- Modify: `docs/superpowers/specs/2026-08-03-scalable-history-runtime-implementer-contract.md`
- Modify: `tests/verify_product_contract.py`

**Interfaces:**

- Consumes: tested `agy-portable-v2`, minimum version, frozen schema, and decoder behavior.
- Produces: one current normative contract and concise operator guidance; historical qualification item 8.5 remains unchanged.

- [ ] **Step 1: Write product-contract RED assertions**

Assert that the tracked registry contains `agy-portable-v2`, README/getting-started require Agy 1.1.8+, and backend documentation names `--output-format json`, `--json-schema`, `structured_output`, and no text fallback.

Run:

```bash
rtk python3 tests/verify_product_contract.py runtime
```

Expected: assertions fail against the current raw-text documentation.

- [ ] **Step 2: Update the normative OpenSpec contract**

Specify three response paths: Grok provider JSON plus terminal fence, Agy provider JSON plus structured output, and raw canonical stdout for Codex/Kimi/OpenCode. Add scenarios for Agy 1.1.8 preflight/launch gates, frozen schema identity, exact schema echo, successful structured output, invalid outer rejection, no `response` recovery, and no text fallback.

Update OpenSpec design decisions 3 and 10. Correct the proposal's provider-default statement so Agy still requires an explicit catalog model. Add unchecked task 8.6 for offline tests plus one final-revision live qualification; do not rewrite the historical raw-transport evidence in task 8.5.

- [ ] **Step 3: Update current user documentation**

Document the Agy minimum version, preflight behavior, exact inline schema, outer checks, canonical inner import, and failure behavior in `docs/backends.md`. Add only concise prerequisites and the canonical backend-doc link to README/getting-started. Add an amendment link from the implementer contract to the 2026-08-06 design; do not copy the full transport description into multiple files.

- [ ] **Step 4: Validate prose, product contract, and OpenSpec**

Run:

```bash
rtk rg -n 'TBD|TODO|PLACEHOLDER|旧图|之前那版|这页|本节|下面来说|边界|一句话' \
  docs/backends.md README.md docs/getting-started.md \
  docs/superpowers/specs/2026-08-03-scalable-history-runtime-implementer-contract.md \
  openspec/changes/scalable-history-runtime
rtk python3 tests/verify_product_contract.py all
rtk openspec validate scalable-history-runtime --strict
rtk git diff --check
```

Expected: prose scan reports no newly introduced violations, product checks pass, OpenSpec is valid, and diff check is clean.

- [ ] **Step 5: Commit Task 4**

```bash
rtk git add openspec/changes/scalable-history-runtime \
  docs/backends.md README.md docs/getting-started.md \
  docs/superpowers/specs/2026-08-03-scalable-history-runtime-implementer-contract.md \
  tests/verify_product_contract.py
rtk git commit -m "docs: specify Agy structured JSON transport"
```

---

### Task 5: Review, qualify one real Agy stage, and close evidence

**Files:**

- Modify after successful qualification: `docs/superpowers/specs/2026-08-06-agy-structured-json-transport-design.md`
- Modify after successful qualification: `docs/backends.md`
- Modify after successful qualification: `openspec/changes/scalable-history-runtime/tasks.md`

**Interfaces:**

- Consumes: final reviewed implementation commit and explicit `gemini-3.6-flash-high`/`high` route.
- Produces: one completion receipt, canonical inner-envelope hash, projected judge hash, cleanup evidence, and checked OpenSpec task 8.6.

- [ ] **Step 1: Run the complete offline verification set**

Run each command separately and require exit zero:

```bash
rtk python3 tests/provider_adapters_smoke.py
rtk python3 tests/history_audit_cli_smoke.py
rtk python3 tests/provider_model_catalog_authority_smoke.py
rtk python3 tests/provider_host_capability_evidence_smoke.py
rtk python3 tests/provider_portable_hardening_smoke.py
rtk python3 tests/portable_stage_runtime_smoke.py
rtk python3 tests/history_runtime_smoke.py
rtk bash tests/portable_runtime_abi_smoke.sh
rtk bash tests/portable_hunt_awr_e2e_smoke.sh
rtk bash tests/runtime_abi_smoke.sh
rtk bash tests/generation_contract_smoke.sh
rtk python3 tests/verify_product_contract.py all
rtk python3 -m py_compile lib/provider_adapters.py lib/portable_agent.py lib/portable_stage.py
rtk bash -n awr-side.sh tests/portable_runtime_abi_smoke.sh tests/portable_hunt_awr_e2e_smoke.sh
rtk openspec validate scalable-history-runtime --strict
rtk git diff --check
```

- [ ] **Step 2: Obtain independent code and contract review**

Use `superpowers:requesting-code-review`. Require reviewers to check the version gate, schema TOCTOU closure, type-exact echo, no fallback, raw-provider/Grok regressions, no-Claude routes, and durable-state failure behavior. Repair every blocking finding with a new RED/GREEN cycle and rerun Step 1.

- [ ] **Step 3: Inspect Agy configuration without starting a model**

Verify Agy version, explicit model catalog membership, agents, plugins, hooks, and fallback configuration. Do not print credentials. Stop before the live call if any automatic route can invoke Claude. The eventual workload command must contain the explicit Gemini model, `--effort high`, JSON output, inline schema, disabled slash expansion, and no fallback provider.

- [ ] **Step 4: Run one bounded real `awr-judge` portable stage**

Create one task-specific temporary root and validate that it is not a broad
filesystem path:

```bash
AGY_SMOKE_ROOT=$(rtk mktemp -d)
case "$AGY_SMOKE_ROOT" in
  /var/folders/*/T/tmp.*|/tmp/tmp.*) ;;
  *) exit 2 ;;
esac
```

Use that resolved value in the single workload call:

```bash
rtk python3 -B lib/portable_stage.py run \
  --surface awr \
  --provider agy \
  --model gemini-3.6-flash-high \
  --reasoning high \
  --stage awr-judge \
  --seat agy-structured-json-live-smoke \
  --serialized-prompt history/provider-adapters-v1.json \
  --input draft.md=docs/superpowers/specs/2026-08-06-agy-structured-json-transport-design.md \
  --input priorwork.md=docs/backends.md \
  --input task.md=README.md \
  --input rubric.md=rubric.md \
  --input brainstorming_policy.md=brainstorming_policy.md \
  --output-root "$AGY_SMOKE_ROOT/output" \
  --state-root "$AGY_SMOKE_ROOT/state" \
  --timeout-seconds 300
```

Run exactly once. A failure requires diagnosis and a new tested revision before any second provider call.

- [ ] **Step 5: Verify and record qualification evidence**

Require all of the following before checking task 8.6:

```text
state/completion.json is canonical JSON with one LF
state/imports/<model_envelope_sha256>.json exists and hashes exactly
output/judge.md is nonempty and matches its completion descriptor
preflight argv selects agy, gemini-3.6-flash-high, high, json, and an inline schema
the imported envelope contains exact request attestation
no portable attempt directory remains
Agy logs confirm Gemini 3.6 Flash (High) and contain no Claude execution
```

Record the final code commit, completion ID, model-envelope SHA, preflight SHA, projected judge SHA, model/effort, one-call status, and cleanup result in the design and backend docs. Check OpenSpec task 8.6 only after every assertion passes. Remove only the exact temporary root after validating its path and recording the evidence.

- [ ] **Step 6: Rerun final verification and commit evidence**

Rerun Task 5 Step 1 after documentation changes. Then commit only the evidence files:

```bash
rtk git add docs/superpowers/specs/2026-08-06-agy-structured-json-transport-design.md \
  docs/backends.md openspec/changes/scalable-history-runtime/tasks.md
rtk git commit -m "docs: record Agy structured transport qualification"
```

- [ ] **Step 7: Finish the branch**

Use `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch`. Preserve the main checkout's `ledger.tsv`; merge or push only after explicit user direction.
