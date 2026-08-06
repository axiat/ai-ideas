# Claude First-Class Provider

## Problem

Portable v2 treats Claude as a denylisted string rather than a provider. Claude Code already exposes the ingredients the runtime requires: non-interactive print, JSON outer envelope, inline JSON Schema, structured final object, effort override, and disposable directory access. Operators can only reach Claude through ad-hoc `AGENT_CMD`, which is outside Hunt/AwR provider identity, outside portable pools, and contradicted by docs that say Claude is unsupported.

## Transport

Grammar revision `claude-portable-v1` renders:

```text
claude --bare --dangerously-skip-permissions --tools ''
  --output-format json --add-dir <mirror>
  [--model <model>] [--effort <low|medium|high|xhigh|max>]
  --json-schema <canonical-schema-without-terminal-LF>
  -p <prompt>
```

Outer stdout acceptance:

1. Strict UTF-8 JSON object; provider metadata floats allowed.
2. `is_error === false` and `subtype === "success"`.
3. Object-valued `structured_output` only.
4. Canonicalize that object; validate response schema and request attestation.
5. Ignore `result`, usage, cost, session ids, and `stop_reason`.
6. No text fallback, fence recovery, mirror-file recovery, or session resume.

Claude does not echo the schema. Host binding and object validation remain authoritative. Binding-covered transport instructions tell the model to return exactly one structured object matching `response_schema` and not to write mirror files.

## Provider identity

- Registry id / executable: `claude`
- Surfaces: Hunt and AwR
- Omitted model: no `--model` flag (CLI default)
- Explicit model: passed through; no catalog probe
- Explicit effort: `low|medium|high|xhigh|max`
- Default provider remains Codex

## Policy rewrite

| Old rule | New rule |
|---|---|
| No registry/executable/model string may contain `claude` | Registered provider `claude` is required |
| Product contract forbids automatic Claude invocation | Product contract forbids *implicit shell fallback* for every provider; explicit `HUNT_PROVIDER=claude` and `./claude-worker.sh` are valid |
| OpenCode/agy Anthropic aliases forbidden | Unchanged; those aliases are not the Claude provider |
| Grok Claude compatibility cells forced off | Unchanged; they disable Grok skill discovery |

## External worker

`claude-worker.sh` supplies the external file-stage contract for selector/prescreen/research/report/panel/side paths that still use process-string commands. Portable internal stages call `render_command` directly and never shell out through the worker.

## Verification

Offline fake-provider suites own the grammar and transport. Product-contract checks own registry membership and no-implicit-fallback scanning. Independent test and audit agents must both pass before the change is complete. One optional live `awr-judge` smoke may pin an explicit Claude model after offline green.
