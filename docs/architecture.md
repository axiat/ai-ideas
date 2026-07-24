# Architecture

## Control Plane

[`PROGRAM.md`](../PROGRAM.md), [`brainstorming_policy.md`](../brainstorming_policy.md), [`rubric.md`](../rubric.md), and `roles/*.md` define the human-owned protocol. Backend processes produce bounded artifacts. `hunt.sh` remains the decision authority for selection, external evidence gates, vote aggregation, database commits, archives, and publication.

```text
policy + SQLite history + generation brief
  -> contained generation
  -> immutable candidate batch
  -> model-free history retrieval
  -> external selection and prescreen
  -> contained history comparison
  -> eligible external prior-work research
  -> contained independent reviews
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
| Startup | None | Policy mode and calibration check; bootstrap or reconcile SQLite; recover search and ledger projections; build generation brief | `.ai-ideas/history.sqlite3`, `generation-brief.json` |
| Generation | Propose candidates under the selected divergence lens | Contained stage with sealed brief, policy, optional research context; validate structure | `ideas.tsv`, `ideas.md` |
| Freeze and observe | None | Freeze candidate bytes; run model-free duplicate, failure, and optional evolution retrieval | batch, packs, traces |
| Selection and prescreen | Rank candidates; kill only a single-paper direct hit | Seal selector and prescreen bytes; enforce `SHORT_MAX`; materialize views | `selection.json`, shortlist views |
| History comparison | Bounded comparator over a sealed pack | Contained stage for complete packs only; archive every status | comparisons, receipts |
| Prior-work research | Produce adversarial neighbors, API queries, overlap, and crack verification | Eligible candidates only; enforcement may mount a receipt-bound history summary | `priorwork.md` |
| Review | Emit one compact verdict and review | Fresh contained mirror per candidate × seat | review plan, matrix, ballots |
| Aggregation and commit | None | Lowest vote, MAJOR and SA gates, one DB transaction, outbox work | commit receipt |
| Projection | None | Publish one immutable snapshot to `ledger.tsv` and `tmp/ledger.good` | target receipts |
| Report and publish | Assemble a report from accepted artifacts | Permit report writes only under `ideas/`; invoke `publish.sh` | `ideas/YYYY-MM-DD_hunt*.md`, daily branch, PR |

A prescreen direct hit becomes `reject/high/novelty-dead` immediately. Internal history retrieval never creates an automatic verdict. In shipped `shadow` mode, history receipts are observational for a fixed generated batch; the normal external research/review protocol remains the sole authority for ledger verdicts. In calibrated `enforcement` mode, complete receipts gate permanent conclusions and nonpermanent statuses produce `history_abstain` without a ledger row.

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

`awr-side.sh` revises `accept-w-rev` ledger entries through independent researcher, prior-work, and reviewer roles. Each queue item uses an append-only physical-row key such as `r000123`; `awr-state-aliases.tsv` maps content-derived compatibility keys onto those row keys, including shared state for duplicate ideas. Cached drafts and prior-work evidence must pass the current artifact ABI before reuse. Final revision artifacts stay under `tmp/awr-side/awr/`; coordination also uses `tmp/awr-side.lock`, the shared agy launch stamp and lock, and disposable `tmp/awr-side/run.*` mirrors. The sidecar does not change verdicts, the SQLite history authority, `ideas/`, or the main loop's `tmp/round/` state.

`litwatch.sh` harvests recent records into trusted staging, optionally annotates a copy, and deterministically admits only annotations whose IDs exist in staging. Its index under `tmp/litwatch/` is an optional prior-work seed; failure does not block the main hunt.

Frozen calibration uses fixed case inputs to test reviewer logic and aggregation. History-retrieval synthetic fixtures under `calib/history-retrieval/` validate schemas and metric code only; they cannot enable production enforcement. [`calib/README.md`](../calib/README.md) owns case semantics and scoring.
