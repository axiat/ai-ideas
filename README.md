# ai-ideas

An auditable research-idea discovery harness for embodied AI.

![ai-ideas pipeline](assets/ai-ideas-hero.png)

`ai-ideas` runs generation, prior-work research, and review in separate processes. Bash owns shortlist construction, minimum-vote aggregation, ledger mutation, archives, and publication. A Strong Accept requires unanimous reviewers plus the mechanical evidence gates defined in [`PROGRAM.md`](PROGRAM.md).

- Independent prior-work evidence prevents the generator from grading its own novelty claim.
- Every prescreen direct hit and deeply reviewed candidate receives an append-only ledger record.
- Per-run archives preserve the inputs, ballots, reasons, overlap judgment, and ledger delta needed to audit a decision.

## Pipeline

```text
policy + bounded generation brief from SQLite
  -> generate -> rank -> direct-hit prescreen -> adversarial prior-work research
  -> independent reviewers -> deterministic minimum vote -> ledger
  -> report -> branch + pull request
```

The main loop writes live state under `tmp/round/`, canonical history to `.ai-ideas/history.sqlite3`, replayable projections to `ledger.tsv` and `tmp/ledger.good`, accepted reports to `ideas/`, and per-run archives outside the checkout. [`docs/architecture.md`](docs/architecture.md) defines stage and artifact ownership.

## Quick Start

The default v2 path requires Bash, Git, an authenticated Codex CLI, network
access, and an authenticated `gh` session for publication. Hunt v2 supports
Codex, Kimi, Grok, and Claude for its internal stages. Selecting a non-Codex
internal provider does not change the Codex default used by selector,
prescreen, external research, and report stages; a host without Codex must
configure compatible `AGENT_CMD` / `FRONT_CMD` / `BACK_CMD` values. With
`HUNT_PROVIDER=kimi` and no Codex executable on `PATH`, those stages fall
back to the kimi CLI automatically; with a Codex binary present but
unauthenticated, set `AGENT_CMD` explicitly instead.

```bash
git clone git@github.com:axiat/ai-ideas.git
cd ai-ideas
HISTORY_RUNTIME_ABI=v2 ./hunt.sh
```

For an explicit model selection, the [full Codex example](docs/backends.md#hunt)
sets every Hunt stage to `gpt-6-astra` with `high` reasoning. It also applies
to directed runs and project mode.

`./hunt.sh` is an active run, not a dry run. It invokes model and search backends, commits canonical history to SQLite, projects `ledger.tsv`, and may push a daily branch and open a pull request after a qualifying report. Shipped history mode is `shadow`: internal retrieval is observational for a fixed generated batch, and external research/review remain the sole ledger authority. Operational defaults and recovery procedures are in [`docs/getting-started.md`](docs/getting-started.md).

## Directed Run

```bash
HISTORY_RUNTIME_ABI=v2 \
RESEARCH_DIRECTION_FILE='directions/dynamic-spatial-memory-vla-v1.json' \
  caffeinate -is ./hunt.sh
```

The repository-relative contract is canonicalized before any agent invocation. Every raw candidate must carry exact `Direction Axis`, `Target Failure`, and `Direction Evidence` fields and pass independent selector classification. A missing, malformed, or `out-of-scope` result rejects the whole batch before history retrieval and research. Resume requires the same canonical direction identity. With `RESEARCH_DIRECTION_FILE` unset, broad generation retains its existing contract.

The semantic classifier is an independent model judgment wrapped by fail-closed orchestration; it is not a proof of natural-language meaning.

## Project Mode

Project mode points hunts at a research topic that lives in its own folder outside the checkout: the folder holds a `direction.json`, the hunt runs the standard pipeline against that direction, the master ledger and cross-project duplicate detection stay centralized here, and each round copies its yield back into the topic folder.

```bash
mkdir -p ~/research/mytopic
cp directions/dynamic-spatial-memory-vla-v1.json ~/research/mytopic/direction.json
python3 lib/history_cli.py project-add mytopic ~/research/mytopic
HUNT_PROJECT=mytopic ./hunt.sh
```

Run `project-add` from the checkout root. It requires an absolute, non-symlink directory outside the checkout and validates `direction.json` at registration. `HUNT_PROJECT_DIR=/abs/path` runs an unregistered directory directly.

A project-mode run stages the direction by copy, prints `mode=project <name> <dir>` at startup, and commits to the master ledger exactly as a default hunt, recording the project name in each new row's provenance. After every round it exports into `<project>/harvest/`:

- `ledger-slice-<run_id>.tsv` — the round's new ledger rows, byte-exact behind the ledger header
- `manifest-<run_id>.json` — run id, direction identity, sequence mark, row count, verdict distribution
- the round's report, when the round produced a Strong Accept
- `README.md` — written on first harvest, describes the harvest format

Harvest is at-least-once: once the first successful export has recorded `last_harvested_sequence`, a crash between commit and harvest re-exports the orphaned rows on the next run, and a failed export overlaps but never loses rows. Slices are read-only snapshots; row identity is position-dependent, so a re-imported slice mints fresh identities. Query the master ledger for history.

The registry is `.ai-ideas/projects.json`, git-ignored with the rest of `.ai-ideas/`, so project names and local paths never reach the remote. Moving a project folder rots the pointer; re-run `project-add` with the new path (the harvest mark is preserved).

Operational rules:

- Project mode requires an existing `.ai-ideas/history.sqlite3`: on a fresh clone, run one default hunt first to create the database.
- `SA_TARGET` is global across projects and modes. Hunts are serial: one `hunt.sh` process holds the repository lock at a time, regardless of mode.
- Editing a project's `direction.json` changes its direction identity, and resume then refuses the run, as with `RESEARCH_DIRECTION_FILE`.
- Ad-hoc per-project lookup runs against the master ledger:

```bash
sqlite3 .ai-ideas/history.sqlite3 \
  "SELECT date, theme, story, verdict FROM candidates
   WHERE json_extract(provenance_json, '$.project') = 'mytopic'
   ORDER BY source_sequence"
```

Operational detail is in [`docs/getting-started.md`](docs/getting-started.md).

## Artifacts

The durable accounting surface is an eight-column TSV:

```text
date  source  theme  idea  verdict  reason  overlap  category
```

Historical seven-column rows remain valid. Accepted reports use `ideas/YYYY-MM-DD_hunt*.md`. Archived rounds contain a manifest, frozen decision inputs, logs, and a ledger delta under `$HOME/.ai-ideas-runs/$(basename "$PWD")/<run_id>/` by default.

## Provider Selection

Omitted reasoning uses the selected CLI's current default. Omitted models use
the Codex, Kimi, Grok, and Claude defaults. OpenCode omission requires a safe host
configuration probe and launches with that effective model pinned; agy
requires an explicit model. Every OpenCode/agy model must exactly match the
current bounded local `models` catalog, whose identity is checked again before
launch. Hunt accepts `codex`, `kimi`, `grok`, and `claude`; AwR additionally
accepts `opencode` and `agy`.

AwR with agy requires Agy 1.1.8+ and an explicit catalog model. Claude and Agy
use structured JSON transports; the contracts are in
[docs/backends.md](docs/backends.md).

```bash
HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=kimi ./hunt.sh
HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=grok ./hunt.sh
HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=claude ./hunt.sh
HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=opencode AWR_MODEL=openai/gpt-6-astra AWR_REASONING_EFFORT=high SIDE_POLL_SEC=0 ./awr-side.sh
HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=agy AWR_MODEL=gemini-3.6-flash-high SIDE_POLL_SEC=0 ./awr-side.sh
HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=claude AWR_MODEL=sonnet SIDE_POLL_SEC=0 ./awr-side.sh
```

`HUNT_PROVIDER` controls the portable internal stages. Route Hunt's external
selector, prescreen, prior-work research, and report stages through an explicit
worker with `AGENT_CMD`. Omitting both model variables and both reasoning
variables keeps the selected CLI's current defaults:

```bash
HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=kimi \
AGENT_CMD='kimi --output-format text -p' \
./hunt.sh

HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=grok \
AGENT_CMD='./grok-worker.sh' \
./hunt.sh

HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=claude \
AGENT_CMD='./claude-worker.sh' \
./hunt.sh
```

Pin the same explicit model and reasoning effort on both paths when a fixed
run configuration is required:

```bash
HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=grok \
HUNT_MODEL=grok-4.7 \
HUNT_REASONING_EFFORT=high \
AGENT_CMD='./grok-worker.sh' \
GROK_MODEL=grok-4.7 \
GROK_REASONING_EFFORT=high \
./hunt.sh

HISTORY_RUNTIME_ABI=v2 \
HUNT_PROVIDER=claude \
HUNT_MODEL=sonnet \
HUNT_REASONING_EFFORT=high \
AGENT_CMD='./claude-worker.sh' \
CLAUDE_MODEL=sonnet \
CLAUDE_REASONING_EFFORT=high \
./hunt.sh
```

Exact model/reasoning spelling for every provider, role-specific overrides,
the external Hunt stage boundary, and the v1 removal are in
[`docs/backends.md`](docs/backends.md).

## Calibration

Frozen panels test verdict logic against fixed evidence; end-to-end negative controls test retrieval recall against known occupants.

```bash
./calib/run_all.sh
./calib/run_e2e.sh calib/cases/neg-replai
```

Both commands invoke configured backends. The deterministic offline ABI gate is `bash tests/calibration_abi_smoke.sh`. Case semantics and the expectation DSL are canonical in [`calib/README.md`](calib/README.md).

## Recovery and Trust Boundaries

Valid interrupted front-stage artifacts resume with fresh review ballots. Decision archives live outside the workspace by default; an incomplete Strong Accept archive creates `tmp/HALTED-ARCHIVE-FAIL` and blocks restart and publication until the archive or ledger state is repaired. Repository guards, disposable mirrors, local hooks, and CI path checks reduce accidental cross-surface writes; they are not an adversarial process or host boundary.

Recovery details are in [`docs/getting-started.md`](docs/getting-started.md). Filesystem, network, process, publishing, and CI guarantees are in [`docs/trust-boundaries.md`](docs/trust-boundaries.md).

## Documentation

- [`docs/getting-started.md`](docs/getting-started.md) — prerequisites, first run, result locations, recovery, and settlement
- [`docs/architecture.md`](docs/architecture.md) — stages, data flow, and artifact ownership
- [`docs/backends.md`](docs/backends.md) — exact backend defaults and explicit overrides
- [`docs/trust-boundaries.md`](docs/trust-boundaries.md) — enforced boundaries and their limits
- [`PROGRAM.md`](PROGRAM.md) — canonical runtime protocol and ledger schema
- [`calib/README.md`](calib/README.md) — calibration cases, tracks, and interpretation
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — local validation and change conventions

## Scope

`ai-ideas` is a local, shell-orchestrated embodied-AI research workflow. It is not a hosted service, general-purpose topic framework, package, or adversarial sandbox. Publication targets the configured Git remote through daily `hunt/<date>` or `weekly/<date>` branches and pull requests.
