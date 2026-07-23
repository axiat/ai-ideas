# ai-ideas

An auditable research-idea discovery harness for embodied AI.

![ai-ideas pipeline](assets/ai-ideas-hero.png)

`ai-ideas` runs generation, prior-work research, and review in separate processes. Bash owns shortlist construction, minimum-vote aggregation, ledger mutation, archives, and publication. A Strong Accept requires unanimous reviewers plus the mechanical evidence gates defined in [`PROGRAM.md`](PROGRAM.md).

- Independent prior-work evidence prevents the generator from grading its own novelty claim.
- Every prescreen direct hit and deeply reviewed candidate receives an append-only ledger record.
- Per-run archives preserve the inputs, ballots, reasons, overlap judgment, and ledger delta needed to audit a decision.

## Pipeline

```text
policy + ledger
  -> generate -> rank -> direct-hit prescreen -> adversarial prior-work research
  -> independent reviewers -> deterministic minimum vote -> ledger
  -> report -> branch + pull request
```

The main loop writes live state under `tmp/round/`, durable decisions to `ledger.tsv`, accepted reports to `ideas/`, and per-run archives outside the checkout. [`docs/architecture.md`](docs/architecture.md) defines stage and artifact ownership.

## Quick Start

The default path requires Bash, Git, an authenticated Codex CLI, network access, and an authenticated `gh` session for publication.

```bash
git clone git@github.com:axiat/ai-ideas.git
cd ai-ideas
./hunt.sh
```

`./hunt.sh` is an active run, not a dry run. It invokes model and search backends, mutates `ledger.tsv`, and may push a daily branch and open a pull request after a qualifying report. Operational defaults and recovery procedures are in [`docs/getting-started.md`](docs/getting-started.md).

## Artifacts

The durable accounting surface is an eight-column TSV:

```text
date  source  theme  idea  verdict  reason  overlap  category
```

Historical seven-column rows remain valid. Accepted reports use `ideas/YYYY-MM-DD_hunt*.md`. Archived rounds contain a manifest, frozen decision inputs, logs, and a ledger delta under `$HOME/.ai-ideas-runs/$(basename "$PWD")/<run_id>/` by default.

## Optional Backends

Codex is the default for the hunt, AwR sidecar, literature monitor, and calibration runners. Overrides are explicit and never automatic fallbacks.

```bash
AGENT_CMD='./grok-worker.sh' ./hunt.sh
FRONT_CMD='./agy-worker.sh' BACK_CMD='./grok-worker.sh' ./hunt.sh
AGENT_CMD='claude -p --strict-mcp-config' ./hunt.sh
```

Claude runs only when the current command explicitly names it; no default, fallback, hook, or worker starts it. Exact defaults, role-specific overrides, and retrieval behavior are in [`docs/backends.md`](docs/backends.md).

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
