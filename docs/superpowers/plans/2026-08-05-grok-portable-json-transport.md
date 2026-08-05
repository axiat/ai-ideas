# Grok Portable JSON Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make real Grok and agy portable stages cross their provider transports while retaining the strict model-response contract, and avoid retry sleep after the final bounded Hunt round.

**Architecture:** Grok emits one provider-owned outer JSON object. `portable_agent` validates that transport, extracts a unique terminal fenced response when provider narration precedes it, strictly parses the inner model envelope, and imports host-canonicalized inner bytes. Agy receives binding-covered portable transport instructions that override the legacy role's file-output channel while leaving its artifact semantics unchanged. Other providers retain the raw canonical-stdout path. Hunt decides whether a future round exists before applying failure cooldown.

**Tech Stack:** Python 3 standard library, Bash, `unittest`, fake CLI executables, OpenSpec.

## Global Constraints

- Never invoke Claude directly or indirectly, including through tests, hooks, providers, or fallbacks.
- Preserve the uncommitted `ledger.tsv` in the main checkout; implement in an isolated worktree.
- Grok SHALL use `--output-format json`; the registry SHALL record `grok-portable-v2` and a new byte-exact registry hash.
- Grok outer JSON SHALL reject invalid UTF-8, duplicate keys, non-object roots, missing/non-string `text`, and any `stopReason` other than `end_turn`.
- Grok outer usage and cost numbers, including finite floating-point values, SHALL be accepted as transport metadata and SHALL never enter the model envelope.
- Grok inner text SHALL reject duplicate keys, floating-point values, non-finite constants, invalid JSON, and non-NFC strings before schema and request-attestation validation.
- Only the host-canonicalized inner model envelope SHALL be hashed, imported, projected, and referenced by completion receipts.
- Codex, Kimi, OpenCode, and agy SHALL retain the current exact raw canonical-stdout contract.
- A failed Hunt round SHALL sleep only when another round can execute; unlimited runs and non-terminal bounded rounds retain `FAIL_SLEEP_MIN`.
- Automated tests SHALL use fake providers. A completed transport revision MAY receive one real Grok portable-stage smoke; it SHALL use `grok-4.5`, `high`, one bounded AwR judge request, and no full Hunt round. A failed smoke SHALL NOT be blindly retried; a new call requires a diagnosed cause and a new tested transport revision.
- Before that live smoke, disable every Claude compatibility source with `GROK_CLAUDE_SKILLS_ENABLED=false`, `GROK_CLAUDE_RULES_ENABLED=false`, `GROK_CLAUDE_AGENTS_ENABLED=false`, `GROK_CLAUDE_MCPS_ENABLED=false`, `GROK_CLAUDE_HOOKS_ENABLED=false`, and `GROK_CLAUDE_SESSIONS_ENABLED=false`; inspect the effective Grok configuration and do not launch if any automatically triggered hook, plugin, MCP, fallback, or orchestration path can invoke Claude. An inert command that requires explicit user selection is not an invocation path for this smoke.

---

### Task 1: Grok JSON transport normalization

**Files:**

- Modify: `history/provider-adapters-v1.json`
- Modify: `lib/provider_adapters.py:104-107, 1784-1820`
- Modify: `lib/portable_agent.py:570-630, 802-890`
- Modify: `tests/fake_portable_stage_provider.py:215-355`
- Modify: `tests/provider_adapters_smoke.py`
- Modify: `tests/history_audit_cli_smoke.py:214-247`
- Modify: `tests/provider_model_catalog_authority_smoke.py:35-105`
- Modify: `tests/provider_portable_hardening_smoke.py:430-670`
- Test: `tests/portable_stage_runtime_smoke.py`

**Interfaces:**

- Consumes: resolver-issued `ProviderCommandIntent.provider`, bounded provider stdout bytes, and the existing response-schema validator.
- Produces: `_parse_provider_stdout(provider: str, raw: bytes) -> tuple[object, bytes]`, where the bytes are the canonical model envelope used for hashing and import.

- [ ] **Step 1: Write the provider-command RED tests**

Change the literal Grok argv expectations in `tests/provider_adapters_smoke.py` and `tests/history_audit_cli_smoke.py` from:

```python
"--output-format", "plain"
```

to:

```python
"--output-format", "json"
```

Assert that Codex, Kimi, OpenCode, and agy expected argv remain byte-for-byte unchanged.

- [ ] **Step 2: Run the provider-command tests and verify RED**

Run:

```bash
python3 tests/provider_adapters_smoke.py
python3 tests/history_audit_cli_smoke.py
```

Expected: both suites fail only because Grok still renders `plain`.

- [ ] **Step 3: Implement the command grammar and registry revision**

In `history/provider-adapters-v1.json`, change the registry revision to
`2026-08-05` and Grok grammar revision to `grok-portable-v2`. In
`_render_command_fields`, render:

```python
"--output-format", "json"
```

Recompute the complete registry file SHA-256 with:

```bash
shasum -a 256 history/provider-adapters-v1.json
```

and replace `_PROVIDER_REGISTRY_V1_SHA256`. Update registry mutation fixtures
that contain the tracked revision literal. Do not loosen the byte-exact
registry check.

- [ ] **Step 4: Run the provider-command tests and verify GREEN**

Run:

```bash
python3 tests/provider_adapters_smoke.py
python3 tests/history_audit_cli_smoke.py
python3 tests/provider_model_catalog_authority_smoke.py
```

Expected: all tests pass.

- [ ] **Step 5: Extend the fake provider with the real Grok transport shape**

When argv selects `--output-format json`, serialize a documented complete
outer response containing at least:

```python
{
    "text": noncanonical_inner_json,
    "stopReason": "end_turn",
    "sessionId": "fixture-session",
    "requestId": "fixture-request",
    "num_turns": 1,
    "usage": {
        "input_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 5,
        "reasoning_tokens": 2,
        "total_tokens": 15,
    },
    "modelUsage": {},
    "total_cost_usd": 0.001,
    "total_cost_usd_ticks": 10000000,
}
```

Generate `noncanonical_inner_json` from the valid inner object with indentation
and unsorted insertion order. Add modes for malformed outer JSON, a duplicate
outer `text` key, missing `text`, and `stopReason=max_tokens`. Existing inner
malformation, schema, NFC, duplicate-key, and attestation modes must still act
inside the Grok outer wrapper.

- [ ] **Step 6: Write the portable-stage RED tests**

Add tests using `_prepare(..., provider="grok")` that assert:

```python
completion = portable_stage.run_stage(prepared, timeout_seconds=2)
imported = pathlib.Path(prepared["state_root"]) / "imports" / (
    completion["model_envelope_sha256"] + ".json"
)
self.assertEqual(imported.read_bytes(), portable_agent._canonical_json_bytes(
    json.loads(imported.read_text(encoding="utf-8"))
))
```

The success fixture must contain non-canonical inner JSON. Separate cases must
assert no import, projected output, or completion for malformed/duplicate
outer JSON, missing text, `max_tokens`, invalid inner JSON, non-NFC inner
strings, floating-point inner values, and wrong request attestation.

Also retain one Codex test showing valid non-canonical raw stdout still raises
`noncanonical_output` before import.

- [ ] **Step 7: Run the portable-stage tests and verify RED**

Run:

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
```

Expected: Grok outer JSON success fails as an invalid model envelope; existing
non-Grok behavior remains green.

- [ ] **Step 8: Implement the two-layer decoder**

Refactor strict JSON parsing so duplicate-key detection and UTF-8 decoding are
shared while canonical-byte enforcement remains selectable. Add:

```python
def _parse_provider_stdout(provider, raw):
    if provider != "grok":
        value = _parse_canonical_stdout(raw)
        return value, raw
    outer = _parse_grok_transport(raw)
    inner_raw = outer["text"].encode("utf-8")
    value = _parse_strict_model_json(inner_raw)
    return value, _canonical_json_bytes(value)
```

`_parse_grok_transport` parses finite JSON numbers without applying the model
envelope's float ban, rejects duplicate keys and non-object roots, and requires
exactly a string `text` plus `stopReason == "end_turn"`. Unknown outer metadata
is ignored because it cannot affect the extracted response.

In `run_portable_stdout_attempt`, replace `_parse_canonical_stdout(stdout)`
with the provider-aware decoder. Validate the returned value against the
response schema, then hash/import/return the canonical model bytes rather than
the Grok outer stdout.

- [ ] **Step 9: Run the portable-stage tests and verify GREEN**

Run:

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
bash tests/portable_hunt_awr_e2e_smoke.sh
```

Expected: all tests pass; the fake Hunt review seat exercises Grok outer JSON.

- [ ] **Step 10: Commit Task 1**

```bash
git add history/provider-adapters-v1.json lib/provider_adapters.py \
  lib/portable_agent.py tests/fake_portable_stage_provider.py \
  tests/provider_adapters_smoke.py tests/history_audit_cli_smoke.py \
  tests/provider_model_catalog_authority_smoke.py \
  tests/provider_portable_hardening_smoke.py \
  tests/portable_stage_runtime_smoke.py
git commit -m "fix: normalize Grok portable JSON output"
```

---

### Task 2: Terminal Hunt failure skips retry cooldown

**Files:**

- Modify: `hunt.sh:1423-1430`
- Modify: `tests/portable_hunt_awr_e2e_smoke.sh`

**Interfaces:**

- Consumes: current shell variables `round`, `ROUND_LIMIT`, `fails`, `MAX_FAILS`, and `FAIL_SLEEP_MIN`.
- Produces: `fail_round(stage)` with unchanged failure accounting and conditional cooldown.

- [ ] **Step 1: Write the terminal-round RED test**

Add an e2e case that clones the repository, installs fake providers, and runs
Hunt with:

```bash
FAKE_PORTABLE_STAGE_MODE=malformed
HISTORY_RUNTIME_ABI=v2
HUNT_PROVIDER=codex
ROUND_LIMIT=1
MAX_FAILS=12
FAIL_SLEEP_MIN=1
SA_TARGET=0
```

Prepend a fake `sleep` executable that appends its arguments to
`$FAKE_SLEEP_LOG` and exits successfully. Assert the run logs one generate
failure and `Reached ROUND_LIMIT=1`, and assert `$FAKE_SLEEP_LOG` does not
exist. This test catches any sleep invocation without spending 60 seconds.

- [ ] **Step 2: Run the e2e test and verify RED**

Run:

```bash
bash tests/portable_hunt_awr_e2e_smoke.sh
```

Expected: the new case fails because current `fail_round` invokes the fake
sleep after the final round.

- [ ] **Step 3: Implement the retry-existence check**

Keep archive and failure accounting unchanged. After the `MAX_FAILS` check,
guard cooldown with:

```bash
if [ "$ROUND_LIMIT" -eq 0 ] || [ "$round" -lt "$ROUND_LIMIT" ]; then
  sleep_minutes "$FAIL_SLEEP_MIN"
fi
```

- [ ] **Step 4: Run the e2e and shell syntax tests and verify GREEN**

Run:

```bash
bash -n hunt.sh tests/portable_hunt_awr_e2e_smoke.sh
bash tests/portable_hunt_awr_e2e_smoke.sh
```

Expected: all cases pass and the terminal-failure case records no sleep.

- [ ] **Step 5: Commit Task 2**

```bash
git add hunt.sh tests/portable_hunt_awr_e2e_smoke.sh
git commit -m "fix: skip cooldown after final Hunt round"
```

---

### Task 3: Contract documentation and full verification

**Files:**

- Modify: `openspec/changes/scalable-history-runtime/specs/provider-neutral-execution/spec.md:43-60`
- Modify: `openspec/changes/scalable-history-runtime/tasks.md:43-49`
- Modify: `docs/backends.md:149-165`

**Interfaces:**

- Consumes: the implemented `grok-portable-v2` behavior and terminal-round cooldown behavior.
- Produces: an OpenSpec scenario and operator documentation matching the tested runtime.

- [ ] **Step 1: Update the OpenSpec provider-response contract**

Add a requirement stating that an adapter may unwrap a provider-owned machine
transport before model-envelope validation. Add a Grok scenario requiring
native JSON mode, successful terminal `text` extraction, strict inner schema
and attestation validation, host canonicalization, and no publication for
malformed or incomplete transport. Add unchecked task `8.4` for the live Grok
transport repair and terminal-round no-sleep regression; Task 4 checks it only
after the real smoke succeeds.

- [ ] **Step 2: Update backend documentation**

Document that Grok portable stages use `--output-format json`; the provider
wrapper is discarded after its final `text` is validated and canonicalized.
State that completion hashes refer to the canonical inner model envelope.
Document that a failed final bounded round exits without `FAIL_SLEEP_MIN`.

- [ ] **Step 3: Check human-readable prose and OpenSpec**

Run:

```bash
rg -n 'TBD|TODO|PLACEHOLDER|旧|之前那版|这页|本节|下面来说|边界' \
  docs/backends.md \
  openspec/changes/scalable-history-runtime/specs/provider-neutral-execution/spec.md \
  openspec/changes/scalable-history-runtime/tasks.md || true
openspec validate scalable-history-runtime --strict
git diff --check
```

Expected: the prose scan has no newly introduced banned/meta phrasing,
OpenSpec is valid, and the diff check is clean.

- [ ] **Step 4: Run focused and product regression suites**

Run:

```bash
python3 tests/provider_adapters_smoke.py
python3 tests/history_audit_cli_smoke.py
python3 tests/provider_model_catalog_authority_smoke.py
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
bash tests/portable_runtime_abi_smoke.sh
bash tests/portable_hunt_awr_e2e_smoke.sh
bash tests/runtime_abi_smoke.sh
bash tests/generation_contract_smoke.sh
python3 tests/verify_product_contract.py runtime
python3 tests/verify_product_contract.py fixtures
```

Expected: every command exits zero.

- [ ] **Step 5: Commit Task 3**

```bash
git add docs/backends.md \
  openspec/changes/scalable-history-runtime/specs/provider-neutral-execution/spec.md \
  openspec/changes/scalable-history-runtime/tasks.md
git commit -m "docs: specify Grok portable JSON transport"
```

---

### Task 4: Normalize Grok's exact JSON fence and verify live

**Files:**

- Modify: `lib/portable_agent.py`
- Modify: `tests/fake_portable_stage_provider.py`
- Modify: `tests/provider_portable_hardening_smoke.py`
- Modify: `tests/portable_stage_runtime_smoke.py`
- Modify: `docs/superpowers/specs/2026-08-05-grok-portable-json-transport-design.md`
- Modify: `docs/backends.md`
- Modify: `openspec/changes/scalable-history-runtime/specs/provider-neutral-execution/spec.md`
- Modify: `openspec/changes/scalable-history-runtime/tasks.md`

**Interfaces:**

- Consumes: validated Grok outer `text` and the Task 1 strict inner JSON parser.
- Produces: `_grok_model_text_bytes(text: str) -> bytes`, accepting bare JSON or one exact whole-text fenced `json` block.

- [ ] **Step 1: Write the exact-fence RED tests**

Make the default fake Grok success response mirror the observed live output:

```python
text = "```json\n" + noncanonical_inner_json + "\n```"
```

Add a separate bare-inner success mode. Add rejection modes for leading text
before the fence, trailing text after the fence, a wrong fence language, and a
missing closing fence. Every rejection asserts no import, projection, or
completion.

- [ ] **Step 2: Run the portable tests and verify RED**

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
```

Expected: the exact fenced success is rejected as `malformed_output`; the bare
success and prior rejection cases retain their existing behavior.

- [ ] **Step 3: Implement narrow fence normalization**

After the outer `text` string is encoded safely, accept either bare JSON bytes
or bytes that start exactly with `b"```json\n"` and end exactly with
`b"\n```"`. Strip only those two markers. Do not trim whitespace, search for a
JSON substring, remove arbitrary prefixes/suffixes, or repair malformed JSON.
The extracted bytes continue through duplicate-key, float, constant, NFC,
closed-schema, and exact-attestation validation before canonical import.

- [ ] **Step 4: Run focused GREEN and commit the code**

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
bash tests/portable_hunt_awr_e2e_smoke.sh
git diff --check
git add lib/portable_agent.py tests/fake_portable_stage_provider.py \
  tests/provider_portable_hardening_smoke.py \
  tests/portable_stage_runtime_smoke.py
git commit -m "fix: accept exact Grok JSON response fence"
```

- [ ] **Step 5: Update the design, OpenSpec, and backend contract**

Record that the real Grok CLI returned one exact whole-text `json` fence.
Permit only that wrapper or bare JSON, with no surrounding text. Keep OpenSpec
task `8.4` unchecked until Step 6 succeeds.

- [ ] **Step 6: Run one real Grok portable-stage smoke**

Repeat the safety inspection from Global Constraints. Create a temporary AwR
judge request with declared `draft.md`, `priorwork.md`, `task.md`, `rubric.md`,
and `brainstorming_policy.md`, then run:

```bash
GROK_CLAUDE_SKILLS_ENABLED=false \
GROK_CLAUDE_RULES_ENABLED=false \
GROK_CLAUDE_AGENTS_ENABLED=false \
GROK_CLAUDE_MCPS_ENABLED=false \
GROK_CLAUDE_HOOKS_ENABLED=false \
GROK_CLAUDE_SESSIONS_ENABLED=false \
python3 -B lib/portable_stage.py run \
  --surface awr --provider grok --model grok-4.5 --reasoning high \
  --stage awr-judge --seat grok-json-fence-live-smoke \
  --serialized-prompt "$SMOKE_ROOT/prompt.json" \
  --input "draft.md=$SMOKE_ROOT/draft.md" \
  --input "priorwork.md=$SMOKE_ROOT/priorwork.md" \
  --input "task.md=$SMOKE_ROOT/task.md" \
  --input "rubric.md=$SMOKE_ROOT/rubric.md" \
  --input "brainstorming_policy.md=$SMOKE_ROOT/brainstorming_policy.md" \
  --output-root "$SMOKE_ROOT/output" \
  --state-root "$SMOKE_ROOT/state" \
  --timeout-seconds 300
```

Run no retry. Require a canonical completion receipt, a canonical imported
model envelope whose SHA matches `model_envelope_sha256`, and a valid nonempty
`output/judge.md`. Check OpenSpec task `8.4` only after these assertions pass.
Remove only the exact temporary directory after recording hashes and status.

- [ ] **Step 7: Run final offline regressions and commit documentation**

Run the 11 commands from Task 3 Step 4, `openspec validate
scalable-history-runtime --strict`, the prose scan, and `git diff --check`.
Commit the design, backend, OpenSpec spec, and task-state updates:

```bash
git add docs/superpowers/specs/2026-08-05-grok-portable-json-transport-design.md \
  docs/backends.md \
  openspec/changes/scalable-history-runtime/specs/provider-neutral-execution/spec.md \
  openspec/changes/scalable-history-runtime/tasks.md
git commit -m "docs: specify exact Grok JSON fence transport"
```

---

### Task 5: Isolate Grok's unique terminal response

**Files:**

- Modify: `lib/portable_agent.py`
- Modify: `tests/fake_portable_stage_provider.py`
- Modify: `tests/provider_portable_hardening_smoke.py`
- Modify: `docs/superpowers/specs/2026-08-05-grok-portable-json-transport-design.md`
- Modify: `docs/backends.md`
- Modify: `openspec/changes/scalable-history-runtime/specs/provider-neutral-execution/spec.md`

**Interfaces:**

- Consumes: the validated Grok outer `text` string.
- Produces: bare JSON bytes or the bytes inside one exact terminal lowercase-`json` fence.

- [ ] **Step 1: Write terminal-fence RED tests**

Add a success fixture with provider narration followed by one LF-delimited
`json` fence whose closing delimiter ends the `text`. Add rejection fixtures
for duplicate or non-line-start delimiters, CRLF, wrong label or case, missing
close, and every byte after the closing delimiter. Every rejection must leave
imports, projections, and completion absent.

- [ ] **Step 2: Implement the bounded extractor**

Keep the complete outer stdout under the existing 128 KiB cap. If no fence
delimiter line exists, pass the complete `text` to strict JSON parsing. If a
delimiter exists, require exactly one opener line `b"```json\n"`, exactly one
closer line `b"```"`, and require the closer's final byte to be the final byte
of `text`. The opener must begin at byte zero or immediately after LF. Discard
only the bytes before that opener. Do not trim, normalize, search for an
arbitrary JSON substring, or repair the extracted body.

- [ ] **Step 3: Run focused tests and review**

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
bash tests/portable_hunt_awr_e2e_smoke.sh
git diff --check
```

Require an independent code review before the live gate.

- [ ] **Step 4: Run one revised real Grok smoke**

Repeat the six compatibility-source disables and effective-configuration
inspection from Task 4. Run one bounded `grok-4.5`/`high` AwR judge request,
with no retry. Require canonical import, valid attestation, a completion
receipt, and nonempty projected `judge.md`. Record hashes and remove only the
exact temporary smoke directory after verification.

---

### Task 6: Bind agy to the portable stdout channel

**Files:**

- Modify: `lib/portable_stage.py`
- Modify: `tests/fake_portable_stage_provider.py`
- Modify: `tests/provider_portable_hardening_smoke.py`
- Modify: `tests/portable_hunt_awr_e2e_smoke.sh`
- Modify: `docs/backends.md`
- Modify: `openspec/changes/scalable-history-runtime/specs/provider-neutral-execution/spec.md`
- Modify: `openspec/changes/scalable-history-runtime/tasks.md`

**Interfaces:**

- Consumes: the portable request base before request-binding computation.
- Produces: binding-covered `transport_instructions` declaring the stdout-only
  response channel and read-only mirror contract.

- [ ] **Step 1: Write request-binding and agy RED tests**

Assert that the request contains closed portable transport instructions and
that changing them changes both the request binding and wire-request hash.
Exercise all three agy AwR stages with a fake provider that sees the legacy
role wording but obeys the portable stdout override. Add a fake agy mode that
writes a mirror file and exits zero; require `unexpected_artifact` with no
import, projection, or completion. Retain v1 file-output regression coverage.

- [ ] **Step 2: Add provider-neutral transport instructions**

Add one closed object to `_provider_request()` before computing its binding.
It declares that `role.md` controls artifact content, the request controls its
transport, the mirror is read-only, and stdout must contain exactly one
UTF-8/NFC canonical response-schema object with its request attestation. It
forbids fences, narration, extra bytes, and file creation or modification.
Do not add an unbound adapter prompt or parse any provider brain/artifact file.

- [ ] **Step 3: Run focused tests and review**

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
bash tests/portable_hunt_awr_e2e_smoke.sh
bash tests/runtime_abi_smoke.sh
git diff --check
```

Require an independent code review before the live gate.

- [ ] **Step 4: Run one real agy smoke**

Inspect current agy plugins, agents, model catalog, and effective explicit
Gemini route without invoking a model. Do not launch if any automatic path can
invoke Claude. Run one bounded `gemini-3.6-flash-high`/`high` AwR judge request
with no retry. Require canonical import, an unchanged mirror, a completion
receipt, and nonempty projected `judge.md`.

- [ ] **Step 5: Run full verification**

Run the eleven Task 3 regression commands, strict OpenSpec validation, prose
scan, shell syntax checks, and `git diff --check`. Obtain an independent
whole-branch review before handoff.
