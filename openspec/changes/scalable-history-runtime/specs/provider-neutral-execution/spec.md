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
If model and reasoning are omitted, the runtime SHALL preserve the selected CLI's current configured defaults and probe an effective immutable capability identity. A provider-default marker without effective model/reasoning or an equivalent context/token/usage-bound identity SHALL be diagnostic or shadow-only and SHALL NOT enter a hard-complete pool. If either override is supplied, the adapter SHALL use the provider's exact supported grammar and SHALL fail when the override is unsupported, ignored, or resolves to a different effective value.

#### Scenario: Provider default is frozen
- **WHEN** a run selects a provider without model or reasoning overrides
- **THEN** its manifest records the effective model/reasoning identity or a diagnostic-only provider-default marker, plus the capability evidence and revision that produced it

#### Scenario: Unsupported reasoning fails closed
- **WHEN** an explicit reasoning value is not supported by the selected provider adapter
- **THEN** preflight fails without silently dropping the setting

### Requirement: Provider execution uses a portable mirror
Every portable provider attempt SHALL run in a disposable mirror containing only declared role and input artifacts. The durable database, full ledger, Git metadata, unrelated candidate state, and destination path SHALL be absent; only schema-valid declared outputs and host-owned attempt metadata SHALL return.

#### Scenario: Undeclared durable input is unavailable
- **WHEN** an agent attempts to read the canonical database or full ledger from the portable mirror
- **THEN** the path is absent and no durable artifact is copied into the mirror

#### Scenario: Extra output is discarded
- **WHEN** an agent writes declared output plus additional files
- **THEN** only the declared output is considered and the attempt fails if the output contract forbids extras

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
The registered provider set, defaults, failover pools, test fixtures, and compatibility adapters SHALL NOT launch Claude or select it as a fallback.

#### Scenario: No declared pool can resolve to Claude
- **WHEN** provider configuration and default pools are validated
- **THEN** no provider entry, executable alias, or fallback target resolves to Claude
