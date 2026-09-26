#!/usr/bin/env bash
# Offline contract for external project mode (HUNT_PROJECT / HUNT_PROJECT_DIR).
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TEMP_BASE=${TMPDIR:-/tmp}
TEMP_BASE=${TEMP_BASE%/}
CASE_ROOT=$(mktemp -d "$TEMP_BASE/ai-ideas-hunt-project.XXXXXX")
FAILURES=0

cleanup() {
  case "$CASE_ROOT" in
    "$TEMP_BASE"/ai-ideas-hunt-project.*) rm -rf -- "$CASE_ROOT" ;;
    *) printf 'Refusing to remove unexpected path: %s\n' "$CASE_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

fail() {
  printf 'not ok: %s\n' "$1" >&2
  FAILURES=$((FAILURES + 1))
}

make_repo() {
  local name=$1 repo="$CASE_ROOT/$1" patch="$CASE_ROOT/$1.diff" file
  git clone -q --no-hardlinks "$ROOT" "$repo" || return 1
  git -C "$repo" checkout -q --detach "$(git -C "$ROOT" rev-parse HEAD)" \
    || return 1
  git -C "$ROOT" diff --binary HEAD -- > "$patch" || return 1
  if [ -s "$patch" ]; then
    git -C "$repo" apply --binary "$patch" || return 1
  fi
  for file in \
    lib/portable_stage.py \
    tests/fake_portable_stage_provider.py \
    tests/history_audit_cli_recorder.py
  do
    if [ -f "$ROOT/$file" ]; then
      mkdir -p "$repo/$(dirname "$file")"
      cp "$ROOT/$file" "$repo/$file"
    fi
  done
  printf '%s\n' "$repo"
}

instrument_audit_cli() {
  local repo=$1
  mv "$repo/lib/history_audit_cli.py" \
    "$repo/lib/history_audit_cli_real.py"
  cp "$repo/tests/history_audit_cli_recorder.py" \
    "$repo/lib/history_audit_cli.py"
}

install_fake_providers() {
  local repo=$1 provider
  mkdir -p "$repo/.test-bin"
  for provider in codex kimi grok opencode agy claude; do
    cp "$repo/tests/fake_portable_stage_provider.py" \
      "$repo/.test-bin/$provider"
    chmod 755 "$repo/.test-bin/$provider"
  done
}

install_noop_publish() {
  local repo=$1
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -eu' \
    'mkdir -p tmp' \
    "printf '%s\\n' publication-no-op >> tmp/publication.noop" \
    > "$repo/publish.sh"
  chmod 755 "$repo/publish.sh"
}

run_bounded() {
  local cwd=$1 log=$2
  shift 2
  python3 - "$cwd" "$log" "$@" <<'PY'
import os
import signal
import subprocess
import sys

cwd, log, *command = sys.argv[1:]
with open(log, "wb") as output:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        status = process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        status = 124
raise SystemExit(status if 0 <= status <= 255 else 125)
PY
}

write_hunt_ledger() {
  local path=$1
  printf '%s\n' \
    $'date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory' \
    $'2026-07-23\thunt\tWorld Models - Architecture\tA bounded historical fixture.\treject\tThe fixture is occupied.\thigh\tnovelty-dead' \
    > "$path"
}

make_project() {
  local name=$1 dir="$CASE_ROOT/$1"
  mkdir -p "$dir" || return 1
  cp "$ROOT/directions/dynamic-spatial-memory-vla-v1.json" \
    "$dir/direction.json" || return 1
  printf '%s\n' "$dir"
}

bootstrap_db() {
  local repo=$1
  (
    cd "$repo" || exit 1
    python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 init \
      > /dev/null || exit 1
    python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 \
      sync-ledger ledger.tsv > /dev/null || exit 1
  )
}

project_state_digest() {
  python3 - "$1" <<'PY'
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
records = []
targets = [
    root / ".git" / "config",
    root / "ledger.tsv",
    root / ".ai-ideas" / "history.sqlite3",
    root / ".ai-ideas" / "projects.json",
]
for target in targets:
    relative = target.relative_to(root).as_posix()
    if target.is_file():
        records.append(
            (relative, hashlib.sha256(target.read_bytes()).hexdigest())
        )
    else:
        records.append((relative, "absent"))
ideas = root / "ideas"
if ideas.is_dir():
    for path in sorted(ideas.rglob("*")):
        if path.is_file() and not path.is_symlink():
            records.append(
                (
                    path.relative_to(root).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
raw = "\n".join("\0".join(record) for record in records).encode("utf-8")
print(hashlib.sha256(raw).hexdigest())
PY
}

prepare_hunt_repo() {
  local name=$1 repo
  repo=$(make_repo "$name") || return 1
  install_fake_providers "$repo" || return 1
  instrument_audit_cli "$repo" || return 1
  install_noop_publish "$repo" || return 1
  write_hunt_ledger "$repo/ledger.tsv" || return 1
  mkdir -p "$repo/tmp" || return 1
  : > "$repo/tmp/near-sa-queue.tsv"
  bootstrap_db "$repo" || return 1
  # hunt.sh sets core.hooksPath on every invocation; make the fixture's
  # .git/config reach that fixed point before the state digest is taken.
  git -C "$repo" config core.hooksPath .githooks || return 1
  printf '%s\n' "$repo"
}

run_project_hunt() {
  local repo=$1 log=$2 provider_log=$3 cli_log=$4 runs=$5 home=$6
  shift 6
  mkdir -p "$runs" "$home"
  run_bounded "$repo" "$log" \
    env \
      "HOME=$home" \
      "CODEX_HOME=$home/codex-config" \
      "EXPECTED_PROVIDER_HOME=$home" \
      "EXPECTED_PROVIDER_CODEX_HOME=$home/codex-config" \
      "PATH=$repo/.test-bin:$PATH" \
      "FAKE_PORTABLE_STAGE_LOG=$provider_log" \
      "HISTORY_AUDIT_CLI_CALL_LOG=$cli_log" \
      FAKE_PORTABLE_STAGE_MODE=mirror-audit \
      HUNT_PROVIDER=claude \
      HUNT_REVIEW_PROVIDER_1=claude \
      HUNT_REVIEW_MODEL_1=sonnet \
      "AGENT_CMD=$repo/tests/fake_agent.sh" \
      HISTORY_NEAR_SA=tmp/near-sa-queue.tsv \
      REVIEWERS=1 \
      RESUME_FRONT=0 \
      THEME_MIN_LOW=0 \
      RESEARCH_RETRY=0 \
      FAIL_SLEEP_MIN=0 \
      NO_HIT_SLEEP_MIN_LO=0 \
      NO_HIT_SLEEP_MIN_HI=0 \
      ALLOW_ZERO_NO_HIT_SLEEP=1 \
      MAX_FAILS=1 \
      SA_TARGET=0 \
      "RUNS_DIR=$runs" \
      "$@" \
      bash ./hunt.sh
}

check_slices_python() {
  python3 - "$@" <<'PY'
import json
import pathlib
import sqlite3
import stat
import sys

repo = pathlib.Path(sys.argv[1])
project_dir = pathlib.Path(sys.argv[2])
project_name = sys.argv[3]
log_text = pathlib.Path(sys.argv[4]).read_text(encoding="utf-8")
runs_root = pathlib.Path(sys.argv[5])
mode = sys.argv[6]
expected_runs = int(sys.argv[7])
today = sys.argv[8]
first_mark = int(sys.argv[9])
orphan_stories = set(json.loads(sys.argv[10]))

banner = f"mode=project {project_name} {project_dir}"
if banner not in log_text:
    raise SystemExit(f"missing banner: {banner!r}")
identity = json.loads(
    (repo / "tmp/history-startup/direction-identity.json").read_text(
        encoding="utf-8"
    )
)
if identity["direction_id"] != "dynamic-spatial-memory-vla-v1":
    raise SystemExit(f"staged direction identity changed: {identity}")
staged = repo / "tmp" / "history-startup" / "direction-project.json"
staged_stat = staged.lstat()
if not stat.S_ISREG(staged_stat.st_mode) or stat.S_ISLNK(
    staged_stat.st_mode
):
    raise SystemExit("staged direction copy is not a regular non-symlink file")

harvest = project_dir / "harvest"
readme = harvest / "README.md"
if not readme.is_file():
    raise SystemExit("harvest README.md missing")
if "re-import" not in readme.read_text(encoding="utf-8"):
    raise SystemExit("harvest README.md omits the re-import rule")

slices = sorted(harvest.glob("ledger-slice-*.tsv"))
if len(slices) != expected_runs:
    raise SystemExit(f"expected {expected_runs} slices, got {slices}")
run_ids = [
    path.name[len("ledger-slice-"):-len(".tsv")] for path in slices
]
if len(set(run_ids)) != len(run_ids):
    raise SystemExit(f"slice run ids collide: {run_ids}")

full = (repo / "ledger.tsv").read_bytes()
lines = full.split(b"\n")
if lines and lines[-1] == b"":
    lines.pop()
header = lines[0]
data_lines = lines[1:]

database = sqlite3.connect(repo / ".ai-ideas" / "history.sqlite3")
db_max = database.execute(
    "SELECT COALESCE(MAX(source_sequence), 0) FROM candidates"
).fetchone()[0]
provenance = [
    json.loads(row[0])
    for row in database.execute(
        "SELECT provenance_json FROM candidates WHERE source_sequence > 1"
    )
]
database.close()
commit_rows = [
    item for item in provenance if item.get("source") == "hunt-runtime-v2"
]
if not commit_rows:
    raise SystemExit("hunt committed no rows")
if any(item.get("project") != project_name for item in commit_rows):
    raise SystemExit(f"row provenance lost the project: {commit_rows}")

expected_keys = {
    "schema_version", "project", "run_id", "date", "direction_id",
    "direction_sha256", "after_sequence", "max_sequence", "row_count",
    "verdicts", "slice_file", "report_files", "note",
}
mark = first_mark
consumed = first_mark
reports_expected = []
for index, run_id in enumerate(run_ids):
    manifest_path = harvest / f"manifest-{run_id}.json"
    if not manifest_path.is_file():
        raise SystemExit(f"manifest missing for run {run_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != expected_keys:
        raise SystemExit(f"manifest keys changed: {sorted(manifest)}")
    slice_path = harvest / f"ledger-slice-{run_id}.tsv"
    slice_bytes = slice_path.read_bytes()
    slice_lines = slice_bytes.split(b"\n")
    if slice_lines and slice_lines[-1] == b"":
        slice_lines.pop()
    if slice_lines[0] != header:
        raise SystemExit(f"slice {run_id} lost the ledger header")
    slice_data = slice_lines[1:]
    archive = runs_root / run_id
    if not archive.is_dir():
        raise SystemExit(f"run archive missing for {run_id}")
    commits = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in archive.rglob("commit-round.json")
    ]
    appended = sum(item.get("appended", 0) for item in commits)
    if manifest["schema_version"] != 1:
        raise SystemExit("manifest schema_version changed")
    if manifest["project"] != project_name:
        raise SystemExit("manifest project mismatch")
    if manifest["run_id"] != run_id:
        raise SystemExit("manifest run_id mismatch")
    if manifest["date"] != today:
        raise SystemExit("manifest date mismatch")
    if manifest["direction_id"] != identity["direction_id"]:
        raise SystemExit("manifest direction_id mismatch")
    if manifest["direction_sha256"] != identity["sha256"]:
        raise SystemExit("manifest direction sha mismatch")
    if manifest["after_sequence"] != mark:
        raise SystemExit(
            f"run {run_id} mark {manifest['after_sequence']} != {mark}"
        )
    if manifest["row_count"] != len(slice_data):
        raise SystemExit(f"run {run_id} row_count mismatch")
    if manifest["row_count"] != appended + (
        len(orphan_stories) if index == expected_runs - 1 else 0
    ):
        raise SystemExit(
            f"run {run_id} slice is not exactly the run's new rows"
        )
    expected_slice = header + b"\n" + b"".join(
        line + b"\n"
        for line in data_lines[consumed:consumed + manifest["row_count"]]
    )
    if slice_bytes != expected_slice:
        raise SystemExit(f"slice {run_id} is not the ledger tail above its mark")
    verdicts = {}
    for line in slice_data:
        verdict = line.split(b"\t")[4].rstrip(b"\r").decode("utf-8")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    if manifest["verdicts"] != verdicts:
        raise SystemExit(f"run {run_id} verdict distribution mismatch")
    if manifest["slice_file"] != slice_path.name:
        raise SystemExit("manifest slice_file mismatch")
    if "Read-only" not in manifest["note"]:
        raise SystemExit("manifest omitted the read-only note")
    orphan_lines = [
        line for line in slice_data
        if any(story.encode("utf-8") in line for story in orphan_stories)
    ]
    if index == expected_runs - 1 and len(orphan_lines) != len(orphan_stories):
        raise SystemExit("orphaned rows were not recovered into the slice")
    accepted = [
        path for path in archive.rglob("accepted.tsv")
    ]
    sa_count = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in accepted
    )
    bindings = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in archive.rglob("report-binding.json")
    ]
    if sa_count > 0:
        if len(bindings) != 1:
            raise SystemExit(f"run {run_id} lost its report binding")
        base = pathlib.PurePath(bindings[0]["report_path"]).name
        if manifest["report_files"] != [base]:
            raise SystemExit(
                f"run {run_id} manifest report files {manifest['report_files']}"
            )
        if not (harvest / base).is_file():
            raise SystemExit(f"run {run_id} report copy missing")
        reports_expected.append(base)
    else:
        if manifest["report_files"] != []:
            raise SystemExit(f"run {run_id} copied a report without SA")
    mark = manifest["max_sequence"]
    consumed += manifest["row_count"]

if consumed != len(data_lines):
    raise SystemExit("slices do not partition the ledger above the first mark")
if manifest["max_sequence"] != db_max:
    raise SystemExit("final slice max does not match the database")
if mode == "registry":
    registry = json.loads(
        (repo / ".ai-ideas" / "projects.json").read_text(encoding="utf-8")
    )
    entry = registry["projects"][project_name]
    if entry.get("last_harvested_sequence") != db_max:
        raise SystemExit("registry harvest mark did not advance")
harvest_reports = [
    path.name for path in harvest.iterdir()
    if path.is_file() and path.name not in {
        "README.md",
        *[f"ledger-slice-{run_id}.tsv" for run_id in run_ids],
        *[f"manifest-{run_id}.json" for run_id in run_ids],
    }
]
if sorted(harvest_reports) != sorted(reports_expected):
    raise SystemExit(f"unexpected harvest files: {harvest_reports}")
PY
}

run_default_flow_parity() {
  local repo log provider_log cli_log runs home status harvest_dirs
  repo=$(prepare_hunt_repo default-parity) || { fail 'default parity fixture'; return; }
  log="$CASE_ROOT/default-parity.log"
  provider_log="$CASE_ROOT/default-parity.providers.jsonl"
  cli_log="$CASE_ROOT/default-parity.audit-cli.calls"
  runs="$CASE_ROOT/default-parity-runs"
  home="$CASE_ROOT/default-parity-home"
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    RESEARCH_DIRECTION_FILE=directions/dynamic-spatial-memory-vla-v1.json \
    ROUND_LIMIT=1
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "default flow exited $status"
    sed -n '1,120p' "$log" >&2
    return
  fi
  if ! grep -q 'mode=default' "$log"; then
    fail 'default flow omitted the mode=default banner'
    return
  fi
  if grep -q 'mode=project' "$log"; then
    fail 'default flow printed a project banner'
    return
  fi
  harvest_dirs=$(find "$repo" -name harvest -type d 2>/dev/null)
  if [ -n "$harvest_dirs" ]; then
    fail "default flow wrote harvest directories: $harvest_dirs"
    return
  fi
  local committed
  committed=$(
    cd "$repo" && python3 lib/history_cli.py \
      --db .ai-ideas/history.sqlite3 max-sequence
  ) || { fail 'default flow max-sequence'; return; }
  case "$committed" in
    *'"max_sequence": '[2-9]*) ;;
    *) fail "default flow did not commit rows: $committed"; return ;;
  esac
  printf 'ok: default flow parity (banner only, no harvest)\n'
}

run_dir_mode_success() {
  local repo project physical log provider_log cli_log runs home status today
  repo=$(prepare_hunt_repo dir-mode) || { fail 'dir mode fixture'; return; }
  project=$(make_project dir-mode-project) || { fail 'dir mode project fixture'; return; }
  physical=$(cd "$project" && pwd -P)
  log="$CASE_ROOT/dir-mode.log"
  provider_log="$CASE_ROOT/dir-mode.providers.jsonl"
  cli_log="$CASE_ROOT/dir-mode.audit-cli.calls"
  runs="$CASE_ROOT/dir-mode-runs"
  home="$CASE_ROOT/dir-mode-home"
  today=$(date +%F)
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    "HUNT_PROJECT_DIR=$project" \
    ROUND_LIMIT=1
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "dir mode hunt exited $status"
    sed -n '1,160p' "$log" >&2
    return
  fi
  if ! check_slices_python "$repo" "$physical" "$(basename "$physical")" \
    "$log" "$runs" dir 1 "$today" 1 '[]'; then
    fail 'dir mode harvest shape'
    return
  fi
  printf 'ok: dir mode stages, tags, and harvests one round\n'
}

run_registry_mode_success() {
  local repo project physical log provider_log cli_log runs home status today
  repo=$(prepare_hunt_repo registry-mode) || { fail 'registry mode fixture'; return; }
  project=$(make_project registry-mode-project) || {
    fail 'registry mode project fixture'; return;
  }
  physical=$(cd "$project" && pwd -P)
  (cd "$repo" && python3 lib/history_cli.py project-add mytopic "$project" \
    > /dev/null) || { fail 'registry mode project-add'; return; }
  log="$CASE_ROOT/registry-mode.log"
  provider_log="$CASE_ROOT/registry-mode.providers.jsonl"
  cli_log="$CASE_ROOT/registry-mode.audit-cli.calls"
  runs="$CASE_ROOT/registry-mode-runs"
  home="$CASE_ROOT/registry-mode-home"
  today=$(date +%F)
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    HUNT_PROJECT=mytopic \
    ROUND_LIMIT=1
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "registry mode hunt exited $status"
    sed -n '1,160p' "$log" >&2
    return
  fi
  if ! check_slices_python "$repo" "$physical" mytopic \
    "$log" "$runs" registry 1 "$today" 1 '[]'; then
    fail 'registry mode harvest shape'
    return
  fi
  printf 'ok: registry mode harvests and advances the registry mark\n'
}

run_project_refusal() {
  local name=$1 log=$2 provider_log=$3
  shift 3
  local repo before after status
  repo=$(prepare_hunt_repo "refuse-$name") || {
    fail "refusal $name fixture"
    return
  }
  before=$(project_state_digest "$repo")
  run_project_hunt "$repo" "$log" "$provider_log" \
    "$CASE_ROOT/refuse-$name.audit-cli.calls" \
    "$CASE_ROOT/refuse-$name-runs" "$CASE_ROOT/refuse-$name-home" \
    ROUND_LIMIT=1 "$@"
  status=$?
  after=$(project_state_digest "$repo")
  if [ "$status" -ne 2 ]; then
    fail "refusal $name exited $status instead of 2"
    sed -n '1,60p' "$log" >&2
    return
  fi
  if [ -e "$provider_log" ]; then
    fail "refusal $name launched a provider"
    return
  fi
  if [ "$after" != "$before" ]; then
    fail "refusal $name mutated git/ledger/ideas/database state"
    return
  fi
  printf 'ok: project refusal %s exits before providers and mutation\n' "$name"
}

run_refusal_matrix() {
  local project physical link_repo linked_dir empty_dir broken_dir
  project=$(make_project refusal-project) || { fail 'refusal project fixture'; return; }

  run_project_refusal both-set \
    "$CASE_ROOT/refuse-both-set.log" \
    "$CASE_ROOT/refuse-both-set.providers.jsonl" \
    HUNT_PROJECT=ghost "HUNT_PROJECT_DIR=$project"

  run_project_refusal direction-conflict \
    "$CASE_ROOT/refuse-direction-conflict.log" \
    "$CASE_ROOT/refuse-direction-conflict.providers.jsonl" \
    "HUNT_PROJECT_DIR=$project" \
    RESEARCH_DIRECTION_FILE=directions/dynamic-memory-vla-v1.json

  run_project_refusal registry-direction-conflict \
    "$CASE_ROOT/refuse-registry-direction-conflict.log" \
    "$CASE_ROOT/refuse-registry-direction-conflict.providers.jsonl" \
    HUNT_PROJECT=ghost \
    RESEARCH_DIRECTION_FILE=directions/dynamic-memory-vla-v1.json

  run_project_refusal relative-dir \
    "$CASE_ROOT/refuse-relative-dir.log" \
    "$CASE_ROOT/refuse-relative-dir.providers.jsonl" \
    HUNT_PROJECT_DIR=relative/project

  ln -s "$project" "$CASE_ROOT/refusal-linked"
  run_project_refusal symlinked-dir \
    "$CASE_ROOT/refuse-symlinked-dir.log" \
    "$CASE_ROOT/refuse-symlinked-dir.providers.jsonl" \
    "HUNT_PROJECT_DIR=$CASE_ROOT/refusal-linked"

  linked_dir="$CASE_ROOT/refusal-linked-direction"
  mkdir -p "$linked_dir"
  ln -s "$project/direction.json" "$linked_dir/direction.json"
  run_project_refusal symlinked-direction \
    "$CASE_ROOT/refuse-symlinked-direction.log" \
    "$CASE_ROOT/refuse-symlinked-direction.providers.jsonl" \
    "HUNT_PROJECT_DIR=$linked_dir"

  empty_dir="$CASE_ROOT/refusal-empty"
  mkdir -p "$empty_dir"
  run_project_refusal missing-direction \
    "$CASE_ROOT/refuse-missing-direction.log" \
    "$CASE_ROOT/refuse-missing-direction.providers.jsonl" \
    "HUNT_PROJECT_DIR=$empty_dir"

  # in-checkout: the hunt repository itself is the proposed project dir.
  local repo before after status
  repo=$(prepare_hunt_repo refuse-in-checkout) || {
    fail 'refusal in-checkout fixture'; return;
  }
  before=$(project_state_digest "$repo")
  run_project_hunt "$repo" \
    "$CASE_ROOT/refuse-in-checkout.log" \
    "$CASE_ROOT/refuse-in-checkout.providers.jsonl" \
    "$CASE_ROOT/refuse-in-checkout.audit-cli.calls" \
    "$CASE_ROOT/refuse-in-checkout-runs" \
    "$CASE_ROOT/refuse-in-checkout-home" \
    ROUND_LIMIT=1 "HUNT_PROJECT_DIR=$repo"
  status=$?
  after=$(project_state_digest "$repo")
  if [ "$status" -ne 2 ] \
    || [ -e "$CASE_ROOT/refuse-in-checkout.providers.jsonl" ] \
    || [ "$after" != "$before" ]; then
    fail "refusal in-checkout misbehaved (status $status)"
    sed -n '1,60p' "$CASE_ROOT/refuse-in-checkout.log" >&2
    return
  fi
  printf 'ok: project refusal in-checkout exits before providers and mutation\n'

  # unknown registry name: the run log must list the registered names.
  local known="$CASE_ROOT/refusal-known"
  mkdir -p "$known"
  cp "$ROOT/directions/dynamic-spatial-memory-vla-v1.json" \
    "$known/direction.json"
  repo=$(prepare_hunt_repo refuse-unknown) || {
    fail 'refusal unknown fixture'; return;
  }
  (cd "$repo" && python3 lib/history_cli.py project-add known "$known" \
    > /dev/null) || { fail 'refusal unknown project-add'; return; }
  before=$(project_state_digest "$repo")
  run_project_hunt "$repo" \
    "$CASE_ROOT/refuse-unknown.log" \
    "$CASE_ROOT/refuse-unknown.providers.jsonl" \
    "$CASE_ROOT/refuse-unknown.audit-cli.calls" \
    "$CASE_ROOT/refuse-unknown-runs" \
    "$CASE_ROOT/refuse-unknown-home" \
    ROUND_LIMIT=1 HUNT_PROJECT=ghost
  status=$?
  after=$(project_state_digest "$repo")
  if [ "$status" -ne 2 ] \
    || [ -e "$CASE_ROOT/refuse-unknown.providers.jsonl" ] \
    || [ "$after" != "$before" ]; then
    fail "refusal unknown-project misbehaved (status $status)"
    sed -n '1,60p' "$CASE_ROOT/refuse-unknown.log" >&2
    return
  fi
  if ! grep -q 'known' "$CASE_ROOT/refuse-unknown.log"; then
    fail 'refusal unknown-project did not list registered names'
    return
  fi
  printf 'ok: project refusal unknown-project lists registered names\n'

  # missing database: no .ai-ideas at all.
  repo=$(make_repo refuse-missing-db) || {
    fail 'refusal missing-db fixture'; return;
  }
  install_fake_providers "$repo"
  instrument_audit_cli "$repo"
  install_noop_publish "$repo"
  write_hunt_ledger "$repo/ledger.tsv"
  mkdir -p "$repo/tmp"
  : > "$repo/tmp/near-sa-queue.tsv"
  git -C "$repo" config core.hooksPath .githooks
  before=$(project_state_digest "$repo")
  run_project_hunt "$repo" \
    "$CASE_ROOT/refuse-missing-db.log" \
    "$CASE_ROOT/refuse-missing-db.providers.jsonl" \
    "$CASE_ROOT/refuse-missing-db.audit-cli.calls" \
    "$CASE_ROOT/refuse-missing-db-runs" \
    "$CASE_ROOT/refuse-missing-db-home" \
    ROUND_LIMIT=1 "HUNT_PROJECT_DIR=$project"
  status=$?
  after=$(project_state_digest "$repo")
  if [ "$status" -ne 2 ] \
    || [ -e "$CASE_ROOT/refuse-missing-db.providers.jsonl" ] \
    || [ "$after" != "$before" ]; then
    fail "refusal missing-db misbehaved (status $status)"
    sed -n '1,60p' "$CASE_ROOT/refuse-missing-db.log" >&2
    return
  fi
  if ! grep -q 'existing history database' "$CASE_ROOT/refuse-missing-db.log"; then
    fail 'refusal missing-db omitted the database reason'
    return
  fi
  printf 'ok: project refusal missing-db fails closed\n'
}

run_same_day_rerun() {
  local repo project physical log provider_log cli_log runs home today
  repo=$(prepare_hunt_repo same-day) || { fail 'same-day fixture'; return; }
  project=$(make_project same-day-project) || { fail 'same-day project'; return; }
  physical=$(cd "$project" && pwd -P)
  runs="$CASE_ROOT/same-day-runs"
  home="$CASE_ROOT/same-day-home"
  today=$(date +%F)
  local run status
  for run in 1 2; do
    log="$CASE_ROOT/same-day-$run.log"
    provider_log="$CASE_ROOT/same-day-$run.providers.jsonl"
    cli_log="$CASE_ROOT/same-day-$run.audit-cli.calls"
    run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
      "HUNT_PROJECT_DIR=$project" \
      ROUND_LIMIT=1
    status=$?
    if [ "$status" -ne 0 ]; then
      fail "same-day run $run exited $status"
      sed -n '1,160p' "$log" >&2
      return
    fi
  done
  cat "$CASE_ROOT/same-day-1.log" "$CASE_ROOT/same-day-2.log" \
    > "$CASE_ROOT/same-day-combined.log"
  if ! check_slices_python "$repo" "$physical" "$(basename "$physical")" \
    "$CASE_ROOT/same-day-combined.log" "$runs" dir 2 "$today" 1 '[]'; then
    fail 'same-day re-run accumulation'
    return
  fi
  printf 'ok: same-day re-runs accumulate distinct slice and manifest files\n'
}

run_second_round_mark() {
  local repo project physical log provider_log cli_log runs home status today
  repo=$(prepare_hunt_repo two-round) || { fail 'two-round fixture'; return; }
  project=$(make_project two-round-project) || { fail 'two-round project'; return; }
  physical=$(cd "$project" && pwd -P)
  log="$CASE_ROOT/two-round.log"
  provider_log="$CASE_ROOT/two-round.providers.jsonl"
  cli_log="$CASE_ROOT/two-round.audit-cli.calls"
  runs="$CASE_ROOT/two-round-runs"
  home="$CASE_ROOT/two-round-home"
  today=$(date +%F)
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    "HUNT_PROJECT_DIR=$project" \
    ROUND_LIMIT=2
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "two-round hunt exited $status"
    sed -n '1,160p' "$log" >&2
    return
  fi
  if ! check_slices_python "$repo" "$physical" "$(basename "$physical")" \
    "$log" "$runs" dir 2 "$today" 1 '[]'; then
    fail 'second round mark advancement'
    return
  fi
  printf 'ok: second round slice holds only the second round rows\n'
}

run_sa_shape() {
  local repo project physical log provider_log cli_log runs home status today
  repo=$(prepare_hunt_repo sa-shape) || { fail 'sa shape fixture'; return; }
  project=$(make_project sa-shape-project) || { fail 'sa shape project'; return; }
  physical=$(cd "$project" && pwd -P)
  log="$CASE_ROOT/sa-shape.log"
  provider_log="$CASE_ROOT/sa-shape.providers.jsonl"
  cli_log="$CASE_ROOT/sa-shape.audit-cli.calls"
  runs="$CASE_ROOT/sa-shape-runs"
  home="$CASE_ROOT/sa-shape-home"
  today=$(date +%F)
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    "HUNT_PROJECT_DIR=$project" \
    ROUND_LIMIT=1
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "sa shape hunt exited $status"
    sed -n '1,160p' "$log" >&2
    return
  fi
  # The checker asserts the report copy exists iff the run archive's
  # accepted.tsv is non-empty, and that its basename equals that run's
  # report-binding.json report_path basename.
  if ! check_slices_python "$repo" "$physical" "$(basename "$physical")" \
    "$log" "$runs" dir 1 "$today" 1 '[]'; then
    fail 'sa shape report copy rule'
    return
  fi
  printf 'ok: report copy copies only the run-bound report\n'
}

run_crash_recovery() {
  local repo project physical runs home today log provider_log cli_log status
  repo=$(prepare_hunt_repo crash) || { fail 'crash fixture'; return; }
  project=$(make_project crash-project) || { fail 'crash project'; return; }
  physical=$(cd "$project" && pwd -P)
  (cd "$repo" && python3 lib/history_cli.py project-add crashed "$project" \
    > /dev/null) || { fail 'crash project-add'; return; }
  runs="$CASE_ROOT/crash-runs"
  home="$CASE_ROOT/crash-home"
  today=$(date +%F)
  log="$CASE_ROOT/crash-1.log"
  provider_log="$CASE_ROOT/crash-1.providers.jsonl"
  cli_log="$CASE_ROOT/crash-1.audit-cli.calls"
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    HUNT_PROJECT=crashed \
    ROUND_LIMIT=1
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "crash run 1 exited $status"
    sed -n '1,160p' "$log" >&2
    return
  fi
  # Stand-in for "committed then died before harvest": rows appended to the
  # master ledger outside any harvest.
  printf '%s\n' \
    $'date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory' \
    $'2026-09-21\thunt\tWorld Models - Architecture\tOrphaned proposition alpha.\treject\tCommitted without harvest.\thigh\tnovelty-dead' \
    $'2026-09-21\thunt\tWorld Models - Architecture\tOrphaned proposition beta.\treject\tCommitted without harvest.\thigh\tnovelty-dead' \
    > "$repo/orphans.tsv"
  (cd "$repo" && python3 lib/history_cli.py \
    --db .ai-ideas/history.sqlite3 append-tsv orphans.tsv > /dev/null) || {
    fail 'crash orphan append'; return;
  }
  log="$CASE_ROOT/crash-2.log"
  provider_log="$CASE_ROOT/crash-2.providers.jsonl"
  cli_log="$CASE_ROOT/crash-2.audit-cli.calls"
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    HUNT_PROJECT=crashed \
    ROUND_LIMIT=1
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "crash run 2 exited $status"
    sed -n '1,160p' "$log" >&2
    return
  fi
  cat "$CASE_ROOT/crash-1.log" "$CASE_ROOT/crash-2.log" \
    > "$CASE_ROOT/crash-combined.log"
  if ! check_slices_python "$repo" "$physical" crashed \
    "$CASE_ROOT/crash-combined.log" "$runs" registry 2 "$today" 1 \
    '["Orphaned proposition alpha.", "Orphaned proposition beta."]'; then
    fail 'crash recovery orphan harvest'
    return
  fi
  printf 'ok: crash before harvest is recovered from the registry mark\n'
}

run_in_checkout_subdir_refusal() {
  # An in-checkout subdirectory holding a valid direction.json must be
  # refused even when the hunt's PWD is a logical (symlinked) spelling of
  # the checkout, because the guard compares physical paths.
  local repo link before after status
  repo=$(prepare_hunt_repo refuse-in-subdir) || {
    fail 'refusal in-subdir fixture'; return;
  }
  mkdir -p "$repo/inner-proj"
  cp "$ROOT/directions/dynamic-spatial-memory-vla-v1.json" \
    "$repo/inner-proj/direction.json"
  before=$(project_state_digest "$repo")
  run_project_hunt "$repo" \
    "$CASE_ROOT/refuse-in-subdir.log" \
    "$CASE_ROOT/refuse-in-subdir.providers.jsonl" \
    "$CASE_ROOT/refuse-in-subdir.audit-cli.calls" \
    "$CASE_ROOT/refuse-in-subdir-runs" \
    "$CASE_ROOT/refuse-in-subdir-home" \
    ROUND_LIMIT=1 "HUNT_PROJECT_DIR=$repo/inner-proj"
  status=$?
  after=$(project_state_digest "$repo")
  if [ "$status" -ne 2 ] \
    || [ -e "$CASE_ROOT/refuse-in-subdir.providers.jsonl" ] \
    || [ "$after" != "$before" ]; then
    fail "refusal in-subdir misbehaved (status $status)"
    sed -n '1,60p' "$CASE_ROOT/refuse-in-subdir.log" >&2
    return
  fi
  printf 'ok: project refusal in-checkout subdir exits before providers\n'

  # Same subdir, but the hunt process runs through a symlinked spelling of
  # the checkout so its PWD is logical; physical comparison must still
  # refuse.
  repo=$(prepare_hunt_repo refuse-in-subdir-logical) || {
    fail 'refusal in-subdir-logical fixture'; return;
  }
  mkdir -p "$repo/inner-proj"
  cp "$ROOT/directions/dynamic-spatial-memory-vla-v1.json" \
    "$repo/inner-proj/direction.json"
  link="$CASE_ROOT/refuse-in-subdir-link"
  ln -s "$repo" "$link"
  before=$(project_state_digest "$repo")
  mkdir -p "$CASE_ROOT/refuse-in-subdir-logical-runs" \
    "$CASE_ROOT/refuse-in-subdir-logical-home"
  run_bounded "$link" "$CASE_ROOT/refuse-in-subdir-logical.log" \
    env \
      "HOME=$CASE_ROOT/refuse-in-subdir-logical-home" \
      "CODEX_HOME=$CASE_ROOT/refuse-in-subdir-logical-home/codex-config" \
      "EXPECTED_PROVIDER_HOME=$CASE_ROOT/refuse-in-subdir-logical-home" \
      "EXPECTED_PROVIDER_CODEX_HOME=$CASE_ROOT/refuse-in-subdir-logical-home/codex-config" \
      "PWD=$link" \
      "PATH=$repo/.test-bin:$PATH" \
      "FAKE_PORTABLE_STAGE_LOG=$CASE_ROOT/refuse-in-subdir-logical.providers.jsonl" \
      "HISTORY_AUDIT_CLI_CALL_LOG=$CASE_ROOT/refuse-in-subdir-logical.audit-cli.calls" \
      FAKE_PORTABLE_STAGE_MODE=mirror-audit \
      HUNT_PROVIDER=claude \
      HUNT_REVIEW_PROVIDER_1=claude \
      HUNT_REVIEW_MODEL_1=sonnet \
      "AGENT_CMD=$repo/tests/fake_agent.sh" \
      HISTORY_NEAR_SA=tmp/near-sa-queue.tsv \
      REVIEWERS=1 \
      RESUME_FRONT=0 \
      THEME_MIN_LOW=0 \
      RESEARCH_RETRY=0 \
      FAIL_SLEEP_MIN=0 \
      NO_HIT_SLEEP_MIN_LO=0 \
      NO_HIT_SLEEP_MIN_HI=0 \
      ALLOW_ZERO_NO_HIT_SLEEP=1 \
      MAX_FAILS=1 \
      SA_TARGET=0 \
      "RUNS_DIR=$CASE_ROOT/refuse-in-subdir-logical-runs" \
      ROUND_LIMIT=1 "HUNT_PROJECT_DIR=$repo/inner-proj" \
      bash ./hunt.sh
  status=$?
  after=$(project_state_digest "$repo")
  if [ "$status" -ne 2 ] \
    || [ -e "$CASE_ROOT/refuse-in-subdir-logical.providers.jsonl" ] \
    || [ "$after" != "$before" ]; then
    fail "refusal in-subdir-logical misbehaved (status $status)"
    sed -n '1,60p' "$CASE_ROOT/refuse-in-subdir-logical.log" >&2
    return
  fi
  if ! grep -q 'cannot be the checkout or inside it' \
    "$CASE_ROOT/refuse-in-subdir-logical.log"; then
    fail 'refusal in-subdir-logical omitted the in-checkout reason'
    return
  fi
  printf 'ok: project refusal in-checkout subdir holds under a logical PWD\n'
}

run_dash_name_mode() {
  local repo project physical log provider_log cli_log runs home status today
  repo=$(prepare_hunt_repo dash-name) || { fail 'dash-name fixture'; return; }
  project=$(make_project -x) || { fail 'dash-name project'; return; }
  physical=$(cd "$project" && pwd -P)
  log="$CASE_ROOT/dash-name.log"
  provider_log="$CASE_ROOT/dash-name.providers.jsonl"
  cli_log="$CASE_ROOT/dash-name.audit-cli.calls"
  runs="$CASE_ROOT/dash-name-runs"
  home="$CASE_ROOT/dash-name-home"
  today=$(date +%F)
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    "HUNT_PROJECT_DIR=$project" \
    ROUND_LIMIT=1
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "dash-name hunt exited $status"
    sed -n '1,160p' "$log" >&2
    return
  fi
  if ! check_slices_python "$repo" "$physical" "$(basename "$physical")" \
    "$log" "$runs" dir 1 "$today" 1 '[]'; then
    fail 'dash-name harvest shape'
    return
  fi
  printf 'ok: dash-led project dir name commits and harvests\n'
}

run_harvest_failure_warns() {
  local repo project physical runs home today log provider_log cli_log status
  repo=$(prepare_hunt_repo harvest-fail) || { fail 'harvest-fail fixture'; return; }
  project=$(make_project harvest-fail-project) || {
    fail 'harvest-fail project'; return;
  }
  physical=$(cd "$project" && pwd -P)
  (cd "$repo" && python3 lib/history_cli.py project-add fragile "$project" \
    > /dev/null) || { fail 'harvest-fail project-add'; return; }
  runs="$CASE_ROOT/harvest-fail-runs"
  home="$CASE_ROOT/harvest-fail-home"
  today=$(date +%F)
  log="$CASE_ROOT/harvest-fail-1.log"
  provider_log="$CASE_ROOT/harvest-fail-1.providers.jsonl"
  cli_log="$CASE_ROOT/harvest-fail-1.audit-cli.calls"
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    HUNT_PROJECT=fragile \
    ROUND_LIMIT=1
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "harvest-fail run 1 exited $status"
    sed -n '1,160p' "$log" >&2
    return
  fi
  # Make the harvest destination unwritable: export-slice fails, the hunt
  # must warn, keep the committed rows, and leave the registry mark.
  chmod -R 555 "$project"
  log="$CASE_ROOT/harvest-fail-2.log"
  provider_log="$CASE_ROOT/harvest-fail-2.providers.jsonl"
  cli_log="$CASE_ROOT/harvest-fail-2.audit-cli.calls"
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    HUNT_PROJECT=fragile \
    ROUND_LIMIT=1
  status=$?
  chmod -R 755 "$project"
  if [ "$status" -ne 0 ]; then
    fail "harvest-fail run 2 exited $status instead of warning through"
    sed -n '1,160p' "$log" >&2
    return
  fi
  if ! grep -q 'WARNING: project harvest failed for run' "$log"; then
    fail 'harvest-fail run 2 omitted the harvest warning'
    sed -n '1,160p' "$log" >&2
    return
  fi
  log="$CASE_ROOT/harvest-fail-3.log"
  provider_log="$CASE_ROOT/harvest-fail-3.providers.jsonl"
  cli_log="$CASE_ROOT/harvest-fail-3.audit-cli.calls"
  run_project_hunt "$repo" "$log" "$provider_log" "$cli_log" "$runs" "$home" \
    HUNT_PROJECT=fragile \
    ROUND_LIMIT=1
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "harvest-fail run 3 exited $status"
    sed -n '1,160p' "$log" >&2
    return
  fi
  if ! python3 - "$repo" "$physical" "$runs" <<'PY'
import json
import pathlib
import sqlite3
import sys

repo = pathlib.Path(sys.argv[1])
harvest = pathlib.Path(sys.argv[2]) / "harvest"
runs_root = pathlib.Path(sys.argv[3])

slices = sorted(harvest.glob("ledger-slice-*.tsv"))
if len(slices) != 2:
    raise SystemExit(f"expected slices from runs 1 and 3 only, got {slices}")
run_ids = [
    path.name[len("ledger-slice-"):-len(".tsv")] for path in slices
]
first = json.loads(
    (harvest / f"manifest-{run_ids[0]}.json").read_text(encoding="utf-8")
)
last = json.loads(
    (harvest / f"manifest-{run_ids[1]}.json").read_text(encoding="utf-8")
)
if last["after_sequence"] != first["max_sequence"]:
    raise SystemExit(
        "run 3 mark did not come from run 1: "
        f"{last['after_sequence']} != {first['max_sequence']}"
    )
full = (repo / "ledger.tsv").read_bytes()
lines = full.split(b"\n")
if lines and lines[-1] == b"":
    lines.pop()
header, data = lines[0], lines[1:]
slice_bytes = (harvest / f"ledger-slice-{run_ids[1]}.tsv").read_bytes()
expected = header + b"\n" + b"".join(
    line + b"\n" for line in data[first["max_sequence"]:]
)
if slice_bytes != expected:
    raise SystemExit("run 3 slice does not contain the unharvested rows")
if last["row_count"] != len(data) - first["max_sequence"]:
    raise SystemExit("run 3 row_count mismatch")
database = sqlite3.connect(repo / ".ai-ideas" / "history.sqlite3")
db_max = database.execute(
    "SELECT COALESCE(MAX(source_sequence), 0) FROM candidates"
).fetchone()[0]
database.close()
registry = json.loads(
    (repo / ".ai-ideas" / "projects.json").read_text(encoding="utf-8")
)
if registry["projects"]["fragile"].get("last_harvested_sequence") != db_max:
    raise SystemExit("registry mark did not advance after recovery")
PY
  then
    fail 'harvest-fail recovery shape'
    return
  fi
  printf 'ok: harvest failure warns without rollback and the next run recovers\n'
}

run_default_flow_parity
run_dir_mode_success
run_registry_mode_success
run_refusal_matrix
run_in_checkout_subdir_refusal
run_same_day_rerun
run_second_round_mark
run_sa_shape
run_crash_recovery
run_dash_name_mode
run_harvest_failure_warns

if [ "$FAILURES" -gt 0 ]; then
  printf 'FAILURES: %s\n' "$FAILURES" >&2
  exit 1
fi
printf 'hunt project mode regression: all cases passed\n'
