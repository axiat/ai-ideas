## Why

Claude Code is a local agent CLI with the same portable ingredients the runtime already requires of Agy: non-interactive print, JSON outer envelope, inline JSON Schema, structured final object, effort override, and disposable working directory. The current provider layer still treats Claude as a denylisted path. Explicit `AGENT_CMD` opt-in is not a portable provider identity, cannot enter Hunt/AwR v2 pools, and leaves operator docs stating that Claude is unsupported. Claude must become a registered first-class provider with the same host-owned grammar, transport, and surface eligibility contract as Codex, Kimi, Grok, OpenCode, and Agy.

## What Changes

- Register provider id `claude` in the tracked `provider-adapters-v1` registry with grammar `claude-portable-v1`.
- Extend Hunt surface eligibility to `codex|kimi|grok|claude` and AwR surface eligibility to `codex|kimi|grok|opencode|agy|claude`.
- Render a closed Claude portable command: bare non-interactive print, JSON output, inline response schema, optional model/effort, disposable `--add-dir` mirror, empty tool set, and permission bypass for unattended execution.
- Parse Claude's provider-owned outer JSON and import only a successful `structured_output` object under the existing portable response contract. No text/`result` fallback.
- Add `claude-worker.sh` for external Hunt stages that still use the `AGENT_CMD` / `FRONT_CMD` / `BACK_CMD` process interface.
- Rewrite product-contract and adapter tests that previously encoded "no Claude path" so they assert first-class registration, closed grammar, structured transport, and the continued absence of *implicit* shell defaults/fallbacks.
- Update operator docs (`README.md`, `docs/backends.md`, `docs/getting-started.md`, `CONTRIBUTING.md`) to document Claude as a supported provider and delete unsupported-Claude claims.
- Keep OpenCode/agy multi-backend model-route denylist for indirect Anthropic aliases; the canonical Claude path is the registered `claude` provider, not an OpenCode/agy backend string.
- Keep Grok's six `GROK_CLAUDE_*_ENABLED=false` compatibility cells. Those cells disable Grok's Claude-skill discovery, not the Claude provider.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `provider-neutral-execution`: Add Claude as a registered Hunt and AwR provider; replace the "Claude is never an automatic execution path" requirement with first-class Claude registration, structured transport, and no-implicit-fallback rules that apply equally to every provider.

## Impact

- `history/provider-adapters-v1.json` gains a `claude` entry and surface membership; registry revision and byte-exact SHA change.
- `lib/provider_adapters.py`, `lib/portable_agent.py`, and `lib/portable_stage.py` gain Claude grammar/transport branches.
- `hunt.sh` / `awr-side.sh` accept `HUNT_PROVIDER=claude` and `AWR_PROVIDER=claude` through existing provider resolution.
- New `claude-worker.sh` for external stages; product-contract scanner must allow the worker while still rejecting shell-level implicit defaults.
- Offline tests and fake providers gain Claude fixtures. One optional live AwR-judge smoke may pin an explicit Claude model after offline green.
- OpenSpec main spec `provider-neutral-execution` is updated by archive of this change.
