## MODIFIED Requirements

### Requirement: Supported providers are explicit by product surface
The runtime SHALL accept `codex`, `kimi`, `grok`, and `claude` for Hunt and SHALL additionally accept `opencode` and `agy` for AwR. It SHALL reject every unregistered provider before launching a process, and SHALL have no implicit provider fallback outside the declared ordered pool.

#### Scenario: Unsupported provider is rejected
- **WHEN** a Hunt run selects `opencode` or an unknown provider
- **THEN** preflight fails before any backend process starts

#### Scenario: AwR accepts its extended provider set
- **WHEN** an AwR role selects `opencode`, `agy`, or `claude`
- **THEN** the role receives the same mirror and artifact-validation contract as the other registered AwR providers

#### Scenario: Hunt accepts Claude
- **WHEN** a Hunt run selects `HUNT_PROVIDER=claude`
- **THEN** generation, history comparison, and review resolve the registered Claude adapter before any backend process starts

### Requirement: Defaults and explicit overrides are distinguishable
Omitted reasoning SHALL preserve the selected CLI's current configured default. Omitted models SHALL preserve the Codex, Kimi, Grok, and Claude defaults. Every OpenCode/agy model SHALL exactly match a bounded host-owned CLI model catalog. The effective model, catalog probe revision, and canonical catalog SHA SHALL enter execution identity and SHALL be re-probed before launch. An omitted OpenCode model SHALL additionally require a host-owned pure configuration probe that returns a backend-qualified non-dynamic model in the same catalog and outside the multi-backend Anthropic-alias denylist; the runtime SHALL re-probe it before launch and pass it as an explicit workload model. An omitted agy model SHALL fail closed because no trusted default-identity probe is registered. Claude SHALL NOT require a model catalog probe. A provider-default marker without effective model/reasoning or an equivalent context/token/usage-bound identity SHALL be diagnostic or shadow-only and SHALL NOT enter a hard-complete pool. If either override is supplied, the adapter SHALL use the provider's exact supported grammar and SHALL fail when the override is unsupported, ignored, absent from the catalog, or resolves to a different effective value.

#### Scenario: Omitted Claude model preserves the CLI default
- **WHEN** Hunt or AwR selects provider `claude` without `HUNT_MODEL` / `AWR_MODEL`
- **THEN** the rendered command contains no `--model` flag

#### Scenario: Explicit Claude effort uses the verified subset
- **WHEN** Claude is selected with reasoning `low`, `medium`, `high`, `xhigh`, or `max`
- **THEN** the rendered command contains `--effort` and that value
- **WHEN** Claude is selected with any other explicit reasoning string
- **THEN** resolution fails before executable lookup completes

### Requirement: Portable command grammars are provider-owned and closed
Each registered provider SHALL expose one tracked grammar revision that renders argv and a closed environment delta from resolver-issued capability or command-intent values. Claude's grammar SHALL use bare non-interactive print, JSON output format, inline JSON Schema, disposable `--add-dir` mirror, empty tool set, and unattended permission bypass. Claude and Agy SHALL require the host-supplied response-schema object and SHALL pass its canonical UTF-8 JSON without the terminal LF as `--json-schema`. Codex, Kimi, Grok, and OpenCode SHALL retain their existing grammars. Grok SHALL continue to force all six `GROK_CLAUDE_*_ENABLED` compatibility cells to `false`.

#### Scenario: Claude portable argv is closed
- **WHEN** a Claude command intent is rendered for mirror `M` and prompt `P` with response schema `S`
- **THEN** argv contains `--bare`, `--dangerously-skip-permissions`, `--tools`, empty tools value, `--output-format`, `json`, `--add-dir`, `M`, `--json-schema`, the compact schema text, `-p`, and `P`
- **AND THEN** argv contains no fallback-model flag and no settings path outside the disposable mirror contract

### Requirement: Structured JSON providers import only structured_output
Agy and Claude SHALL decode a provider-owned outer JSON object and SHALL import only an object-valued `structured_output` member after host validation. Agy SHALL additionally require `status=SUCCESS` and a canonical `json_schema` byte match to the frozen request schema. Claude SHALL require `is_error=false` and `subtype=success`, and SHALL NOT require a schema echo. Neither provider SHALL recover an artifact from plain `result`/`response` text, Markdown fences, mirror files, session state, or retry narration. Codex, Kimi, and OpenCode SHALL retain raw canonical stdout. Grok SHALL retain provider JSON with a unique terminal lowercase-`json` fence or complete bare text under the existing Grok rules.

#### Scenario: Claude success imports structured_output only
- **WHEN** Claude outer stdout is a success object whose `structured_output` matches the response schema and request attestation
- **THEN** the host canonicalizes that object, imports it, and ignores `result`, usage, and session metadata

#### Scenario: Claude text-only success is rejected
- **WHEN** Claude outer stdout has `is_error=false` and a string `result` but no object `structured_output`
- **THEN** the attempt fails as malformed output and creates no import, projection, or completion receipt

### Requirement: No provider is an implicit shell default or undeclared fallback
The registered provider set, defaults, failover pools, test fixtures, and compatibility adapters SHALL launch a provider only through explicit surface selection or a declared ordered pool. Shell scripts SHALL NOT embed Claude, Codex, Kimi, Grok, OpenCode, or Agy as an unselected `${VAR:-provider ...}` fallback. Explicit OpenCode and agy model routes SHALL continue to reject Anthropic-family aliases (`anthropic`, `claude`, `haiku`, `opus`, `sonnet`) and dynamic `auto|default|current|configured` markers before executable lookup, even when a local catalog lists them. The canonical Claude execution path is the registered provider id `claude`. The v1 registry SHALL match the tracked byte ABI exactly after the Claude entry and surface membership are added.

#### Scenario: No undeclared pool member appears
- **WHEN** provider configuration and default pools are validated
- **THEN** every resolvable provider id is present in the tracked registry and the selected surface list

#### Scenario: OpenCode Anthropic alias remains rejected
- **WHEN** AwR selects `opencode` with model `anthropic/claude-sonnet`
- **THEN** resolution fails before executable lookup

#### Scenario: Grok compatibility discovery remains disabled
- **WHEN** a portable Grok stage or the external Grok worker is launched while host compatibility variables are unset or true
- **THEN** the child environment contains `false` for all six Claude compatibility cells before Grok starts
- **AND THEN** a portable preflight records the same closed environment

#### Scenario: Explicit Claude selection is permitted
- **WHEN** an operator sets `HUNT_PROVIDER=claude` or `AWR_PROVIDER=claude`, or invokes `./claude-worker.sh` as an explicit external command
- **THEN** product-contract checks accept the configuration as an explicit selection rather than an implicit fallback
