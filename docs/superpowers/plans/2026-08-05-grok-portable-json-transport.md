# Grok Portable JSON Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make real Grok and agy portable stages cross their provider transports while retaining the strict model-response contract, and avoid retry sleep after the final bounded Hunt round.

**Architecture:** Grok emits one provider-owned outer JSON object and receives a binding-covered provider-specific instruction requiring its final assistant response to be one exact lowercase-`json` LF fence. `portable_agent` validates the outer transport, isolates that unique terminal fence after any reducer-concatenated assistant prefix, and strictly parses the inner model envelope. Every non-Grok provider retains the raw canonical-stdout path; for agy, the binding-covered instruction overrides the legacy role's file-output channel while leaving artifact semantics unchanged. Stdout attempts allow only bounded ignored provider scratch under `.tmp`. Both stdout and legacy file-output attempts quiesce the provider process group, validate the disposable mirror, and remove the attempt before durable import. Hunt decides whether a future round exists before applying failure cooldown.

**Tech Stack:** Python 3 standard library, Bash, `unittest`, fake CLI executables, OpenSpec.

## Global Constraints

- Never invoke Claude directly or indirectly, including through tests, hooks, providers, or fallbacks.
- Preserve the uncommitted `ledger.tsv` in the main checkout; implement in an isolated worktree.
- Grok SHALL use `--output-format json`; the registry SHALL record `grok-portable-v2` and a new byte-exact registry hash.
- Grok outer JSON SHALL reject invalid UTF-8, duplicate keys, non-object roots, missing/non-string `text`, and any `stopReason` other than `end_turn`.
- Grok outer usage and cost numbers, including finite floating-point values, SHALL be accepted as transport metadata and SHALL never enter the model envelope.
- Grok inner text SHALL reject duplicate keys, floating-point values, non-finite constants, invalid JSON, and non-NFC strings before schema and request-attestation validation.
- Only the host-canonicalized inner model envelope SHALL be hashed, imported, projected, and referenced by completion receipts.
- Grok's binding-covered stdout instruction SHALL require the final assistant response to be one exact lowercase-`json` LF fence, with the canonical object's single trailing LF immediately before the terminal close and no outside bytes or earlier triple-backtick sequence.
- Codex, Kimi, OpenCode, and agy SHALL retain the current exact raw canonical-stdout contract.
- Stdout attempts SHALL treat `.tmp` as ignored scratch only when descriptor-relative no-follow validation finds real directories, at most 32 regular single-link files, at most 64 total entries, and at most 1 MiB of stable-read bytes; scratch SHALL never be imported.
- Provider completion SHALL be followed by process-group termination and a wait for the provider process before validation. Attempt cleanup SHALL repair directory permissions and succeed before durable import; cleanup failure SHALL return `attempt_cleanup_failed` without publication.
- Legacy `forbid_extra_files` enumeration SHALL use descriptor-relative no-follow traversal and SHALL reject unreadable directories, traversal failures, links, special files, and raced directory replacement instead of skipping them.
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

- [x] **Step 1: Write the provider-command RED tests**

Change the literal Grok argv expectations in `tests/provider_adapters_smoke.py` and `tests/history_audit_cli_smoke.py` from:

```python
"--output-format", "plain"
```

to:

```python
"--output-format", "json"
```

Assert that Codex, Kimi, OpenCode, and agy expected argv remain byte-for-byte unchanged.

- [x] **Step 2: Run the provider-command tests and verify RED**

Run:

```bash
python3 tests/provider_adapters_smoke.py
python3 tests/history_audit_cli_smoke.py
```

Expected: both suites fail only because Grok still renders `plain`.

- [x] **Step 3: Implement the command grammar and registry revision**

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

- [x] **Step 4: Run the provider-command tests and verify GREEN**

Run:

```bash
python3 tests/provider_adapters_smoke.py
python3 tests/history_audit_cli_smoke.py
python3 tests/provider_model_catalog_authority_smoke.py
```

Expected: all tests pass.

- [x] **Step 5: Extend the fake provider with the real Grok transport shape**

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

- [x] **Step 6: Write the portable-stage RED tests**

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

- [x] **Step 7: Run the portable-stage tests and verify RED**

Run:

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
```

Expected: Grok outer JSON success fails as an invalid model envelope; existing
non-Grok behavior remains green.

- [x] **Step 8: Implement the two-layer decoder**

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

- [x] **Step 9: Run the portable-stage tests and verify GREEN**

Run:

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
bash tests/portable_hunt_awr_e2e_smoke.sh
```

Expected: all tests pass; the fake Hunt review seat exercises Grok outer JSON.

- [x] **Step 10: Commit Task 1**

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

- [x] **Step 1: Write the terminal-round RED test**

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

- [x] **Step 2: Run the e2e test and verify RED**

Run:

```bash
bash tests/portable_hunt_awr_e2e_smoke.sh
```

Expected: the new case fails because current `fail_round` invokes the fake
sleep after the final round.

- [x] **Step 3: Implement the retry-existence check**

Keep archive and failure accounting unchanged. After the `MAX_FAILS` check,
guard cooldown with:

```bash
if [ "$ROUND_LIMIT" -eq 0 ] || [ "$round" -lt "$ROUND_LIMIT" ]; then
  sleep_minutes "$FAIL_SLEEP_MIN"
fi
```

- [x] **Step 4: Run the e2e and shell syntax tests and verify GREEN**

Run:

```bash
bash -n hunt.sh tests/portable_hunt_awr_e2e_smoke.sh
bash tests/portable_hunt_awr_e2e_smoke.sh
```

Expected: all cases pass and the terminal-failure case records no sleep.

- [x] **Step 5: Commit Task 2**

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

- [x] **Step 1: Update the OpenSpec provider-response contract**

Add a requirement stating that an adapter may unwrap a provider-owned machine
transport before model-envelope validation. Add a Grok scenario requiring
native JSON mode, successful terminal `text` extraction, strict inner schema
and attestation validation, host canonicalization, and no publication for
malformed or incomplete transport. Add unchecked task `8.4` for the live Grok
transport repair and terminal-round no-sleep regression; Task 4 checks it only
after the real smoke succeeds.

- [x] **Step 2: Update backend documentation**

Document that Grok portable stages use `--output-format json`; the provider
wrapper is discarded after its final `text` is validated and canonicalized.
State that completion hashes refer to the canonical inner model envelope.
Document that a failed final bounded round exits without `FAIL_SLEEP_MIN`.

- [x] **Step 3: Check human-readable prose and OpenSpec**

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

- [x] **Step 4: Run focused and product regression suites**

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

- [x] **Step 5: Commit Task 3**

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
- Produces: `_grok_model_text_bytes(text: str) -> bytes`, accepting complete
  bare JSON or one unique terminal lowercase-`json` fence after an optional
  accumulated prefix.

- [x] **Step 1: Write the exact-fence RED tests**

Make the default fake Grok success response mirror the observed live output:

```python
text = "```json\n" + noncanonical_inner_json + "\n```"
```

Add separate bare-inner and reducer-concatenated-prefix success modes. The
prefix fixture places the exact opener immediately after a non-LF byte. Add
rejection modes for narration followed by bare JSON, another triple-backtick
sequence, trailing bytes, a wrong fence language or case, CR, and a missing
closing fence. Every rejection asserts no import, projection, or completion.

- [x] **Step 2: Run the portable tests and verify RED**

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
```

Expected: the exact fenced and reducer-concatenated-prefix successes are
rejected as `malformed_output`; the bare success and prior rejection cases
retain their existing behavior.

- [x] **Step 3: Implement narrow fence normalization**

After the outer `text` string is encoded safely, pass the complete bytes to
strict JSON parsing when no exact `b"```json\n"` opener exists. Otherwise
require exactly two triple-backtick sequences, exactly one opener, no CR byte,
LF before the terminal EOF closer, and no trailing byte. The opener may begin
at any byte because the reducer inserts no chunk separator. Strip only the
accumulated prefix and the two markers. Do not trim whitespace, search for a
JSON suffix, remove arbitrary suffixes, or repair malformed JSON. The extracted
bytes continue through duplicate-key, float, constant, NFC, closed-schema, and
exact-attestation validation before canonical import.

- [x] **Step 4: Run focused GREEN and commit the code**

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

- [x] **Step 5: Update the design, OpenSpec, and backend contract**

Record the exact final-response fence instruction and the reducer's
separator-free assistant-chunk concatenation. Permit complete bare JSON or one
unique terminal exact fence after an accumulated prefix; reject narrated bare
JSON and never search for a JSON suffix. Keep OpenSpec task `8.4` unchecked
until Step 6 succeeds.

- [x] **Step 6: Run one real Grok portable-stage smoke**

The first live attempt exposed reducer-concatenated prefix handling and did not
qualify. Task 5 records the later transport success and the current-revision
qualification state.

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

- [x] **Step 7: Run final offline regressions and commit documentation**

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

- [x] **Step 1: Write terminal-fence RED tests**

Add success fixtures for an exact whole-text fence and for an accumulated
assistant prefix immediately followed by the exact opener without an inserted
LF. Add rejection fixtures for narration followed by bare JSON, duplicate or
extra delimiters, any CR byte, wrong label or case, missing close, missing LF
before the close, and every byte after the closing delimiter. Every rejection
must leave imports, projections, and completion absent.

- [x] **Step 2: Implement the bounded extractor**

Keep the complete outer stdout under the existing 128 KiB cap. If no fence
opener exists, pass the complete `text` to strict JSON parsing, so narrated bare
JSON remains invalid. In fenced mode require exactly two triple-backtick
sequences, exactly one `b"```json\n"` opener, and no CR byte. Accept the opener
at any byte because the Grok CLI reducer concatenates assistant chunks without
a separator. Require LF immediately before the terminal `b"```"` closer and
require the closer's final byte to end `text`. Discard only the bytes before the
opener and the two markers. Do not trim, normalize, search for a JSON suffix, or
repair the extracted body.

- [x] **Step 3: Run focused tests and review**

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
bash tests/portable_hunt_awr_e2e_smoke.sh
git diff --check
```

Require an independent code review before the live gate.

- [ ] **Step 4: Run one revised real Grok smoke**

The transport smoke before namespace-race commit `e6c8586` completed with
canonical import, matching model-envelope hash, valid attestation, a completion
receipt, and a nonempty projected `judge.md`. Completion:
`ec8e2d309f4617279fd5814840114b4f8a67c08f0094c7adee7511643de25b6e`.
Model envelope:
`857ab8bc500f8a04d33389ec76c2fce88dd021364c458cede59e48b28a49d16f`.
Projected judge:
`7366478a991437b0589789a784f127f393dd6bde3130a434811b50fd86963d80`.
No full Hunt or AwR sidecar round ran. The current revision still requires one
bounded requalification.

Repeat the six compatibility-source disables and effective-configuration
inspection from Task 4. Run one bounded `grok-4.5`/`high` AwR judge request,
with no retry. Require canonical import, valid attestation, a completion
receipt, and nonempty projected `judge.md`. Record hashes and remove only the
exact temporary smoke directory after verification.

---

### Task 6: Bind provider-specific portable stdout channels

**Files:**

- Modify: `lib/portable_stage.py`
- Modify: `lib/portable_agent.py`
- Modify: `tests/fake_portable_agent.py`
- Modify: `tests/fake_portable_stage_provider.py`
- Modify: `tests/provider_portable_hardening_smoke.py`
- Modify: `tests/portable_hunt_awr_e2e_smoke.sh`
- Modify: `docs/backends.md`
- Modify: `openspec/changes/scalable-history-runtime/specs/provider-neutral-execution/spec.md`
- Modify: `openspec/changes/scalable-history-runtime/tasks.md`

**Interfaces:**

- Consumes: the portable request base before request-binding computation.
- Produces: binding-covered `transport_instructions` declaring the
  provider-specific stdout response channel, declared-file integrity, bounded
  scratch, process quiescence, and cleanup-before-import contract.

- [x] **Step 1: Write request-binding and agy RED tests**

Assert that the request contains closed portable transport instructions and
that changing them changes both the request binding and wire-request hash.
Require Grok's stdout member to bind the exact final-response fence and every
non-Grok member, including agy, to bind raw canonical stdout without a fence.
Exercise all three agy AwR stages with a fake provider that sees the legacy
role wording but obeys the portable stdout override. Add a fake agy mode that
writes a mirror file and exits zero; require `unexpected_artifact` with no
import, projection, or completion. Cover same-size role and declared-input
overwrites plus exact-mode drift under the same fail-closed outcome. Retain v1
file-output regression coverage.

- [x] **Step 2: Add provider-specific transport instructions**

Add one closed object to `_provider_request()` before computing its binding.
It declares that `role.md` controls artifact content, the request controls its
transport, and instructs the model not to create, modify, or delete mirror
files. Select the stdout member by provider before binding: Grok requires the
final assistant response to be one exact lowercase-`json` LF fence with one
trailing LF immediately before the terminal close and no outside bytes or
earlier delimiter; every non-Grok
provider requires one raw canonical UTF-8/NFC response-schema
object with no fence, narration, or extra bytes. Both forms require exact
request attestation and forbid model-authored file creation or modification.
After process-group quiescence, require the closed declared-file path set and
each entry's regular/single-link type, exact `st_mode`, stable byte count, and
SHA-256. Undeclared non-scratch files reject; empty directories are ignored.
The runtime-created `.tmp` is an ignored provider-scratch exception, not a
response channel. Do not describe post-exit checks as an OS read-only mount,
add an unbound adapter prompt, or parse any provider brain/artifact file.

- [x] **Step 3: Run focused tests and review**

```bash
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
bash tests/portable_hunt_awr_e2e_smoke.sh
bash tests/runtime_abi_smoke.sh
git diff --check
```

Require an independent code review before the live gate.

- [x] **Step 4: Bound ignored stdout scratch**

Validate `.tmp` through descriptor-relative no-follow traversal. Require the
root and nested directories to remain real directories and every file to be
regular and single-link. Permit at most 32 files, 64 total entries, and 1 MiB
of stable-read bytes. Reject missing or replaced roots, links, special files,
unreadable or unstable entries, traversal races, and exceeded limits. Never
import scratch bytes. Traverse the remaining mirror with the same no-follow
descriptor discipline, skip only the exact validated root `.tmp`, verify final
child and root namespace identity, and continue to ignore stable empty
directories.
Regression fixtures cover a mode-zero directory containing a hidden file,
declared-path symlink/hardlink/FIFO replacement, and child/root namespace swap.

- [x] **Step 5: Quiesce and remove attempts before import**

After provider communication completes, terminate its process group and wait
for the provider process before mirror or stdout validation. Before durable
import, recursively repair attempt-directory permissions, remove the attempt,
and verify its absence.
Return `attempt_cleanup_failed` without import, projection, or completion when
cleanup cannot finish.

- [x] **Step 6: Make legacy extra-file enumeration fail closed**

Replace path-recursive enumeration with descriptor-relative, no-follow
traversal. Require every non-directory entry to be regular and single-link,
and reject unreadable directories, traversal failures, links, special files,
or directory identity drift. Cover a hidden file under a mode-zero directory,
a symlink occupying an expected declared-input path, and a directory namespace
replacement that swaps the observed descriptor away from the mirror path.
This legacy check closes the observed path set and file type/link identity; it
does not add content or mode immutability for ordinary declared inputs.

- [ ] **Step 7: Run one real agy smoke**

Inspect current agy plugins, agents, model catalog, and effective explicit
Gemini route without invoking a model. Do not launch if any automatic path can
invoke Claude. Run one bounded `gemini-3.6-flash-high`/`high` AwR judge request
with no retry. Require canonical import, the expected declared regular-file set
and exact type/link/mode/byte-count/SHA snapshot, a completion receipt, and
nonempty projected `judge.md`.

The explicit `gemini-3.6-flash-high`/`high` transport smoke before commit
`e6c8586` completed after bounded runtime scratch validation, with no residual
attempt directory. Completion:
`ed8a293bc94c7f060e960e683edef36f89be75e556f9e22757b193c0486a8e5a`.
Model envelope:
`0e216d395b06328ad67c4fa6173e1152f2672a717e2457adfee3b2225b615284`.
Projected judge:
`81114067dedaf34af0503d23d24ed88e05f9634440f62619246865bd9982ffc2`.
The current revision still requires one bounded requalification.

- [ ] **Step 8: Run full verification**

Run the eleven Task 3 regression commands, strict OpenSpec validation, prose
scan, shell syntax checks, and `git diff --check`. Obtain an independent
whole-branch review before handoff.
