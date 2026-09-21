## Why

The repository currently mixes tool (scripts, `lib/`, `roles/`, policies) and content (`directions/`, `ideas/`, `ledger.tsv`) in one git tree, so a research topic that lives in an external folder cannot drive a hunt without copying files into the checkout by hand. The operator wants to keep research topics in external folders, run the adversarial hunt against a topic's direction file, have the master ledger in this repository update exactly as today, and leave a per-round harvest (the round's ledger rows plus idea report; `run_id` embeds the round, so one hunt process produces one slice per round) in the external folder. A symlink-based mount was evaluated and rejected: entry scripts resolve roots via `dirname "$0"` without symlink resolution, and the codebase deliberately hardens against symlinked inputs (`lib/direction_contract.py` `O_NOFOLLOW` + `st_nlink==1`, `lib/history_store.py` projection targets, `hunt.sh` ideas-dir checks).

## What Changes

- Add opt-in project mode: `HUNT_PROJECT_DIR=/abs/path` (or `HUNT_PROJECT=<name>` resolved through a local registry) tells `hunt.sh` to take the run's direction from the external folder and to write a harvest back to it. The ledger, database, `ideas/`, `tmp/`, and all state roots remain in this repository and update exactly as today. Setting both variables, or either together with an explicit `RESEARCH_DIRECTION_FILE`, fails closed.
- Direction staging: `hunt.sh` copies `<project>/direction.json` into the freshly rebuilt `tmp/history-startup/` directory (a regular single-link file, so the existing direction contract loads it unchanged) and points `RESEARCH_DIRECTION_FILE` at the staged copy.
- Provenance: `commit-round` gains an explicit `--project <name>` flag, passed through by `hunt.sh`'s `history_append_rows`; the name is recorded in each appended row's `provenance_json`. Additive; no schema change; idempotent replay is unchanged.
- Harvest: new `history_cli.py export-slice --after-sequence N --dest PATH` exports rows above a mark as a byte-exact TSV slice. The mark is the registry's `last_harvested_sequence` when known, else the maximum sequence captured at preflight (inside the hunt lock, before any commit), so in registry mode a crash between commit and harvest cannot silently drop rows (at-least-once). After a successful round, `hunt.sh` writes `<project>/harvest/ledger-slice-<run_id>.tsv`, a small `manifest-<run_id>.json` (run id, direction identity, row count, verdict distribution, report files), and copies the reports created by this round. Run-id names make same-day re-runs accumulate instead of overwrite; an existing destination fails closed. Slices are read-only snapshots; re-import is unsupported by design.
- Local registry: `.ai-ideas/projects.json` (already gitignored) maps project names to absolute external paths plus harvest state. `project-add` validates the directory (real, non-symlink, outside the checkout) **and** parses `direction.json` through the real contract so configuration errors surface at registration, not first run. `project-list` / `project-path` complete the read paths.
- Operator affordances: startup banner prints the effective mode (`project <name>` vs `default`) before any provider starts; unknown project names error with the registered list; docs state that `SA_TARGET` is global across projects and hunts are serial.
- Safety rails: project directory must be a real (non-symlink) directory outside the checkout containing a non-symlink `direction.json`; project mode refuses the repository root and requires an absolute path.

## Capabilities

### New Capabilities

- `external-project-harvest`: Project-mode direction staging, provenance tagging, per-round harvest slice export with crash-safe marks, and the local project registry.

### Modified Capabilities

None. Ledger, retrieval, audit, provider, and publication semantics are unchanged.

## Impact

- `hunt.sh`: project-mode preflight (resolution, conflicts, banner), direction staging, two-point harvest emission (slice after ledger materialize, run-bound report copy after report finalize). Default path unchanged.
- `lib/history_cli.py` / `lib/history_store.py`: new `export-slice` and `max-sequence` subcommands reusing the existing render and destination-validation machinery; new no-DB `project-add` / `project-list` / `project-path` subcommands (same bypass shape as `evaluate`).
- `lib/history_runtime.py`: commit-round gains `--project`, threaded through `hunt.sh`'s `history_append_rows`.
- New tests: export-slice byte-exactness (CRLF terminators, custom meta header, existing dest), registry accept/refuse matrix, provenance presence/absence/replay, shell-level project-mode regression; existing history/hunt suites re-run for parity.
- Docs: `README.md` / `docs/getting-started.md` / `hunt.sh` env header gain the project-mode spelling, the read-only-snapshot rule, global `SA_TARGET`, serial-only hunts, and pointer-rot guidance.
- Explicit non-goals: per-project databases or ledgers; symlink content mounts; moving `ideas/`/`tmp/`/`.ai-ideas/` out of the checkout; slice re-import; `publish.sh`/`settle.sh` changes; weekly reports (do not flow through `hunt.sh`); awr-side revision artifacts (never enter the ledger; not part of harvest); predicate/project-filtered exports across runs; backfilling provenance on historical rows.
