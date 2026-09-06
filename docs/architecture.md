# Architecture

## Control Plane

[`PROGRAM.md`](../PROGRAM.md), [`brainstorming_policy.md`](../brainstorming_policy.md), [`rubric.md`](../rubric.md), and `roles/*.md` define the human-owned protocol. Backend processes produce bounded artifacts. `hunt.sh` remains the decision authority for selection, external evidence gates, vote aggregation, database commits, archives, and publication.

```text
optional canonical direction snapshot
  -> policy + SQLite history + generation brief
  -> internal generation (portable)
  -> immutable candidate batch
  -> directed selector classification and all-in-scope gate, when configured
  -> model-free history retrieval
  -> undirected selector ranking or reuse of directed ranking, then prescreen
  -> internal history comparison (portable)
  -> eligible external prior-work research
  -> independent reviews (portable)
  -> atomic DB commit
  -> replayable TSV projection
  -> decision archive
  -> accepted report + publication
```

## History authority

`.ai-ideas/history.sqlite3` is the sole structured history authority. `ledger.tsv` and `tmp/ledger.good` are replayable projections of one immutable database snapshot. Search indexes are rebuildable projections fed by `search_projection_outbox`. Canonical near-SA priority lives in `near_sa_observations`; `tmp/near-sa-queue.tsv` is a disposable compatibility view.

Startup imports an operator TSV baseline only when the durable bootstrap marker is absent. Later runs reconcile both TSV targets from the database before any agent starts. Shell code never appends canonical rows and never copies one TSV over the other.

## Main Loop

| Stage | Backend responsibility | Host responsibility | Primary artifacts |
| --- | --- | --- | --- |
| Startup | None | Canonicalize optional direction first; check policy mode and calibration; bootstrap or reconcile SQLite; recover search and ledger projections; build generation brief | direction identity, `.ai-ideas/history.sqlite3`, `generation-brief.json` |
| Generation | Propose candidates under the selected divergence lens | Portable stage with sealed brief, policy, optional direction/context; validate structure | `ideas.tsv`, `ideas.md` |
| Freeze | None | Freeze candidate bytes and direction identity | batch |
| Directed gate | Rank and classify every candidate when a direction is active | Reject the complete batch unless every ordered row is valid and `in-scope`; run before history observation | `select.tsv`, `direction.tsv` |
| Observe | None | Run model-free duplicate, failure, and optional evolution retrieval | packs, traces |
| Selection and prescreen | Rank undirected candidates; kill only a single-paper direct hit | Reuse directed ranking or seal undirected ranking; seal prescreen bytes; enforce `SHORT_MAX`; materialize views | `selection.json`, shortlist views |
| History comparison | Bounded comparator over a sealed pack | Portable stage for complete packs only; archive every status | comparisons, receipts |
| Prior-work research | Produce adversarial neighbors, API queries, overlap, and crack verification | Eligible candidates only; enforcement may mount a receipt-bound history summary | `priorwork.md` |
| Review | Emit one compact verdict and review | Fresh portable mirror per candidate × seat | review plan, matrix, ballots |
| Aggregation and commit | None | Lowest vote, MAJOR and SA gates, one DB transaction, outbox work | commit receipt |
| Projection | None | Publish one immutable snapshot to `ledger.tsv` and `tmp/ledger.good` | target receipts |
| Report and publish | Assemble a report from accepted artifacts | Permit report writes only under `ideas/`; invoke `publish.sh` | `ideas/YYYY-MM-DD_hunt*.md`, daily branch, PR |

A prescreen direct hit becomes `reject/high/novelty-dead` immediately. Internal history retrieval never creates an automatic verdict. In shipped `shadow` mode, history receipts are observational for a fixed generated batch; the normal external research/review protocol remains the sole authority for ledger verdicts. In calibrated `enforcement` mode, complete receipts gate permanent conclusions and nonpermanent statuses produce `history_abstain` without a ledger row.

## Directed Runs

`RESEARCH_DIRECTION_FILE` selects a repository-relative closed contract. The
host canonicalizes it before any agent invocation and preserves the canonical
snapshot for the process lifetime.

```text
canonical direction snapshot
-> portable generation with direction_constraint.json
-> Direction Axis, Target Failure, Direction Evidence validation
-> schema-v2 frozen batch with direction identity
-> selector advisory ranking and direction.tsv classification
-> all in-scope gate
-> history retrieval, prescreen, prior-work research, review, commit
```

Generation receives optional `direction_constraint.json`; the disposable
selector mirror receives the same bytes at
`tmp/round/history/direction-constraint.json`. Generation supplies exact
structural fields. The selector supplies advisory `select.tsv` plus ordered
`direction.tsv` rows: `in-scope` or `out-of-scope` and one evidence sentence
per candidate. This classifier is an independent model judgment under
fail-closed orchestration, not a proof of natural-language meaning.

Missing or malformed output, selector failure, or one `out-of-scope` result
archives the entire batch as `rejected:direction` before history retrieval and
research. The rejection consumes no backend-failure budget and takes the short no-hit retry.
A schema-v2 batch binds `direction: null` or the exact canonical identity;
resume compares it through `expected-direction`. Direction mode sets
`theme_min_low=0`, while all other quality gates remain active. Undirected
runs retain broad generation, selector fallback, theme quota, schema-v1 batch
compatibility, and existing resume behavior.

## Portable Boundary

The v1 contained runtime was removed; portable-v2 is the only runtime.

The runtime selects a registered provider command intent independently of history and
candidate identity. Each attempt runs directly on the host in a disposable
mirror containing `role.md` plus declared `input/*` files. The full ledger,
SQLite database, Git metadata, unrelated round state, and `.claude` are absent.
The host applies a fixed request-byte ceiling before launch, accepts one
bounded JSON envelope on stdout, validates it, and publishes derived outputs
before writing the completion receipt.

Portable mirrors reduce accidental data exposure; they are not containers or
an OS security boundary. Provider configuration and authentication remain
host-local. Grammar-only model/reasoning requests are shadow execution
evidence and cannot create hard-complete audit authority.

Fresh Hunt v2 initializes the audit schema and records one
`producer_unavailable/unbudgetable_provider` shadow plan per shortlisted
candidate. It starts no hard L2 task, attempt, or production no-match release.
The existing portable comparison/review path remains useful while that hard
authority is unavailable.

## Data Flow and Ownership

| Surface | Writer | Persistence |
| --- | --- | --- |
| Protocol, policy, rubric, and role prompts | Human-maintained repository changes | tracked |
| `.ai-ideas/history.sqlite3` | Host runtime only | local, gitignored |
| `ledger.tsv`, `tmp/ledger.good` | Host materializer from DB snapshot | tracked / recovery projection |
| `tmp/round/` and runtime indices | Host assembly; backends within stage contracts | local, gitignored |
| `ideas/` | Report backend under host stage guard | tracked |
| Per-run archive | Host archive publisher | external to the checkout by default |
| Git branch, commit, push, and pull request | `publish.sh` | repository and remote state |

Each round receives a stable `run_id`. Resume mints a distinct run ID and seals a resume-attempt receipt that binds the sealed front state and, when present, the verified prior failure archive. Reviews and aggregate verdicts are always fresh.

## Auxiliary Loops

`awr-side.sh` revises `accept-w-rev` ledger entries through independent researcher, prior-work, and reviewer roles. Each queue item uses an append-only physical-row key such as `r000123`; `awr-state-aliases.tsv` maps content-derived compatibility keys onto those row keys, including shared state for duplicate ideas. Cached drafts and prior-work evidence must pass the current artifact ABI before reuse. It uses the same portable boundary as Hunt and accepts Codex, Kimi, Grok, OpenCode, or agy per role. Final revision artifacts stay under `tmp/awr-side/awr/`. The sidecar does not change verdicts, the SQLite history authority, `ideas/`, or the main loop's `tmp/round/` state.

`litwatch.sh` harvests recent records into trusted staging, optionally annotates a copy, and deterministically admits only annotations whose IDs exist in staging. Its index under `tmp/litwatch/` is an optional prior-work seed; failure does not block the main hunt.

Frozen calibration uses fixed case inputs to test reviewer logic and aggregation. History-retrieval synthetic fixtures under `calib/history-retrieval/` validate schemas and metric code only; they cannot enable production enforcement. [`calib/README.md`](../calib/README.md) owns case semantics and scoring.
