# External Project Harvest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to implement this plan task by
> task. Use `superpowers:test-driven-development` for every behavior and
> `superpowers:verification-before-completion` before any completion claim.

**Goal:** Let an external topic folder drive a hunt (`HUNT_PROJECT=mytopic ./hunt.sh`) while the master ledger updates as today, leaving a crash-safe per-round harvest (byte-exact ledger slice + manifest + this round's report copies; `run_id` embeds the round, so one hunt process produces one slice per round) in the external folder, with a git-ignored local registry mapping project names to paths and harvest marks.

**Architecture:** The external folder is a direction source + harvest destination only. `hunt.sh` stages `direction.json` by copy into `tmp/history-startup/` (the existing contract loads it unchanged), and after a successful run calls the new `export-slice` with a registry-tracked `last_harvested_sequence` mark (at-least-once across crashes), writing run-id-named slice/manifest into `<project>/harvest/` and copying only this round's reports. Commit-round takes an explicit `--project` flag for row provenance. No symlink mounts, no dual state roots, no env reads in `lib/`, no changes to default flow, publish, settle, retrieval, or audit.

**Tech Stack:** Bash 3.2, Python 3.9+ standard library, SQLite, `unittest`, OpenSpec, Git.

## Authoritative Inputs

- OpenSpec: `openspec/changes/external-project-harvest/` — on any conflict, **OpenSpec wins**; design details live in its `design.md`.
- Design: `docs/superpowers/specs/2026-09-21-external-project-harvest-design.md`

If a task brief conflicts with OpenSpec, OpenSpec wins and the task stops for controller resolution.

## Global Constraints

- Never invoke a real provider during implementation or tests. Shell tests use fake project dirs and fake providers only.
- The default (no project env) hunt flow must remain byte-for-byte identical: no new preflight failure modes, no path changes (the single approved deviation: one startup mode-banner line printed in both modes).
- Do not relax `lib/direction_contract.py` path rules; do not introduce symlink acceptance anywhere in the new path; do not add env reads to `lib/` (project name arrives via explicit flag).
- The only writes outside the checkout permitted by this change are the harvest destination files.
- New shell code must avoid `tests/verify_product_contract.py` forbidden patterns (e.g. `>> ledger.tsv`, `cp ledger.tsv`) and must not break `tests/shell_harness_correctness_regression.py` function-slice extraction of `hunt.sh`.
- Marks come from the registry or a DB `max-sequence` query — never from `ledger-current.json` (its `sequence` is the projection sequence).

## Tasks

### Task 1: Slice export and sequence query (`lib/history_store.py`, `lib/history_cli.py`)

`export_slice` selecting `(raw_row, row_terminator)` above the mark, meta-priority header, `_render_projection_rows`, `_validate_destination` + refuse pre-existing dest, `_atomic_replace`; `export-slice --after-sequence N --dest PATH [--project NAME]` and `max-sequence` subcommands. Tests first: `tests/history_store_export_slice_smoke.py` (CRLF byte-exactness, custom meta header, header-only empty, mark above max, existing dest refused, canonical targets refused, external dest auto-creates parent). Verification: new test green plus `tests/history_store_smoke.py` parity.

### Task 2: Project registry (`lib/history_cli.py`)

No-DB subcommands (`evaluate`-shaped bypass): `project-add` (abs normalization, name charset, dir + `direction.json` validation via `parse_contract_bytes` plus an explicit `lstat` non-symlink check — `load_contract` is repository-relative only — with verbatim errors; no `last_harvested_sequence` written at registration), `project-path`, `project-list`; unknown `version` fails closed. Tests: `tests/history_project_registry_smoke.py` accept/refuse matrix, version refusal, mark advancement via `export-slice --project`.

### Task 3: Provenance flag (`lib/history_runtime.py`, `hunt.sh`)

`commit-round --project <name>` (parser `:9229`, dispatch `:9497-9509`, `commit_round` `:7868-7878`) recording row provenance — the flag value enters only `commit_metadata` (`lib/history_runtime.py:8033-8050`), never `request` / `request_sha` (`:7965-7998`), or idempotent replay fails on request mismatch; threaded through `history_append_rows` (`hunt.sh:943-953`). Tests: presence in project mode, absence by default, idempotent replay keeps prior provenance.

### Task 4: hunt.sh project mode

Preflight resolution + conflict/path refusals + one-line mode banner (`mode=project <name> <dir>` / `mode=default`, printed after the direction snapshot succeeds, after `hunt.sh:2164`); capture the fallback mark (`max-sequence`) at preflight inside `hunt.lock`, before any commit, into a shell variable; staging into `tmp/history-startup/` (rebuilt at `:2146-2147`, staged copy lands after `2147`, before the snapshot at `:2153-2164`); two-point harvest (slice+manifest after `hunt.sh:2659-2663` using the current mark — captured at preflight with registry `last_harvested_sequence` taking precedence, advanced to the exported maximum after each successful export; report copy after `:2692` in the `sa_count>0` block, copying the run-bound report at `RECOVERY_REPORT_PATH`); harvest failure warns without rollback; env header comment update. Shell regression with a fake project dir covering every refusal, banner, same-day accumulation, harvest/row match, report-copy exclusion, `sa_count==0` shape, second-round slice containing only second-round rows (mark advances per round), crash-after-commit orphan recovery in registry mode, and default-flow parity; `verify_product_contract.py` and shell-harness suites green.

### Task 5: Docs and spec hygiene

`README.md` / `docs/getting-started.md` project-mode section (verified minimal spelling, direction-file minimum, read-only-snapshot rule, global `SA_TARGET`, serial-only hunts, direction-edit identity note, pointer-rot refresh, per-project lookup SQL); `export-slice --help` snapshot warning; `openspec validate external-project-harvest --strict`.

### Task 6: Independent verification

Independent agent runs the full offline suites plus new tests; independent diff audit for default-flow parity, no symlink acceptance, no out-of-checkout writes except harvest dest, no env reads in `lib/`, docs/code parity. Repair every blocking finding before archive.
