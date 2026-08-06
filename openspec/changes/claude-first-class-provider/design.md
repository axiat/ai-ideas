## Context

Portable v2 already isolates provider differences behind a registry, command intent, disposable mirror, binding-covered request, and host-validated stdout import. Agy is the closest precedent: `--output-format json`, inline `--json-schema`, outer provider JSON, import only from `structured_output`. Live probes of Claude Code `2.1.223` show the same shape:

- `claude -p --bare --output-format json --json-schema '<schema>' --model <id> --effort <level> '<prompt>'`
- Outer object contains `subtype`, `is_error`, optional string `result`, and object `structured_output` when a schema is supplied
- `--bare` skips hooks, plugin sync, CLAUDE.md auto-discovery, and keychain-side discovery noise
- `--tools ''` keeps the portable stage from mutating the mirror through tools
- `--add-dir <mirror>` scopes filesystem tool access when tools are later re-enabled by an operator override outside this grammar
- `--dangerously-skip-permissions` is required for unattended print in a disposable mirror

The current code intentionally fails closed on any registry/executable/model string containing `claude`/`anthropic`, and product-contract scans reject automatic shell invocation. Those controls must be replaced by ordinary provider registration plus the generic no-implicit-fallback rule already applied to every other backend.

## Goals / Non-Goals

**Goals:**

- Make `claude` a registry-backed Hunt and AwR provider with omitted-model default preservation and explicit effort overrides.
- Give Claude a strict structured-JSON transport with no text fallback.
- Expose operator configuration parity: `HUNT_PROVIDER=claude`, `AWR_PROVIDER=claude`, role overrides, and `./claude-worker.sh` for external stages.
- Delete unsupported-Claude documentation and tests; retain only security-relevant distinctions (no implicit shell default, no OpenCode/agy Anthropic alias route, Grok compatibility cells remain off).
- Keep offline tests on fake providers; optional one-shot live smoke after offline green.

**Non-Goals:**

- Making Claude the product default (Codex remains default).
- Allowing OpenCode or agy model strings such as `anthropic/claude-sonnet` as a substitute for the Claude provider.
- Changing ledger, review-vote, archive, or publication semantics.
- OS-level sandboxing beyond the existing disposable-mirror contract.
- Streaming (`stream-json`) or interactive Claude sessions inside portable stages.

## Decisions

### 1. Register `claude` on both Hunt and AwR surfaces

Hunt becomes `codex|kimi|grok|claude`. AwR becomes `codex|kimi|grok|opencode|agy|claude`. Claude is not multi-backend: omitted model preserves the CLI default; explicit `--model` is passed through unchanged; no catalog probe is required. Explicit reasoning/effort values are the CLI-verified set `low|medium|high|xhigh|max`. Omitted effort preserves the CLI default.

Rejected alternative: AwR-only Claude. Hunt generation/review is the primary operator path and already has three peer providers.

### 2. Mirror the Agy structured transport with Claude-specific acceptance

Command grammar `claude-portable-v1`:

```text
claude --bare --dangerously-skip-permissions --tools ''
  --output-format json --add-dir <mirror>
  [--model <model>] [--effort <reasoning>]
  --json-schema <canonical-schema-without-terminal-LF>
  -p <prompt>
```

Stdout acceptance:

1. Parse outer JSON with strict UTF-8 and duplicate-key rejection; provider metadata numbers may be floats.
2. Require `is_error is false` and `subtype == "success"`.
3. Require object-valued `structured_output`.
4. Canonicalize `structured_output` to UTF-8 JSON with trailing LF; parse as the model envelope; validate closed response schema and request attestation.
5. Ignore string `result`, usage, cost, session ids, and `stop_reason`. Claude may report `stop_reason=tool_use` even for a completed structured final; success is defined by `subtype`/`is_error`/`structured_output`, not by `stop_reason`.
6. No recovery from `result`, Markdown fences, mirror files, or Claude session state.

Claude does not echo the schema the way Agy does, so schema-echo equality is not required. The host already binds the schema into the request and re-validates the imported object.

Rejected alternative: raw canonical stdout like Codex. Claude's default print path wraps the model text in a provider envelope; forcing raw stdout would fight the CLI.

### 3. Remove blanket Claude denylist; keep three narrower controls

| Control | After this change |
|---|---|
| Registry entry / executable named `claude` | Allowed and required |
| OpenCode/agy model tokens `anthropic\|claude\|haiku\|opus\|sonnet` | Still forbidden; canonical path is provider `claude` |
| Dynamic model markers `auto\|default\|current\|configured` | Still forbidden for multi-backend routes |
| Grok `GROK_CLAUDE_*_ENABLED` | Still forced `false` |
| Shell implicit default `${CMD:-claude ...}` | Still rejected by product contract |
| Explicit `HUNT_PROVIDER=claude` / `./claude-worker.sh` | Allowed |

`_FORBIDDEN = "claude"` registry/executable scans are deleted. Product-contract registry verification includes `claude` in the closed provider set instead of scanning for the substring.

### 4. External worker parity

`claude-worker.sh` follows `grok-worker.sh` shape: one positional prompt, `CLAUDE_*` configuration, absolute work root, explicit model/effort optional, unattended permissions, and no subagent/tool defaults beyond what external stages already tolerate. Portable v2 internal stages never call the worker; they use `render_command` directly.

### 5. Documentation and verification boundary

Operator docs gain one verified spelling example:

```bash
HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=claude \
HUNT_MODEL=sonnet \
HUNT_REASONING_EFFORT=high \
./hunt.sh
```

Offline suites own correctness. One bounded live `awr-judge` smoke with an explicit catalog-free model (for example `haiku` or `sonnet`) and effort `high` may qualify the transport after offline green; failure is not retried without a diagnosed transport revision.

## Risks / Trade-offs

- **Permission bypass in portable mirrors** — Same class of risk as Agy's `--dangerously-skip-permissions`. Mitigated by disposable mirrors, empty tool set, host-side declared-file integrity, and no durable import before cleanup.
- **CLI envelope drift** — Claude's outer JSON is not a public frozen ABI. Mitigated by narrow field requirements (`is_error`, `subtype`, `structured_output`) and offline fixtures pinned to observed keys.
- **Alias model ids** — `sonnet` / `opus` / `haiku` are CLI aliases, not full ids. Accepted: identity records the requested string the operator passed; capacity remains shadow/unverified until a later calibration, matching other providers' unverified model strings.
- **Cost** — First-class support makes accidental Claude spend easier than the old denylist. Mitigated by Codex remaining default and no implicit shell fallback.
