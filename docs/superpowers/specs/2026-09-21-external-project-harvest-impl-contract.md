# External Project Harvest — Implementer Contract

Repo: `/Users/qinningxu/code/ai-ideas`, branch `feat/external-project-harvest`. Scope: OpenSpec `external-project-harvest` tasks 1.1–5.3. Authoritative order on conflict: OpenSpec spec.md → design.md → tasks.md → this contract (this contract only *pins* those sources to verified code; section 7 lists every place the architect had to resolve under-specification, with the resolution).

Every line number below was re-verified against the checked-out source on 2026-09-21. Where tasks.md/design.md citations drifted, the anchor is marked **DRIFT** with the corrected value. Nothing cited is semantically broken — all drift is ≤4 lines.

## 1. Verified anchors

### `lib/history_store.py` (5422 lines)

| Item | Anchor | Notes |
|---|---|---|
| `HEADER` | `:71` | `b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"` |
| `connect(path)` | `:569-578` | sets `conn.row_factory = sqlite3.Row` (index *and* key access); creates DB + parent via `_durable_mkdir` — **a missing DB is silently created**, see §5 preflight note |
| `_meta` / `_set_meta` | `:1741-1743` / `:1746-1754` | |
| `_store_state_root(conn)` | `:1756-1760` | raises `HistoryStoreError("store is not bound to a durable state root")` if unset |
| `_render_projection_rows(header, rows)` | `:1763-1773` | `rows` iterable of `(raw_row, row_terminator)`; index access `row_value[0]`, `row_value[1]`; joins `header` + each raw row with its stored terminator, inserting `b"\n"` only if the header lacks a trailing newline. Byte-exact by construction |
| `_render_tsv_in_transaction(conn)` | `:1776-1782` | header priority at `:1777-1778`: `header_b64 = _meta(conn, "ledger_header_b64")`; `header = HEADER if header_b64 is None else base64.b64decode(header_b64)`; rows `SELECT raw_row, row_terminator FROM candidates ORDER BY source_sequence` |
| `origin_stable_id(...)` | `:895-912` | position-dependent (`row_number` in hash material) — the re-import caveat; matches design cite exactly |
| `_durable_mkdir(path)` | `:1190-1204` | recursive, refuses symlinked/non-dir components (`HistoryStoreError`) |
| `_is_within(path, directory)` | `:4098-4103` | `relative_to` try/except |
| `_validate_destination(conn, path, state_root=None, allow_ledger_projection=False)` | `:4106-4158` | refuses symlinked final component `:4110-4111`; protected set = DB + `-wal/-shm/-journal`, `Path.cwd()/"ledger.tsv"`, `Path.cwd()/"tmp"/"ledger.good"`, plus state-root-parent `ledger.tsv`/`tmp/ledger.good` when `state_root` given `:4114-4135`; refuses existing **non-file** `:4136-4137` and aliasing `:4138-4143`; refuses anything inside resolved state root `:4144-4147`; returns `destination.resolve()`. **Allows pre-existing regular files** — export_slice adds its own refusal (§2) |
| `export_tsv(conn, path)` | `:4161-4167` | model for `export_slice`; reads `state_root` from meta (tolerates `None`), validates, renders, `_atomic_replace(destination, data, None, None, None)`; returns `{"path", "sha256", "byte_count"}`. Takes **no** `_export_lock` |
| per-row provenance dump | `:3918-3923` | `json.dumps(dict(provenance), sort_keys=True, separators=(",", ":"), ensure_ascii=False)` stored verbatim in `provenance_json`. Design cite `:3918-3925` — **DRIFT: dump is :3918-3923** (:3924-3925 are INSERT close) |
| `append_rows` / `append_rows_idempotent` | `:4026-4036` / `:4039-4061` | both wrap `_append_rows_locked` in `_export_lock(state_root)` |
| `lookup_round_commit(conn, *, commit_key, request_sha256, request_json)` | `:4064-4088` | `None` if no row; raises `HistoryStoreError("round commit replay conflicts with durable receipt")` on request mismatch; else returns stored `result_json` with `replayed: True` |
| `_atomic_replace(path, data, temp_fault, rename_fault, parent_fault, requested=None)` | `:4742-4761` | **DRIFT: design said :4742-4763** (4762-4763 are blank). Creates parents via `_durable_mkdir(path.parent)` `:4744` (so `harvest/` auto-created, symlink parents refused), mkstemp in parent + fsync + `os.replace` + dir fsync |
| `_export_lock(state_root)` | `:4765-4779` | |
| state-root binding | `:2736-2743` | DB binds absolute resolved state root at first import; exact match to design |
| header meta binding | `:2768-2772` | import seals `ledger_header_b64` from plan |
| `COALESCE(MAX(source_sequence),0)` idiom | `:3852-3855` | reuse for `max_source_sequence` |

### `lib/history_cli.py` (302 lines — whole file read)

- Imports `:10-19`: dual-import block (`try: from lib import history_store ... except ImportError: import history_store ...`). `direction_contract` is **not yet imported** — add it to both branches. Add `import re`, `import stat`, `import datetime` to the stdlib imports `:4-8`.
- `_print(value)` `:29-32`: `json.dumps(value, sort_keys=True, ensure_ascii=False)` — **every subcommand prints one JSON object**; new commands must conform (hunt.sh parses this, §3).
- `_fsync_directory` `:35-40`; durable-write pattern to copy: `write_json_artifact` `:66-93` (mkstemp in parent → write → flush → fsync → `os.replace` → `_fsync_directory(parent)` → unlink temp in finally). Do **not** call `_validate_destination` for the registry file — it lives inside the state root, which `_validate_destination` forbids.
- **Naming DRIFT**: the parser builder is `parser()` `:96-155`, not `build_parser`. `--db` is a global flag `:98`, subparsers `:99` (`required=True`). `export-tsv` wiring `:113-114`. `evaluate` wiring `:149-154`.
- `main()` `:158-298`. `evaluate` no-DB bypass `:161-176` (dispatches, prints, `return`); `--db` gate `:177-180` (`argument_parser.error("--db is required for store and retrieval commands")`); `conn = history_store.connect(args.db)` + `init_schema` `:181-182`; dispatch chain `if/elif` `:184-295`; `export-tsv` dispatch `:221-222`; `_print(value)` `:296`; `finally: conn.close()` `:297-298`.
- Error style precedent for operator-facing refusals: `lib/direction_contract.py:711-719` — `except (DirectionContractError, OSError) as exc: parser.error(str(exc))` (exit 2, message on stderr).

### `lib/history_runtime.py` (9524 lines)

- `direction_contract` imported as `direction_contract_lib` `:25` / `:34`. **Zero `os.environ`/`os.getenv` in the file** (grep-verified) — keep it that way.
- `commit_round(...)` signature `:7868-7879` — keyword-only: `db_path, policy_path, batch_path, selection_path, comparison_index_path, review_plan_path, review_index_path, aggregation_path, authority`.
- `request` dict `:7965-7983`; portable extension `:7984-7987`; `request_json` `:7988-7990`; `request_sha` `:7991-7998`; `commit_key` `:7999-8002` — **`project` must touch none of these**.
- Replay: `lookup_round_commit` `:8007-8014`; `if prior is not None: return prior` `:8015-8016` — returns **before** `commit_metadata` is built, so replay can never rewrite provenance.
- `commit_metadata` `:8033-8050`; portable extension `:8051-8054`; `append_rows_idempotent(...)` `:8055-8066`. **Insertion point for `project`: between `:8054` and `:8055`.**
- Parser: `commit = subparsers.add_parser("commit-round")` `:9229`; args `:9230-9237`; `_add_cli_authority_arguments(commit)` `:9238`.
- Dispatch `:9497-9509` (`commit_round(...)` kwargs); result printed as compact JSON `:9512-9519`.

### `lib/direction_contract.py`

- `DirectionContractError(ValueError)` `:35-36`; `_error` `:39-40`.
- `parse_contract_bytes(raw)` `:138-157` → returns `(value, canonical_raw, {"direction_id": ..., "sha256": ...})`; raises `DirectionContractError` with human text (e.g. `"contract is not valid UTF8 JSON"`-style messages via `_error`).
- `_relative_components` `:160-169` rejects absolute paths `:167-168` → `load_contract(source, repo_root)` `:220-222` is **repository-relative only** (via `_open_contract` `:172-217`, `O_NOFOLLOW` + `st_nlink==1` `:194`). Registration of an external file must therefore use `parse_contract_bytes(file.read_bytes())` + explicit `os.lstat` symlink/regular checks, exactly as design Decision 6 says.
- `write_snapshot` `:439-460`: with a source, identity output = `canonical_bytes({"direction_id","sha256"})`; without source, identity output is `b"null\n"`. hunt.sh's snapshot writes `tmp/history-startup/direction-identity.json` — the manifest reads `direction_id`/`sha256` from it (always non-null in project mode).

### `hunt.sh` (2708 lines)

| Item | Anchor |
|---|---|
| env header comment block | `:30-37` (`RESEARCH_DIRECTION_FILE` `:36-37`) |
| `set -u` `:38`; `cd "$(dirname "$0")"` `:40` | cwd == checkout for everything below |
| env defaults block | `:255-267` (`RESEARCH_DIRECTION_FILE=${RESEARCH_DIRECTION_FILE:-}` `:267`) |
| globals | `LOG=hunt.log` `:269`, `RD=tmp/round` `:270`, `LOCK=tmp/hunt.lock` `:271`, `RECOVERY_REPORT_PATH=` `:281`, `HISTORY_DB=.ai-ideas/history.sqlite3` `:284`, `HISTORY_STATE_ROOT=.ai-ideas` `:285`, `HISTORY_POLICY` `:286`, `RUNS_DIR` `:292` |
| `log()` `:295-297` | `[ts] msg` to stdout **and** hunt.log — the banner channel |
| `validate_config` `:315-368` | called `:2121` |
| `acquire_hunt_lock` `:381`; called `:2124`; cleanup trap `:2125-2135` | refusals must happen **after** the trap is armed so the lock holder is reaped |
| `history_runtime_authorized` `:575-589`; `history_audit_init` `:591-595`; `history_reconcile_ledger` `:709-714` (the `python3 lib/history_cli.py --db "$HISTORY_DB" <cmd>` shape to copy); `history_policy_mode` `:716-725` (python-heredoc JSON-parse idiom) | |
| `history_append_rows` `:943-953`; `history_commit_round` `:955-957`; `history_materialize_ledger` `:959-961` | |
| `verify_pending_report_binding` `:1661-1787` | report path matches `ideas/<today>_hunt[-N].md` `:1736-1738`; prints repo-relative `report_path` `:1785` |
| `finalize_strong_accept` `:2045-2081` | `RECOVERY_REPORT_PATH` set on success `:2050` and `:2067`; do not modify this function |
| main flow | HALT sentinel `:2138-2141`; `startup_root=tmp/history-startup` `:2145`; rebuild `rm -rf`/`mkdir -p` `:2146-2147`; snapshot args `:2148-2158` (consumes `RESEARCH_DIRECTION_FILE` at `:2153`); snapshot run `:2159-2163`; `direction_active` `:2164-2167`; `history_audit_init` `:2168-2171`; `history_sync` `:2172-2176`; `today=$(date +%F)` `:2180` |
| round loop | `rm -rf "$RD"` `:2313`; `run_id="$(date +%Y%m%dT%H%M%S)-p$$-r${round}"` `:2316` — fresh per round |
| commit/materialize/finalize | `sa_count` `:2646-2647`; `history_commit_round` `:2648-2658`; **`history_materialize_ledger` success `:2659-2663`**; `fails=0` `:2664`; `seal_decision_outcome` `:2665`; archive `:2669-2689`; **`finalize_strong_accept || exit $?` `:2692`** inside `sa_count>0` block `:2691-2699` |

**Function-slice extraction constraints** (`tests/shell_harness_correctness_regression.py`, whole file read): the test extracts and *executes* three slices of hunt.sh — `acquire_hunt_lock() {`→`pick_lens() {` (`:24`,`:58`; spans hunt.sh `:381-523`), `finalize_strong_accept() {`→`fail_round() {` (`:86`; spans `:2045-2082`), `theme_in_vocabulary() {`→`axiom_ok() {` (`:123`; spans `:1422-1451`). It also asserts presence of needles incl. `finalize_strong_accept || exit $?` (`:115`). **New function definitions must not land inside those three spans; `finalize_strong_accept`'s body and its call-site text must stay.**

**Forbidden patterns** (`tests/verify_product_contract.py:975-988`): `>> ledger.tsv`, `cp ledger.tsv` / `cp $LEDGER_GOOD` / `cp $HISTORY_LEDGER_GOOD`, `HISTORY_RUNTIME_TEST_MODE`, meta-role invocations. `tests/history_runtime_smoke.sh:23-44` and `tests/runtime_abi_smoke.sh:489-505` grep hunt.sh structurally — additive code is safe; never write `cp ledger.tsv`. The product-contract needles `:1028-1095` are presence-only → additive changes safe.

**`.gitignore:3` = `.ai-ideas/`** → `.ai-ideas/projects.json` is git-ignored as designed. `ledger.instance-id` is tracked (present in fresh clones), so `sync-ledger` bootstraps a fresh DB.

## 2. Python API specs

### 2.1 `lib/history_store.py` — insert immediately after `export_tsv` (`:4167`)

```python
def export_slice(conn, after_sequence, dest):
```

Behavior, in this exact order:
1. `type(after_sequence) is not int or after_sequence < 0` → `ValueError("after_sequence must be a nonnegative integer")`.
2. Rows: `conn.execute("SELECT raw_row, row_terminator, source_sequence FROM candidates WHERE source_sequence > ? ORDER BY source_sequence", (after_sequence,)).fetchall()`.
3. Header — same priority as `_render_tsv_in_transaction` (`:1777-1778`): `header_b64 = _meta(conn, "ledger_header_b64")`; `header = HEADER if header_b64 is None else base64.b64decode(header_b64)`.
4. `data = _render_projection_rows(header, rows)` (`:1763`). Byte-exact incl. CRLF `row_terminator`s.
5. `max_exported = max((row[2] for row in rows), default=None)`; if `None` → `max_exported = after_sequence` (mark returned when empty).
6. Destination: `state_value = _meta(conn, "state_root")`; `state_root = None if state_value is None else pathlib.Path(state_value)`; `destination = _validate_destination(conn, dest, state_root)`; then **`if destination.exists(): raise ValueError(f"destination already exists: {destination}")`** (the pre-existing refusal — `_validate_destination` alone permits existing regular files).
7. `_atomic_replace(destination, data, None, None, None)` — auto-creates `harvest/` parent via `_durable_mkdir`, refuses symlinked parents, atomic + durable.
8. No `_export_lock` (matches `export_tsv`; the hunt is the only writer and commits precede harvest in-process).
9. Return: `{"path": str(destination), "sha256": _sha(data), "byte_count": len(data), "row_count": len(rows), "after_sequence": after_sequence, "max_sequence": max_exported}`.

```python
def max_source_sequence(conn):
    return conn.execute(
        "SELECT COALESCE(MAX(source_sequence), 0) FROM candidates"
    ).fetchone()[0]
```

### 2.2 `lib/history_cli.py` — registry helpers (module level, above `parser()`)

Registry path is **cwd-relative**: `_REGISTRY_PATH = pathlib.Path(".ai-ideas") / "projects.json"`. Justification: every sibling CLI path default is cwd-relative (`--ledger ledger.tsv`, `--state-root .ai-ideas`, `_validate_destination`'s `Path.cwd()` protection `:4121-4122`), and hunt.sh guarantees cwd == checkout (`hunt.sh:40`). This also lets tests run the CLI with a temp cwd without touching the real registry. Do **not** anchor at `__file__` and do **not** add an env override (no env reads in `lib/`).

- `_load_project_registry()` → missing file ⇒ `{"version": 1, "projects": {}}`; JSON decode failure / top level not a dict / `projects` not a dict ⇒ `ValueError("project registry is corrupt: ...")`; `version != 1` ⇒ `ValueError(f"unsupported project registry version: {version!r}")` **before touching any entry** (fail closed for every command, read and write).
- `_save_project_registry(registry)` → `history_store._durable_mkdir(_REGISTRY_PATH.parent)`, then the `write_json_artifact` durable pattern (`:76-88`: mkstemp in parent, fsync, `os.replace`, `_fsync_directory`), bytes = `json.dumps(registry, sort_keys=True, indent=2, ensure_ascii=False) + "\n"`.
- `_validate_project_name(name)` → `re.fullmatch(r"[A-Za-z0-9._-]+", name)` and `name not in (".", "..")`, else `ValueError(f"invalid project name: {name!r}")`.
- `_project_add(name, raw_path)`:
  1. `_validate_project_name(name)`.
  2. `os.path.isabs(raw_path)` else `ValueError("project path must be absolute")`.
  3. `candidate = pathlib.Path(raw_path)`; `candidate.is_symlink()` ⇒ `ValueError("project directory cannot be a symlink")`.
  4. `resolved = candidate.resolve(strict=True)` (`FileNotFoundError`/`NotADirectoryError` ⇒ `ValueError("project directory does not exist: ...")`); `resolved.is_dir()` else same error.
  5. In-checkout: `checkout = pathlib.Path.cwd().resolve()`; `resolved == checkout or checkout in resolved.parents` ⇒ `ValueError("project directory cannot be the checkout or inside it")`.
  6. `direction = resolved / "direction.json"`; `st = os.lstat(direction)` (`FileNotFoundError` ⇒ `ValueError("project direction.json is missing")`); `stat.S_ISLNK(st.st_mode)` ⇒ `ValueError("project direction.json cannot be a symlink")`; `stat.S_ISREG(st.st_mode)` else `ValueError("project direction.json is not a regular file")`.
  7. `value, canonical_raw, identity = direction_contract.parse_contract_bytes(direction.read_bytes())` — `DirectionContractError` propagates **verbatim** (main wraps it into `argument_parser.error`, §3).
  8. Load registry (version fail-closed). Re-adding an existing name **replaces** `path`/`added` and **preserves** an existing `last_harvested_sequence` (pointer-rot flow, see §7.6). Otherwise entry = `{"path": str(resolved), "added": datetime.date.today().isoformat()}` — **never** write `last_harvested_sequence` here.
  9. Save; return `{"name": name, "path": str(resolved), "direction_id": identity["direction_id"], "direction_sha256": identity["sha256"], "registered": True}`.
- `_project_path(name)` → entry lookup; unknown ⇒ `ValueError(f"unknown project {name!r}; registered: {', '.join(sorted(projects)) or 'none'}")`. Return `{"name": name, "path": entry["path"], "last_harvested_sequence": entry.get("last_harvested_sequence")}` (JSON `null` when absent).
- `_project_list()` → `{"projects": [{"name","path","last_harvested_sequence"} for each, sorted by name]}` (`null` marks when absent).
- `_advance_project_mark(name, sequence)` → load registry; unknown name ⇒ same `ValueError` as `_project_path`; `entry["last_harvested_sequence"] = max(int(sequence), entry.get("last_harvested_sequence", 0))` (monotone — never regress); save; return the entry.

### 2.3 `lib/history_runtime.py` — provenance flag

1. Parser, after `:9237` (`commit.add_argument("--aggregation", required=True)`): `commit.add_argument("--project")`.
2. Signature `:7868-7879`: add `project=None` after `authority` (stays keyword-only).
3. Body, between `:8054` and `:8055` (after the portable `execution_boundary` extension, before `append_rows_idempotent`):
   ```python
   if project is not None:
       commit_metadata["project"] = project
   ```
4. Dispatch `:9499-9508`: add `project=args.project`.
5. **Do not touch** `request` (`:7965-7983`), `request_json`/`request_sha` (`:7988-7998`), `commit_key` (`:7999-8002`). Replay (`:8015-8016`) returns prior provenance unchanged by construction — the Task 3.2 test must prove it.
6. No env reads. The value arrives only from hunt.sh.

## 3. CLI specs (`lib/history_cli.py`)

Parser additions in `parser()` (after `export-tsv` `:113-114`; help text must state the read-only rule, Task 5.2):

```python
slice_export = commands.add_parser(
    "export-slice",
    help=("export ledger rows above a sequence mark as a byte-exact TSV "
          "slice; the slice is a read-only snapshot and re-import is unsupported"),
)
slice_export.add_argument("--after-sequence", required=True, type=int)
slice_export.add_argument("--dest", required=True)
slice_export.add_argument("--project")
commands.add_parser("max-sequence")
project_add = commands.add_parser("project-add")
project_add.add_argument("name")
project_add.add_argument("path")
project_path = commands.add_parser("project-path")
project_path.add_argument("name")
commands.add_parser("project-list")
```

Dispatch in `main()`:

- **No-DB bypass** — insert after the `evaluate` block (`:176`), before the `--db` gate (`:177`), same shape:
  ```python
  if args.command in ("project-add", "project-path", "project-list"):
      try:
          if args.command == "project-add":
              value = _project_add(args.name, args.path)
          elif args.command == "project-path":
              value = _project_path(args.name)
          else:
              value = _project_list()
      except (ValueError, OSError) as exc:
          argument_parser.error(str(exc))
      _print(value)
      return
  ```
  (`DirectionContractError` subclasses `ValueError`, so contract errors surface verbatim through this path — exit 2, stderr message.)
- **DB commands** — in the dispatch chain after `export-tsv` (`:221-222`):
  ```python
  elif args.command == "export-slice":
      if args.after_sequence < 0:
          argument_parser.error("--after-sequence must be nonnegative")
      if args.project is not None:
          _project_path(args.project)  # refuse unknown names before writing
      value = history_store.export_slice(conn, args.after_sequence, args.dest)
      if args.project is not None:
          _advance_project_mark(args.project, value["max_sequence"])
          value = dict(value, project=args.project)
  elif args.command == "max-sequence":
      value = {"max_sequence": history_store.max_source_sequence(conn)}
  ```
  Wrap the two registry calls in the same `try/except ValueError → argument_parser.error` style (hoist a small helper or duplicate the 3 lines). Order is deliberate: validate name → export → advance mark.

Exact stdout shapes (all via `_print`, one JSON object, `sort_keys=True` — these literal key names are the contract hunt.sh parses):
- `max-sequence` → `{"max_sequence": 604}` (0 when no rows).
- `export-slice` → `{"after_sequence": N, "byte_count": B, "max_sequence": M, "path": "/abs/…tsv", "row_count": K, "sha256": "…"}` (+ `"project"` when passed).
- `project-path` → `{"last_harvested_sequence": null, "name": "mytopic", "path": "/abs"}` (integer after first harvest).
- `project-list` → `{"projects": [{"last_harvested_sequence": …, "name": …, "path": …}, …]}`.
- `project-add` → `{"direction_id": …, "direction_sha256": …, "name": …, "path": …, "registered": true}`.

Exit codes: `argument_parser.error` ⇒ 2 for every refusal (unknown name, bad version, invalid dir/direction); uncaught store `ValueError`s from `export_slice` (existing dest, canonical target) may propagate as today (traceback, exit 1) — hunt.sh treats any nonzero as harvest failure.

## 4. Data schemas

### `.ai-ideas/projects.json` (version 1)

```json
{
  "version": 1,
  "projects": {
    "mytopic": {
      "path": "/abs/resolved/path",
      "added": "YYYY-MM-DD",
      "last_harvested_sequence": 604
    }
  }
}
```
- `last_harvested_sequence` is **absent until the first successful `export-slice --project`**; absence = "no harvest mark recorded" (never write `0` at registration).
- Unknown `version` ⇒ every registry command fails closed without reading/writing entries.
- Written with `sort_keys=True, indent=2`, trailing newline, durable replace (§2.2).

### `<project>/harvest/manifest-<run_id>.json`

Exact key list (written by hunt.sh, atomic replace):
```json
{
  "schema_version": 1,
  "project": "<name>",
  "run_id": "<run_id>",
  "date": "<$today>",
  "direction_id": "<id>",
  "direction_sha256": "<64-hex>",
  "after_sequence": N,
  "max_sequence": M,
  "row_count": K,
  "verdicts": {"accept": 1, "accept-w-rev": 2},
  "slice_file": "ledger-slice-<run_id>.tsv",
  "report_files": [],
  "note": "Read-only snapshot; re-import is unsupported (row identity is position-dependent)."
}
```
- `direction_id`/`direction_sha256` from `tmp/history-startup/direction-identity.json` (never null in project mode).
- `after_sequence` = the mark actually used; `max_sequence`/`row_count` from the export-slice JSON.
- `verdicts`: distribution computed from the slice's own data rows (skip header line; verdict = field index 4 of the tab split; strip trailing `\r`); only observed verdicts, keys sorted.
- `report_files`: `[]` at slice time; the report-copy step rewrites the manifest with the copied basename(s) — see §7.4.

### `<project>/harvest/README.md` (one-time, static)

Written only if absent. States: these files are per-round read-only snapshots of the master ledger in the ai-ideas checkout; slices are byte-exact row copies; re-import is unsupported because row identity (`origin_stable_id`) is position-dependent and a re-import mints new identities; query the master ledger for history.

## 5. hunt.sh spec

### 5.1 Variables

After `:267` (`RESEARCH_DIRECTION_FILE=${RESEARCH_DIRECTION_FILE:-}`):
```bash
HUNT_PROJECT=${HUNT_PROJECT:-}
HUNT_PROJECT_DIR=${HUNT_PROJECT_DIR:-}
```
After `:281` (`RECOVERY_REPORT_PATH=`):
```bash
PROJECT_MODE=0
PROJECT_NAME=
PROJECT_DIR=
PROJECT_MARK=0
```

Env header comment (after `:37`, same style):
```
#   HUNT_PROJECT / HUNT_PROJECT_DIR
#       Optional external project mode: stage <project>/direction.json by
#       copy, tag committed rows with the project name, and harvest a
#       per-round ledger slice back to <project>/harvest/.
```

### 5.2 Functions — two functions, inserted between `history_materialize_ledger` (`:959-961`) and `prepare_external_mirror` (`:963`)

This spot is outside all three extracted slices (§1). `project_mode_harvest` takes a phase argument so the design's "two functions" budget is honored literally.

**`project_mode_preflight()`** — returns 0 (or 0 immediately when both env vars empty: default flow untouched), 2 on any refusal; caller exits. Uses `log` for every refusal reason. Steps in order:
1. Both unset → `return 0`. Set `PROJECT_MODE=1`.
2. Both set → refuse. `RESEARCH_DIRECTION_FILE` non-empty → refuse ("project mode cannot combine with RESEARCH_DIRECTION_FILE").
3. Resolve:
   - `HUNT_PROJECT`: one python heredoc that runs `project-path` via subprocess (mirrors the `history_policy_mode` idiom `:716-725`; on nonzero exit writes the CLI's stderr through and `raise SystemExit(2)` — this carries the "unknown project … registered: …" message) and prints two lines: `path`, then `last_harvested_sequence` (empty line when JSON null). Capture and split with `sed -n 1p`/`2p`. `PROJECT_NAME=$HUNT_PROJECT`.
   - `HUNT_PROJECT_DIR`: `case "$HUNT_PROJECT_DIR" in /*) ;; *) refuse relative`; `[ -L … ]` refuse symlink; `[ -d … ]` else refuse; `PROJECT_DIR=$(cd "$HUNT_PROJECT_DIR" && pwd -P)`; `PROJECT_NAME=$(basename "$PROJECT_DIR")`.
4. Shared refusals: `[ -L "$PROJECT_DIR" ]` refuse; `project_physical=$(cd "$PROJECT_DIR" && pwd -P)` else refuse; `case "$project_physical" in "$PWD"|"$PWD"/*) refuse in-checkout`; `PROJECT_DIR=$project_physical`; `direction.json` must exist (`-e`), not be a symlink (`-L`), be a regular file (`-f`). (Contract *parse* validation: registry mode had it at registration; dir mode gets it at the startup snapshot `:2159`, still before any provider.)
5. DB mark (inside the hunt lock, before any commit): **`[ -f "$HISTORY_DB" ] && [ ! -L "$HISTORY_DB" ]` else refuse "project mode requires an existing history database"** — mandatory because `history_store.connect()` silently *creates* a missing DB (`:569-578`), which would turn the fallback mark into 0 and export the entire future ledger. Then capture the fallback mark with a heredoc wrapping `python3 lib/history_cli.py --db "$HISTORY_DB" max-sequence` (nonzero ⇒ refuse "project harvest mark is unreadable").
6. Mark precedence: registry mode **with a recorded** `last_harvested_sequence` ⇒ `PROJECT_MARK=<that>`; otherwise `PROJECT_MARK=<preflight max-sequence>`. Dir mode ⇒ always the preflight mark.

**`project_mode_harvest(slice|report)`** — returns nonzero on failure; callers warn and continue (never roll back, never exit).

Phase `slice` (called right after ledger materialize succeeds):
1. `harvest_dir="$PROJECT_DIR/harvest"`, `slice="$harvest_dir/ledger-slice-$run_id.tsv"`.
2. Build argv: `python3 lib/history_cli.py --db "$HISTORY_DB" export-slice --after-sequence "$PROJECT_MARK" --dest "$slice"`; append `--project "$PROJECT_NAME"` **only when `HUNT_PROJECT` is non-empty** (dir mode: no `--project`, no registry write — shell variable is the only advancement).
3. Run, capture stdout JSON; nonzero ⇒ `return 1` (mark NOT advanced — next round re-exports, overlapping but never losing).
4. One python heredoc (args: export JSON on argv, identity path `tmp/history-startup/direction-identity.json`, project, run_id, `$today`, slice path, harvest dir) that: computes the verdict distribution from the slice; writes `manifest-$run_id.json` atomically with `report_files: []`; writes `README.md` if absent; prints `max_sequence` as its only stdout. `PROJECT_MARK=$(that heredoc)` — shell mark advanced **only on full success**.

Phase `report` (called only in the `sa_count>0` block after finalize):
1. `[ -n "$RECOVERY_REPORT_PATH" ] || return 0` (guaranteed non-empty there — set at `:2050`/`:2067`).
2. `base=$(basename "$RECOVERY_REPORT_PATH")`; `dest="$PROJECT_DIR/harvest/$base"`; `[ -e "$dest" ] || [ -L "$dest" ]` ⇒ warn + `return 1` (fail closed, no overwrite).
3. `cp "$RECOVERY_REPORT_PATH" "$dest" || return 1` (repo-relative source — cwd is the checkout).
4. Small heredoc: load `manifest-$run_id.json`, append `base` to `report_files`, rewrite atomically.

### 5.3 Main-flow sequencing (exact insertion points)

1. **Preflight call** — after the HALT sentinel block (`:2141`), before `startup_root=…` (`:2145`): `project_mode_preflight || exit $?`. (Inside the lock `:2124`, after the cleanup trap `:2125-2135` is armed, before any commit/provider.)
2. **Staging** — between `:2147` and `:2148` (after rebuild, before snapshot args consume the variable at `:2153`):
   ```bash
   if [ "$PROJECT_MODE" = 1 ]; then
     cp "$PROJECT_DIR/direction.json" "$startup_root/direction-project.json" || {
       log "Project direction staging failed"; exit 2; }
     RESEARCH_DIRECTION_FILE="$startup_root/direction-project.json"
   fi
   ```
   The name `direction-project.json` collides with nothing (`direction-identity.json`, `direction-constraint.json` are outputs); the copy is a fresh single-link regular file under non-symlink parents → loads through the unchanged `O_NOFOLLOW`/`st_nlink==1` contract path.
3. **Banner** — after the `direction_active` block's closing `fi` (`:2167`), before `history_audit_init` (`:2168`). This is the precise reading of "after hunt.sh:2164":
   ```bash
   if [ "$PROJECT_MODE" = 1 ]; then
     log "mode=project $PROJECT_NAME $PROJECT_DIR"
   else
     log "mode=default"
   fi
   ```
   `log` puts the line on stdout and in hunt.log; the default flow's only deviation is this one line.
4. **Provenance threading** — rewrite `history_append_rows` (`:943-953`) to build a `local -a append_args=(--db "$HISTORY_DB" --policy "$HISTORY_POLICY" --batch "$1" … --aggregation "$6")` array, append `--project "$PROJECT_NAME"` when `PROJECT_MODE = 1`, then `history_runtime_authorized commit-round "${append_args[@]}"`. All call sites (`:2648` via `history_commit_round`) unchanged.
5. **Harvest point 1** — after the materialize block's `fi` (`:2663`), before `fails=0` (`:2664`):
   ```bash
   if [ "$PROJECT_MODE" = 1 ]; then
     project_mode_harvest slice || log "WARNING: project harvest failed for run $run_id; committed rows remain in the master ledger"
   fi
   ```
6. **Harvest point 2** — after `finalize_strong_accept || exit $?` (`:2692`), before `rm -rf "$ARCHIVE_SOURCE"` (`:2693`):
   ```bash
   if [ "$PROJECT_MODE" = 1 ]; then
     project_mode_harvest report || log "WARNING: project report harvest failed for run $run_id"
   fi
   ```

Per-round behavior falls out: `run_id` is regenerated each round (`:2316`) → one slice+manifest per round; `PROJECT_MARK` advances after each successful slice → round N's slice holds only round N's rows; registry mode persists the same advancement via `--project`.

## 6. Test matrix

Conventions (verified): python tests are `unittest` with `if __name__ == "__main__": unittest.main()`, `ROOT = pathlib.Path(__file__).resolve().parents[1]` + `sys.path.insert` (`tests/history_store_smoke.py:14-19`), run as `python3 tests/x.py`; shell tests are TAP-ish `bash tests/x.sh` with a `FAILURES` counter and `exit 1` (`tests/portable_runtime_abi_smoke.sh:18-21,:488-491`). Suite inventory lives in `CONTRIBUTING.md:24-41` — add the three new files there (Task 5). Fixture model for store tests: `history_store_smoke.py:38-69` (tempdir, `ledger.instance-id`, HEADER+rows ledger, `connect`+`init_schema`, `_import()` = `import_tsv_epoch`; DB binds state root `<temp>/.ai-ideas`).

### 6.1 `tests/history_store_export_slice_smoke.py` (new, unittest)

- `test_slice_is_byte_exact_with_crlf_rows` — import a ledger mixing `\n` and `\r\n` terminators; `export_slice(conn, 1, dest)`; assert `dest.read_bytes() == HEADER + rows[1:]` with original terminators, `row_count == 2`, `max_sequence == 3`.
- `test_slice_honors_custom_meta_header` — after import, `_set_meta(conn, "ledger_header_b64", base64.b64encode(custom).decode())`; slice starts with `custom` verbatim (meta beats `HEADER`).
- `test_slice_empty_is_header_only` — mark = current max ⇒ bytes == header, `row_count == 0`, `max_sequence == mark`.
- `test_slice_mark_above_max` — mark `10**9` ⇒ header-only, `max_sequence == 10**9` (mark monotone).
- `test_slice_refuses_existing_destination` — pre-create dest ⇒ `ValueError`; dest bytes untouched.
- `test_slice_refuses_canonical_targets` — `Path.cwd()/"ledger.tsv"`, `Path.cwd()/"tmp/ledger.good"`, and `self.state_root/"x.tsv"` (after `_import()` binds the state root) each raise `ValueError`; nothing written. (cwd targets are the real repo's — refusal precedes any write, so this is safe.)
- `test_slice_allows_outside_checkout_and_creates_parent` — dest `root/"external"/"harvest"/"slice.tsv"` with `harvest/` absent ⇒ succeeds; returned `sha256`/`byte_count` match the bytes.
- `test_max_source_sequence` — 0 on fresh schema; N after import.

### 6.2 `tests/history_project_registry_smoke.py` (new, unittest, subprocess CLI)

Run `[sys.executable, str(ROOT/"lib/history_cli.py"), …]` with `cwd=<temp fake checkout>` (just needs to exist; registry lands in `<temp>/.ai-ideas/`). External project fixture: dir outside the temp cwd with `direction.json` copied from `directions/dynamic-memory-vla-v1.json`.
- `test_add_accepts_valid_project` — exit 0; stdout JSON has name/resolved path/direction_id; registry file has **no** `last_harvested_sequence`.
- `test_add_rejects_relative_path` / `test_add_rejects_invalid_name` (`"a/b"`, `"a b"`) / `test_add_rejects_nonexistent_dir` / `test_add_rejects_symlinked_dir` — each exit 2, registry unwritten.
- `test_add_rejects_checkout_and_in_checkout_dirs` — path = cwd itself, and a subdir of cwd ⇒ exit 2.
- `test_add_rejects_missing_direction` / `_symlinked_direction` / `_contract_invalid_direction` — exit 2; stderr carries the verbatim contract error; nothing written.
- `test_path_unknown_lists_registered` — stderr contains the registered name; exit 2.
- `test_path_prints_null_mark_before_first_harvest` — `last_harvested_sequence` is JSON null.
- `test_list_enumerates_sorted`.
- `test_unknown_version_fails_closed` — hand-write `"version": 2` ⇒ add/path/list all exit 2; entries untouched.
- `test_export_slice_project_advances_mark` — `init` + `sync-ledger` a small ledger to create rows (the clone has `ledger.instance-id`; here just write one in the temp cwd); `export-slice --after-sequence 0 --dest … --project p1` ⇒ registry mark == exported max; then `project-path` shows it; second export with the registry mark as `--after-sequence` ⇒ header-only.
- `test_export_slice_unknown_project_refused_before_write` — dest not created.

### 6.3 Runtime regression (Task 3.2 — add to `tests/history_runtime_smoke.py`, modeled on `:3470-3488` + `:3619-3630`)

- `test_commit_round_project_provenance` — build `commit_arguments` exactly as `:3470-3483` plus `project="mytopic"`; commit; query `candidates` provenance_json for the new `candidate_ids` ⇒ each parses to a dict with `"project": "mytopic"`; identity hashes unchanged (compare `candidate_ids` against a project-free commit of the same inputs in a sibling DB — or assert `request_sha256` equal between the two).
- `test_commit_round_default_omits_project` — same without `project` ⇒ provenance has no `"project"` key.
- `test_commit_round_replay_keeps_prior_provenance` — commit with `project="mytopic"`, replay same args with `project="other"` (and once with it absent) ⇒ `replayed` is True and stored provenance still says `"mytopic"` (replay returns before `commit_metadata`, `:8015-8016`).

### 6.4 Shell regression (Task 4.4 — new `tests/hunt_project_mode_regression.sh`)

Copy the harness verbatim from `tests/portable_hunt_awr_e2e_smoke.sh`: `make_repo` `:24-44`, `install_fake_providers` (copies `tests/fake_portable_stage_provider.py`) `:54-62`, `instrument_audit_cli` `:46-52`, `install_noop_publish` `:64-73`, `run_bounded` `:75-102` (raise timeout to 60s — cases run multiple hunts), `write_hunt_ledger` `:104-110`. Base env = `run_hunt_v2`'s `:455-484` **minus `RESEARCH_DIRECTION_FILE`** for project cases. Project fixture: `$CASE_ROOT/<case>-project/direction.json` copied from `directions/dynamic-spatial-memory-vla-v1.json` (outside the clone ⇒ outside-checkout). **Every project-mode case needs a pre-existing DB** (fresh clone has none; preflight refuses): cheapest is `python3 lib/history_cli.py --db "$repo/.ai-ideas/history.sqlite3" init` (sync happens in-hunt). Cases:

- `default_flow_parity` — no project env ⇒ log has `mode=default`; no `harvest/` anywhere; round commits as before.
- `dir_mode_success` — `HUNT_PROJECT_DIR` ⇒ log has `mode=project <basename> <dir>`; `direction-identity.json` names the external direction; slice begins with the ledger header; slice data rows == the round's new ledger rows (byte compare against `ledger.tsv` tail); manifest keys/values per §4 (`row_count` == slice data lines); `harvest/README.md` written; provenance of new rows has `project=<basename>`.
- `registry_mode_success` — `project-add` (cwd=clone) then `HUNT_PROJECT` ⇒ same assertions + registry `last_harvested_sequence` == slice max.
- Refusals (each: exit 2, no provider launch marker, `state_digest` unchanged — pattern from `run_hunt_rejection` `:149-185`): both vars set; project var + `RESEARCH_DIRECTION_FILE`; relative `HUNT_PROJECT_DIR`; symlinked dir; symlinked/missing `direction.json`; `HUNT_PROJECT_DIR=$repo` (in-checkout); unknown `HUNT_PROJECT` (log lists registered names); missing DB (delete `.ai-ideas`).
- `same_day_rerun_accumulates` — two `ROUND_LIMIT=1` runs ⇒ two distinct run-id slice+manifest pairs, neither overwritten. (Deterministic fakes make run 2's commit an idempotent **replay** ⇒ run 2's slice is header-only with manifest `row_count: 0` — assert exactly this; it doubles as the empty-round scenario.)
- `second_round_mark_advances` — `ROUND_LIMIT=2` ⇒ two slices; every row in slice 2 has `source_sequence` greater than slice 1's max (tolerate header-only for the replay reason above).
- `sa_shape` — derive the round's `sa_count` from the run archive (`$RUNS_DIR/<run_id>/…/accepted.tsv` line count, cf. `:2646-2647`); assert a report copy exists in `harvest/` **iff** `sa_count>0`, and any copied basename equals the basename of that run's `report-binding.json` `report_path` (never another run's report).
- `crash_recovery_registry_mode` — see §7.5 for why this shape: registry run 1 completes (mark M recorded); then `python3 lib/history_cli.py --db "$db" append-tsv orphans.tsv` appends K rows **without** harvesting (deterministic stand-in for "committed then died before harvest" — the fake provider replays deterministically, so a second live round cannot create orphans); registry run 2 completes ⇒ its slice contains the K orphan rows (plus any new rows), because the mark came from `last_harvested_sequence`, not preflight max.
- Suite hygiene: after the feature lands, `python3 tests/verify_product_contract.py all`, `python3 tests/shell_harness_correctness_regression.py`, `bash tests/history_runtime_smoke.sh`, `bash tests/runtime_abi_smoke.sh`, `bash tests/portable_runtime_abi_smoke.sh`, `bash tests/portable_hunt_awr_e2e_smoke.sh`, and the full python history suite (`CONTRIBUTING.md:24-41`) must stay green; add the new files to that list.

## 7. Mismatches found and resolutions (OpenSpec wins)

1. **Anchor drift (all minor)**: `history_cli.py` parser is `parser()` not `build_parser` (`:96`); evaluate bypass is `:161-176` with the `--db` gate at `:177-180`; provenance dump is `:3918-3923` (design: `:3918-3925`); `_atomic_replace` body ends `:4761` (design: `:4763`). All history_runtime.py and hunt.sh citations in tasks.md/design.md are exact. "After `hunt.sh:2164`" for the banner resolves to **after the `fi` at `:2167`** (end of `direction_active`), before `:2168`.
2. **`_validate_destination` permits pre-existing regular files** (`:4136-4137` refuses only non-files). The spec's "pre-existing files" refusal must be a separate `destination.exists()` check inside `export_slice` (§2.1 step 6). Implied by tasks 1.1; made explicit here.
3. **`history_store.connect()` silently creates a missing DB** (`:569-578`). Without a guard, `max-sequence` on a fresh clone returns 0 and the first harvest would export the entire future ledger. Resolution (design Risk "Fresh clones have no DB … fails closed"): preflight requires `[ -f "$HISTORY_DB" ]` before the mark read (§5.2 step 5).
4. **Manifest lists report files, but the manifest is written before the report exists** (slice point `:2663` precedes finalize `:2692`). Resolution: manifest written with `report_files: []`; the report phase rewrites the same manifest adding the copied basename. The refuse-existing-dest rule applies to `export_slice` slice destinations, not to hunt.sh's own manifest. No spec text forbids this; it is the only ordering satisfying both SHALLs.
5. **The crash-recovery scenario only works when a mark was previously recorded.** If the *first-ever* project run crashes between commit and first harvest, the next run's mark falls back to preflight max-sequence — which already includes the orphans — so they are skipped. This follows necessarily from design Decision 3 (mark absent ⇒ preflight max) and the spec scenario's own wording ("because the mark came from `last_harvested_sequence`"). The test must therefore establish a recorded mark first (§6.4). Not a contradiction; flagged so the implementer doesn't "fix" the fallback.
6. **`project-add` on an already-registered name**: design Decision 6 is silent, but the Risks section makes pointer-rot recovery "re-running `project-add`" — a refusal would break that flow. Resolution: re-registration replaces `path`/`added`, preserves `last_harvested_sequence` (§2.2).
7. **`export_slice` takes no `_export_lock`** — matches `export_tsv` (`:4161-4167`); reads are transactional and the hunt is serial. Design says "reusing the existing render and destination-validation machinery"; adding a lock would be new behavior.
8. **Registry anchored cwd-relative, not `__file__`-relative** — matches the CLI's path convention and hunt.sh's guaranteed cwd (`:40`); `__file__` anchoring would make black-box CLI tests write into the real repo's registry. No env override (no env reads in `lib/`).
9. **Two functions, three entry behaviors**: design's "~100 lines in two functions" is met via `project_mode_preflight` + `project_mode_harvest(slice|report)` (phase argument covers both emission points). JSON lookup heredocs are inline in preflight, not separate functions.
10. **Deterministic fake providers replay commits** (same inputs ⇒ same `selection_sha256` ⇒ `lookup_round_commit` replay, zero new rows). Consequences baked into the test matrix: same-day re-run yields a header-only slice (asserted as the empty-round scenario); crash-recovery orphans are created with `append-tsv` (the documented non-hunt writer) instead of a second live round, because a live second round cannot deterministically commit new rows.

No genuine blocker contradictions found. Do not redesign any of the above; if implementation uncovers new drift, stop and report rather than working around it.
