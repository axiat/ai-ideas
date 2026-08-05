## Purpose

Provide one observable execution contract for local agent CLIs without binding canonical research or history identity to a provider, model, CLI version, or safety wrapper.

## ADDED Requirements

### Requirement: Supported providers are explicit by product surface
The runtime SHALL accept `codex`, `kimi`, and `grok` for Hunt and SHALL additionally accept `opencode` and `agy` for AwR. It SHALL reject every unregistered provider before launching a process, and SHALL have no implicit provider fallback outside the declared ordered pool.

#### Scenario: Unsupported provider is rejected
- **WHEN** a Hunt run selects `opencode` or an unknown provider
- **THEN** preflight fails before any backend process starts

#### Scenario: AwR accepts its extended provider set
- **WHEN** an AwR role selects `opencode` or `agy`
- **THEN** the role receives the same mirror and artifact-validation contract as the other registered AwR providers

### Requirement: Defaults and explicit overrides are distinguishable
Omitted reasoning SHALL preserve the selected CLI's current configured default. Omitted models SHALL preserve the Codex, Kimi, and Grok defaults. Every OpenCode/agy model SHALL exactly match a bounded host-owned CLI model catalog. The effective model, catalog probe revision, and canonical catalog SHA SHALL enter execution identity and SHALL be re-probed before launch. An omitted OpenCode model SHALL additionally require a host-owned pure configuration probe that returns a backend-qualified non-Claude, non-dynamic model in the same catalog; the runtime SHALL re-probe it before launch and pass it as an explicit workload model. An omitted agy model SHALL fail closed because no trusted default-identity probe is registered. A provider-default marker without effective model/reasoning or an equivalent context/token/usage-bound identity SHALL be diagnostic or shadow-only and SHALL NOT enter a hard-complete pool. If either override is supplied, the adapter SHALL use the provider's exact supported grammar and SHALL fail when the override is unsupported, ignored, absent from the catalog, or resolves to a different effective value.

#### Scenario: Provider default is frozen
- **WHEN** a run selects a provider without model or reasoning overrides
- **THEN** its manifest records the effective model/reasoning identity or a diagnostic-only provider-default marker, plus the capability evidence and revision that produced it

#### Scenario: OpenCode default is pinned and revalidated
- **WHEN** OpenCode is selected without a model and the pure host probe returns a safe backend-qualified model
- **THEN** the effective model enters execution identity, the launch-time probe must match, and workload argv contains an explicit model override

#### Scenario: Agy default is unavailable
- **WHEN** agy is selected without an explicit model
- **THEN** preflight fails before workload execution

#### Scenario: Multi-backend catalog authority drifts
- **WHEN** an OpenCode or agy model is absent from the bounded catalog, uses a dynamic route marker, or the launch-time catalog identity differs from preflight
- **THEN** preflight or launch revalidation fails before workload execution

#### Scenario: Unsupported reasoning fails closed
- **WHEN** an explicit reasoning value is not supported by the selected provider adapter
- **THEN** preflight fails without silently dropping the setting

The adapter's explicit reasoning grammar SHALL be the conservative verified subset recorded in the registry. Omitted reasoning SHALL continue to use the CLI default. Kimi SHALL accept no explicit reasoning value.

### Requirement: Provider execution uses a portable mirror
Every portable provider attempt SHALL run in a disposable mirror containing only declared role and input artifacts. The durable database, full ledger, Git metadata, unrelated candidate state, and destination path SHALL be absent; only schema-valid declared outputs and host-owned attempt metadata SHALL return. Provider processes SHALL run in a dedicated process group. After provider communication ends, the host SHALL terminate that process group and wait for the provider process before validating the mirror or response. The host SHALL repair attempt-directory permissions, remove the attempt tree, and verify its absence before creating or reusing any durable import. Cleanup failure SHALL return `attempt_cleanup_failed` without an import, projection, or completion receipt.

#### Scenario: Undeclared durable input is unavailable
- **WHEN** an agent attempts to read the canonical database or full ledger from the portable mirror
- **THEN** the path is absent and no durable artifact is copied into the mirror

#### Scenario: Extra output is discarded
- **WHEN** an agent writes declared output plus additional files
- **THEN** only the declared output is considered and the attempt fails if the output contract forbids extras

#### Scenario: Provider descendants are quiesced before validation
- **WHEN** a provider command returns while a descendant in its process group remains alive
- **THEN** the host terminates the process group and waits for the provider process before mirror or response validation, so descendants in that group cannot continue mutating the attempt during validation

#### Scenario: Attempt cleanup gates durable import
- **WHEN** an otherwise valid attempt cannot be removed after permission repair
- **THEN** the stage fails with `attempt_cleanup_failed` and creates no durable import, projection, or completion receipt

#### Scenario: Legacy extra-file enumeration fails closed
- **WHEN** a legacy file-output attempt sets `forbid_extra_files=true`
- **THEN** the host enumerates the complete mirror through directory descriptors without following links, accepts only regular single-link non-directory entries, and rejects unreadable directories, traversal failures, symlinks, hardlinks, special files, or raced child/root namespace identity changes instead of omitting them from the observed path set
- **AND THEN** this check closes the observed path set and file type/link identity without adding content or mode immutability for ordinary declared inputs

### Requirement: Provider responses attest to the host request
Every portable request SHALL carry a host-computed base-request binding over its stage, seat, serialized prompt, role SHA, declared-input names and SHAs, and response schema, plus a separate serialized-prompt SHA. The response SHALL echo both values exactly in its closed envelope. The runtime SHALL record the full wire-request SHA separately and SHALL NOT derive provider attestation from host state after the response.

An adapter MAY unwrap provider-owned machine transport before model-envelope validation. The extracted model envelope SHALL still satisfy the strict JSON, closed-schema, request-attestation, and host-canonicalization requirements before import or publication.

The complete Grok outer stdout SHALL remain under the 128 KiB model-output limit. Grok terminal text MAY contain complete bare JSON or an accumulated assistant-chunk prefix followed by one unique terminal Markdown fence. Because the Grok CLI reducer concatenates assistant chunks without inserting a separator, the exact opener bytes `b"```json\n"` MAY begin at any byte and SHALL NOT require line-start placement. Fenced text SHALL contain exactly two triple-backtick sequences, exactly one opener, and no CR byte; LF SHALL immediately precede the terminal closing delimiter, whose final byte SHALL end the text. The adapter SHALL discard only the accumulated prefix and those two markers. Narration followed by bare JSON SHALL fail strict parsing. The adapter SHALL NOT trim, normalize, search for a JSON suffix, or repair the response.

#### Scenario: Missing or mismatched request attestation is rejected
- **WHEN** a provider response omits either attestation value or returns a different request or prompt SHA
- **THEN** the runtime publishes no projected artifact and no completion receipt

#### Scenario: Grok native JSON transport is unwrapped safely
- **WHEN** a portable Grok stage requests native JSON mode and receives a provider transport with terminal `stopReason=end_turn` and `text`
- **THEN** the adapter validates and extracts terminal `text`, accepts complete bare JSON or one unique terminal lowercase-`json` fence after an optional accumulated prefix even when the exact opener has no preceding LF, strictly validates the inner closed model envelope and attestations, canonicalizes that inner envelope on the host, and records its canonical import and completion hash
- **AND WHEN** the transport is malformed, incomplete, non-terminal, lacks valid text, contains narration followed by bare JSON, or fenced text contains another triple-backtick sequence, more than one exact opener, any CR byte, a different label or case, no LF before the terminal close, a missing close, or any trailing byte
- **THEN** the runtime imports and publishes no artifact and writes no completion receipt

### Requirement: Portable requests bind stdout transport precedence
Every portable request SHALL include closed `transport_instructions` in its base request binding. Those instructions SHALL make the request authoritative for transport while preserving `role.md` as artifact-content instructions only and SHALL instruct the model not to create, modify, or delete mirror files. Before computing the binding, the host SHALL select the stdout member by provider. For Grok, it SHALL require the final assistant response itself to be exactly one UTF-8/NFC canonical response-schema object inside one exact lowercase-`json` LF fence, with one trailing LF immediately before the terminal close, no bytes outside the fence in that final response, and no triple-backtick sequence in an earlier assistant response. For every non-Grok provider, including agy, it SHALL require the raw canonical object followed by one LF, without narration, a fence, or extra bytes. Both forms SHALL require exact request attestation. After process-group quiescence, the host SHALL independently verify the closed declared-file path set through descriptor-relative no-follow traversal, SHALL retain parent descriptors through child recursion, SHALL revalidate final child and root namespace identity, and SHALL require every declared entry to remain a regular single-link file with its exact original `st_mode`, stable byte count, and SHA-256. Undeclared non-scratch files SHALL reject; stable empty directories SHALL NOT count as outputs. For stdout portable attempts, the runtime-created `.tmp` MAY contain ignored provider scratch only while `.tmp` and every nested directory remain real directories, descriptor-relative traversal follows no links, every file is regular and single-link, and the complete tree contains at most 32 files, 64 entries, and 1,048,576 stable-read file bytes. Scratch SHALL never be imported. The main mirror walk SHALL skip only the exact validated root `.tmp` snapshot and SHALL require it to be observed and unchanged after traversal. A missing or replaced `.tmp`, unreadable or unstable entry, symlink, hardlink, special file, traversal failure, namespace race, or exceeded limit SHALL reject the attempt before durable state. The host SHALL validate canonical stdout, the closed schema, and attestation, then successfully remove the attempt before import, and SHALL NOT recover output from provider brain state or a role-named artifact file. This fail-closed validation SHALL NOT be represented as an OS-enforced read-only mount.

#### Scenario: Stdout declared-file enumeration fails closed
- **WHEN** a stdout portable mirror contains a file hidden under an unreadable directory, a link or special file, or a child/root namespace replacement races descriptor traversal
- **THEN** the host rejects the attempt before import instead of omitting the entry or validating one namespace and reading another
- **AND WHEN** the mirror contains only stable empty directories in addition to its declared files and bounded `.tmp` scratch
- **THEN** those empty directories do not count as outputs

#### Scenario: Grok receives a binding-covered final-response fence
- **WHEN** the host prepares a portable Grok request
- **THEN** its binding-covered stdout instruction requires the final assistant response to contain exactly one canonical response-schema object inside the exact lowercase-`json` LF fence, without outside bytes in that final response or an earlier triple-backtick sequence

#### Scenario: Non-Grok providers retain raw canonical stdout
- **WHEN** the host prepares a portable request for any non-Grok provider, including agy
- **THEN** its binding-covered stdout instruction requires raw canonical JSON with one trailing LF and forbids fences

#### Scenario: Stdout provider scratch is bounded and ignored
- **WHEN** a stdout portable provider writes runtime cache data under `.tmp`
- **THEN** the attempt may proceed only when the scratch tree satisfies the no-follow type, link, entry-count, file-count, byte-count, and stable-read constraints, and no scratch byte enters an import, projection, completion, or declared-file identity
- **AND WHEN** any scratch constraint fails
- **THEN** the runtime rejects the attempt before durable state

#### Scenario: Portable agy overrides legacy file-output wording
- **WHEN** a portable agy AwR stage receives a legacy role that names an output file
- **THEN** the binding-covered transport instructions override only that output location and file-writing channel while the role continues to define artifact content
- **AND WHEN** agy adds or removes a declared mirror file, changes a declared entry's type, link count, exact mode, stable byte count, or SHA-256, violates a `.tmp` scratch constraint, or emits empty, prefixed, fenced, non-canonical, schema-invalid, or unattested stdout
- **THEN** the runtime imports and publishes no artifact, writes no completion receipt, and does not recover output from agy brain state or a mirror artifact

### Requirement: Ordered pools define execution identity and failover
Each stage SHALL use one ordered provider list that is both its allowlist and failover order. Pool order, resolved-default capability, capacity profile, prompt/schema, or settlement policy changes SHALL create a new plan identity; an attempt's actual provider/model/reasoning SHALL affect attempt provenance but not the logical task identity.

#### Scenario: Reordering providers changes the plan
- **WHEN** the same request changes a stage pool from `[codex, kimi]` to `[kimi, codex]`
- **THEN** the plan hash and logical task keys change deterministically

#### Scenario: Failover stays inside the pool
- **WHEN** the first provider returns a retryable infrastructure failure
- **THEN** the next attempt selects the next declared provider and never a provider outside the list

### Requirement: Capacity and usage are measured before authority
Hard-complete work SHALL require an exact token counter or a validated conservative bound over the final serialized request for every member of the selected stage pool. The planner SHALL use `B_pool=min(B_p)` across the ordered pool and SHALL pack and recount final requests against a utilization-adjusted target derived from that minimum. Every started attempt SHALL record calls, input/output/cache tokens when available, provider usage units, elapsed time, outcome, and evidence sources; unknown price SHALL remain unknown rather than becoming zero.

#### Scenario: Unbudgetable provider cannot perform hard-complete work
- **WHEN** an adapter lacks both an exact counter and a validated upper bound
- **THEN** planning returns `unbudgetable` and the provider cannot enter a hard-complete stage pool

#### Scenario: Secondary capacity constrains the shared shard
- **WHEN** a primary provider can accept a serialized shard but a later failover provider has a smaller valid bound
- **THEN** planning splits or rejects the shard before launch using the later provider's smaller bound

#### Scenario: Default drift invalidates capacity identity
- **WHEN** a provider-default model, reasoning value, CLI revision, serializer, or capability evidence changes
- **THEN** the prior capacity profile and plan become stale before hard-complete work starts

#### Scenario: Failed and cancelled work remains counted
- **WHEN** an attempt fails, retries, or is cancelled after the provider reports billable usage
- **THEN** its actual or reserved usage remains included in the run and intent totals

### Requirement: Claude is never an automatic execution path
The registered provider set, defaults, failover pools, test fixtures, compatibility adapters, and indirect multi-backend model routes SHALL NOT launch Claude or select it as a fallback. Explicit OpenCode and agy model routes SHALL be normalized and checked before executable lookup; Claude aliases and dynamic `auto|default|current|configured` markers SHALL fail even if a local catalog lists them. The v1 registry SHALL match the tracked byte ABI exactly.

#### Scenario: No declared pool can resolve to Claude
- **WHEN** provider configuration and default pools are validated
- **THEN** no provider entry, executable alias, or fallback target resolves to Claude
