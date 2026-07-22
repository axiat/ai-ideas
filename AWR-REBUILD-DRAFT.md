# AwR Re-entry Architecture (Deferred)

Status: deferred in full and retained as a complete architecture specification. The experiment gate ran on 2026-07-14. The strongest candidate, `2b500d736c99`, sat on the overlap-calibration boundary: the original all-Claude round classified overlap as low and returned a 2,2,1 near-SA result, while the stricter review classified it as high and rejected it. The candidate never reached SA, so this architecture is not implemented. See P1 "AwR re-entry architecture" in `DEVELOPMENT.md` for the result and decision.

## 1. Scope and Outcome

Three related paths exist:

| Path | Input | Output | Boundary |
|---|---|---|---|
| A: main-loop recheck/evolution | An evolution or recheck parent satisfying `PROGRAM.md:12`; near-SA is only a priority hint | Prior work plus a formal verdict from the configured N reviewers | Existing main loop |
| B: AwR sidecar | Latest-wins, low-overlap, design-fixable AwR | An evidence-backed `SA-possible` artifact with provenance | P1 |
| C: manual promotion | A manually confirmed sidecar artifact | Manual handoff for main-loop reevaluation | Transition until #3 is complete |

The P1 sidecar loop does not write `SA-possible` as a formal verdict and does not modify `ledger.tsv`. The explicit `promote` operation in §3.8 is a separate administrative workflow that hands work to the existing main loop. A trusted artifact must satisfy all of these conditions:

- The judge passes the startup trusted-configuration gate.
- `check_judge` passes the structural gates for novelty, feasibility, and clear acceptance.
- The novelty section contains mandatory Strongest Counterexample evidence.
- The final marker and manifest trace the complete artifact DAG through terminal draft, judge, feedback, and finalization.
- The latest revision after the third feedback round still receives a judge evaluation.

The automatic bridge returns trusted artifacts to the main loop with at-least-once external execution and exactly-once committed effect. External agents, prior-work search, and reviews stay outside long DB transactions. The DB atomically commits only the formal verdict and committed state; file materialization replays idempotently outside the transaction.

## 2. Invariants

### P1

1. The sidecar loop may write only under `tmp/awr-side/`. It does not write `tmp/round/`, `ideas/`, or `ledger.tsv`, and it does not run Git. The §3.8 `promote` operation crosses a separate main-loop administrative boundary. Every agent runs in a per-call mirror, and only allowlisted artifacts are collected. A backend without OS-level confinement for external side effects cannot serve as a trusted judge.
2. Without a configured trusted judge, the default is dormant `exit 0`: no queue access, agent calls, or task-state writes.
3. Configuration, infrastructure, and contract failures cannot produce `not-ready` or another permanent scientific terminal state. Only a valid content rejection consumes a feedback round.
4. The trusted loop acquires the legacy inhibit lock `tmp/agy-side.lock` and `tmp/awr-side.lock` in order. Migration and empty-installation bootstrap acquire three locks in `hunt -> legacy -> new` order. Holding only the new lock cannot justify an absent check followed by an empty `DONE`.
5. Every action in a sealed migration plan completes independently. Completion of one action for a key cannot overwrite completion state for another action on the same key.
6. An existing legacy target does not prove migration completion. The corresponding source must be removed or explicitly archived before the action is marked done.
7. Every publication point for `tmp/ledger.good` uses a temporary file in the same directory, fsync, and atomic rename. Migration reads only a frozen snapshot.
8. Provenance records an artifact DAG for invocations and deterministic transforms. `prepared` and `started` records cannot impersonate actual execution or artifact attribution. A terminal artifact requires an atomic commit marker.
9. Durability order is fixed. A copy-and-delete path writes, fsyncs, and renames the target, fsyncs the target parent, unlinks the source, then fsyncs the source parent. A move path performs atomic rename and fsyncs both source and target parents. Both paths append and fsync the successor event only after directory entries are durable; first creation of an event file also fsyncs its parent. An event cannot precede the directory entry it claims.
10. Persistent epoch, archive, CAS, provenance, and similar directories use `durable_mkdir`: reject symlinks at every level and fsync the parent after creation. No unique source may be deleted or renamed until the entire directory chain is durable.

### Automatic bridge

1. `lineages` contains lineage identity only. Path A/B eligibility evidence resides in `reentry_grants`, and readiness resides in independent `reentry_requests`.
2. Ordinary ledger import creates lineage and candidate records without creating a grant or ready request. Only a complete Path A/B eligibility gate may write an active grant and create or activate a request.
3. Evolution, recheck, and re-entry for one lineage share one re-entry allowance and one slot per round.
4. An expired claim is visible to the allocator. After reclaim, its old token cannot commit.
5. Revoked eligibility prevents new claims and prevents submission of completed work that has become ineligible.
6. The DB atomically commits verdict plus committed state. Files materialize idempotently outside the transaction by `candidate_id`.

## 3. P1 Runtime Contract

### 3.1 Entry ordering and startup truth table

The entry point parses the subcommand and raw environment variables before creating directories, writing logs, or migrating files:

| Mode | Condition | Behavior |
|---|---|---|
| migration | `awr-side.sh migrate-legacy` | Calls no agent; pins the migration release bundle, then acquires the hunt/legacy/new locks in order and executes §4 |
| ledger adoption | `awr-side.sh adopt-ledger-baseline --source <git-object|file> --expect-sha <sha> --authority <ref>` | Calls no agent and requires no judge environment; holds the hunt lock and atomically publishes baseline plus attestation from explicit authority and expected SHA |
| lock recovery | `awr-side.sh recover-lock <hunt|legacy|new> --expect-digest <sha> --confirm-stale` | Calls no agent and requires no judge environment; uses the independent recovery guard and quarantine protocol below |
| reopen | `awr-side.sh reopen <key> <research|judge>` | Calls no agent and requires no judge environment; validates arguments, release, `DONE`, and runtime state, then acquires legacy/new locks in order and applies the §3.3 archival transition |
| promote | `awr-side.sh promote <key>` | Does not enter dormant mode; validates the trusted final artifact, formal-pipeline policy, and outer-confinement/DAG capabilities before executing §3.8; remains disabled until those capabilities exist |
| dormant | No subcommand, with both `SIDE_JUDGE_CMD` and `SIDE_JUDGE_TRUSTED` unset | Records dormant on stderr and returns `exit 0` |
| invalid | Exactly one of those two variables is set | Names the missing variable and returns `exit 2` |
| trusted | Both variables are set, the research adapter resolves, and every §3.2 predicate passes | Acquires legacy/new inhibit locks in order, validates the migration gate, and enters the main loop |
| invalid | Both variables are set, but a research or judge capability predicate fails | Names the seat and failed predicate and returns `exit 2` |

Subcommand parsing completes before dormant/environment evaluation. Unknown subcommands, missing or extra arguments, and invalid key or seat values always return `exit 2`; none may fall through to the default loop. The truth table uses whether variables were explicitly set, not a derived value such as `judge_cmd=${SIDE_JUDGE_CMD:-$SIDE_CMD}`. `SIDE_CMD` alone still means dormant. In trusted mode, research resolves strictly from explicit `SIDE_RESEARCH_CMD`, then explicit `SIDE_CMD`, then `agy-builtin`. An explicitly empty value is invalid and never falls through silently. All three research choices require a registered outer-confinement profile and a separate fail-fast startup check. `agy-builtin` may serve only as research, never as the trusted judge.

Before trusted mode or `promote` creates any directory, calls an agent, or writes a reservation, it parses effective `REVIEWERS` as a decimal integer `N>=2`, default 3, enforcing the `PROGRAM.md` rule that no single agent decides a verdict. It also requires `5<=PRIOR_MIN_LINKS<=8`, `PRIOR_MIN_API>=1`, `MIN_READ>=5`, and `AXIOM_MIN_CRACKS>=2`. Configuration may only tighten these floors and cannot conflict with fixed upper bounds. Zero or one reviewer, values below a policy floor, non-integers, out-of-range values, and internally inconsistent settings return `exit 2`. A weakened environment or promotion-policy bundle cannot bypass a hard gate.

New code first builds `tmp/.lock-owners/<nonce>/` under control and fsyncs each directory level. Its receipt encodes pid, process-start token, hostname, random nonce, and schema version, and retains a compatible `pid` file. A single atomic symlink then points the contested lockpath at that owner directory. Owner identity is complete when the lockpath becomes visible, eliminating the `mkdir -> write pid` window. A new reader uses `lstat/readlink`, accepts only a normalized relative target within `.lock-owners/`, then validates the receipt through dirfd and `openat(O_NOFOLLOW)`. It never follows an arbitrary target. Release matches the complete nonce and receipt, unlinks the lockpath, fsyncs the parent, and only then reclaims the owner directory, preventing ABA.

The compatible `pid` file lets old `hunt.sh` code using `cat "$LOCK/pid" && kill -0` detect a live migration owner and exit instead of treating a symlink as an empty stale lock and running `rm -rf`. An old sidecar's `mkdir` also fails. Global order is `tmp/hunt.lock -> tmp/agy-side.lock -> tmp/awr-side.lock`. Migration takes all three; trusted/reopen skips hunt but retains legacy -> new. Any live owner causes release of already acquired locks and `exit 2`. A legacy directory lock with a pid for which `kill -0` succeeds is conservatively live. Only a dead pid permits archival cleanup. A legacy directory with no pid or an unrecognized format cannot be checked safely and therefore fails closed; age alone never triggers deletion. New lockpaths permit only controlled symlinks. Legacy directories are read only for compatibility and recovery, never created by ordinary acquisition.

`recover-lock` does not acquire the target lock. It first acquires independent `tmp/awr-lock-recovery.guard` through a new-style symlink receipt, then freezes the target lock's inode, uid, type, and tree digest with `lstat/openat`. The command-line `--expect-digest` must match the observed digest exactly, and the operator must supply `--confirm-stale`. When an owner resolves, recovery still verifies that pid plus start token is no longer valid. It rejects a live owner, a remote owner that cannot be verified, any target change after guard acquisition, and any symlink target. On confirmation, it only atomically renames the old lock to `tmp/lock-quarantine/<kind>-<digest>`, fsyncs both parents, and durably writes a recovery receipt. It does not delete the lock. It then releases the guard; the next migration performs ordinary ordered acquisition. Replaying recovery for the same digest is idempotent only when quarantine and receipt postconditions match exactly.

Trusted mode acquires the legacy inhibit and new lock before evaluating the migration gate:

1. If `tmp/agy-side` exists, return `exit 2` and require explicit `migrate-legacy`; the main loop cannot rename it.
2. If `migrations/v1/DONE` exists, validate the immutable migration receipt, confirm that its `migration_bundle_id` appears in the current `P1_RELEASE` trusted-predecessor allowlist, and run the runtime validator against the current queue. Proceed only if all three checks pass.
3. If a migration epoch exists without valid `DONE`, return `exit 2`.
4. If no epoch exists and the queue outdir is nonempty, return `exit 2`.
5. If `tmp/agy-side` is absent, no epoch exists, and the queue outdir is absent or empty, do not create `DONE` while holding only the two current locks. Validate receipts while releasing in reverse order, enter the pinned migration empty-bootstrap path, acquire hunt/legacy/new locks in order, and recheck roots, epoch, and inventory. Only if the installation remains empty under all three locks may it atomically create a sealed plan with expected=0 and `snapshot_sha=none`, `done-all`, and `DONE`. Release all three locks, reacquire legacy/new, and restart validation at gate item 1.

`side.log` is fixed at `tmp/awr-side/side.log`, outside the queue outdir. Normal runtime archival such as reopen and unfreeze goes to independent `tmp/awr-side/runtime-archive/`, managed by runtime events and CAS, and never writes to or changes the immutable digest of `migrations/v1/archive/`. Diagnostics before the migration gate go only to stderr so logging cannot alter the empty-installation decision.

### 3.2 Trusted judge

Trusted mode requires all of the following:

1. `SIDE_JUDGE_TRUSTED=1`.
2. `SIDE_JUDGE_CMD` is explicitly set and is not inherited from `SIDE_CMD`.
3. Research and judge use different `call_id` values, independent processes and contexts, and role-specific inputs. The same adapter command may start both, but the seats cannot share a session or resume id or a writable sandbox.
4. Research and judge both run through a registered OS sandbox/container profile. It exposes only this call's fixed input bundle and immutable CLI runtime as read-only and grants write access only to one output staging directory. It denies the real repository, other keys/outdirs, user home and configuration, SSH/Git credentials, and host sockets; it restricts executable programs and child processes. Network access uses explicit research/model egress policy with no user credentials. A per-call mirror, cwd, prompt, Codex `workspace-write`, or a Claude permission allowlist alone does not prove read/write confinement.
5. The judge passes an adapter capability predicate, not merely a basename check. Within outer confinement, the Codex adapter fixes its mirror as the workspace, uses `--ignore-user-config` with an isolated `CODEX_HOME`, verifies empty hooks/plugins/MCP, then sets `workspace-write`, `approval_policy=never`, explicit network policy, and `--skip-git-repo-check --ephemeral`; danger/full-access is rejected. The Claude adapter uses an isolated HOME/config, sanitized settings, explicit empty hooks/plugins/MCP, and `-p --strict-mcp-config`.
6. The judge's resolved executable is an immutable `claude` or `codex` bundle in the outer profile. It rejects `agy*`, repository-local name shadowing, `env` or `timeout` prefixes, and unknown wrappers. Research may use `agy-builtin` only after its independent outer profile and minimal authentication mount pass equivalent read/write/process probes. The current `grok-worker.sh` permits git/gh/curl and inherits user hooks/plugins/MCP, so it is not allowlisted until equivalent read/write/process/credential confinement is accepted.

The manifest records independence in three states:

- `verified`: both seats resolve backend and exact model, and the combinations differ.
- `asserted`: only independent invocations are proven; model independence is asserted by `SIDE_JUDGE_TRUSTED=1`.
- Configuration gate failed: no manifest and no content verdict are produced.

Different command strings prove separate launches only; they do not establish independent models. Trusted status establishes judge eligibility, not reproducible configuration.

### 3.3 Failure classification, quarantine, and feedback state machine

Each call has independent stdout, stderr, return code, and temporary artifacts. Classification is mutually exclusive and follows this priority:

1. `infra-failure`: `rc != 0`, an empty artifact, or network, authentication, or quota failure detected in stderr or the artifact. Retry, cool down, or trip the circuit breaker only. Do not create `.badN` or increment the key counter.
2. `contract-failure`: `rc = 0`, the artifact is nonempty, no infrastructure failure matched, and `check_draft` or `check_judge` fails. Write `<key>.research.contract.badN` or `<key>.judge.contract.badN` for the corresponding seat.
3. `content-reject`: the judge artifact satisfies its contract and identifies a content defect explicitly. Only this class is written to `## Reviewer Feedback` and consumes `SIDE_MAX_ROUNDS`.

When one seat's contract counter reaches `SIDE_MAX_BAD`, write `<key>.<seat>.quarantined`. The marker contains reason, count, `updated_at`, and an invocation-configuration summary. It is audit evidence and does not reopen automatically.

`reopen <key> <seat>` is an explicit administrative action under the shared locks:

1. Archive every `.contract.badN` for the seat.
2. Verify that the outdir contains no remaining bad artifact for that seat.
3. Archive the quarantine marker.
4. Recompute the counter from 0.

The state machine evaluates the current draft before testing the feedback limit:

```text
revision required -> research -> contract check -> install draft
draft             -> judge    -> contract check
judge=SA-possible                  -> finalize ready
judge=not-ready and rounds >= max -> finalize not-ready
judge=not-ready and rounds < max  -> apply feedback, rounds += 1
```

With `max=3`, all three feedback rounds are applied, and the latest draft at rounds=3 is still judged. A condition such as `rounds + 1 >= max` cannot finalize early.

### 3.4 Invocation-level provenance

Every research and judge call uses a unique `call_id` and immutable invocation bundle. At trusted-sidecar startup, the `P1_RELEASE` manifest is parsed into a read-only, content-addressed release bundle. It contains the complete entry/orchestrator/libs, roles, parsers, policies, rubrics, adapters, outer profiles and capability predicates, identity/CAS/current-pointer/selector implementations, and schemas. The manifest is checked item by item before fixing `process_bundle_id`. During execution, executable policy bytes no longer come from the worktree; only explicit data inputs such as task and current pointers do. Before writing `prepared`, every queue scan and call revalidates release-bundle digest/schema and process pin, failing closed on absence or change. Worktree upgrades do not affect a running process. Switching release requires a new process. A later call cannot record a one-component change as acceptable drift. Every call also fixes:

- Prompt, policy, rubric, and repository adapter copied only from the process release bundle; repository code executes from that read-only copy.
- Outer sandbox/container profile instantiated only from the release bundle, including read-only mounts, writable output, exec and egress policy, and hashes of sanitized HOME/config/CODEX_HOME.
- Expanded final argv, resolved executable, backend, exact model, and CLI/package version.
- Effective environment that changes behavior, including model, sandbox, turns, web controls, and binary path, plus the loaded settings, plugin, hook, and MCP inventories and content hashes. Secrets record only source, presence, and an irreversible fingerprint, never plaintext.
- Effective `REVIEWERS`, `PRIOR_MIN_LINKS`, `PRIOR_MIN_API`, `MIN_READ`, and `AXIOM_MIN_CRACKS` values frozen and actually provided to prompts and parsers.
- Policy SHA, `process_bundle_id`, gate SHA, every loaded repository-library SHA, and adapter SHA.
- `SIDE_JUDGE_TRUSTED=1`, every adapter capability-predicate result, and the independence classification.
- Git HEAD, role, key, round, time, and input artifact ids and SHAs.
- Repository-scoped `ledger_instance_id`; origin-ledger snapshot artifact id, SHA, and row count; row number; raw-row SHA; canonical story/lineage key; and stable `origin_stable_id` used to map a revision to its original lineage. Snapshot SHA is audit context only; CAS retains the complete bytes.

Policy and profile hashes are computed over release-bundle copies actually used by the invocation. Gate SHA comes from `process_bundle_id` fixed at process startup and is compared again with the release manifest during call preflight. An external CLI receives `execution_identity_pinned=true` only when it runs from an immutable self-contained image or a content-addressed, read-only executable plus complete package/dependency tree for the entire invocation. A one-time path/version/hash observation after successful child exec proves only the launch point; it cannot exclude a Node or Python launcher loading an upgraded module during execution. Such unfrozen packages always record false. Missing model, effective configuration, or execution identity records `unknown`; a command string is not a substitute.

`tmp/awr-side/provenance/<key>.jsonl` records the invocation lifecycle as append-only events:

1. `prepared`: the bundle is fixed, but agent startup is not proven.
2. `started`: appended after the child completes a successful `exec` handshake, recording pid and observed image identity; this still does not attribute an artifact.
3. `completed`: the child returned; records rc, failure class, output artifact id/SHA, and contract result.
4. `installed`: a contract-valid artifact was committed as current draft or judge through a call-bound current pointer.

Events use canonical JSON, deterministic event ids, and line checksums, with fsync after append. Only one crash-truncated record may appear at the tail. Replaying the same event id and content is idempotent; a conflict or corrupt interior line fails closed. Call output first enters a per-call temporary file. Its CAS object must become durable before a `completed` event can reference it. Only completed, contract-valid output can be installed. The call-bound current pointer must become durable before appending `installed`. If the process dies after pointer rename but before `installed`, resume appends `installed` only when the pointer names the same `call_id` and artifact id/SHA. Matching cache or target SHA alone cannot trigger repair.

All task, draft, judge, final, and ledger-snapshot content enters the content-addressed artifact store first. Every mutable logical artifact uses `<key>.<role>.current.json` as its sole commit point. The pointer fixes `call_id/transform_event_id`, artifact id/SHA, and sequence: make the CAS object durable, then atomically replace the pointer with temp-file write, file fsync, rename, and parent-directory fsync. Human-readable `.task.md`, `.draft.md`, and `.judge.md` files are caches materialized from pointers and can be rebuilt after a crash. Repairing `installed` requires the pointer to bind the call or event explicitly; a target cache that happens to equal the completed SHA does not establish attribution. CAS objects referenced by current/final markers, migration receipts, promotion staging or attestations, or DB artifact rows cannot be garbage-collected.

The artifact DAG also records orchestrator transforms:

- `task-created`: immutable ledger snapshot artifact plus origin row -> initial task.
- `feedback-applied`: prior task plus installed judge -> new task.
- `draft-installed` / `judge-installed`: invocation inputs -> current artifact.
- `finalized`: terminal draft plus terminal judge plus manifest -> `<key>.md`.

Every transform records input artifact ids, output id/SHA, `process_bundle_id`, and event sequence. Finalization walks the complete DAG backward from the terminal artifact. Manifest producer calls include only installed calls on that path; transform nodes appear separately. Prepared, started, uninstalled completed, failed, and superseded calls remain under attempts and cannot contribute terminal attribution. Release components cannot drift within a process. When crash recovery explicitly switches to another completely validated release/process, the cross-process chain lists every version and sets `drift=true`.

The terminal `<key>.md` is written completely to a temporary file, validated against its manifest and DAG, fsynced, atomically renamed, and followed by a parent-directory fsync. The same durable helper then atomically writes the `<key>.finalized` commit marker with final SHA, manifest SHA, and terminal draft/judge ids. If a crash occurs after rename and before the marker, resume repairs the marker only after validating the full DAG. The main loop treats an artifact as terminal only when marker, file, manifest, and DAG all match. A nonempty `<key>.md` alone is incomplete.

The runtime validator executes at every trusted startup and before every queue scan. Before strict validation, it may recover only a prepared filesystem transition identified by a deterministic event id: append the commit event when postconditions match exactly, continue idempotently when the complete pre-state remains, and fail closed for every other combination. It then requires every terminal to have a valid final marker, every new draft/judge to trace to an installed event, and every task change to have a transform. A sealed `adopt-current-artifact` may act as a `legacy-untrusted` task/draft root input but cannot grant terminal trust alone. Frozen and unfrozen states require migration/runtime receipts. An unknown or provenance-free `<key>.md` fails closed.

An artifact may claim reproducible configuration only when final argv, backend, exact model, CLI/package, every behavior-relevant environment and configuration value, policy, gate, adapter, and execution identity are fixed and readable. Any `unknown`, unfixed user-level plugin/hook/MCP, or `execution_identity_pinned=false` remains auditable and reviewable but cannot claim reproducibility.

### 3.5 Judge evidence contract

The per-call mirror copies only fixed prompt/configuration plus the `task.md` and `draft.md` named by the current pointer for this key; `cp "$outdir"/*.md` is forbidden. Other keys, terminal `<key>.md`, `.legacy-frozen.md`, migration archives, quarantine files, and bad artifacts cannot enter the agent sandbox. The outer profile also read-denies absolute paths to the real repository and outdir. The orchestrator pre-creates the staging parent and output directory without granting the agent permission to rename the parent. After the call ends and all children are confirmed exited, the orchestrator opens the exact basename through a pre-opened dirfd with `openat(O_NOFOLLOW|O_NONBLOCK)`. `fstat` requires expected uid, regular-file type, `nlink=1`, no special mode, and size under the limit. Hashing, parsing, and CAS copy use that same FD, followed by an inode and size recheck. A symlink, hardlink, FIFO, device, socket, directory, path replacement, or extra output is a contract or infrastructure-boundary failure and is never collected through ordinary path open or copy.

The base contract retains the current early-stop gates:

- `check_draft`: contains `## Revised Idea`, exactly one valid `Shape:`, and at least 3 search records with URLs; the final nonempty line is exactly `AGY-DONE`.
- `check_judge(task,draft,judge)`: contains exactly one `Decision: SA-possible|not-ready` and one valid `Confirmed Shape:`. A `not-ready` artifact contains at least one `- Defect:`. Both paths end with exact final nonempty line `AGY-DONE`.

Shape uses a stable enum: `mechanism-or-new-problem`, `math-exploration`, `cs-principle-transfer`, `bottleneck-probe`, and `load-bearing-assumption`. `task-created` fixes `origin_shape` from the source run artifact. When it cannot be proven, record `unknown`; research cannot overwrite it. The draft declares a revised shape, and the independent judge confirms it. If origin, draft, or judge names `load-bearing-assumption`, or the task/draft matches reserved structural fields for that shape, effective shape is always load-bearing-assumption and activates the crack gate. A draft alone cannot downgrade `unknown` to an ordinary shape.

The `SA-possible` path in `roles/awr-judge.md` has this fixed form:

```text
Confirmed Shape: <one enum value above>

## Novelty Evidence
- <neighbor-id> | <nearest neighbor and URL, 5–8 entries>
- Strongest Counterexample: <neighbor-id above> | <critical difference>
- API Query: <arXiv or Semantic Scholar API URL>
- Papers Read: <integer>

## Feasibility Evidence
- Minimum Falsification Experiment: <complete description of at least 30 bytes>
- data: <nonempty>
- compute: <nonempty>
- signal: <nonempty>

## Clear-Accept Gate
<at least 30 bytes explaining why the work approaches 6/6/8+>
```

Only `Decision: SA-possible` requires exactly one instance of each section in Novelty -> Feasibility -> Clear-Accept order, with the strong evidence gates below enforced by section boundaries. `Decision: not-ready` uses only the base contract: a valid `Confirmed Shape:`, at least one `- Defect:`, and terminal `AGY-DONE`. Missing the three positive-path sections cannot reclassify a content rejection as a contract failure. `SA-possible` requires at least:

- `effective PRIOR_MIN_LINKS..8` distinct neighbor identities in Novelty. Use an extractable arXiv id or DOI as identity; otherwise use a canonical URL with fragment and tracking removed and host/path normalized. Exclude reserved fields. Abs/pdf/version URLs or repeated URLs for one paper count once. Every `neighbor-id` is unique.
- `effective PRIOR_MIN_API` distinct HTTPS queries accepted by the URL parser. The template may repeat `- API Query:`. arXiv permits only host=`export.arxiv.org`, path=`/api/query`, with nonempty decoded `search_query` or `id_list`. Semantic Scholar permits only host=`api.semanticscholar.org`, path=`/graph/v1/paper/search`, with nonempty decoded `query`. Reject bare domains, incorrect endpoints, empty parameters, fragments, userinfo, and placeholders such as `<...>`, `${...}`, `TODO`, or `example`.
- Exactly one `Strongest Counterexample:` line in the section, referencing one listed neighbor-id and containing a nonempty critical difference.
- A complete line matching `^- Papers Read: [0-9]+$` with value `>= effective MIN_READ`.
- A Minimum Falsification Experiment in Feasibility, with at least one non-whitespace character in each of data, compute, and signal.
- Clear-Accept body text meeting its length gate.

The revised draft in `roles/awr.md` always contains enum field `Shape:`, and the judge contains `Confirmed Shape:`. An `SA-possible` artifact whose effective shape removes a load-bearing assumption also requires `## Crack Evidence Verification`, with every entry matching one of these forms exactly:

```text
- Crack: <evidence-id> | <source URL> — supports: <reason>
- Crack: <evidence-id> | <source URL> — contradicts: <reason>
```

The parser resolves evidence-id plus normalized URL against frozen, self-reported crack evidence in the task or draft and rejects unknown sources. The same normalized evidence counts once even under different ids or duplicate lines. Distinct `— supports` entries must be `>= AXIOM_MIN_CRACKS`. Prompt template and parser regex share a fixture to prevent spacing or field-name drift. Negative fixtures cover an origin marked load-bearing-assumption while draft or judge claims an ordinary shape, duplicate neighbor or crack evidence, a bare API endpoint, empty query, and placeholder query.

These are honor-system structural gates, not independent deduplication. The automatic bridge reruns main-loop prior-work search for formal novelty.

### 3.6 Queue selection

P1 first establishes an identity helper shared with #3 and commits read-only `ledger.instance-id` at repository root. Its normalized single-line value is immutable `ledger_instance_id` and cannot be regenerated during an upgrade or clone:

- `canonical_story_v1`: Unicode NFC, trim, collapse internal consecutive whitespace to one space, normalize line endings, and preserve punctuation and quotation marks; `origin_lineage_key = sha256("tsv-v1\0" + UTF8(canonical_story))`.
- `origin-row-v2`: `row_number` is the 1-based data-row ordinal after the header. `raw_row_sha` hashes the exact bytes of that TSV row, excluding one terminal `LF` or `CRLF`; `origin_stable_id = sha256("tsv-row-v2\0" + ledger_instance_id + "\0" + decimal(row_number) + "\0" + raw_row_sha)`.

Snapshot SHA is provenance only. Sidecar tasks, promotion receipts, and the #3 importer call the same helper. Physical file line numbers, hashes including the terminator, and unversioned private implementations are invalid. A missing or duplicate file or a changed value fails closed. When a human confirms that two canonical stories belong to one semantic lineage, the resolution enters a tracked promotion attestation. Later receipts reuse its existing `origin_lineage_key`; an absent mapping or ambiguous evidence blocks promotion. #3 later imports these resolutions into `story_aliases`.

The sidecar reads a complete `tmp/ledger.good` snapshot:

1. Support 7/8 columns and explicitly read `date source theme idea verdict reason overlap category`, without filtering by source first.
2. Aggregate all sources by `origin_lineage_key`; the last row wins in the append-only file. A local filename key is only a registry-validated display key and extends its hash on prefix collision without changing aggregate identity.
3. Select only a winning row satisfying `source=hunt && verdict=accept-w-rev && overlap=low`. A later weekly or other-source row is a lineage tombstone; selection cannot fall back to an older hunt AwR row.
4. Validate the winning row's origin fingerprint.
5. Subtract stories present in the current `near-sa-queue.tsv`.

Before each scan, persist the complete `ledger.good` bytes as an immutable CAS snapshot object and record artifact id/SHA plus data-row count. The CAS object and parent directory must be durable before `task-created` can be written. When a selected key gets a task, fix that snapshot artifact, repository-scoped `ledger_instance_id`, winning row number, raw-row SHA, canonical story/lineage key, and `origin_stable_id` in task provenance and the DAG. Later ledger appends do not rewrite origin identity; they affect only dynamic pool eligibility. #3 revalidates the origin row from that snapshot object and requires the current ledger prefix to continue matching row by row.

The near-SA file is pruned, and the ledger does not retain complete vote vectors, so step 5 is best-effort deduplication for P1. Strict A/B routing by `sa_votes` begins only after #3 persists candidates and requests.

A ledger verdict of evidence-incomplete is reject and therefore never enters the sidecar mechanically; Path A handles it. Design-fixable remains a coarse label. Research and judge determine semantic eligibility without reconstructing a classifier from the reason prefix.

### 3.7 Atomic publication of `ledger.good`

Every initialization, direct-hit entry, and end-of-round verdict publication in `hunt.sh` calls one helper:

1. Create a unique temporary file in `tmp/` on the same filesystem.
2. Write the complete ledger to the temporary file and validate its header and row structure.
3. Fsync the file.
4. Atomically rename it to `tmp/ledger.good`.
5. Fsync the `tmp/` directory.
6. Generate a canonical publisher receipt containing at least publisher bundle id, target SHA and row count, source authority/ref/SHA, previous receipt SHA, and any applicable run id, validated run/commit event ids, and appended-row digest. Write and fsync the receipt in a temporary file, atomically rename it to `tmp/ledger.good.commit`, then fsync `tmp/`.
7. Consumers accept a pair only when target bytes match receipt target SHA/row count and the authority chain validates. A crash after target rename and before receipt commit leaves a mismatch that the same authoritative source must republish idempotently. A new target paired with an old receipt is untrusted.

Machine-authoritative initialization sources are restricted to an exact Git blob/object or an existing valid publisher/run/commit receipt chain. Even `git show HEAD:ledger.tsv` first pins an object id and passes through the helper; direct redirection is forbidden. A live `ledger.tsv` file does not become authoritative from structure and hash alone. If no upstream chain can be established, fail closed and require the operator to run `adopt-ledger-baseline` explicitly with source, expected SHA, and authority/attestation. Under the hunt lock, that command revalidates the source through a safe FD and publishes target plus receipt. Its receipt records `authority=operator-adopt`, complete arguments, HEAD, source identity, and tool bundle. Migration cannot sign on its behalf. Readers see either an old or new complete pair with a matching authority receipt.

### 3.8 Transitional manual promotion

Before #3 is complete, human handoff to the main loop uses only the `promote` administrative command:

1. Validate the final marker, three evidence sections, origin snapshot object and row fingerprint, and complete artifact DAG.
2. Recheck under `PROGRAM.md:12` that the original parent remains low-overlap with an experiment-design, fixable failure and that `origin_lineage_key` is unconsumed. A novelty-capped or occupied parent cannot be promoted.
3. Under the short `reentry-reservation` lock, `promote` confirms there is no Path A claim or promotion, then writes a durable `pending` reservation for `origin_lineage_key`, allocates `promotion_id`, and binds candidate and round to a promotion context. The context includes verified `N>=2` as `expected_reviewers=N` and a complete policy/confinement bundle no weaker than the §3.1 PROGRAM floors. The candidate block records evolution parent, origin lineage, and concrete delta. The main loop reruns candidate generation or transform when applicable, prior-work search, and N-reviewer evaluation; old votes are never inherited.
4. Promotion context suppresses ordinary automatic publication on the Strong Accept path. Before the formal ledger write, durable promotion staging seals `precommit.json`. It contains origin snapshot id, row bytes, stable and lineage ids; complete sidecar final plus manifest; candidate block and delta; run and candidate; complete installed-producer DAG; N reviewer inputs/outputs and call/process/context/session ids; confinement bundle and probe results; and deterministic predicted formal-row bytes and hash. After complete validation, the formal ledger/run commit must write exactly those predicted bytes and set the reservation to `committed`. Compare the actual committed row, location, and commit id with the prediction, then seal tracked `attestations/promotions/<promotion_id>.json`. The final attestation references immutable precommit SHA and actual row/commit, using canonical JSON plus SHA.
5. A helper holding the main-loop write lock idempotently inserts one unique row in `promoted.tsv`. The fixed header is `promotion_id<TAB>date<TAB>local_key<TAB>origin_lineage_key<TAB>origin_stable_id<TAB>origin_snapshot_sha<TAB>origin_row_number<TAB>origin_row_sha<TAB>final_artifact_sha<TAB>run_id<TAB>candidate_id<TAB>committed_row_number<TAB>committed_row_sha<TAB>attestation_path<TAB>attestation_sha`. Snapshot SHA is not part of uniqueness. `promotion_id`, `origin_lineage_key`, `origin_stable_id`, and `candidate_id` are independently UNIQUE.
6. `publish.sh promote <promotion_id>` uses at-least-once invocation and exactly-once logical publication. It pins branch/ref, base, content digest, and PR identity, then commits `ledger.tsv`, `promoted.tsv`, the corresponding attestation, and an optional `ideas/` report. Promotion staging records every attempt in `publication.jsonl` as `prepared -> started(exec handshake) -> completed(remote postcondition verified)`. Resume queries local and remote branch, commit, and PR by promotion_id before running. It repairs completed when the digest matches, fails closed on mismatch, and retries an unfinished publication with the same promotion_id. The publication backend enforces at most one logical PR through promotion_id/idempotency key or an equivalent unique head ref. CI recomputes final/manifest, candidate/delta, formal row, stable id, and lineage key from tracked attestation without relying on ignored sidecar or run files. Ordinary report and weekly modes cannot include receipts or attestations.

The Path A allocator, near-SA pruning, and generation-input preparation in `hunt.sh` share the reservation helper. Ordinary Path A also atomically writes a lineage claim before releasing the short lock; it cannot merely check first and let an agent choose later. A claim becomes consumed with the formal ledger commit and can be released only through an explicit safe abort before commit. Promotion `pending` occupies the lineage until resume or explicit abort. After any formal verdict commit, state becomes `committed` and story-once is consumed even when attestation or PR is unpublished. Merging the receipt advances state to `published`, and `promoted.tsv` blocks the lineage permanently. Missing or conflicting state fails closed globally; agent self-discipline is insufficient.

Although manual promotion precedes #3, formal calls must implement the equivalent of §5.5 boundaries. Generate/transform agent producers, research, and N reviewers use registered outer sandbox/container profiles. The real repository, other keys, sibling seats, home/config, and prior or later reviewer output are OS read-denied. Each seat's mirror contains only frozen candidate, prior work, and policy, and output is collected through the §3.5 dirfd/FD contract. Every role and seat has independent `call_id`, `process_instance_id`, `context_id`, and `session_or_resume_lineage`. `REV_STAGGER_SEC` changes scheduling only, never visible input. Before formal commit, the sealed precommit DAG revalidates exact N, all inputs, confinement, producer independence, and predicted row. If any capability predicate is absent, `promote` returns `exit 2` before reservation and never falls back to an unconfined `REV_CMD` in the repository cwd.

A crash before formal commit writes no receipt and therefore creates no false consumption; sealed precommit may be reused idempotently. A crash after commit at final-attestation, receipt, or publication-attempt stages resumes the same promotion_id from staging after requiring actual row bytes and hash to equal the precommit prediction. It does not rerun the candidate. Publication may be invoked again idempotently; a mismatch fails closed. Publication failure blocks a new promotion for the lineage. Only a receipt whose formal row and final attestation are verifiable in the same controlled Git history proves cross-installation consumption for #3. Canonically equal stories in different rows, different append-only snapshots, or manually aliased to one lineage still permit only one promotion. An ambiguous mapping stops instead of silently creating a new lineage.

## 4. Legacy Migration

The current migration fixture contains 89 legacy terminal artifacts and 35 keys in revision; one revision key also has mixed legacy `.badN` artifacts. These counts apply only to acceptance of the current fixture. The frozen snapshot and sealed plan define the actual action set.

### 4.1 Root ownership

Before touching locks or data roots, `migrate-legacy` validates an independent `MIGRATION_RELEASE` manifest. It pins entry, lock, durability, atomic-ledger-publisher, planner, selector, action, recovery, identity/CAS helpers, and every schema into a read-only content-addressed bundle. `migration_bundle_id` is the canonical digest of the manifest and all bytes. The manifest also pins the upgraded `hunt.sh` hash: its lock acquisition fails closed on a non-directory or unknown owner, and its publisher uses the §3.7 helper. Migration cannot start when an on-disk component mismatches. A thin wrapper then only execs that bundle and cannot load helpers from the worktree.

The exact bundle acquires hunt -> legacy sidecar -> new sidecar locks exclusively in global order and retains all three through `DONE`. Directory locks from old `hunt.sh` or `agy-side.sh` conflict with new symlink acquisition. A live owner causes exit; stale or unknown formats use the §3.1 recovery rule. Migration cannot hold only the new lock while a plain-`cp` writer updates `ledger.good`. After all locks are acquired and before any old/new root or ledger-baseline mutation, write durable sibling intent `tmp/awr-migration-v1.bundle.json` with bundle id, manifest digest, schema, and created event, then fsync the file and `tmp/`. Existing intent is accepted only on exact match.

Resume reads the intent and epoch bundle receipt, then execs the same preserved read-only bundle. A current wrapper or release B cannot use B's planner or action code to continue an epoch from A. If the original bundle is unreadable or its digest differs, fail closed before the next source mutation or event. A current release may accept `DONE` produced by A through its trusted-predecessor allowlist, but A remains the producer of every action and event. If old `agy-side.sh` still holds the old lock, exit immediately; never move a directory being written while holding only the new lock.

Before freezing, prove that the ledger publisher has been upgraded. `tmp/ledger.good` requires a durable commit receipt produced under the hunt lock by the atomic publisher in the bundle. The receipt includes publisher bundle id, source authority/ref/SHA, previous receipt, target SHA, and row count. A legacy plain-copy file without a receipt cannot be adopted directly. Migration also cannot reseed merely from the structure or hash of live `ledger.tsv`. A machine-verifiable Git object or complete hunt run/commit receipt chain may be rebuilt through the new helper; every other case fails closed and first requires independent §3.7 `adopt-ledger-baseline --expect-sha ...`. Migration intent references an existing authoritative adoption or publisher receipt and never signs upstream trust itself.

Root handling under the locks:

- Old root exists and new root does not: verify both paths are local real directories, not symlinks, then atomically rename old -> new on the same filesystem and fsync parent `tmp/`. If killed after rename, resume from the new root.
- Old and new roots both exist: move no objects before planning. After freezing the snapshot, construct a logical merged inventory from both trees. Every old object first receives a sealed `import-old-object` transport action. If the target is absent, move it atomically; if target SHA is identical, delete the duplicate source; if target SHA differs, fail closed before sealing and report the conflict. At seal time every transport action also gets a dependent freeze/archive/reset/adopt or other semantic action. Only after all semantic and import actions complete may an independent `remove-old-root` action delete the empty old root.
- Inventory and open use root dirfd plus `openat(O_NOFOLLOW|O_NONBLOCK)` and hash through the same FD. Accept only expected-uid regular files with `nlink=1` and controlled real directories. Fail closed on symlinks, hardlinks, FIFO/device/socket, paths containing `..` or control characters, normalized paths escaping old/new/migration roots, or an inode replacement between scan and open.
- Neither the main loop nor dormant mode performs a compatibility rename. A trusted fresh-install bootstrap first confirms the old root is absent.

The current top-level unlocked `agy-side -> awr-side` rename in `awr-side.sh` must be removed.

### 4.2 Frozen inputs and sealed plan

The epoch resides at `tmp/awr-side/migrations/v1/`:

```text
MIGRATION-BUNDLE.json # migration_bundle_id + manifest digest
ledger.snap           # sole ledger input for decisions
plan.jsonl            # immutable canonical JSON action objects
events.jsonl          # canonical JSON append-only action lifecycle
archive/              # legacy originals and feedback archive
DONE                  # bundle id + plan digest + done-all summary
```

Execution order:

1. After an old-only root is atomically renamed, or while both trees remain stable under all three locks and before merge, atomically copy or validate sibling intent into epoch `MIGRATION-BUNDLE.json` and fsync it. Then require `tmp/ledger.good` and its atomic publisher receipt. Header, column structure, receipt target SHA/row count, source authority chain, and actual bytes must match. Atomically copy those bytes to `ledger.snap` under durability invariants 9–10 from §2, recording row count and SHA, and register the same bytes as an immutable snapshot artifact id. Snapshot metadata also references publisher/adoption receipt SHA. The bundle receipt establishes epoch producer identity; the ledger snapshot is the decision input. Any missing or invalid element returns `exit 2`, without falling back to an unreceipted live file. Later adopt and task semantic actions use that snapshot artifact plus origin row as DAG inputs.
2. Enumerate actions only from `ledger.snap` and the locked file trees. Planning and resume cannot read live `ledger.good`.
3. Write all actions to `plan.jsonl.tmp`, fsync it, atomically rename to `plan.jsonl`, and fsync the epoch directory. Compute object count and complete file digest, then append and fsync a `plan-sealed` event containing `migration_bundle_id` in `events.jsonl`.
4. Discard a crash-left `plan.jsonl.tmp`. If `ledger.snap` exists without a sealed plan, rebuild the plan from the same snapshot.
5. If a crash occurs after plan rename and before `plan-sealed`, resume validates plan integrity and frozen inventory before appending the same deterministic event. An event with a missing plan directory entry violates durability and fails closed.
6. Once sealed, `plan.jsonl` never gains rows. A missing snapshot, mismatched plan digest, or unknown legacy object outside the sealed plan fails closed.

Every line is a canonical JSON object containing at least:

```text
migration_bundle_id, epoch_id, action_id, key, action, source_path, source_sha256, target_path, depends_on, eligibility_snapshot
```

Paths use JSON strings, but input rejects NUL, TAB, LF, CR, and every other control character directly. Normalized paths remain inside their allowed root. Action and event hashes use canonical length-prefixed bytes rather than ambiguous delimited strings.

Sort inventory relative paths by unsigned UTF-8 bytes under `LC_ALL=C`, then number them. The tree digest and `epoch_id = sha256(length-prefixed(schema_version, migration_bundle_id, ledger_snapshot_sha, inventory_digest))` are deterministically reproducible under fixed canonical JSON rules. Compute `action_id` from the canonical length-prefixed action specification excluding itself: schema version, migration_bundle_id, epoch_id, action, key, stable inventory ordinal, normalized source path, source SHA, logical target kind, and eligibility digest. First compute `action_id = sha256(spec)`, then derive the concrete archive or target path from logical target kind plus action_id. The plan row seals spec digest, action_id, target path, and dependencies. Concrete target path cannot feed back into id calculation. One key may have multiple rows. For example, `6afd532f9125` has both `reset-feedback` and `archive-legacy-bad`; their action_id values differ and both must complete independently.

Action types:

- `archive-terminal-for-rejudge`
- `freeze-terminal`
- `reset-feedback`
- `archive-legacy-bad`
- `archive-legacy-log`
- `remove-legacy-judge`
- `adopt-current-artifact`
- `import-old-object`
- `remove-old-root`
- `move-side-log`
- `remove-legacy-ledger-snap`

Every old/new outdir object is explained exactly once by a semantic action source or postcondition. `import-old-object` handles transport only and cannot establish live queue semantics. Every imported object that will be moved or rewritten names exactly one consuming semantic successor in the plan; other dependents may only read it. The transport target retains exact SHA until the consumer begins. Before its first mutation, the consumer validates the target through the same safe FD, then appends and fsyncs an `input-accepted` event binding import action_id and source SHA to consumer action_id and input digest. Mutation begins only after this durable handoff. The optional transport and successor semantic actions have fixed `depends_on` edges and execute as a DAG. Even unchanged draft/task artifacts receive `adopt-current-artifact` to fix migration-time path and SHA and create a current pointer with producer=`migration:<migration_bundle_id>:<action_id>` and trust=`legacy-untrusted`; a temporary broad allowlist cannot bypass sealed inventory. Adoption is a one-time migration receipt. Later content changes use the runtime artifact DAG and need not preserve migration-time SHA forever. Terminal binning reuses the §3.6 selector over `ledger.snap`: aggregate all sources by lineage, then require the winning row to satisfy `source=hunt && verdict=accept-w-rev && overlap=low`. In-pool terminal artifacts are archived or reset for trusted reevaluation. Out-of-pool terminal artifacts freeze as `legacy_status: frozen-out-of-pool`. Old agy ready/not-ready status does not affect binning.

### 4.3 Action-level recovery

The state key in `events.jsonl` is `action_id`, not key. Every event carries `migration_bundle_id` matching the epoch receipt and plan. The latest valid event for each action advances independently through `planned -> working -> done`. Intermediate events may represent finer steps, but `done` is appended and fsynced only after every action postcondition holds. Events are canonical JSON with deterministic `event_id=sha256(length-prefixed(migration_bundle_id,action_id,state,postcondition_digest))` and a line checksum. Reappending the same event id after a crash is idempotent; conflicting content fails closed. Exactly one corrupt crash-truncated record is permitted at the file tail and is truncated under the lock. Any corrupt interior line stops recovery.

General rules:

- When a source exists, validate its SHA against the sealed row; mismatch stops immediately.
- Create a target through temporary file, fsync, and atomic rename, then fsync the target parent before any source deletion or done event.
- When a target already exists, validate format, action_id/source SHA, and content digest. Mismatch stops. For an ordinary immutable `import-old-object` payload, if the planned target is a non-symlink regular file whose SHA exactly matches source and plan, the payload need not be rewritten to add metadata. Plan plus checksum event serves as the transport receipt. Delete the matching duplicate source and fsync its parent before done. After transport done and before consumer handoff, the target must still match exactly. After durable `input-accepted`, use a phase-aware postcondition: the consumer event must bind import and source SHA exactly, and the consuming successor's current postcondition must hold; the legally consumed intermediate target need no longer exist.
- Source deletion, archival, or deterministic rewrite is part of the postcondition. Fsync the source parent after delete or rename. An existing target cannot justify early done.
- A replay checks postconditions first and resumes from the missing step. Seeing a target does not imply completion.

`freeze-terminal` is complete only when both conditions hold:

1. `<key>.legacy-frozen.md` exists with correct metadata and digest.
2. Original `<key>.md` does not exist.

Only then may the action append its done event.

If a crash occurs after target rename and before source deletion, resume validates the target, deletes the source only if it still matches sealed SHA, then records done. If target and source both exist, it cannot mark done directly.

`archive-terminal-for-rejudge` uses an archive path containing action_id. Completion requires correct archive digest, absence of original `<key>.md`, and separate completion of old judge and feedback reset actions. A new terminal artifact with a trusted manifest cannot be mistaken for a legacy source; sealed source SHA distinguishes it.

`reset-feedback` archives the full original `## Reviewer Feedback`, rewrites the task through a temporary file, and atomically renames it; the draft remains. `archive-legacy-bad` moves old mixed `.badN` artifacts out of the outdir so they do not enter new research or judge counters.

### 4.4 `done-all` and startup release

Only when every condition below holds at migration commit may the system append and fsync `done-all`, then atomically write `DONE` with the durable helper and fsync the epoch directory:

1. The `plan.jsonl` digest matches `plan-sealed`.
2. Every action_id in the sealed plan has at least one idempotent done event with matching checksum and postcondition and no conflicting event.
3. Every action postcondition passes a fresh check. Import transport uses the phase-aware handoff rule above and does not require an intermediate target to remain exact after a semantic successor legally rewrites or archives it.
4. The old root is absent.
5. The queue outdir has no legacy or housekeeping file unexplained by the sealed plan, and every import transport has a dependent semantic action at its live terminal postcondition.
6. `DONE` records migration bundle id and manifest digest, plan digest, expected action count, done action count, and snapshot SHA. No event from a different producer id is accepted.

`DONE` is an immutable receipt for migration time, not a permanent SHA snapshot of the live queue. A repeat migration first execs the exactly pinned bundle from intent or receipt, then validates producer id across plan/events/DONE, immutable archive digest, and absence of the old root. Normal-loop successor events govern task/draft/final/frozen states after migration, so rerun does not demand that an old live target retain its SHA or continue to exist and does not replay completed source actions. Partial completion of actions for one key cannot produce `done-all`.

### 4.5 Conditional unfreeze

`frozen-out-of-pool` is not a scientific terminal state. When the trusted loop applies the same §3.6 all-source, latest-wins selector to the latest atomic `ledger.good` and finds that the winning row has regained pool eligibility, it performs this transition under the shared locks:

1. Archive and clear old task feedback, atomically rewrite the task, and record the task transform; retain the draft.
2. Append and fsync an `unfreeze-prepared` runtime event fixing frozen action_id, marker SHA, event-addressed target under `runtime-archive/unfreeze/`, and new task artifact id.
3. After confirming prepared inputs still match, atomically rename `.legacy-frozen.md` to that runtime-archive target and fsync both parents. Removing the marker is the commit point.
4. Append and fsync deterministic `unfreeze-committed`.
5. After a crash, continue idempotently from the prepared event when the source marker remains. If source is absent and archive plus task transform match, repair the committed event. Every other source/archive combination stops. The runtime validator finishes this recovery before interpreting live state and does not require the migration frozen marker to exist forever.

## 5. Automatic Re-entry Bridge (Storage Milestone #3)

### 5.1 Minimal schema

| Table | Critical constraints | Responsibility |
|---|---|---|
| `lineages` | `lineage_key PK NOT NULL`; `root_candidate_id UNIQUE NOT NULL`; deferred FK/commit constraint requires the root candidate to belong to the same lineage | Immutable lineage identity; one deterministic root candidate per lineage |
| `story_aliases` | Canonical version/hash/bytes and lineage are `NOT NULL`; `UNIQUE(canonical_version, canonical_hash)` | Each historical or revised story belongs to one lineage |
| `candidates` | Candidate id, lineage, and policy fields are `NOT NULL`; `origin_stable_id UNIQUE NULL`; `expected_reviewer_count CHECK(N>=2)` | One record per historical row or new commit; claim creates a placeholder with independent N-reviewer configuration frozen |
| `reentry_grants` | Identity, FK, path, gate, evidence, rule, priority, and state are `NOT NULL`; deterministic id and fact are `UNIQUE` | Independent Path A/B eligibility evidence |
| `reentry_requests` | Lineage, state, and generation are `NOT NULL`, with lineage `UNIQUE`; claimed fields are collectively NULL or NOT NULL according to state | Readiness, story-once, and claim state without copying grant facts |
| `round_slots` | `round_id NOT NULL UNIQUE`; `slot_kind NOT NULL CHECK(slot_kind='reentry')`; state `NOT NULL`; binding fields constrained collectively by state | One shared commit opportunity per round across all three re-entry kinds |
| `reviews` | Candidate, slot, producer call, artifact, and policy refs are `NOT NULL`; two composite `UNIQUE` constraints | Formal review votes with independent producers; one batch call may serve different candidates |
| `artifacts`/`invocations` | Artifact/call id, type, state, content, and provenance refs are `NOT NULL` with idempotency keys | Auditable inputs, outputs, and provenance |
| `import_epochs` | Epoch id, plan SHA, input manifest SHA, and state are `NOT NULL`; plan SHA is `UNIQUE` | Sealed historical union plan and single-transaction import receipt |
| `materialization_outbox` | Candidate, payload version and hash, state, generation, and projection sequence are `NOT NULL`; candidate is `UNIQUE`; processing token/lease constrained by state | Fenced, idempotent file effects for new bridge commits only |

Except for explicitly nullable `origin_stable_id` and fields outside claimed/processing states, every identity, FK, state, slot, review, and outbox binding column is explicitly `NOT NULL`; the design does not rely on SQLite's permissive NULL behavior in `UNIQUE` or `CHECK`. State-dependent `CHECK` constraints require the complete binding/token/lease group to be non-null in claimed or processing state. Other states either clear the group or retain only immutable audit fields as specified. `slot_kind=NULL`, `round_id=NULL`, and NULL review slot or call are rejected. Every DB connection executes and validates `PRAGMA foreign_keys=ON`, and startup runs `foreign_key_check` plus schema SQL/hash validation. A disabled pragma or constraint/schema drift fails closed before any transaction.

Ordinary ledger import cannot write provisional lineages one row at a time. The importer first completes the sealed equivalence and union plan in §5.2, then writes `lineages`, `story_aliases`, `candidates`, and any historical-committed requests needed for story-once in one transaction. An unconsumed ordinary identity import creates no request or grant and cannot initialize as ready. Every historical canonical story registers only to the lineage selected by the plan. Equal canonical hashes with unequal canonical bytes are a hash collision and fail closed.

An eligibility-gate transaction writes a grant with evidence and rule version, then derives request state from active grants for that lineage:

- If no request exists and at least one active grant exists, create `state=ready`.
- If the request is inactive and an active grant reappears, conditionally update it to ready.
- If the request is claimed or committed, create no second row. §5.3 defines revocation of a claimed grant.

A consumed historical lineage imports as a request with `state=committed, commit_kind=historical-import` and no materialization outbox. It blocks story-once but does not represent a new bridge effect.

### 5.2 Lineage import

The orchestrator creates immutable `lineage_key` at the first ledger commit for new data. Evolution, recheck, and re-entry copy it; no revision recalculates it from rewritten story text. Every new submission gets a new `candidate_id`.

Historical TSV canonicalization is fixed: Unicode NFC, trim, collapse internal consecutive whitespace to one space, normalize line endings, and preserve every punctuation mark and quotation mark. NFKC is excluded because compatibility folding can merge distinct propositions. Each historical data row first obtains `origin_stable_id` from the same §3.6 helper and stores it on the corresponding candidate. Import candidate id is generated deterministically with domain-tagged, length-prefixed `sha256("candidate-import-v1", origin_stable_id)`. Snapshot SHA remains provenance and does not enter row identity, so appending a row leaves existing row ids unchanged. Import retry reads the existing candidate by ledger instance, row number, and raw SHA. A change or insertion at an existing location violates append-only semantics and fails closed. If canonical hashes match but bytes differ, preserve both originals for audit and fail closed; only an explicit canonical/hash schema upgrade can resolve it, never an automatic new key.

Historical relationships are resolved before any DB write:

- Create a node for every row in the frozen ledger. Exact canonical match, verified `Evolution From` parent pointers in run archives, tracked promotion attestations, and explicit manual mappings form union edges. High similarity produces mapping candidates only and cannot create an edge automatically. A missing archive or unresolvable parent pointer stops the affected node instead of silently creating a new lineage.
- Validate deterministic connected components and the parent DAG across all edges. A component with one existing lineage uses it as the sole anchor. Multiple existing lineages, a parent cycle, or conflicting mappings fail closed and cannot be silently merged by the importer. With no existing anchor, use the sole parentless ancestor as root. Among multiple parentless duplicate rows with exact canonical text, choose minimum `(row_number, origin_stable_id)`. When manual mapping combines multiple parentless nodes with different canonical text, that mapping names the root explicitly.
- Generate a new historical lineage key from root canonical bytes through the shared §3.6 `canonical_story_v1/origin_lineage_key` helper. `lineages.root_candidate_id` points to the root candidate. Multiple rows with the same canonical text retain distinct candidate and `origin_stable_id` values, while aliases map the canonical form once to the common lineage.
- Before writing the plan, persist ledger, run/archive parent-pointer inputs, promotion attestations and promoted receipt, and manual mapping/version as immutable CAS snapshots, then fsync objects and parents. The canonical input manifest references only those artifact ids and SHAs. Canonical JSON fixes input manifest, nodes, verified edges, components, root/key, consumed decision, and plan digest. Only after that immutable CAS artifact is durable may DB writes begin. Later changes to live mappings or sources do not affect resume.
- After the durable plan is conflict-free, one DB transaction insert-or-verifies `import_epochs(epoch_id,plan_sha,input_manifest_sha,state=done)`, every lineage, alias, candidate, and historical-committed request, with every result traceable to epoch and plan. Any mismatch against an existing alias or anchor rolls back the transaction. The importer cannot first create `lineage_R` and later merge R into L. A crash before DB commit replays from the sealed plan; after commit, epoch/done and every result exist together, so union results cannot exist without their decision plan.
- A lineage with at least two candidate rows is marked consumed under current story-once semantics. `promoted.tsv` and its attestation also import as consumed evidence.
- Published keys remain permanently frozen. A canonicalization-version upgrade never recalculates old keys.

The sidecar's local filename key is a path component only and does not enter DB uniqueness. Every task and final manifest carries the §3.4 origin snapshot, row, and raw SHA. Import validates the row from the origin snapshot, derives `origin_stable_id` from stable row number and raw SHA, resolves one import candidate, then reads its DB `lineage_key`. The sidecar `origin_lineage_key` is the §3.6 grouping key over the observed canonical story. The DB `lineage_key` is the component identity from the sealed union plan. They must match byte-for-byte only when that row is the component root. A child or manually merged row resolves through its origin candidate and cannot infer identity by key equality. The current ledger snapshot may append rows after the origin row; exact snapshot SHA equality with origin time is not required. For revised text `R != L`, an ambiguous fingerprint, or an origin-row mismatch, fail closed instead of canonicalizing R into a new lineage. R can only become a story alias for the origin lineage. `promoted.tsv` uses the §3.8 origin, formal-commit, and final-artifact fields rather than story text alone.

### 5.3 Grants, requests, and dynamic eligibility

```text
no active grant -> no request / inactive
active grant    -> ready --claim(bound grant)--> claimed --commit--> committed
                    ^                    |
                    |                    +--bound grant revoked and fenced-->
                    |                         ready (another grant remains) / inactive
                    +----------------active grant returns------------------+
```

The near-SA queue is a discovery hint, not a grant. Path A grants encode three structured predicates from `PROGRAM.md:12` and retain evidence candidate, ledger row, `reason_class`, attempt count, and rule/policy version:

1. `A-evolve`: latest row is `accept-w-rev && overlap=low`; the failure is a structured experiment-design class such as strong baseline, statistical power, estimand, or attribution control; it is not novelty-capped or occupied; and the lineage is unconsumed.
2. `A-recheck-awr`: latest row is accept-w-rev with a structured weak-prior-work failure; resubmit the original story, and require the lineage to be unconsumed.
3. `A-recheck-evidence`: latest row is reject with `category=evidence-incomplete`, persistent evidence proves `all_reviewers_sa_before_hard_gate=1`, and the lineage is unconsumed.

A legacy free-text reason that does not map uniquely to a structured class enters manual mapping; no agent may guess and activate it. A Path B grant comes only from a trusted `SA-possible` artifact that passes its final marker and runtime validator. In the grant transaction, revalidate that the origin lineage remains unconsumed; the latest ledger row still has `source=hunt && accept-w-rev && overlap=low` with a structured, fixable experiment-design failure; and the result is not novelty- or ceiling-capped. The grant records origin lineage, latest eligibility row, final artifact, trusted judge gate, and bridge-policy version. Every Path A/B grant derives from current latest ledger, consumption, and evidence. Any ledger append, promotion or commit, or eligibility-evidence change reevaluates the relevant gate in the same transaction and revokes it when false. Strong Accept, novelty-dead, high-overlap, or an occupied new winner revokes stale A grants. Claim and formal commit recompute the bound gate from evidence and current latest row rather than trusting `active=1`. The third parent rule must be merged into `PROGRAM.md` before enabling the automatic bridge.

`grant_id = sha256(canonical_length_prefixed("reentry-grant-v1", lineage_key, path, gate, evidence_id, rule_version))`. Every column participating in identity or uniqueness is `NOT NULL`. Eligibility transactions upsert by the unique key so retrying the same fact creates no extra row, and activation of new evidence revokes a superseded grant under the same path and gate. Only the structured gate evaluator may set active state; the allocator cannot synthesize grants. A golden fixture with different boundaries among variable-length fields must produce different ids.

Each path and gate keeps independent active/revoked state. The allocator view chooses `winner_grant = first(ORDER BY priority, grant_id)` per lineage, then orders lineages by `(winner_grant.priority, requested_at, lineage_key)`. Claim binds that exact winner `grant_id`; it cannot sort by `MIN(priority)` and then select an arbitrary active grant. Requests do not cache drifting eligibility or priority. Revoking B cannot erase valid A, and revoking A cannot erase valid B. Each claim binds one `claimed_grant_id`; another grant that did not participate in the candidate cannot replace it. Candidate or ledger append and grant updates advance request state in one transaction:

- If the revoked grant is not bound to the current claim and another active grant remains, keep the request in its current valid state and update effective priority only.
- Revoking the last grant while ready applies `ready -> inactive`.
- Revoking the grant bound to a claimed request increments `claim_generation`, clears claimed grant/token/candidate/round/lease, marks the old candidate abandoned, and marks the matching slot cancelled in the same transaction. If another active grant remains, return to ready; otherwise become inactive. The old worker is fenced immediately.
- When an inactive lineage becomes eligible again, create a new ready generation; never restore an old token.
- Committed never returns to ready.

Strong Accept, novelty-dead, and high, medium, or near-SA lineages without a complete Path A/B predicate have no active request and consume no slot.

### 5.4 Claims, leases, and slots

SQLite uses `BEGIN IMMEDIATE` to serialize allocator writers; other databases use equivalent locking. A-evolve, A-recheck, and Path B always request literal `slot_kind='reentry'`. `reentry_kind` exists only on candidate and grant and never participates in slot uniqueness. The allocator first evaluates the current round slot:

1. If state is `committed`, the round's allowance is permanently consumed, regardless of lease, and nothing else is allocated.
2. If state is `claimed` with an unexpired lease, allocate nothing else for the round.
3. If state is `claimed` with an expired lease, lock the slot and conditionally fence the original request by the slot's lineage, request generation, and token. Return that request to ready when an active grant remains or to inactive otherwise; mark the old candidate abandoned and the slot `expired`.
4. Only a `cancelled` or `expired` slot may be overwritten by the next claim. Fencing the original request and releasing the slot occur in one transaction. A slot cannot switch from A to B while preserving A's valid token. Candidate and request audit records retain the old attempt.

When a slot is available, the transaction:

1. Joins active grants and keeps only `state='ready' OR (state='claimed' AND lease_until < DB-now)`. For each lineage, choose one winner by `(priority, grant_id)`, then select a lineage by `(winner.priority, requested_at, lineage_key)` and recompute that winner's current gate inside the transaction.
2. For an expired claimed request, first mark the old candidate abandoned and increment request generation, invalidating the old token; mark any prior-round slot still matching the old token as expired. An old round slot cannot block reclaim in a new round.
3. Bind the one winner `grant_id` from the previous step and create a `candidate_id` placeholder. Freeze its lineage, grant and evidence, `reentry_kind`, origin canonical story and artifact, allowed-change rules, and round policy: verified `N>=2` satisfying every PROGRAM floor, `expected_reviewer_count=N`, exact slot set `1..N`, and reviewer configuration/policy bundle id. A runtime change to `REVIEWERS` never rewrites the candidate.
4. Increment request generation for every claim and write `state=claimed, claimed_grant_id, claim_round_id, claimed_candidate_id, claim_generation, claim_token, lease_until`.
5. Write the round slot as `state=claimed` with the same `(round_id, lineage_key, grant_id, candidate_id, request_generation, token, lease)`. Any failed conditional update or uniqueness constraint rolls back the complete transaction.

Every later write matches request lineage, candidate, round, generation, token, and state, and matches the same group on the current claimed slot. Before prior-work search and again at commit, `A-recheck-awr` and `A-recheck-evidence` require candidate canonical story to equal origin exactly; added search or evidence can change only the evidence artifact. `A-evolve` fixes parent candidate and artifact SHA, origin lineage, candidate artifact SHA, and structured delta. Every changed field in the delta records parent and candidate before/after hashes. A deterministic diff proves at least one allowed field changed; a candidate artifact identical to its parent is rejected. A Path B placeholder also binds trusted final artifact, terminal draft id/SHA referenced by its manifest, origin parent artifact, and a versioned extractor/diff; nonempty prose does not substitute for a real change. Before prior-work search, evolve and Path B check whether candidate canonical hash already maps to another lineage. That check saves calls only; the commit transaction provides the final uniqueness guarantee. Workers renew through short transactions using DB time and extend both request and slot lease only when the complete field group matches. Failure to update either row stops the worker. Reclaim or grant revocation invalidates the old worker token permanently. Path A generation fills the already claimed candidate placeholder and never creates a second candidate or story-once record.

### 5.5 Commit and materialization

The candidate pipeline first constructs a role-separated invocation DAG. Candidate source is an installed generation call or a versioned recheck/Path-B deterministic transform. Every deterministic transform continues backward to every agent producer that affected candidate content. Prior work comes from an installed `role=research` call. Every review comes from an installed `role=review` call. All generation, research, and review invocations use registered outer read/write/process confinement equivalent to §3.2 and §3.5, sanitized configuration, and FD-based safe output ingestion. Each formal reviewer mirror contains only frozen candidate, prior work, and policy; OS policy read-denies the real repository, sibling output, other artifacts, and home. The candidate's upstream agents, formal research, and all formal reviewers have pairwise-disjoint role sets, and no two seats share `call_id`, `process_instance_id`, `context_id`, or `session_or_resume_lineage`. One call or install edge cannot fill multiple roles or slots. Equal content SHA may serve different seats only when each has a distinct installed producer call.

After the frozen N seats finish, a short transaction:

- Join-validates that candidate lineage/grant equals request and slot lineage/grant/candidate/round/generation/token, with request still claimed. Recompute the relevant Path A/B gate from bound evidence plus current latest ledger and consumption, rather than checking the active bit alone. Revalidate all §5.4 story, parent/delta, and final-artifact constraints for recheck, evolve, or Path B.
- First require frozen `N>=2` and a policy bundle no weaker than PROGRAM floors. Walk backward from the candidate to validate the complete role-separated invocation DAG. Prior work and frozen slot set `1..N` are present exactly with no extra slot; every vote is contract-valid and has an installed producer whose confinement predicates passed; candidate/priorwork/policy input SHAs match; distinct producer/process/context constraints hold. Empty or single-seat sets and cardinality other than N fail closed. Write reviews and artifacts idempotently under `(candidate,slot)` and `(candidate,producer_call)` unique keys.
- Invoke a versioned deterministic aggregator inside the transaction. It first asserts `N>=2` and cardinality=N, then takes the minimum vote from frozen records and recomputes novelty, feasibility, and other hard gates. Empty or single-seat input has no identity or default verdict. Only this computed formal verdict is written; a caller-provided verdict is an optional consistency assertion.
- Atomically insert or verify `story_aliases` from candidate canonical story to the current lineage. If the unique key already points to another lineage, fail the entire commit and require manual lineage resolution; one story cannot attach to two lineages.
- Set both request and matching round slot to `committed`, and insert `materialization_outbox(candidate_id, effect_payload, state=pending)`.
- Any failed predicate produces no formal effect; external output is archived as an attempt only.

After transaction commit, a consumer uses a short transaction to claim a `pending` row or a `processing` row whose lease expired, increments generation, and writes a random token with a DB-clock lease. At any time, only the consumer holding the matching token may advance the row. Payload fixes schema version, canonical bytes and hash, and candidate id. A single-object effect uses candidate_id as the unique target key: fsync temporary file, atomically replace target, and fsync target parent. An existing identical hash still requires file revalidation and target-parent fsync; a different hash fails closed. Marking waits until a durable target is reread and hashed.

Set-valued TSV and export effects cannot use blind append. The DB maintains monotonic `projection_sequence`, incremented only by a business transaction that changes export projection, including a formal candidate or ledger commit and an applicable bulk import. Outbox claim, lease, renewal, mark, and other internal-state transactions never increment it. Export writes immutable `<projection_sequence>-<hash>` objects through temporary file, file fsync, rename, and object-parent fsync, then publishes the pointer through temporary file, file fsync, rename, and pointer-parent fsync. Every claim, steal, and pointer publication acquires the same kernel advisory export lock. The consumer builds desired `(projection_sequence,hash)` from the current complete DB projection. Under the lock it revalidates outbox token, current DB projection sequence, and desired sequence. Publish only if the pointer is absent or has a lower sequence. Exact pointer `(sequence,hash)` means the effect is already durable and permits conditional marking without another publication. Equal sequence with a different hash is a deterministic violation and fails closed. If pointer sequence is higher, discard the old snapshot and rebuild from current DB projection; mark only when the same DB proves that the newer deterministic export contains this candidate commit, otherwise fail closed. Multiple pending rows may therefore share one latest full export without remaining pending forever for lack of an extra pointer increment.

No row becomes materialized until durability succeeds for object, object parent, pointer, pointer parent, or single-object target parent as applicable. After rereading every postcondition, the consumer conditionally marks by candidate, generation, and token. A failed renewal or completion update stops that consumer. If a crash occurs after effect and before DB mark, the new claim does not change projection sequence; its token validates the same durable `(sequence,hash)` target and only repairs the mark. A historical `commit_kind=historical-import` creates no outbox and cannot be mistaken for a new bridge effect. A crash after DB commit never repeats the formal verdict. Two consumers or a crash at any materialization point still produce one candidate-keyed effect.

## 6. Delivery Order

1. Phase 0: startup truth table, ordered-lock migration gate across hunt/legacy/new, failure classification, artifact DAG and atomic final commit, runtime validator, mandatory judgment of the latest draft, mirror allowlist, and atomic `ledger.good` publication with durable receipt.
2. Phase 1: update `roles/awr.md`, `roles/awr-judge.md`, `check_draft`, and `check_judge` with Strongest Counterexample and per-section gates.
3. Legacy migration: remove the top-level `agy-side -> awr-side` rename; pin an independent migration release bundle, then execute a sealed action plan with producer id carried through intent, epoch, action, event, and `DONE`. Release the trusted loop only after migration completes.
4. Phase 2: latest-wins single-pool selector, 7/8-column compatibility, and best-effort near-SA deduplication.
5. Operations entry points: update `README.md` and the `awr-side.sh` header comment in the same commit. Every trusted example sets both `SIDE_JUDGE_TRUSTED=1` and explicit `SIDE_JUDGE_CMD`. Grok is not listed as a trusted judge until OS-level side-effect confinement passes acceptance. The manual path delivers the `promote` administrative command, `publish.sh promote`, path guards, and CI receipt validation together.
6. Storage #3: implement lineage/grant/request/outbox schema and TSV import before claim-bound candidates and slots, formal-review commit, and materialization.

Phase 0, Phase 1, migration, and Phase 2 are implementation and test slices, not independently enabled production states. Until compatible versions of entry point, outer profiles, roles and parsers, identity/CAS, provenance and current pointers, migration, and selector all pass and publish atomically under one `P1_RELEASE` manifest, the trusted path always returns `exit 2` before calling an agent or creating a terminal artifact. `migrate-legacy` may recover independently but does not unlock the loop. Startup compares actual component hashes and schema versions exactly with the release manifest and pins the complete read-only release bundle. Every queue scan and call preflight revalidates that bundle and process pin. Any mixed, old, mutated, or missing component fails closed, and a release upgrade requires restart. An example setting only the two judge variables is valid only after the `agy-builtin` research outer profile is registered and passes its probe; otherwise it also sets a valid `SIDE_RESEARCH_CMD`.

Phases 0–2 do not change the human-only protocols in `PROGRAM.md` or `brainstorming_policy.md`. When the automatic bridge ships, add re-entry of revised artifacts at the same time: it shares one request and slot with evolution and recheck, permits at most one use per lineage, inherits no old votes, and reruns prior-work search plus the frozen N-reviewer evaluation.

## 7. Acceptance Matrix

### P1

- The startup matrix covers dormant, one-variable misconfiguration, invalid backend, trusted, and migrate. Dormant produces identical before/after hashes for the queue tree and 0 fake-agent calls. Trusted and promote reject `REVIEWERS=0/1`, negative values, non-integers, out-of-range values, `PRIOR_MIN_LINKS<5`, `PRIOR_MIN_API<1`, `MIN_READ<5`, and `AXIOM_MIN_CRACKS<2` with `exit 2` before any agent call or reservation. Mixed Phase 0/1/2 components, old role with new parser, new role with old parser, and an unregistered research profile all fail before agent invocation. A trusted probe must fail to read absolute paths to the real repository, other keys, or home; fail to write outside staging or invoke git/gh; and prove that Codex or Claude user config, hooks, plugins, and MCP did not load.
- Migration refuses while a live hunt or legacy process holds its lock. Only `migrate-legacy`, holding hunt/old/new locks exclusively in fixed order, may inspect baseline or move directories. Trusted mode holds legacy inhibit plus new lock. Empty installation releases those locks and enters three-lock migration bootstrap. In a fixture where an old process tries to acquire the old lock or create the root between absent-check and empty `DONE`, three-lock revalidation must detect and reject it or the inhibit lock must prevent it; a false expected=0 receipt cannot seal. When migration owns hunt through a compatible owner-directory symlink, old hunt acquisition must observe the live pid, exit, and leave the lock linked. Migration first refuses when on-disk `hunt.sh` does not match the cutover hash. A legacy plain-`cp` writer paused on a valid row boundary prevents hunt-lock acquisition; if that writer crashes leaving a valid truncated prefix without a receipt, migration still refuses. In a fixture where an agent stage writes a structurally valid fake row to live `ledger.tsv` then receives SIGKILL, migration cannot reseed automatically. Only verifiable Git/run authority or an independently explicit operator adoption with exact-SHA attestation establishes baseline. If release A seals or partially executes actions and crashes before wrapper upgrades to B, resume re-execs preserved migration bundle A and keeps A as producer id for all plan/events/DONE. A missing or changed A bundle or a current P1 release that distrusts A's receipt fails before exec or source mutation; B cannot mix writes. With both old and new roots, every non-conflicting object receives a sealed import action and a differing-SHA collision fails closed. Hardlink to another key or ledger, symlink, FIFO/device, and scan/open swap fixtures all fail before planning.
- Lock acquisition recovers from kill before or after the symlink syscall without an empty receipt. A stale receipt is cleaned only after pid plus start-token validation. An unknown legacy directory uses explicit recover-lock only; no fixture can produce two owners. `reopen` works without judge environment, holds the new lock, archives the selected seat, and makes 0 fake-agent calls. An unknown legacy lock can be followed by ordinary migration acquisition only after exact digest plus explicit stale confirmation produce quarantine and receipt under the recovery guard. A live or remotely unverifiable owner is rejected. `promote` cannot fall into dormant mode.
- Fixtures reject an `SA-possible` artifact missing Strongest Counterexample, enough nearest neighbors, API query, non-bare endpoint, nonempty and non-placeholder query, sufficient Papers Read, nonempty feasibility fields, nonempty Clear-Accept section, consistent shape, correctly formatted crack evidence, or terminal `AGY-DONE`. They also reject a negative artifact missing `- Defect:`. A valid shape plus `- Defect:` and `AGY-DONE` without the three positive sections must classify as content rejection and may feed back. When tightened to `PRIOR_MIN_LINKS=7`, `PRIOR_MIN_API=2`, `MIN_READ=10`, and `AXIOM_MIN_CRACKS=3`, the parser uses frozen effective values and separately rejects evidence counts 6/1/9/2.
- Provenance crash fixtures cover kill after prepared, after started, after completed but before install, after current-pointer rename but before installed, and after final rename but before marker. A new completed call whose content matches an old current cache SHA cannot repair installed unless the pointer binds the new call. Output-ingestion probes cover symlink to another key, hardlink, FIFO/device, oversized file, and lstat/open swap; none may be read or installed. The manifest attributes only installed calls and transforms on the complete artifact DAG. A nonempty terminal without a marker is rejected.
- If one release component such as role, parser, policy, adapter, or outer profile changes after startup, the next queue scan or call can only continue from pinned old bytes or reject before exec. A mixed release cannot be ordinary drift; upgrade requires a new process. When external model, effective configuration, or execution identity changes across calls, the manifest lists actual invocation-bundle SHAs separately and sets drift. Worktree changes at finalization do not change the manifest. Any unfixed argv, environment, settings, plugin, hook, MCP, or external CLI/package dependency tree prevents a reproducible-configuration claim. A fixture that only observes an unfrozen Node or Python package after exec must record false.
- Infrastructure failures do not increment bad; contract failures increment only the corresponding seat; manual reopen archives bad before marker; only content rejection increments rounds.
- With rounds=3, the latest draft is still judged, and judge input draft SHA equals finalization draft SHA.
- At every `ledger.good` publication point, a concurrent reader sees a complete old or complete new version.
- `origin-row-v2` golden fixtures cover header, LF and CRLF, old-row identity after append, changed snapshot SHA, and duplicate raw rows. P1 task/receipt and #3 importer derive the same `origin_stable_id` for one data row.
- Selector aggregates all sources per lineage before applying latest-wins. When an old hunt AwR row is followed by weekly SA or reject, the lineage does not enter the pool again until a later valid hunt row becomes winner.
- Durable-directory fixtures inject crashes around every mkdir and parent fsync level for epoch, archive, CAS, and provenance; the complete target path is reachable before any source mutation. Migration recovers from crashes at snapshot, plan build, plan seal, target rename, target-parent fsync, source delete, source-parent fsync, and event append. A both-root fixture proves that an imported terminal still receives a semantic action. Unfreeze recovers from crashes at task rewrite, prepared event, marker rename, parent fsync, and committed event. Tail truncation and replay of one event id are idempotent, and the second complete run performs zero migration actions.
- When target exists and legacy source remains, resume deletes or reconciles the source before marking done. After durable handoff, imported tasks rewritten by `reset-feedback` and imported terminals archived or frozen allow done-all through `input-accepted` bound to transport/source SHA plus successor postcondition; disappearance of a legally consumed intermediate target cannot block completion.
- Completion of one of two sealed actions for the same key cannot write `done-all`. `DONE` appears only after every action_id completes and every postcondition passes.
- A canonical action-spec golden fixture produces a stable action_id first and then derives the archive path containing that id. Repeated planning is byte-identical and has no target-path/id self-reference. A legacy filename containing TAB, LF, or another control character fails safely before planning and cannot inject another plan or event object. Migration rerun after unfreeze leaves the epoch archive digest unchanged; only a matching event validates runtime archive.
- An unknown file outside the sealed plan, snapshot or digest mismatch, or source-SHA drift blocks `DONE`. After `DONE`, every added or changed queue object requires a valid runtime successor and provenance; `DONE` alone grants no trust.
- Every latest-wins in-pool terminal in the current fixture enters trusted reevaluation. Every out-of-pool terminal has one `legacy_status` marker. Every task in revision has old feedback reset. Old bad artifacts never enter new counters.
- Every P1 `SA-possible` artifact passes `check_judge`, has a trusted-configuration manifest and producer chain, and supports manual reevaluation. Its count is not a success metric.
- Promotion context blocks ordinary main-loop automatic publication. Under pending reservation, formal-committed-but-unpublished, and published-receipt states, ordinary Path A cannot select the lineage again; only explicit abort before commit releases it. A crash before formal commit creates no consumed receipt. Crashes after commit during attestation, receipt, or publication replay the same promotion_id, ultimately producing one logical branch, commit, and PR effect matching the content digest, with publication journal completed. Promotion generation, research, and N-reviewer mirrors remain unable to read absolute paths to real repository, sibling seats, or earlier-seat output when `REV_STAGGER_SEC>0`. Reused call/process/context/resume lineage or an unconfined `REV_CMD` fails before formal commit. The final PR persists formal ledger row, the `promoted.tsv` receipt unique by origin_lineage_key, and an independently hash-verifiable attestation together. Equal canonical stories in different rows or snapshots cannot each be promoted. Ordinary publication modes cannot include receipts or attestations.

### Automatic bridge

- Import of ordinary SA, novelty-dead, and high, medium, or near-SA without a complete structured predicate creates lineage and candidate only, with no grant or ready request. Each of the three Path A gates has positive and negative fixtures.
- If A and B are active together, revoking the path not bound to the claim preserves the other. Revoking the currently bound grant atomically fences the claimed token and moves request to ready or inactive according to remaining active grants. Regaining eligibility creates a new generation only. With multiple grants on one lineage, `(priority,grant_id)` fixes the winner; concurrent reruns of equal-priority A and B still bind the same grant and candidate policy.
- If an append changes the latest winner after A-evolve or A-recheck activation to Strong Accept, novelty-dead, high-overlap, or otherwise outside the structured gate, the append transaction revokes stale A and fences any bound claim; the old worker produces zero effect at formal commit. Path B uses the same dynamic revalidation.
- When a worker crashes after claiming in an old round, a new round can select the expired claim with a new generation and token. A commit using the old token changes 0 rows.
- Concurrent evolve, recheck, and Path B in one round compete for one `reentry` row. Moving an expired slot from A to B fences A in the same transaction. A committed slot cannot be allocated again after lease time, so serial or concurrent execution commits at most once per round.
- Commit produces zero effect if candidate belongs to the wrong lineage; recheck rewrites origin story; evolve or Path B artifact has no real change or mismatched delta/diff; Path B mismatches trusted final; or any candidate/token/generation/round/slot, role DAG, confinement, producer independence, reviewer slot, input SHA, or caller-verdict validation fails. Fixtures fail when one review/call/install edge fills multiple seats, when candidate producer and formal research or reviewer share a resume lineage, when a reviewer reads sibling output or real repository by absolute path, or when execution falls back to unconfined `REV_CMD`. Candidate, schema, and aggregator reject N=0/1 or policy below PROGRAM floors. With N=5, having only slots 1–3, missing slots 4/5, or changing the environment back to N=3 during the run cannot commit.
- Kill after DB commit but before file write: restart replays outbox without repeating verdict. A historical-import committed request creates no outbox.
- In fixtures with two concurrent outbox consumers, kill after claim, kill after effect rename, expired-lease takeover, and stale-token completion, an existing identical-hash target repairs only the mark and a different-hash target fails closed. Crashes are injected before and after every fsync of object file, object parent, pointer file, pointer parent, and single-object target parent; DB marking occurs only after all durable postconditions. Set export also pauses old consumer after building S, lets a new consumer publish S+1, then resumes the old consumer. Kernel lock plus token and projection-sequence recheck must block old pointer rename, so TSV never regresses or duplicates a row. With A(seq1) and B(seq2) both pending, A may publish the current seq2/hash and mark A; B then recognizes the same `(seq2,hash)` as complete and marks B. One full export can materialize all merged pending rows. Reclaim, renew, and mark after a post-effect crash never increments projection sequence; it validates the same pointer and repairs the mark.
- Historical recheck, evolution parent pointers, sidecar origin fingerprints, `promoted.tsv`, and manual-mapping fixtures all preserve story-once. An unproven parent/child relation fails closed. Historical L -> R belongs to one lineage through a sealed union plan before the first DB write. Exact-canonical duplicate rows, including a real ledger duplicate, create multiple candidates with distinct `origin_stable_id` values, one deterministic root, and one lineage; import retry produces identical bytes and row counts. Crash fixtures before and after DB commit, after all input snapshots and plan CAS become durable, resume only from the original epoch plan. Later changes to external mappings do not alter results. Epoch, results, and done are either all absent or all present from one transaction.
- If a Path B or evolved revision canonicalizes to a story already owned by another lineage, or two lineages concurrently generate the same new story, at most one alias and commit succeeds. The other produces zero formal effect.
