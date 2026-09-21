## 1. Slice export and sequence query

- [ ] 1.1 Add `export_slice(conn, after_sequence, dest)` to `lib/history_store.py`: select `(raw_row, row_terminator)` where `source_sequence > ?` ordered by `source_sequence`; resolve the header with the same meta `ledger_header_b64` priority as `_render_tsv_in_transaction`; render via `_render_projection_rows`; validate through `_validate_destination` with the resolved state root; refuse a pre-existing destination file; write via `_atomic_replace`. Return `row_count` and the max exported `source_sequence` (or the mark when empty).
- [ ] 1.2 Wire `history_cli.py --db <db> export-slice --after-sequence N --dest PATH [--project NAME]`: `--project` advances the registry's `last_harvested_sequence` on success. Add `history_cli.py --db <db> max-sequence` printing `MAX(source_sequence)`. Flag style follows sibling store commands (`--db` global).
- [ ] 1.3 Add `tests/history_store_export_slice_smoke.py`: byte-exact slice including CRLF `row_terminator` rows; custom meta header honored; header-only empty slice; mark above max; existing destination refused; canonical targets (`ledger.tsv`, `tmp/ledger.good`, state root) refused; outside-checkout destination allowed with auto-created `harvest/` parent.

## 2. Project registry

- [ ] 2.1 Add no-DB subcommands (same bypass shape as `evaluate`): `project-add` (absolute-path normalization; name charset `[A-Za-z0-9._-]+`; rejects nonexistent/symlinked/in-checkout dirs; validates `direction.json` via `direction_contract.parse_contract_bytes` plus an explicit `lstat` non-symlink check — `load_contract` only accepts repository-relative paths — with verbatim errors; does **not** write `last_harvested_sequence`, absence means no mark recorded), `project-path` (prints path + `last_harvested_sequence`, unset until first harvest; unknown name lists registered names), `project-list`. Unknown registry `version` fails closed.
- [ ] 2.2 Add `tests/history_project_registry_smoke.py` covering the accept/refuse matrix, version refusal, and `last_harvested_sequence` advancement via `export-slice --project`.

## 3. Provenance flag plumbing

- [ ] 3.1 Add `--project` to commit-round (parser `lib/history_runtime.py:9229`, dispatch `:9497-9509`, `commit_round` `:7868-7878`) recording `"project": "<name>"` in each appended row's `provenance_json`; omitted when the flag is absent. The flag value enters **only** `commit_metadata` (`lib/history_runtime.py:8033-8050`); it must never enter `request` / `request_sha` (`:7965-7998`), or idempotent replay would fail on request mismatch. Thread through `history_append_rows` (`hunt.sh:943-953`) from resolved project-mode state. No env reads in `lib/`.
- [ ] 3.2 Runtime regression: presence in project mode, absence in default mode, idempotent replay (`lookup_round_commit`) does not rewrite provenance.

## 4. hunt.sh project mode

- [ ] 4.1 Preflight (before any provider, under no new default-flow failure modes): resolve `HUNT_PROJECT_DIR` directly or `HUNT_PROJECT` via `project-path`; refuse both set, either plus explicit `RESEARCH_DIRECTION_FILE`, relative paths, symlink dirs, in-checkout dirs, missing/symlinked `direction.json`, missing/corrupt DB mark read. Capture the fallback mark (`max-sequence`) at preflight — inside `hunt.lock`, before any commit — into a shell variable. Print the one-line mode banner (`mode=project <name> <dir>` or `mode=default`) after the direction snapshot succeeds (after `hunt.sh:2164`), before any provider starts.
- [ ] 4.2 Stage direction: `cp` into `tmp/history-startup/` (rebuilt at `hunt.sh:2146-2147`; the staged copy lands after `2147`, before the one-shot direction snapshot at `:2153-2164`) and set `RESEARCH_DIRECTION_FILE` to the staged relative path.
- [ ] 4.3 Two-point harvest: after ledger materialize succeeds (`hunt.sh:2659-2663`), use the current mark (captured at preflight — registry `last_harvested_sequence` takes precedence when recorded — and advanced to the exported maximum after each successful export), run `export-slice` to `<project>/harvest/ledger-slice-<run_id>.tsv`, and write `manifest-<run_id>.json` (plus one-time `harvest/README.md`). After `finalize_strong_accept` succeeds (`hunt.sh:2692`, `sa_count>0` block), copy the report at `RECOVERY_REPORT_PATH` (guaranteed non-empty there). Harvest failure after a successful commit warns loudly and never rolls back.
- [ ] 4.4 Shell regression with a fake project dir: staging loads through the unchanged contract; every refusal in 4.1; banner; same-day re-run accumulates distinct slice files; harvest slice/manifest match the run's rows; report copy copies only `RECOVERY_REPORT_PATH`; `sa_count==0` round yields slice+manifest and no report copy; a multi-round single-process hunt's second-round slice contains only the second round's rows (mark advances per round); simulated crash after commit but before harvest (kill mid-run) makes the next registry-mode run's slice include the orphaned rows; default-flow parity. Confirm `tests/verify_product_contract.py` forbidden-pattern scans and `shell_harness_correctness_regression.py` function-slice extraction stay green.
- [ ] 4.5 Update the `hunt.sh` env header comment (style of `:30-37`) with `HUNT_PROJECT` / `HUNT_PROJECT_DIR`.

## 5. Documentation and spec hygiene

- [ ] 5.1 `README.md` / `docs/getting-started.md`: project-mode section with one verified minimal spelling (`HUNT_PROJECT=mytopic ./hunt.sh`), the direction-file minimum (copy `directions/*.json`, rename to `direction.json`), read-only-snapshot rule, global `SA_TARGET` across projects, serial-only hunts, direction edits change identity (resume refuses), pointer-rot refresh, and the ad-hoc per-project lookup SQL (`json_extract(provenance_json,'$.project')`).
- [ ] 5.2 `export-slice --help` and `harvest/README.md` state the slice is a read-only snapshot not intended for re-import.
- [ ] 5.3 `openspec validate external-project-harvest --strict` passes; archive only after tasks complete.

## 6. Independent verification

- [ ] 6.1 Independent agent runs the full offline history/hunt suites plus new tests; repair every blocking failure.
- [ ] 6.2 Independent diff audit for: default-flow parity, no symlink acceptance in the new path, no writes outside the checkout except the harvest dest, no env reads added to `lib/`, docs/code parity. Repair every blocking finding.
