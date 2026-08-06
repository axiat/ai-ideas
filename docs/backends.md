# Backends

`HISTORY_RUNTIME_ABI=v2` is the provider-neutral runtime. Internal stages use
registered provider IDs and portable mirrors. Omitted reasoning preserves the
selected CLI's current configuration. Omitted models preserve the Codex, Kimi,
and Grok defaults; OpenCode requires a safe host probe and is launched with the
resolved model pinned; agy requires an explicit model. OpenCode and agy models
must be exact members of their current bounded local CLI catalogs.

## Hunt

Generation, history comparison, and review accept `codex`, `kimi`, or `grok`.
The default is Codex with its current configured model and reasoning effort:

```bash
HISTORY_RUNTIME_ABI=v2 ./hunt.sh
```

Explicit override grammar examples:

```bash
HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=codex \
HUNT_MODEL=gpt-5.6-sol \
HUNT_REASONING_EFFORT=xhigh \
./hunt.sh

HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=kimi \
HUNT_MODEL=kimi-code/k3 \
./hunt.sh

HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=grok \
HUNT_MODEL=grok-4.5 \
HUNT_REASONING_EFFORT=high \
./hunt.sh
```

These spellings are adapter inputs only. The checks did not query Codex, Kimi,
or Grok for model availability, account entitlement, or capacity.

The accepted explicit reasoning values are a conservative verified subset:
Codex `high|xhigh`, Grok `high`, and no value for Kimi. Other explicit values
fail before executable lookup. Omission continues to use the CLI default.

Kimi CLI `0.31.1` has no reasoning flag; setting
`HUNT_REASONING_EFFORT` with `HUNT_PROVIDER=kimi` fails preflight. Per-seat
overrides use `HUNT_REVIEW_PROVIDER_<N>`, `HUNT_REVIEW_MODEL_<N>`, and
`HUNT_REVIEW_REASONING_EFFORT_<N>`:

```bash
HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=kimi \
HUNT_REVIEW_PROVIDER_1=grok \
HUNT_REVIEW_MODEL_1=grok-4.5 \
HUNT_REVIEW_REASONING_EFFORT_1=high \
./hunt.sh
```

Selector, prescreen, external prior-work research, and report assembly retain
the existing `AGENT_CMD` / `FRONT_CMD` / `BACK_CMD` process interface. These
variables never enter v2 internal stages, and `HUNT_PROVIDER` does not change
their Codex default. A host without Codex must set `AGENT_CMD` or both
`FRONT_CMD` and `BACK_CMD` to a prompt-taking command that satisfies the
external stage's file contract. A Kimi command shape is:

```bash
HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=kimi \
AGENT_CMD='kimi --auto --output-format text -p' \
./hunt.sh
```

`grok-worker.sh` supplies the external file contract and disables all six
Grok compatibility sources for Claude skills, rules, agents, MCPs, hooks, and
sessions. With no model or reasoning override, both the portable Grok stages
and external Grok stages preserve the CLI's current defaults:

```bash
HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=grok \
AGENT_CMD='./grok-worker.sh' \
./hunt.sh
```

The internal `HUNT_*` settings and external-wrapper `GROK_*` settings are
independent. Pin both paths explicitly when the complete Hunt run must use one
model and reasoning effort:

```bash
HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=grok \
HUNT_MODEL=grok-4.5 \
HUNT_REASONING_EFFORT=high \
AGENT_CMD='./grok-worker.sh' \
GROK_MODEL=grok-4.5 \
GROK_REASONING_EFFORT=high \
./hunt.sh
```

The wrapper accepts only explicit `GROK_REASONING_EFFORT=high`; omission uses
the Grok CLI default, and every other value fails before Grok starts.

## AwR Sidecar

AwR adds `opencode` and `agy`. Codex with its current CLI configuration remains
the default:

```bash
HISTORY_RUNTIME_ABI=v2 SIDE_POLL_SEC=0 ./awr-side.sh
```

Explicit OpenCode and agy grammar examples:

```bash
HISTORY_RUNTIME_ABI=v2 \
AWR_PROVIDER=opencode \
AWR_MODEL=openai/gpt-5.6-sol \
AWR_REASONING_EFFORT=high \
SIDE_POLL_SEC=0 \
./awr-side.sh

HISTORY_RUNTIME_ABI=v2 \
AWR_PROVIDER=agy \
AWR_MODEL=gemini-3.6-flash-high \
AWR_REASONING_EFFORT=high \
SIDE_POLL_SEC=0 \
./awr-side.sh
```

The host checks OpenCode models against `opencode models --pure` and agy models
against `agy models`. Catalog membership does not establish account
entitlement, capacity, or price.

OpenCode accepts an omitted model only when the host-owned
`opencode --pure debug config` probe returns a backend-qualified model outside
the forbidden Claude/Anthropic and dynamic routes. The effective model must
also appear exactly in `opencode models --pure`. The effective model, catalog
probe revision, and canonical catalog SHA enter the profile. Configuration and
catalog are re-probed immediately before launch, and the model is passed to
`opencode run` with explicit `-m`. Probe failure or drift fails before model
workload execution.
Agy has no trusted default-identity probe, so every agy role requires
`AWR_MODEL` or its role-specific `AWR_*_MODEL`; the value must appear exactly
in `agy models`.

## Agy Structured JSON Transport

Agy AwR requires Agy 1.1.8+ and an explicit catalog model. The host probes
`agy --version` during preflight and immediately before launch; an unavailable
CLI, malformed version, or version below 1.1.8 at either check prevents
workload execution.

The portable command uses `--output-format json` and an inline
`--json-schema`. The schema is the exact frozen compact canonical response
schema already covered by the request binding, with only its terminal LF
removed for argv. The launch command and provider object therefore use the
same schema identity.

The outer provider JSON must have `status=SUCCESS`, an object-valued
`structured_output`, and a `json_schema` canonical byte match to the frozen
schema. The host validates the selected inner object with the closed response
schema and exact request attestation, then imports its canonical UTF-8 JSON
bytes. `response`, usage, duration, and other outer metadata cannot supply an
artifact. Malformed outer JSON, a non-success status, missing or mismatched
schema, invalid inner value, or attestation mismatch produces no import,
projection, or completion receipt. Agy has no text fallback and does not
recover JSON from `response`, fences, mirror artifacts, or brain state.

`auto`, `default`, `current`, and `configured` route markers are rejected even
when a CLI catalog lists them. The v1 provider registry is an exact tracked
byte ABI; changed registry revision, grammar, reasoning set, key duplication,
or reformatting is rejected.

The accepted AwR-specific reasoning values are OpenCode `high` and agy
`low|medium|high`. OpenCode `max|minimal` may appear in CLI examples but are
not verified by this adapter revision. Omitted reasoning uses the CLI default.

Role-specific controls are `AWR_RESEARCH_*`, `AWR_PRIORWORK_*`, and
`AWR_JUDGE_*`. For example:

```bash
HISTORY_RUNTIME_ABI=v2 \
AWR_PROVIDER=codex \
AWR_PRIORWORK_PROVIDER=opencode \
AWR_PRIORWORK_MODEL=openai/gpt-5.6-sol \
AWR_JUDGE_PROVIDER=agy \
AWR_JUDGE_MODEL=gemini-3.6-flash-high \
SIDE_POLL_SEC=0 \
./awr-side.sh
```

V2 rejects `SIDE_CMD`, `SIDE_RESEARCH_CMD`, `SIDE_PRIORWORK_CMD`, and
`SIDE_JUDGE_CMD`. V1 retains those compatibility controls.

## Portable Boundary

Each v2 attempt runs directly on the host in a disposable bounded mirror. The
mirror contains one role plus declared inputs; it omits the full ledger,
SQLite database, Git metadata, unrelated round state, and `.claude`. The host
sets a fixed request-byte ceiling, accepts one closed JSON envelope on stdout,
validates it, and publishes outputs atomically.

The host injects a base-request binding over the stage, seat, prompt, role SHA,
declared-input names and SHAs, and response schema, plus a separate prompt SHA.
The response must echo both values exactly. Missing or wrong attestation fails
before any artifact is projected or completion is published.

Portable Grok stages use the `grok-portable-v3` command grammar and request
`--output-format json`. The rendered command environment forces all six
`GROK_CLAUDE_*_ENABLED` compatibility cells to `false`, and the command record
and preflight bind those values. The complete
provider-owned outer stdout remains under the 128 KiB capture limit. After the
host validates that transport and its terminal `text`, it accepts complete bare
inner JSON or one unique terminal Markdown fence after an optional accumulated
prefix. Grok's CLI reducer concatenates assistant chunks without inserting a
separator, so the exact opener bytes `b"```json\n"` may begin at any byte of
`text`; line-start placement is not required. Fenced text must contain exactly
two triple-backtick sequences, exactly one opener, and no CR byte. The closing
delimiter must be preceded by LF and its final byte must end `text`. The host
discards only the accumulated prefix and the two fence markers. Narration plus
bare JSON still fails strict parsing. An additional delimiter, a different
label or case, a missing close, or any trailing byte also rejects the response.
No trimming, JSON-suffix search, normalization, or repair occurs.

Every v2 portable request carries binding-covered `transport_instructions`.
Grok receives a provider-specific stdout instruction: the final assistant
response itself must be one exact lowercase-`json` LF fence containing the
canonical UTF-8/NFC response-schema object, whose single trailing LF immediately
precedes the terminal close. No byte may sit outside that fence, and no
triple-backtick sequence may occur in an earlier assistant response. Codex,
Kimi, and OpenCode retain the raw canonical-stdout instruction with no fence
or narration. Agy requires structured output as specified above. Its request
instruction overrides legacy output-location and file-writing statements in
the AwR role; `role.md` defines artifact content only. All instructions forbid
model-authored mirror writes and require exact request attestation. After the provider command finishes,
the host terminates its process group and waits for the provider process before
validating any mirror or response bytes.
Every declared entry must remain a regular single-link file with its exact
original `st_mode`; a stable read must preserve its exact byte count and
SHA-256. Added or removed files, entry-type, link-count, or mode changes, and
content drift reject the attempt before import. The non-scratch mirror uses
descriptor-relative, no-follow traversal with final child and root namespace
identity checks. Traversal failures or a raced namespace replacement reject;
stable empty directories remain ignored.

Stdout portable attempts reserve `.tmp` as bounded ignored provider scratch.
It must remain a real directory. Descriptor-relative, no-follow traversal
permits only real nested directories and regular single-link files, with at
most 32 files, 64 total entries, and 1 MiB of stable-read file bytes. Scratch
is never imported. A missing or replaced `.tmp`, a symlink, hardlink, special
file, unreadable or unstable entry, traversal race, or exceeded limit rejects
the attempt. The host also validates the selected transport, the closed schema, and
attestation. It does not recover output from an agy brain directory or a
role-named mirror artifact.

The disposable attempt is removed before any durable import. Cleanup first
repairs directory permissions, removes the attempt tree, and verifies its
absence. Failure returns `attempt_cleanup_failed`; no import, projection, or
completion is published. Legacy file-output attempts with
`forbid_extra_files=true` enumerate the complete mirror through directory
descriptors without following links. Only regular single-link files may occupy
expected paths; unreadable directories, traversal failures, special files,
symlinks, and raced child or root directory replacement fail closed instead of
being skipped. This legacy check closes path, type, and link identity; it does
not add content or mode immutability for ordinary declared inputs.

Completion hashes, including `model_envelope_sha256`, identify the canonical
inner model envelope, not a discarded provider transport.

This post-exit fail-closed check is not an OS-enforced read-only mount or a
container sandbox. A host-privileged CLI can still access absolute host paths
outside the mirror.
Provider authentication and current CLI defaults remain available through the
provider's normal host configuration.

## V1 Compatibility

With `HISTORY_RUNTIME_ABI=v1`, Hunt retains `CONTAINED_AGENT_CMD_JSON` and
`CONTAINED_REV_CMD_<N>_JSON`; AwR retains `SIDE_*`. V2 rejects those internal
overrides before state mutation. Hunt v2 continues to allow
`AGENT_CMD` / `FRONT_CMD` / `BACK_CMD` only for its four external stages.

Operational AwR defaults remain `SIDE_POLL_SEC=9000`, `SIDE_MAX_BAD=3`,
`SIDE_MAX_ROUNDS=3`, `SIDE_GAP_MIN_SEC=60`, `SIDE_GAP_MAX_SEC=600`, and
`SIDE_COOLDOWN_SEC=3600`.

A failed final bounded Hunt round exits without `FAIL_SLEEP_MIN`; failed rounds
with another bounded attempt remaining retain the configured cooldown.

## Literature Monitor

The default assignment in `litwatch.sh` is:

```bash
LITWATCH_CMD=${LITWATCH_CMD:-codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral}
```

`LITWATCH_CMD` controls optional annotation. `LITWATCH_AGY_CMD` is a compatibility override consulted only when `LITWATCH_CMD` was unset; an explicitly set neutral variable always wins. `LITWATCH_NO_AGY=1` skips annotation while deterministic ingest still runs.

```bash
LITWATCH_CMD='./agy-worker.sh' ./litwatch.sh
LITWATCH_NO_AGY=1 ./litwatch.sh
```

OAI harvesting defaults to the last `LITWATCH_OAI_DAYS=4` days and at most `LITWATCH_OAI_MAXPAGES=8` pages. The default source is OAI; arXiv and Semantic Scholar are explicit `LITWATCH_SOURCES` selections.

## Calibration

Frozen panels and the all-case runner share this retrieval-disabled default:

```bash
PANEL_CMD=${PANEL_CMD:-codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral}
```

End-to-end retrieval calibration uses:

```bash
E2E_CMD=${E2E_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral}
```

```bash
PANEL_CMD='./grok-worker.sh' ./calib/run_panel.sh calib/cases/pos-axiom-torque
E2E_CMD='./grok-worker.sh' ./calib/run_e2e.sh calib/cases/neg-replai
```

Frozen reviewers must not retrieve because published source papers can invalidate reconstructed positive controls. `run_e2e.sh` intentionally enables retrieval and additionally requires neighbor-link and structured API-query density. Neither runner proves that an arbitrary custom backend actually used or avoided the network; conclusions inherit the configured backend's behavior.
