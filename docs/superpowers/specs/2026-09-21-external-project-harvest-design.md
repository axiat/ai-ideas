# External Project Harvest

## Problem

Research topics live in external folders, but the hunt only accepts in-repo `directions/*.json` inputs and only leaves outputs in the checkout. The operator wants: external topic folder drives the adversarial hunt; the master ledger in this repo updates as usual; the external folder keeps a per-round harvest (its ledger rows + idea report; `run_id` embeds the round, so one hunt process produces one slice per round); a git-ignored local pointer remembers where external projects live.

## Rejected: symlink mounting

An external-folder symlink into this repo fails immediately (`hunt.sh:40` resolves root from `dirname "$0"` without symlink resolution, then `source lib/mirror_pre.sh` fails) and fights deliberate hardening: direction contract `O_NOFOLLOW` + `st_nlink==1` (`lib/direction_contract.py:172-220`), projection targets may not be symlinks (`lib/history_store.py:4110-4111`), ideas-dir parent checks (`hunt.sh:1745-1749`). Industry comparison (Hugo modules vs symlinked content, chezmoi vs stow, taskwarrior `data.location`, conda `environments.txt`) says: explicit path parameter + tool-side local registry, not links.

## Design

The external folder is a **direction source + harvest destination**. The ledger, DB, `ideas/`, `tmp/`, and state roots never leave the checkout — so dedup, retrieval, audit, and publish semantics are untouched, and cross-project duplicate detection keeps working because every row lands in one master ledger.

```text
~/research/<topic>/
  direction.json                      # the topic's 主旨 (same contract as directions/*.json)
  harvest/
    README.md                         # one-time: what these files are; do not re-import
    ledger-slice-<run_id>.tsv         # this round's new rows, byte-exact, read-only
    manifest-<run_id>.json            # run id, direction identity, counts, verdicts, files
    <today>_hunt[-N].md               # copy of reports created by this round (SA rounds only)

ai-ideas/
  .ai-ideas/projects.json             # git-ignored registry: name -> path + harvest mark
  ledger.tsv / ideas/ / .ai-ideas/    # updated exactly as today
```

Operator flow:

```bash
python3 lib/history_cli.py project-add mytopic ~/research/mytopic   # once; validates direction.json
HUNT_PROJECT=mytopic ./hunt.sh                                      # every run
```

Mechanisms (each small, all verified against source):

1. **Project mode preflight** (`hunt.sh`): resolve `HUNT_PROJECT_DIR` or `HUNT_PROJECT` via `project-path`; refuse both set, either plus explicit `RESEARCH_DIRECTION_FILE`, relative paths, symlink dirs, in-checkout dirs, missing/symlinked `direction.json`, unreadable mark. Startup banner prints `mode=project <name> <dir>` / `mode=default` after the direction snapshot succeeds (after `hunt.sh:2164`), before any provider starts — a misspelled env var is immediately visible.
2. **Direction staging**: `cp <project>/direction.json` into `tmp/history-startup/` (rebuilt at `hunt.sh:2146-2147`; the staged copy lands after `2147`, before the one-shot direction snapshot at `:2153-2164`); point `RESEARCH_DIRECTION_FILE` at the staged relative path. Contract unchanged; the first round's `rm -rf tmp/round` cannot touch it.
3. **Crash-safe mark**: the registry stores `last_harvested_sequence` per project (absent until the first harvest records it; absence means no mark); the harvest mark is that value when present, else the `MAX(source_sequence)` captured at preflight — inside the hunt lock, before any commit — from a new `max-sequence` subcommand. `export-slice --project <name>` advances the mark on success — in registry mode a crash between commit and harvest re-exports orphaned rows next run (at-least-once). `HUNT_PROJECT_DIR` (no registry entry) uses the preflight session mark, advanced in shell memory after each successful harvest, with no crash recovery. **Do not** read the mark from `ledger-current.json`: its `sequence` is the projection sequence, not `source_sequence`.
4. **Slice export**: `history_cli.py --db <db> export-slice --after-sequence N --dest PATH` — select `(raw_row, row_terminator)` above the mark in `source_sequence` order, header via the same meta `ledger_header_b64` priority as `_render_tsv_in_transaction`, render via `_render_projection_rows`, write via `_atomic_replace` through `_validate_destination` (external paths allowed, canonical targets refused) plus refuse a pre-existing destination. Run-id filenames make same-day re-runs accumulate.
5. **Provenance**: commit-round gains an explicit `--project <name>` flag threaded through `history_append_rows` (`hunt.sh:943-953`) — `lib/history_runtime.py` reads no env today and stays that way. Idempotent replay returns prior provenance unchanged.
6. **Registry**: no-DB subcommands on `history_cli.py` (same bypass shape as `evaluate`): `project-add` (validates dir + parses `direction.json` through the real contract at registration), `project-path`, `project-list`. Unknown `version` fails closed. Removal is a one-key hand edit of the JSON; no subcommand.
7. **Two-point harvest**: slice+manifest after ledger materialize (`hunt.sh:2659-2663`); report copy after `finalize_strong_accept` (`hunt.sh:2692`, gated on `sa_count>0`) copies the run-bound report at `RECOVERY_REPORT_PATH` (set on finalize success by `verify_pending_report_binding`, `hunt.sh:2050/2067`, guaranteed non-empty there), so a previous run's report is never copied. `sa_count==0` rounds yield slice+manifest and no report copy, which the manifest makes legible.

## Boundaries

- Slices are **read-only snapshots**. `origin_stable_id` is position-dependent (`lib/history_store.py:895-912`); re-importing a slice mints new identities. `--help`, manifest, and `harvest/README.md` all say so.
- Harvest failure after a successful commit warns but never rolls back.
- `SA_TARGET` is global across projects; hunts are serial (`tmp/hunt.lock`); editing a project's direction changes its identity (resume refuses) — all documented.
- No changes to `publish.sh`, `settle.sh`, awr-side (its revision artifacts never enter the ledger and are not part of harvest), weekly reports (do not flow through `hunt.sh`), retrieval, audit, or the default flow.
- Pointer rot (moved external folder) is fixed by re-running `project-add`; already-written harvests are unaffected.

## Test plan

- `tests/history_store_export_slice_smoke.py`: byte-exact slice with CRLF rows, custom meta header, header-only empty slice, mark above max, existing destination refused, canonical-target refusal, outside-checkout dest with auto-created parent.
- `tests/history_project_registry_smoke.py`: accept/refuse matrix, version refusal, mark advancement via `export-slice --project`.
- Provenance presence/absence/replay regression in runtime smoke.
- Shell regression with a fake project dir: staging, every refusal, banner, same-day accumulation, harvest contents match the run's rows, report-copy exclusion, `sa_count==0` shape, second-round slice containing only second-round rows (mark advances per round), crash-after-commit orphan rows recovered by the next registry-mode run, default-flow parity; `verify_product_contract.py` and shell-harness slicing stay green.
- Existing history/hunt suites re-run for parity; independent verification agent over the diff.
