# Getting Started

## Prerequisites

- Bash with arrays, process substitution, `PIPESTATUS`, and indirect variable expansion
- Git with a writable checkout and a configured `origin`
- An authenticated Codex CLI for the default Hunt v2 path
- For Kimi, Grok, or Claude internal stages: that authenticated CLI plus either authenticated Codex for external stages or explicit compatible `AGENT_CMD` / `FRONT_CMD` / `BACK_CMD`; with `HUNT_PROVIDER=kimi` and no Codex on `PATH`, external stages fall back to the kimi CLI
- An authenticated supported CLI for AwR v2; AwR accepts Codex, Kimi, Grok, OpenCode, agy, and Claude
- AwR with agy requires Agy 1.1.8+ and an explicit catalog model; Claude uses grammar `claude-portable-v2`; [docs/backends.md](backends.md) defines both structured-JSON transports
- Network access for model search, repository publication, and settlement fetches
- `gh auth status` passing for pull-request creation
- A writable archive root; the default is `$HOME/.ai-ideas-runs/$(basename "$PWD")`
- Writable local state under `.ai-ideas/` for the SQLite history database

Minimal preflight:

```bash
command -v bash git gh
command -v codex
gh auth status
git remote get-url origin
mkdir -p "$HOME/.ai-ideas-runs/$(basename "$PWD")" .ai-ideas
```

On first start without a durable bootstrap marker, `hunt.sh` imports the working-tree `ledger.tsv` into `.ai-ideas/history.sqlite3` and publishes both TSV projections from that snapshot. Later starts treat the database as authority and reconcile `ledger.tsv` and `tmp/ledger.good` before any agent runs. Other pre-existing dirty paths remain outside the run's owned output surface.

## First Run

```bash
git clone git@github.com:axiat/ai-ideas.git
cd ai-ideas
HISTORY_RUNTIME_ABI=v2 ./hunt.sh
```

`./hunt.sh` immediately starts model and retrieval work. It has no dry-run mode. Canonical decisions commit to SQLite and project to `ledger.tsv`. A successful Strong Accept path also creates `ideas/YYYY-MM-DD_hunt*.md`, invokes `publish.sh`, pushes `hunt/YYYY-MM-DD`, and creates or repairs its pull request.

Selecting `HUNT_PROVIDER=kimi`, `HUNT_PROVIDER=grok`, or `HUNT_PROVIDER=claude`
changes only internal generation, comparison, and review. Selector, prescreen,
external prior-work research, and report assembly keep their Codex command
default. A host without Codex must configure compatible external commands as
described in [`backends.md`](backends.md).

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
| Internal provider | `codex`, using its current configured model/reasoning |

Examples:

```bash
REVIEWERS=5 ./hunt.sh
SA_TARGET=3 ./hunt.sh
./hunt.sh 30
HISTORY_NEAR_SA=tmp/near-sa-queue.tsv ./hunt.sh
HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=kimi ./hunt.sh
HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=claude HUNT_MODEL=sonnet ./hunt.sh
```

The positional argument changes the failure cooldown in minutes. `SA_TARGET=0` removes the daily target and leaves termination to the operator. `HISTORY_NEAR_SA` participates only in a first-time bootstrap epoch; a missing, unsafe, or semantically mismatched queue fails closed before agents start.

## Directed Run

```bash
HISTORY_RUNTIME_ABI=v2 \
RESEARCH_DIRECTION_FILE='directions/dynamic-spatial-memory-vla-v1.json' \
  caffeinate -is ./hunt.sh
```

`RESEARCH_DIRECTION_FILE` names a repository-relative direction contract. The file is canonicalized before any agent invocation. Every raw candidate must provide exact `Direction Axis`, `Target Failure`, and `Direction Evidence` fields, then pass independent selector classification. Any malformed field, selector failure, missing verdict, or `out-of-scope` verdict rejects the whole batch before history retrieval and research. Resume accepts only the same canonical direction identity. Without `RESEARCH_DIRECTION_FILE`, broad generation preserves the existing undirected contract.

The classifier is an independent model judgment inside fail-closed orchestration, not a proof of natural-language meaning.

## Project Mode

Project mode lets an external topic folder drive hunts while canonical history stays in the checkout's master ledger. The folder is a direction source and harvest destination only; there are no per-project ledgers.

Register the folder once (the registry lives at `.ai-ideas/projects.json`, gitignored), then hunt with one environment variable. Run every `project-*` command from the checkout root: the registry path is relative to the current directory, so running them elsewhere creates a stray `.ai-ideas/projects.json`:

```bash
python3 lib/history_cli.py project-add mytopic /abs/path/to/mytopic
python3 lib/history_cli.py project-list
python3 lib/history_cli.py project-path mytopic
HUNT_PROJECT=mytopic ./hunt.sh
```

`HUNT_PROJECT_DIR=/abs/path` selects an unregistered directory directly; the harvest mark then lives only in the hunt process, so there is no cross-run crash recovery. Setting both variables, or either together with an explicit `RESEARCH_DIRECTION_FILE`, fails closed before any provider starts.

The direction-file minimum: copy any `directions/*.json` contract into the folder as `direction.json`. The directory must be absolute, existing, non-symlink, and outside the checkout; `direction.json` must be a regular, non-symlink file that parses as a direction contract, validated at registration. Project mode also requires an existing `.ai-ideas/history.sqlite3`; on a fresh clone, run one default hunt first so startup creates the database.

A project-mode run:

- stages `<project>/direction.json` by copy into `tmp/history-startup/` and loads it through the unchanged direction contract;
- prints `mode=project <name> <dir>` at startup (`mode=default` otherwise);
- records the project name in each committed row's `provenance_json` without altering row identity;
- after each round, exports that round's new ledger rows byte-exactly to `<project>/harvest/ledger-slice-<run_id>.tsv` plus a `manifest-<run_id>.json` (run id, direction identity, harvest mark, row count, verdict distribution), and copies the run-bound report after a Strong Accept;
- advances `last_harvested_sequence` per registered project, so a crash between commit and harvest re-exports the orphaned rows on the next run. Harvest is at-least-once: the worst case is a duplicated slice, never a lost row. The at-least-once guarantee holds once the first successful harvest has recorded a mark; on a project's first-ever run there is no recorded mark, so a crash between commit and the first harvest leaves those rows only in the master ledger — queryable there, never exported to `harvest/`.

Harvest slices are read-only snapshots. Re-import is unsupported: row identity (`origin_stable_id`) is position-dependent, so a re-imported slice mints new identities rather than rejoining the ledger. Query the master ledger for history.

Operational rules:

- `SA_TARGET` is global across projects and modes; Strong Accepts from any hunt count toward the same daily target.
- Hunts are serial: `tmp/hunt.lock` admits one `hunt.sh` process at a time regardless of mode.
- Editing a project's `direction.json` changes its canonical direction identity, and resume then refuses the run, exactly as with `RESEARCH_DIRECTION_FILE`.
- Moving a project folder rots the registry pointer; re-run `project-add` with the new path. The recorded harvest mark is preserved and already-written harvests are unaffected.
- Ad-hoc per-project lookup runs against the master ledger:

```bash
sqlite3 .ai-ideas/history.sqlite3 \
  "SELECT date, theme, story, verdict FROM candidates
   WHERE json_extract(provenance_json, '$.project') = 'mytopic'
   ORDER BY source_sequence"
```

## History maintenance

```bash
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 validate
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 reconcile-ledger
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 export-tsv tmp/ledger.export.tsv
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 rebuild-projections
```

`shadow` is the shipped mode: generation uses the bounded brief, history retrieval archives evidence, and external research/review remain the sole authority for ledger verdicts. In v2, the fresh audit plan records `producer_unavailable` while real provider capacity remains unbudgetable; it creates no hard L2 task or production no-match authority. `enforcement` requires a matching sealed production calibration capability and trust root; synthetic fixtures never enable it. `complete_no_match` is a scoped internal result, not academic novelty.

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
HISTORY_RUNTIME_ABI=v2 ./hunt.sh
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
