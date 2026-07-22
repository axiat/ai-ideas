# Architecture

## Control Plane

[`PROGRAM.md`](../PROGRAM.md), [`brainstorming_policy.md`](../brainstorming_policy.md), [`rubric.md`](../rubric.md), and `roles/*.md` define the human-owned protocol. Backend processes produce bounded artifacts. `hunt.sh` remains the decision authority for shortlist construction, evidence gates, vote aggregation, ledger writes, archives, and publication.

```text
protocol + ledger + failure digest
  -> generated candidates
  -> independent rank and direct-hit prescreen
  -> prioritized shortlist
  -> independent prior-work evidence
  -> isolated reviewer ballots
  -> Bash minimum vote and hard gates
  -> ledger + run archive
  -> accepted report + publication
```

## Main Loop

| Stage | Backend responsibility | Bash responsibility | Primary artifacts |
| --- | --- | --- | --- |
| Failure distillation | Summarize recurring reject and AwR patterns | Schedule the fallible stage; continue on failure | `tmp/deathlist.md` |
| Generation | Propose candidates under the selected divergence lens | Validate IDs, themes, structure, and assumption-removal fields; preserve the full set before shortlisting | `tmp/round/ideas.all.md`, `ideas.all.tsv` |
| Selection and prescreen | Rank candidates; kill only a single-paper direct hit | Fail open malformed prescreen decisions, record valid kills, enforce `SHORT_MAX` | `select.tsv`, `prescreen.md`, `kills.tsv`, `ideas.md`, `ideas.tsv` |
| Prior-work research | Produce adversarial neighbors, API queries, overlap, and crack verification | Enforce linked-neighbor, query, and evidence structure | `priorwork.md` |
| Review | Emit one verdict row and complete review blocks | Launch independent seats with copied inputs; reject missing or malformed ballots | `rev/<seat>/verdict.tsv`, `rev/<seat>/review.md` |
| Aggregation | None | Take the lowest vote, apply MAJOR and Strong Accept gates, classify non-SA rows, append the ledger | `ledger.tsv`, `accepted.tsv`, `rejects.tsv` |
| Report and publish | Assemble a report from accepted artifacts | Permit report writes only under `ideas/`; invoke `publish.sh` | `ideas/YYYY-MM-DD_hunt*.md`, daily branch, pull request |

A prescreen direct hit becomes `reject/high/novelty-dead` immediately. Prescreen survivors beyond `SHORT_MAX` do not enter deep research or the ledger. Every shortlisted candidate receives independent prior-work evidence and fresh review ballots before aggregation.

## Data Flow and Ownership

`hunt.sh` copies the operator's startup `ledger.tsv` into `tmp/ledger.good`. Each round restores that baseline before backend work, then Bash appends validated decisions and advances the baseline. Backends never own the ledger.

Each round receives a stable `run_id`. The first decision archive freezes the decision inputs and ballots under `RUNS_DIR/<run_id>`. Later archive passes rewrite `manifest.tsv` and `ledger.delta.tsv`, then refresh `round/stages.tsv` and `round/logs/`; the remaining decision artifacts stay frozen. An archive failure after a Strong Accept creates a blocking sentinel before publication.

| Surface | Writer | Persistence |
| --- | --- | --- |
| Protocol, policy, rubric, and role prompts | Human-maintained repository changes | tracked |
| `tmp/round/` and runtime indices | Backends within stage contracts; Bash validation and assembly | local, gitignored |
| `ledger.tsv` | Bash orchestrator | tracked, append-only |
| `ideas/` | Report backend under Bash stage guard | tracked |
| Per-run archive | Bash orchestrator | external to the checkout by default |
| Git branch, commit, push, and pull request | `publish.sh` | repository and remote state |

## Auxiliary Loops

`awr-side.sh` revises `accept-w-rev` ledger entries through independent researcher, prior-work, and reviewer roles. Final revision artifacts stay under `tmp/awr-side/awr/`; coordination also uses `tmp/awr-side.lock`, the shared agy launch stamp and lock, and disposable `tmp/awr-side/run.*` mirrors. It does not change verdicts, `ledger.tsv`, `ideas/`, or the main loop's `tmp/round/` state.

`litwatch.sh` harvests recent records into trusted staging, optionally annotates a copy, and deterministically admits only annotations whose IDs exist in staging. Its index under `tmp/litwatch/` is an optional prior-work seed; failure does not block the main hunt.

Frozen calibration uses fixed case inputs to test reviewer logic and aggregation. End-to-end calibration separately tests retrieval recall. [`calib/README.md`](../calib/README.md) owns case semantics and scoring.
