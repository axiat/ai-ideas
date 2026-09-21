## Context

Tool/content separation was evaluated in two directions. A symlink mount from the external folder into the checkout fails mechanically (`hunt.sh:40` `cd "$(dirname "$0")"` takes the symlink's directory as root, then `source lib/mirror_pre.sh` fails) and contradicts deliberate anti-symlink hardening (`lib/direction_contract.py:172-220`, `lib/history_store.py:4110-4111`, `hunt.sh:1745-1749`). A full dual-root split (separate `content_root` owning the ledger/database) would force every external project to bootstrap its own `.ai-ideas/history.sqlite3` — the DB binds an absolute state root (`lib/history_store.py:2736-2743`) — and would silently lose cross-project duplicate detection, which is the ledger's main value.

The chosen design keeps one master ledger in the repository and treats the external folder as a *direction source + harvest destination* only. This matches the operator requirement ("ai-ideas 账本照常更新，外部文件夹留存自己收获的副本") and industry precedent for single-user CLI tools (taskwarrior `data.location`, DVC external cache, conda `environments.txt` local registry): the tool takes an explicit path, the tool's own state directory holds the local registry, and content copies are explicit exports rather than filesystem links.

Key enablers already in the codebase, all verified against source:

- The direction contract accepts any safe repository-relative path (`lib/direction_contract.py:160-220`); a staged regular copy loads with zero Python changes. `RESEARCH_DIRECTION_FILE` is consumed exactly once (`hunt.sh:2153-2158`), at the startup direction snapshot.
- `export_tsv` destination validation forbids canonical state targets but permits paths outside the checkout (`lib/history_store.py:4106-4158`); `_atomic_replace` (`:4742-4763`) creates parent directories via `_durable_mkdir` and refuses symlinked ones.
- `provenance_json` is a free-form per-row dict stored verbatim (`lib/history_store.py:3918-3925`); adding a `project` key needs no migration. `lib/history_runtime.py` reads **no** environment variables today; all commit inputs arrive as explicit flags from `hunt.sh:943-953` (`history_append_rows`).
- `.ai-ideas/` is gitignored and already hosts local JSON state (`ledger-current.json`).
- The hunt lock (`tmp/hunt.lock`, `hunt.sh:2124`) is held for the whole run, so marks taken after acquisition are race-free against other hunts. (Non-hunt writers such as `append-tsv` do not take this lock; accepted as a documented low-risk window.)
- Reports are generated only in `finalize_strong_accept` (`hunt.sh:2692`, gated on `sa_count>0`), while ledger materialize succeeds earlier (`hunt.sh:2659-2663`) — harvest therefore has two distinct emission points.

## Goals / Non-Goals

**Goals:**

- Run `./hunt.sh` unchanged for the default in-repo flow; project mode is strictly opt-in via environment.
- One command (`HUNT_PROJECT=mytopic ./hunt.sh`) runs a hunt against an external topic and leaves a harvest in the external folder.
- Harvest is reliable: same-day re-runs never overwrite, and a crash between commit and harvest cannot silently drop rows.
- Configuration mistakes surface early: registration validates the direction file; startup prints the effective mode.
- Master ledger, dedup retrieval, audit, and publication behave byte-for-byte as today in both modes.

**Non-Goals:**

- Per-project ledgers, databases, or state roots.
- Symlink-based content mounting (rejected; see Context).
- Re-import or merge of harvest slices. Slices are read-only snapshots; `origin_stable_id` is position-dependent (`lib/history_store.py:895-912`), so a re-imported slice would mint fresh identities.
- Changes to `publish.sh` / `settle.sh` / awr-side. Weekly reports do not flow through `hunt.sh`; awr-side revision artifacts never enter the ledger. Neither is part of harvest.
- Predicate/project-filtered exports across runs (the registry mark covers per-project harvest; ad-hoc per-project lookup is documented SQL, not a new command).
- Backfilling `project` provenance onto historical rows.

## Decisions

### 1. Project mode selects a direction source and a harvest destination — nothing else

`HUNT_PROJECT_DIR` (absolute path) or `HUNT_PROJECT` (name looked up in the registry) puts `hunt.sh` in project mode. All ledger/DB/`ideas/`/`tmp/` paths stay checkout-relative constants as today. Setting both variables, or either together with an explicit `RESEARCH_DIRECTION_FILE`, fails closed — silently picking one would be worse than an error. At startup, `hunt.sh` prints a one-line mode banner (`mode=project <name> <dir>` or `mode=default`) after the direction snapshot succeeds (after `hunt.sh:2164`), before any provider starts, so a misspelled env var is obvious. The direction identity is deliberately not printed: the banner stays one line, and the identity is already persisted in `tmp/history-startup/direction-identity.json`.

Rejected alternative: dual `tool_root`/`content_root` roots. Roughly ten `lib/...` relative call sites in `hunt.sh` plus per-project DB bootstrap, for a benefit (ledger outside the repo) the operator did not ask for.

### 2. Direction staging by copy into `tmp/history-startup/`

`hunt.sh` requires `<project>/direction.json` to be a regular non-symlink file inside a real non-symlink project directory outside the checkout, then `cp`s it into `tmp/history-startup/` (the directory `hunt.sh:2146-2147` already rebuilds immediately before the startup direction snapshot; the staged copy lands after the rebuild at `2147` and before the snapshot at `2153`) and sets `RESEARCH_DIRECTION_FILE` to the staged relative path. The staged copy is an owned single-link regular file, so `lib/direction_contract.py` loads it through the existing `O_NOFOLLOW` path unchanged. The first fresh round's `rm -rf tmp/round` (`hunt.sh:2313`) cannot touch it, and the staged file only needs to survive until the one-shot snapshot at `hunt.sh:2153-2164`.

Rejected alternatives: teaching the contract to accept absolute paths (weakens a deliberate security posture), or staging into `tmp/round/` (created too late and wiped per round).

### 3. Harvest mark comes from the registry, with crash-safe at-least-once semantics

The registry entry per project carries `last_harvested_sequence`. The harvest mark is that value when present, else the `MAX(source_sequence)` captured at preflight — inside the hunt lock, before any commit — via a new `history_cli.py --db <db> max-sequence` subcommand and held in a shell variable. `export-slice` gains an optional `--project <name>` that, on success, advances `last_harvested_sequence` to the exported maximum (or the mark for an empty slice). A crash after commit but before harvest therefore re-exports the orphaned rows on the next run instead of skipping them.

`HUNT_PROJECT_DIR` (no registry entry) uses the preflight-captured session mark, advanced in shell memory after each successful harvest; there is no crash recovery — the crash-recovery scenario in the spec applies to registry-backed projects only.

Rejected alternative: reading the mark from `.ai-ideas/ledger-current.json`. Its `sequence` is the *projection* sequence (observed 25 against 604 rows) and the pointer lags a crash between commit and reconcile — it is not a `source_sequence` source.

### 4. Run-scoped harvest filenames; the report copy uses the run-bound path

Slice and manifest names carry the run id (`ledger-slice-<run_id>.tsv`, `manifest-<run_id>.json`, run id = the existing per-round identifier behind `$RUNS_DIR/<run_id>`, generated per round at `hunt.sh:2316`), so same-day re-runs accumulate. `export_slice` additionally refuses an existing destination (fail closed, not silent overwrite). The report copy uses `RECOVERY_REPORT_PATH`, which `finalize_strong_accept` sets on success via `verify_pending_report_binding` (`hunt.sh:2050/2067`), so it is guaranteed non-empty at that point and names exactly this run's report — never a previous run's. Rounds with `sa_count==0` legitimately produce a slice plus manifest and no report copy.

The manifest is small JSON: project, run id, date, direction id + sha256, mark, max exported sequence, row count, verdict distribution, slice/report file names, and a read-only note. The first harvest in a project also writes a static `harvest/README.md` stating what these files are and why re-import is unsupported.

### 5. Provenance injection is an explicit flag, matching module convention

`commit-round` gains `--project <name>` (parser `lib/history_runtime.py:9229`, dispatch `:9497-9509`, `commit_round` `:7868-7878`), passed through by `history_append_rows` (`hunt.sh:943-953`). The value is the registry name for `HUNT_PROJECT`, or the directory basename for `HUNT_PROJECT_DIR`; resolution happens in shell, never by the library reading env. Idempotent replay (`lookup_round_commit`, `:8007-8016`) returns the prior commit without rewriting provenance — verified by test.

Rejected alternative: letting `history_runtime.py` read `HUNT_PROJECT*` from the environment. The module reads no env today; introducing the first hidden input for a convenience the shell already provides is a coupling regression.

### 6. Registry: validated writes, real read paths, no-DB subcommands

`.ai-ideas/projects.json` — `{"version": 1, "projects": {"<name>": {"path": "/abs", "added": "YYYY-MM-DD"}}}`. Registration deliberately does **not** write `last_harvested_sequence`: the field stays absent until the first successful `export-slice --project` records it, and absence means "no harvest mark recorded" (the run falls back to the preflight mark). Writing `0` at registration would export the entire historical ledger on the first run. Readers fail closed on an unknown `version`. Subcommands on `history_cli.py`, handled before the `--db` requirement exactly like `evaluate` (the registry is hunt-level local state, not store data):

- `project-add <name> <abs-path>` — normalizes to an absolute path; rejects relative input, nonexistent/symlinked/in-checkout directories, a symlinked or unparseable `direction.json`, and invalid names (`[A-Za-z0-9._-]+`). The external `direction.json` is validated with `direction_contract.parse_contract_bytes` plus an explicit `lstat` symlink check — `load_contract` only accepts repository-relative paths, so it cannot be used here; contract errors are surfaced verbatim.
- `project-path <name>` — prints the stored path and `last_harvested_sequence` (unset until the first harvest; the shell read path — unknown name errors with the registered list).
- `project-list` — enumeration. Removal is a one-key hand edit of the JSON; no subcommand.

Rejected alternative: hand-editing the JSON. It stays possible (one object), but the shell needs a real resolution path and registration is the cheapest place to fail direction-file errors.

### 7. Safety rails mirror the repository's existing posture

Preflight refuses, before any provider starts: symlinked project directory or `direction.json`; project directory equal to or inside the checkout; missing `direction.json`; relative `HUNT_PROJECT_DIR`; conflicting configuration (Decision 1); unreadable mark (missing/corrupt DB fails closed). Slice destinations refuse canonical state targets, symlinked final components, and pre-existing files.

## Risks / Trade-offs

- **Slice misuse**: an operator might try to re-import a slice. Mitigation: `--help`, manifest, and `harvest/README.md` all state the read-only rule; re-import remains possible only through the normal `append-tsv` path, which mints new identities — accepted and documented.
- **Non-hunt writers during a project run**: `append-tsv`/`sync-ledger` do not take `tmp/hunt.lock`; manual imports mid-run land in the slice. Low risk for a single-operator tool; documented.
- **Pointer rot**: moving an external folder requires re-running `project-add`; already-written harvests are unaffected. Documented.
- **Registry statefulness**: `last_harvested_sequence` is the first mutable field in an otherwise declarative registry. Accepted for crash-safe harvest; it is advisory (worst case is a duplicated slice, never a lost one).
- **hunt.sh growth**: project-mode logic (~100 lines) is isolated in two functions near the existing preflight/finalize blocks, following the `lib/mirror_pre.sh` sourcing precedent if it grows further.
- **Fresh clones have no DB**: project mode requires an existing `.ai-ideas/history.sqlite3` (the mark must be readable); on a fresh clone the operator runs one default hunt first so `history_sync` creates the DB. Preflight fails closed on a missing DB.
