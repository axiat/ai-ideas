# Getting Started

## Prerequisites

- Bash with arrays, process substitution, `PIPESTATUS`, and indirect variable expansion
- Git with a writable checkout and a configured `origin`
- An authenticated Codex CLI for the default backend
- Network access for model search, repository publication, and settlement fetches
- `gh auth status` passing for pull-request creation
- A writable archive root; the default is `$HOME/.ai-ideas-runs/$(basename "$PWD")`

Minimal preflight:

```bash
command -v bash git gh codex
gh auth status
git remote get-url origin
mkdir -p "$HOME/.ai-ideas-runs/$(basename "$PWD")"
```

The checkout may contain an intentional `ledger.tsv` change; `hunt.sh` adopts the working-tree ledger as its startup baseline. Other pre-existing changes remain outside the run's owned output surface and should be understood before launch.

## First Run

```bash
git clone git@github.com:axiat/ai-ideas.git
cd ai-ideas
./hunt.sh
```

`./hunt.sh` immediately starts model and retrieval work. It has no dry-run mode. Prescreen and review decisions update `ledger.tsv`. A successful Strong Accept path also creates `ideas/YYYY-MM-DD_hunt*.md`, invokes `publish.sh`, pushes `hunt/YYYY-MM-DD`, and creates or repairs its pull request.

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

Examples:

```bash
REVIEWERS=5 ./hunt.sh
SA_TARGET=3 ./hunt.sh
./hunt.sh 30
```

The positional argument changes the failure cooldown in minutes. `SA_TARGET=0` removes the daily target and leaves termination to the operator.

## Result Locations

| Path | Lifetime | Contents |
| --- | --- | --- |
| `ledger.tsv` | tracked, append-only | Prescreen direct hits and reviewed decisions |
| `ideas/YYYY-MM-DD_hunt*.md` | tracked | Strong Accept reports |
| `tmp/round/` | live run state | Generated set, shortlist, prior work, ballots, reviews, stage logs, and timing |
| `tmp/ledger.good` | live recovery state | Last Bash-owned ledger baseline |
| `tmp/hunt.metrics.tsv` | local runtime history | Round outcomes, counts, vote vectors, and run IDs |
| `hunt.log` | local runtime history | Operator log and backend-stage summaries |
| `$HOME/.ai-ideas-runs/$(basename "$PWD")/<run_id>/` | external durable archive | Frozen round inputs, manifest, stage logs, and ledger delta |

`tmp/` is gitignored runtime state. Per-run archives are not stored under `tmp/runs/`.

## Recovery

An ordinary interruption is restartable:

```bash
./hunt.sh
```

Mechanically valid `tmp/round/ideas.tsv`, `ideas.md`, and `priorwork.md` resume once when `RESUME_FRONT=1`; all review ballots and aggregate verdicts are discarded and rerun. Set `RESUME_FRONT=0` to force a fresh front stage. A stale `tmp/hunt.lock` is removed automatically only when its recorded process is absent.

If a report exists but publication stopped between commit, push, and pull-request creation, startup reruns the idempotent publication path. Full repair still requires network access, a valid `origin`, push permission, and authenticated `gh`.

`tmp/HALTED-ARCHIVE-FAIL` marks a Strong Accept recorded without a complete decision archive. Resolve the decision before removing the sentinel:

1. Read the sentinel and `hunt.log` to recover the `run_id` and affected count.
2. Either restore the complete archive at `RUNS_DIR/<run_id>` or remove the unarchived Strong Accept rows from both `ledger.tsv` and `tmp/ledger.good`.
3. Verify `ledger.tsv`, `tmp/ledger.good`, and the archive encode the same resolved decision state.
4. Remove `tmp/HALTED-ARCHIVE-FAIL` and restart.

Deleting the sentinel alone permits a decision without its audit trail and is not a valid recovery.

## Settlement

After the pull request is merged into `origin/main`, inspect settlement first:

```bash
DRY_RUN=1 ./settle.sh
./settle.sh
```

`DRY_RUN=1` still runs `git fetch --prune`, resolves `origin/main`, and validates local residue. It suppresses branch switches, resets, and deletions. The real command may switch to `main`, verifies permitted local report and ledger files byte-for-byte against the upstream tree, runs `git reset --hard origin/main`, and removes only routine branches whose commits or tree diff are already represented upstream.
