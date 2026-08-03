# Getting Started

## Prerequisites

- Bash with arrays, process substitution, `PIPESTATUS`, and indirect variable expansion
- Git with a writable checkout and a configured `origin`
- An authenticated Codex CLI for the default backend
- Network access for model search, repository publication, and settlement fetches
- `gh auth status` passing for pull-request creation
- A writable archive root; the default is `$HOME/.ai-ideas-runs/$(basename "$PWD")`
- Writable local state under `.ai-ideas/` for the SQLite history database

Minimal preflight:

```bash
command -v bash git gh codex
codex login status
gh auth status
git remote get-url origin
mkdir -p "$HOME/.ai-ideas-runs/$(basename "$PWD")" .ai-ideas
```

On first start without a durable bootstrap marker, `hunt.sh` imports the working-tree `ledger.tsv` into `.ai-ideas/history.sqlite3` and publishes both TSV projections from that snapshot. Later starts treat the database as authority and reconcile `ledger.tsv` and `tmp/ledger.good` before any agent runs. Other pre-existing dirty paths remain outside the run's owned output surface.

## First Run

```bash
git clone git@github.com:axiat/ai-ideas.git
cd ai-ideas
./hunt.sh
```

`./hunt.sh` immediately starts model and retrieval work. It has no dry-run mode. Canonical decisions commit to SQLite and project to `ledger.tsv`. A successful Strong Accept path also creates `ideas/YYYY-MM-DD_hunt*.md`, invokes `publish.sh`, pushes `hunt/YYYY-MM-DD`, and creates or repairs its pull request.

Primary defaults:

| Control | Default |
| --- | --- |
| Review seats | `REVIEWERS=3` |
| Papers required for the Strong Accept gate | `MIN_READ=5` |
| Daily Strong Accept target | `SA_TARGET=1` |
| Deep-research shortlist | `SHORT_MAX=3` |
| Front-stage empty retries | `EMPTY_MAX=3` |
| Failure cooldown | `FAIL_SLEEP_MIN=150` minutes |
| Complete no-report retry | `NO_HIT_SLEEP_MIN_LO=1` to `NO_HIT_SLEEP_MIN_HI=8` minutes |
| Consecutive backend failure cap | `MAX_FAILS=12` |
| History policy | `history/retrieval-policy-v1.json` (`shadow`) |

Examples:

```bash
REVIEWERS=5 ./hunt.sh
SA_TARGET=3 ./hunt.sh
./hunt.sh 30
HISTORY_NEAR_SA=tmp/near-sa-queue.tsv ./hunt.sh
```

The positional argument changes the failure cooldown in minutes. `SA_TARGET=0` removes the daily target and leaves termination to the operator. `HISTORY_NEAR_SA` participates only in a first-time bootstrap epoch; a missing, unsafe, or semantically mismatched queue fails closed before agents start.

## Directed Run

```bash
RESEARCH_DIRECTION_FILE='directions/dynamic-spatial-memory-vla-v1.json' \
  caffeinate -is ./hunt.sh
```

`RESEARCH_DIRECTION_FILE` names a repository-relative direction contract. The file is canonicalized before any agent invocation. Every raw candidate must provide exact `Direction Axis`, `Target Failure`, and `Direction Evidence` fields, then pass independent selector classification. Any malformed field, selector failure, missing verdict, or `out-of-scope` verdict rejects the whole batch before history retrieval and research. Resume accepts only the same canonical direction identity. Without `RESEARCH_DIRECTION_FILE`, broad generation preserves the existing undirected contract.

The classifier is an independent model judgment inside fail-closed orchestration, not a proof of natural-language meaning.

## History maintenance

```bash
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 validate
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 reconcile-ledger
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 export-tsv tmp/ledger.export.tsv
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 rebuild-projections
```

`shadow` is the shipped mode: generation uses the bounded brief, history retrieval archives evidence, and external research/review remain the sole authority for ledger verdicts. `enforcement` requires a matching sealed production calibration capability and trust root; synthetic fixtures never enable it. `complete_no_match` is a scoped internal result, not academic novelty.

## Result Locations

| Path | Lifetime | Contents |
| --- | --- | --- |
| `.ai-ideas/history.sqlite3` | local authority | Canonical candidates, verdicts, lineage, observations, outboxes |
| `ledger.tsv` | tracked projection | Replayable export of the current DB snapshot |
| `tmp/ledger.good` | recovery projection | Same snapshot used for crash recovery |
| `ideas/YYYY-MM-DD_hunt*.md` | tracked | Strong Accept reports |
| `tmp/round/` | live run state | Brief, batch, observations, views, reviews, stage logs |
| `tmp/near-sa-queue.tsv` | disposable view | Compatibility projection of canonical near-SA observations |
| `tmp/hunt.metrics.tsv` | local runtime history | Round outcomes, counts, vote vectors, and run IDs |
| `tmp/awr-side/awr/` | local AwR history | Stable row-keyed tasks, drafts, evidence, reviews, and terminal results |
| `hunt.log` | local runtime history | Operator log and backend-stage summaries |
| `$HOME/.ai-ideas-runs/$(basename "$PWD")/<run_id>/` | external durable archive | Frozen round inputs, history artifacts, receipts, and ledger delta |

`.ai-ideas/` and `tmp/` are gitignored. Per-run archives are not stored under `tmp/runs/`.

## Recovery

An ordinary interruption is restartable:

```bash
./hunt.sh
```

A sealed `tmp/round/history/resume-state.json` resumes once when `RESUME_FRONT=1`. Resume mints a new run ID, seals a resume-attempt receipt, and reuses only matching policy, watermark, pack, comparator, adapter, and preflight identities. Review ballots and aggregate verdicts are always fresh. Set `RESUME_FRONT=0` to force a full front stage. A stale `tmp/hunt.lock` is removed automatically only when its recorded process is absent.

After a projection crash, startup reconciliation converges both TSV targets to the current database snapshot without duplicating rows:

```bash
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 reconcile-ledger
```

AwR restarts use stable physical-row keys. On first access, `awr-state-aliases.tsv` copies compatible content-derived state to the row key; terminal results remain terminal, feedback rounds retain their order, and cached artifacts that fail the current ABI are regenerated.

If a report exists but publication stopped between commit, push, and pull-request creation, startup reruns the idempotent publication path. Full repair still requires network access, a valid `origin`, push permission, and authenticated `gh`.

`tmp/HALTED-ARCHIVE-FAIL` marks a Strong Accept recorded without a complete decision archive. Resolve the decision before removing the sentinel:

1. Read the sentinel and `hunt.log` to recover the `run_id` and affected count.
2. Either restore the complete archive at `RUNS_DIR/<run_id>` or repair the canonical commit and both projections so they encode the same resolved decision.
3. Verify the archive, database snapshot, and both TSV projections agree.
4. Remove `tmp/HALTED-ARCHIVE-FAIL` and restart.

Deleting the sentinel alone permits a decision without its audit trail and is not a valid recovery.

## Settlement

After the pull request is merged into `origin/main`, inspect settlement first:

```bash
DRY_RUN=1 ./settle.sh
```
