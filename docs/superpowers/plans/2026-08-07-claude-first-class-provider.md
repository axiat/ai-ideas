# Claude First-Class Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register Claude Code as a first-class Hunt and AwR portable provider with structured JSON transport, delete unsupported-Claude operator claims, and prove the path with offline suites plus independent test and audit agents.

**Architecture:** Add `claude` to the tracked provider registry and surface lists. Render a closed bare-print JSON+schema command. Decode Claude's provider-owned outer envelope and import only `structured_output`, following the Agy structured-transport pattern without schema echo. Replace blanket Claude denylists with ordinary provider registration plus generic no-implicit-fallback scanning. Keep OpenCode/agy Anthropic-alias denial and Grok compatibility cells.

**Tech Stack:** Python 3 standard library, Bash, `unittest`, fake CLI executables, OpenSpec, Claude Code CLI `2.1.223+`.

## Global Constraints

- Codex remains the default provider. Claude is explicit selection only.
- Automated implementation tests use fake providers only. Real Claude may run only in one bounded post-offline smoke.
- Preserve the uncommitted or local `ledger.tsv`; prefer an isolated worktree for implementation commits.
- Registry bytes remain a tracked ABI: every registry edit updates `registry_revision` and `_PROVIDER_REGISTRY_V1_SHA256` together.
- Claude portable stages SHALL NOT fall back to string `result`, Markdown fences, mirror artifacts, or session resume.
- OpenCode/agy model routes containing `anthropic|claude|haiku|opus|sonnet` or dynamic markers remain rejected.
- Grok portable commands and `grok-worker.sh` continue forcing all six `GROK_CLAUDE_*_ENABLED` cells to `false`.
- Product-contract scanners MUST keep rejecting `${VAR:-claude ...}` style implicit shell defaults while accepting explicit provider selection and `claude-worker.sh`.
- Do not change ledger schema, vote aggregation, archive layout, or publication gates.

---

### Task 1: Registry and closed provider set

**Files:**

- Modify: `history/provider-adapters-v1.json`
- Modify: `lib/provider_adapters.py`
- Modify: `tests/provider_adapters_smoke.py`
- Modify: `tests/verify_product_contract.py`

- [ ] **Step 1: Write RED surface-acceptance tests**

Update Hunt acceptance to require `codex|kimi|grok|claude` and AwR acceptance to require those plus `opencode|agy`. Assert unknown providers still fail. Delete or invert any test that requires the registry JSON to lack the substring `claude`.

- [ ] **Step 2: Run the focused smoke and verify RED**

```bash
python3 tests/provider_adapters_smoke.py
python3 tests/verify_product_contract.py
```

Expected: failures only from missing Claude registration / outdated expected provider sets.

- [ ] **Step 3: Implement registry and loader membership**

In `history/provider-adapters-v1.json`:

```json
"registry_revision": "2026-08-07",
"providers": {
  "...existing...",
  "claude": {
    "executable": "claude",
    "grammar_revision": "claude-portable-v1",
    "reasoning_values": ["low", "medium", "high", "xhigh", "max"]
  }
},
"surfaces": {
  "hunt": ["codex", "kimi", "grok", "claude"],
  "awr": ["codex", "kimi", "grok", "opencode", "agy", "claude"]
}
```

In `lib/provider_adapters.py`, set:

```python
_PROVIDERS = ("codex", "kimi", "grok", "opencode", "agy", "claude")
_SURFACES = {
    "hunt": ("codex", "kimi", "grok", "claude"),
    "awr": _PROVIDERS,
}
```

Remove `_FORBIDDEN` and the registry/executable scans that reject any string containing `claude`. Keep `_FORBIDDEN_MODEL_ROUTE_TOKENS` for multi-backend routes only.

Recompute:

```bash
shasum -a 256 history/provider-adapters-v1.json
```

and replace `_PROVIDER_REGISTRY_V1_SHA256`.

Update `verify_product_contract.py` expected provider lists and delete `provider_registry_forbidden_paths` Claude-substring rejection against the real registry. Retain a generic implicit-shell-fallback scanner.

- [ ] **Step 4: Verify GREEN for registry membership**

```bash
python3 tests/provider_adapters_smoke.py
python3 tests/verify_product_contract.py
```

---

### Task 2: Claude command grammar

**Files:**

- Modify: `lib/provider_adapters.py` (`_render_command_fields`, `render_command`)
- Modify: `tests/provider_adapters_smoke.py`
- Modify: `tests/provider_portable_hardening_smoke.py`
- Modify: `tests/history_audit_cli_smoke.py` (if it freezes argv tables)

- [ ] **Step 1: RED argv expectations**

Assert Claude render produces, in order of concern rather than exact index fragility:

```python
["--bare", "--dangerously-skip-permissions", "--tools", "",
 "--output-format", "json", "--add-dir", str(mirror),
 "--json-schema", schema_text, "-p", prompt]
```

With model/effort:

```python
["--model", "sonnet", "--effort", "high"]
```

Assert unsupported effort fails before render. Assert Claude requires `response_schema` like Agy.

- [ ] **Step 2: Implement render branch**

```python
elif provider == "claude":
    if type(response_schema_argument) is not str:
        raise ProviderResolutionError("Claude requires an inline response schema")
    argv += [
        "--bare", "--dangerously-skip-permissions", "--tools", "",
        "--output-format", "json", "--add-dir", mirror,
    ]
    if model is not None:
        argv += ["--model", model]
    if reasoning is not None:
        argv += ["--effort", reasoning]
    argv += ["--json-schema", response_schema_argument, "-p", prompt]
```

Extend `render_command` so Claude shares Agy's schema canonicalization path (`raw_schema[:-1]`).

- [ ] **Step 3: GREEN command tests**

```bash
python3 tests/provider_adapters_smoke.py
python3 tests/provider_portable_hardening_smoke.py
```

---

### Task 3: Claude stdout transport and stage instructions

**Files:**

- Modify: `lib/portable_agent.py`
- Modify: `lib/portable_stage.py`
- Modify: `tests/fake_portable_stage_provider.py`
- Modify: `tests/portable_stage_runtime_smoke.py`
- Modify: `tests/provider_portable_hardening_smoke.py`

- [ ] **Step 1: RED transport fixtures**

Fake Claude success envelope:

```python
{
  "type": "result",
  "subtype": "success",
  "is_error": False,
  "result": "{\"ignored\":true}",
  "structured_output": <inner-object>,
  "stop_reason": "end_turn",
  "session_id": "fixture",
  "total_cost_usd": 0.001,
  "usage": {"input_tokens": 1, "output_tokens": 1},
}
```

Add negative modes: `is_error=true`, missing `structured_output`, non-object `structured_output`, `subtype!="success"`, text-only `result`.

- [ ] **Step 2: Implement parser**

```python
def _parse_claude_transport(raw, response_schema):
    outer = _parse_strict_json(raw, reject_floats=False, require_nfc=False)
    if type(outer) is not dict:
        raise PortableAgentError("malformed_output")
    if outer.get("is_error") is not False or outer.get("subtype") != "success":
        raise PortableAgentError("malformed_output")
    if type(outer.get("structured_output")) is not dict:
        raise PortableAgentError("malformed_output")
    model_raw = _canonical_json_bytes(outer["structured_output"])
    value = _parse_strict_model_json(model_raw)
    return value, model_raw
```

Wire through `_parse_provider_stdout`. Do not compare schema echo.

- [ ] **Step 3: Transport instructions**

In `_build_request` / transport instruction selection, add:

```python
elif provider == "claude":
    stdout_instruction = (
        "Return exactly one JSON object matching response_schema as the "
        "structured final result. The Claude CLI owns the outer stdout JSON; "
        "only subtype=success structured_output is eligible for import. "
        "Do not put Markdown fences or narration inside the structured value."
    )
```

Ensure Claude requests include `response_schema` in command rendering the same way Agy does.

- [ ] **Step 4: GREEN portable runtime**

```bash
python3 tests/portable_stage_runtime_smoke.py
python3 tests/provider_portable_hardening_smoke.py
```

---

### Task 4: External worker and shell/docs contract

**Files:**

- Create: `claude-worker.sh`
- Modify: `README.md`, `docs/backends.md`, `docs/getting-started.md`, `CONTRIBUTING.md`
- Modify: `tests/verify_product_contract.py`
- Modify: `tests/runtime_policy_smoke.py` as needed
- Modify: `agy-worker.sh` header example if it still shows Claude only as a contrast default

- [ ] **Step 1: Implement `claude-worker.sh`**

Shape after `grok-worker.sh`:

- one positional prompt
- `CLAUDE_REPO` absolute work root
- optional `CLAUDE_MODEL`, `CLAUDE_REASONING_EFFORT` (`low|medium|high|xhigh|max`)
- `CLAUDE_BIN` default `claude`
- launch: bare, dangerously-skip-permissions, tools empty or stage-appropriate read/write denies if external stages need tools; default empty tools for parity with portable grammar unless external file stages require Write under `tmp/round`
- For external Hunt file stages that must write artifacts, allow only the stage output tree under the work root and keep ledger/program paths denied, matching grok-worker deny discipline

If external stages cannot function with empty tools, document the narrower tool allowlist in the worker header and keep portable internal stages on empty tools.

- [ ] **Step 2: Docs**

Document:

```bash
HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=claude \
HUNT_MODEL=sonnet \
HUNT_REASONING_EFFORT=high \
./hunt.sh
```

and external:

```bash
AGENT_CMD='./claude-worker.sh' \
CLAUDE_MODEL=sonnet \
CLAUDE_REASONING_EFFORT=high \
./hunt.sh
```

Delete operator sentences that say Claude is unsupported, never registered, or only opt-in-by-denylist. Historical plan ledgers under `docs/superpowers/plans/` that record past constraints may remain as history; active operator docs must not.

- [ ] **Step 3: Product contract**

- expected providers include `claude`
- no forbidden-substring scan on the real registry
- implicit fallback scanner still flags `${BACKEND:-claude -p}` and bare unselected `claude -p` in orchestration scripts
- explicit examples in comments/docs are fine

- [ ] **Step 4: GREEN contract/docs gate**

```bash
python3 tests/verify_product_contract.py
python3 tests/runtime_policy_smoke.py
bash -n claude-worker.sh hunt.sh awr-side.sh
openspec validate claude-first-class-provider --strict
```

---

### Task 5: Full offline regression

- [ ] **Step 1: Run provider/portable suites**

```bash
python3 tests/provider_adapters_smoke.py
python3 tests/provider_model_catalog_authority_smoke.py
python3 tests/provider_host_capability_evidence_smoke.py
python3 tests/provider_portable_hardening_smoke.py
python3 tests/portable_stage_runtime_smoke.py
python3 tests/verify_product_contract.py
python3 tests/runtime_policy_smoke.py
bash tests/portable_runtime_abi_smoke.sh
bash tests/portable_hunt_awr_e2e_smoke.sh
```

- [ ] **Step 2: Repair every failure with a new RED/GREEN cycle; do not weaken assertions to pass**

---

### Task 6: Independent test agent

- [ ] **Step 1: Spawn an independent test agent** with read/exec access and no implementation bias. Require it to:

  - enumerate provider registration, Hunt/AwR surface membership, argv grammar, stdout transport positives/negatives, product-contract no-implicit-fallback, docs examples
  - run the offline suites above
  - attempt a no-network fake-only proof that Claude is selectable end-to-end through portable stage runtime
  - return blocking failures with exact file/line and command output

- [ ] **Step 2: Repair every blocking failure and rerun Task 5 + independent tests until PASS**

---

### Task 7: Independent code audit agent

- [ ] **Step 1: Spawn an independent auditor** over the full diff. Require checks for:

  - no text/`result` fallback on Claude transport
  - schema required at render and launch
  - OpenCode/agy Anthropic aliases still denied
  - Grok compatibility cells still forced false
  - no implicit shell default to Claude or any provider
  - registry SHA and revision coherence
  - docs/registry/test expected sets agree
  - mirror isolation and empty-tool portable grammar retained
  - worker cannot write ledger/program paths

- [ ] **Step 2: Repair every blocking finding; rerun independent test + audit until both PASS**

---

### Task 8: Optional live smoke

- [ ] **Step 1: Only after Tasks 6–7 PASS**, consider one bounded:

```bash
HISTORY_RUNTIME_ABI=v2 \
AWR_PROVIDER=claude \
AWR_MODEL=haiku \
AWR_REASONING_EFFORT=high \
SIDE_POLL_SEC=0 \
# single awr-judge portable invocation through the existing smoke harness
```

Do not retry a failed live smoke without a diagnosed transport bug and a new offline revision. Record completion/import hashes or an explicit skip reason in `.superpowers/sdd/2026-08-07-claude-first-class-provider/progress.md`.

---

## Done when

- `HUNT_PROVIDER=claude` and `AWR_PROVIDER=claude` resolve through the tracked registry
- Claude portable stages import only validated `structured_output`
- Operator docs describe Claude as supported and no longer claim it is unsupported
- Offline suites, product contract, independent test agent, and independent audit agent all PASS
- OpenSpec change `claude-first-class-provider` validates strictly
