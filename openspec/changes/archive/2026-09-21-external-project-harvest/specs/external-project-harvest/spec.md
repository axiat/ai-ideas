## ADDED Requirements

### Requirement: Project mode direction staging
When `HUNT_PROJECT_DIR` or `HUNT_PROJECT` selects an external project, the hunt SHALL stage the project's `direction.json` by copying it to a repository-relative regular file under `tmp/history-startup/` and SHALL point `RESEARCH_DIRECTION_FILE` at the staged copy. The hunt SHALL refuse, before any provider process starts: both variables set at once; either combined with an explicit `RESEARCH_DIRECTION_FILE`; a relative `HUNT_PROJECT_DIR`; a project directory or `direction.json` that is a symlink, is missing, or is the checkout root or inside it. When neither variable is set, the hunt SHALL behave exactly as before, and SHALL print the effective mode at startup in both cases.

#### Scenario: Staged direction loads through the unchanged contract
- **WHEN** project mode is active with a valid external project
- **THEN** the direction contract loads the staged copy and the run proceeds with the external topic

#### Scenario: Conflicting configuration is refused
- **WHEN** `HUNT_PROJECT` and `HUNT_PROJECT_DIR` are both set, or either is set together with an explicit `RESEARCH_DIRECTION_FILE`
- **THEN** preflight fails with a conflict error before any provider process starts

#### Scenario: Symlinked or in-checkout project paths are refused
- **WHEN** `HUNT_PROJECT_DIR` is relative, resolves through a symlink, equals or sits inside the checkout, or its `direction.json` is missing or a symlink
- **THEN** preflight fails before any provider process starts

#### Scenario: Startup banner shows the effective mode
- **WHEN** any hunt starts
- **THEN** the startup output states `mode=project <name> <dir>` or `mode=default` after the direction snapshot succeeds (after `hunt.sh:2164`), before any provider process starts

#### Scenario: Default flow unchanged
- **WHEN** neither `HUNT_PROJECT` nor `HUNT_PROJECT_DIR` is set
- **THEN** direction resolution, ledger writes, and publications are identical to the pre-change flow

### Requirement: Run provenance records the project
When project mode is active, commit-round SHALL receive the resolved project name via an explicit `--project` flag and SHALL record it in each appended row's `provenance_json`. When project mode is inactive, provenance SHALL NOT contain a project key. Provenance SHALL NOT appear in the TSV projection and SHALL NOT alter row identity. Idempotent replay SHALL NOT rewrite stored provenance. Library code SHALL NOT read project configuration from the environment.

#### Scenario: Project-tagged rows
- **WHEN** a project-mode round commits rows
- **THEN** each row's stored provenance contains the project name and all identity hashes are computed as before

#### Scenario: Replay keeps prior provenance
- **WHEN** a commit-round is replayed idempotently with a different or absent `--project`
- **THEN** the stored rows and provenance are returned unchanged

### Requirement: Harvest slice export
The tool SHALL provide `export-slice --after-sequence N --dest PATH` exporting rows with `source_sequence > N` in sequence order as a byte-exact TSV slice with the ledger's current header, written atomically. Destination validation SHALL reject canonical state targets, symlinked final components, and pre-existing files, and SHALL permit paths outside the checkout. A project-mode hunt SHALL, after a successful ledger materialize, export the slice to `<project>/harvest/ledger-slice-<run_id>.tsv` using as mark the registry's `last_harvested_sequence` when recorded, else the maximum sequence captured at preflight (inside the hunt lock, before any commit), and SHALL write a `manifest-<run_id>.json` with run id, direction identity, mark, row count, verdict distribution, and file names. After report finalization succeeds, the hunt SHALL copy into `harvest/` only the run-bound report at `RECOVERY_REPORT_PATH`. After each successful export, the shell SHALL advance the process mark variable to the exported maximum sequence; in registry mode that advancement is persisted to `last_harvested_sequence` by `export-slice --project`, while in `HUNT_PROJECT_DIR` mode the shell variable is the only advancement. A failed export SHALL NOT advance the mark (the next round re-exports, overlapping but never losing rows). An empty round SHALL produce a header-only slice and a manifest with `row_count: 0`. Harvest failure after a successful commit SHALL warn and SHALL NOT roll back the committed round. Slices are read-only snapshots; re-import is unsupported.

#### Scenario: Slice contains exactly the run's new rows
- **WHEN** a project-mode run appends K rows above the resolved mark
- **THEN** `ledger-slice-<run_id>.tsv` contains exactly those K rows byte-exactly behind the ledger header, and the manifest records the row count and verdict distribution

#### Scenario: Same-day re-runs accumulate
- **WHEN** two project-mode runs finish on the same day
- **THEN** two distinct run-id-named slice and manifest files exist and neither is overwritten

#### Scenario: Crash before harvest is recovered
- **WHEN** a registry-backed project run commits rows but dies before harvesting, and a later registry-mode run of the same project completes
- **THEN** the later run's slice includes the orphaned rows, because the mark came from `last_harvested_sequence`

#### Scenario: Report copy excludes other runs
- **WHEN** a project-mode run finalizes reports on a day with pre-existing reports
- **THEN** only the run-bound report path (`RECOVERY_REPORT_PATH`, set by `finalize_strong_accept` on success) is copied into `harvest/`

#### Scenario: Empty round yields header-only slice
- **WHEN** a project-mode round appends no rows
- **THEN** the slice contains only the header and the manifest reports `row_count: 0`

#### Scenario: Protected and pre-existing destinations are refused
- **WHEN** `--dest` resolves to `ledger.tsv`, `tmp/ledger.good`, a state-root path, or an existing file
- **THEN** the export fails without writing

### Requirement: Local project registry
The tool SHALL maintain a local, git-ignored registry at `.ai-ideas/projects.json` mapping project names to absolute external paths, with a `version` field whose unknown values fail closed. Registration SHALL validate the directory (absolute, existing, non-symlink, outside the checkout) and SHALL parse `direction.json` through the direction contract, surfacing errors at registration. Registration SHALL NOT write `last_harvested_sequence`: the field appears only after the first successful harvest records it, and absence SHALL mean no harvest mark has been recorded. The registry SHALL provide read paths (`project-path`, `project-list`), and SHALL NOT be required for the default flow.

#### Scenario: Registration validates the direction file
- **WHEN** `project-add` is given a directory whose `direction.json` is missing, a symlink, or contract-invalid
- **THEN** registration fails with the contract error and writes nothing

#### Scenario: Unknown project name lists registered names
- **WHEN** `HUNT_PROJECT` names an unregistered project
- **THEN** preflight fails and prints the registered project names

#### Scenario: Registry advances the harvest mark
- **WHEN** `export-slice --project <name>` succeeds
- **THEN** the registry records the exported maximum sequence as `last_harvested_sequence`

#### Scenario: Unknown registry version fails closed
- **WHEN** the registry `version` is not recognized
- **THEN** every registry command fails without reading or writing entries
