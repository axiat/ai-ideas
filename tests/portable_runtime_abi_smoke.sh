#!/usr/bin/env bash
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TEMP_BASE=${TMPDIR:-/tmp}
TEMP_BASE=${TEMP_BASE%/}
SANDBOX_ROOT=$(mktemp -d "$TEMP_BASE/ai-ideas-portable-abi.XXXXXX")
FAILURES=0

cleanup() {
  case "$SANDBOX_ROOT" in
    "$TEMP_BASE"/ai-ideas-portable-abi.*) rm -rf -- "$SANDBOX_ROOT" ;;
    *) printf 'Refusing to remove unexpected path: %s\n' "$SANDBOX_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

record_failure() {
  printf 'not ok: %s\n' "$1" >&2
  FAILURES=$((FAILURES + 1))
}

make_repo() {
  local name=$1 repo="$SANDBOX_ROOT/$1" patch="$SANDBOX_ROOT/$1.diff" file
  git clone -q --no-hardlinks "$ROOT" "$repo" || return 1
  git -C "$repo" checkout -q --detach "$(git -C "$ROOT" rev-parse HEAD)" || return 1
  git -C "$ROOT" diff --binary HEAD -- > "$patch" || return 1
  if [ -s "$patch" ]; then
    git -C "$repo" apply --binary "$patch" || return 1
  fi
  for file in lib/history_audit_cli.py lib/portable_stage.py; do
    if [ -f "$ROOT/$file" ]; then
      mkdir -p "$repo/$(dirname "$file")"
      cp "$ROOT/$file" "$repo/$file"
    fi
  done
  printf '%s\n' "$repo"
}

install_fake_providers() {
  local repo=$1 provider executable
  mkdir -p "$repo/.test-bin"
  for provider in codex kimi grok opencode agy claude; do
    executable="$repo/.test-bin/$provider"
    printf '%s\n' \
      '#!/bin/sh' \
      'if [ "$1 $2 $3" = "--pure debug config" ]; then' \
      '  printf "%s\n" '\''{"model":"openai/fixture-model"}'\''' \
      '  exit 0' \
      'fi' \
      'if [ "$1 $2" = "models --pure" ]; then' \
      '  printf "%s\n" openai/fixture-model' \
      '  exit 0' \
      'fi' \
      'if [ "$1" = "--version" ]; then' \
      '  printf "%s\n" 1.1.10' \
      '  exit 0' \
      'fi' \
      'if [ "$1" = "models" ]; then' \
      '  if [ -n "${XDG_CONFIG_HOME:-}" ] && [ -e "$XDG_CONFIG_HOME/agy-catalog-count.enabled" ]; then' \
      '    case "$0" in */agy) printf "%s\n" "$0" >> "$XDG_CONFIG_HOME/agy-catalog-count" ;; esac' \
      '  fi' \
      '  printf "%s\n" gemini-3.6-flash-high gemini/fixture-model' \
      '  exit 0' \
      'fi' \
      'printf "%s\\n" "$0" >> "$PROVIDER_LAUNCH_LOG"' \
      'exit 91' > "$executable"
    chmod 755 "$executable"
  done
}

state_digest() {
  python3 - "$1" <<'PY'
import hashlib
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
targets = (
    root / ".git/config",
    root / "ledger.tsv",
    root / ".ai-ideas",
    root / "tmp",
    root / "ideas",
    root / "hunt.log",
    root / "run-archive",
)
records = []

def visit(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        records.append((path.relative_to(root).as_posix(), "missing", ""))
        return
    relative = path.relative_to(root).as_posix()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        records.append((relative, f"link:{mode:o}", os.readlink(path)))
    elif stat.S_ISREG(info.st_mode):
        records.append(
            (relative, f"file:{mode:o}", hashlib.sha256(path.read_bytes()).hexdigest())
        )
    elif stat.S_ISDIR(info.st_mode):
        records.append((relative, f"dir:{mode:o}", ""))
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            visit(child)
    else:
        records.append((relative, f"other:{mode:o}", ""))

for target in targets:
    visit(target)
raw = "\n".join("\0".join(record) for record in records).encode("utf-8")
print(hashlib.sha256(raw).hexdigest())
PY
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
        status = process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        status = 124
raise SystemExit(status if 0 <= status <= 255 else 125)
PY
}

run_hunt_rejection() {
  local name=$1
  shift
  local repo before after status marker log
  repo=$(make_repo "hunt-$name") || {
    record_failure "hunt $name fixture setup"
    return
  }
  sed -n '1p' "$repo/ledger.tsv" > "$repo/ledger.tsv.empty"
  mv "$repo/ledger.tsv.empty" "$repo/ledger.tsv"
  install_fake_providers "$repo"
  marker="$SANDBOX_ROOT/hunt-$name.provider-launched"
  log="$SANDBOX_ROOT/hunt-$name.log"
  before=$(state_digest "$repo")
  run_bounded "$repo" "$log" \
    env \
      "PATH=$repo/.test-bin:$PATH" \
      "PROVIDER_LAUNCH_LOG=$marker" \
      "HISTORY_PRODUCTION_TRUST_ROOT=$repo/missing-trust-root.json" \
      "RUNS_DIR=$repo/run-archive" \
      ROUND_LIMIT=1 MAX_FAILS=1 FAIL_SLEEP_MIN=0 SA_TARGET=0 REVIEWERS=1 \
      "$@" bash ./hunt.sh
  status=$?
  after=$(state_digest "$repo")
  if [ "$status" -ne 2 ]; then
    record_failure "hunt $name exited $status instead of 2"
  fi
  if [ "$after" != "$before" ]; then
    record_failure "hunt $name mutated git/history/ledger/tmp/output state"
  fi
  if [ -e "$marker" ]; then
    record_failure "hunt $name launched a provider"
  fi
  if [ "$status" -eq 2 ] && [ "$after" = "$before" ] && [ ! -e "$marker" ]; then
    printf 'ok: hunt rejects %s before history mutation\n' "$name"
  fi
}

run_awr_rejection() {
  local name=$1
  shift
  local repo before after status marker log
  repo=$(make_repo "awr-$name") || {
    record_failure "awr $name fixture setup"
    return
  }
  # A header-only queue guarantees the legacy implementation cannot launch a
  # backend while a missing portable preflight is being exposed.
  sed -n '1p' "$repo/ledger.tsv" > "$repo/ledger.tsv.empty"
  mv "$repo/ledger.tsv.empty" "$repo/ledger.tsv"
  install_fake_providers "$repo"
  marker="$SANDBOX_ROOT/awr-$name.provider-launched"
  log="$SANDBOX_ROOT/awr-$name.log"
  before=$(state_digest "$repo")
  run_bounded "$repo" "$log" \
    env \
      "PATH=$repo/.test-bin:$PATH" \
      "PROVIDER_LAUNCH_LOG=$marker" \
      SIDE_POLL_SEC=0 \
      SIDE_GAP_SEC=0 \
      SIDE_GAP_MIN_SEC=0 \
      SIDE_GAP_MAX_SEC=0 \
      "$@" bash ./awr-side.sh
  status=$?
  after=$(state_digest "$repo")
  if [ "$status" -ne 2 ]; then
    record_failure "awr $name exited $status instead of 2"
  fi
  if [ "$after" != "$before" ]; then
    record_failure "awr $name mutated git/history/ledger/tmp/output state"
  fi
  if [ -e "$marker" ]; then
    record_failure "awr $name launched a provider"
  fi
  if [ "$status" -eq 2 ] && [ "$after" = "$before" ] && [ ! -e "$marker" ]; then
    printf 'ok: awr rejects %s before queue mutation\n' "$name"
  fi
}

run_hunt_unset_regression() {
  local repo status log marker
  repo=$(make_repo "hunt-unset-v1") || {
    record_failure "hunt unset v1 fixture setup"
    return
  }
  install_fake_providers "$repo"
  log="$SANDBOX_ROOT/hunt-unset-v1.log"
  marker="$SANDBOX_ROOT/hunt-unset-v1.provider-launched"
  run_bounded "$repo" "$log" \
    env \
      -u HUNT_PROVIDER -u HUNT_MODEL -u HUNT_REASONING_EFFORT \
      "PATH=$repo/.test-bin:$PATH" \
      "PROVIDER_LAUNCH_LOG=$marker" \
      HISTORY_RUNTIME_ABI=v1 MAX_FAILS=bad \
      bash ./hunt.sh
  status=$?
  if [ "$status" -ne 2 ] || ! grep -q 'MAX_FAILS must' "$log"; then
    record_failure "unset v2 controls changed the default v1 validation path"
  elif [ -e "$marker" ]; then
    record_failure "default v1 validation launched a provider"
  else
    printf 'ok: unset v2 controls preserve default v1 validation\n'
  fi
}

run_hunt_external_legacy_regression() {
  local repo before after status log marker
  repo=$(make_repo "hunt-v2-external-legacy") || {
    record_failure "hunt v2 external legacy fixture setup"
    return
  }
  sed -n '1p' "$repo/ledger.tsv" > "$repo/ledger.tsv.empty"
  mv "$repo/ledger.tsv.empty" "$repo/ledger.tsv"
  install_fake_providers "$repo"
  git -C "$repo" config core.hooksPath .githooks
  log="$SANDBOX_ROOT/hunt-v2-external-legacy.log"
  marker="$SANDBOX_ROOT/hunt-v2-external-legacy.provider-launched"
  before=$(state_digest "$repo")
  run_bounded "$repo" "$log" \
    env \
      "PATH=$repo/.test-bin:$PATH" \
      "PROVIDER_LAUNCH_LOG=$marker" \
      HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=claude \
      AGENT_CMD=/usr/bin/false \
      MAX_FAILS=bad \
      bash ./hunt.sh
  status=$?
  after=$(state_digest "$repo")
  if [ "$status" -ne 2 ] \
    || ! grep -q 'MAX_FAILS must' "$log" \
    || grep -Eq 'AGENT_CMD.*(mixed|legacy|forbidden|required)' "$log"; then
    record_failure "Hunt v2 external fallback did not reach normal validation"
  elif [ "$after" != "$before" ] || [ -e "$marker" ]; then
    record_failure "v2 external-stage regression mutated state or launched a provider"
  else
    printf 'ok: Hunt v2 retains AGENT_CMD only as the external-stage fallback\n'
  fi
}

run_hunt_review_isolation() {
  local repo before after status log marker
  repo=$(make_repo "hunt-v2-review-isolation") || {
    record_failure "hunt v2 review isolation fixture setup"
    return
  }
  sed -n '1p' "$repo/ledger.tsv" > "$repo/ledger.tsv.empty"
  mv "$repo/ledger.tsv.empty" "$repo/ledger.tsv"
  install_fake_providers "$repo"
  git -C "$repo" config core.hooksPath .githooks
  log="$SANDBOX_ROOT/hunt-v2-review-isolation.log"
  marker="$SANDBOX_ROOT/hunt-v2-review-isolation.provider-launched"
  before=$(state_digest "$repo")
  run_bounded "$repo" "$log" \
    env \
      "PATH=$repo/.test-bin:$PATH" \
      "PROVIDER_LAUNCH_LOG=$marker" \
      HISTORY_RUNTIME_ABI=v2 REVIEWERS=1 \
      HUNT_PROVIDER=claude HUNT_MODEL=sonnet HUNT_REASONING_EFFORT=high \
      HUNT_REVIEW_PROVIDER_1=claude \
      HUNT_REVIEW_MODEL_1= HUNT_REVIEW_REASONING_EFFORT_1= \
      MAX_FAILS=bad \
      bash ./hunt.sh
  status=$?
  after=$(state_digest "$repo")
  if [ "$status" -ne 2 ] || ! grep -q 'MAX_FAILS must' "$log"; then
    record_failure "Hunt review provider inherited incompatible base overrides"
  elif [ "$after" != "$before" ] || [ -e "$marker" ]; then
    record_failure "Hunt review isolation mutated state or launched a provider"
  else
    printf 'ok: Hunt review provider can clear base model and reasoning overrides\n'
  fi
}

run_hunt_review_index_bound() {
  local repo before after status log marker
  repo=$(make_repo "hunt-v2-review-index") || {
    record_failure "hunt v2 review index fixture setup"
    return
  }
  install_fake_providers "$repo"
  log="$SANDBOX_ROOT/hunt-v2-review-index.log"
  marker="$SANDBOX_ROOT/hunt-v2-review-index.provider-launched"
  before=$(state_digest "$repo")
  run_bounded "$repo" "$log" \
    env \
      "PATH=$repo/.test-bin:$PATH" \
      "PROVIDER_LAUNCH_LOG=$marker" \
      HISTORY_RUNTIME_ABI=v2 REVIEWERS=1 HUNT_PROVIDER=claude \
      HUNT_REVIEW_PROVIDER_2=claude \
      bash ./hunt.sh
  status=$?
  after=$(state_digest "$repo")
  if [ "$status" -ne 2 ] \
    || ! grep -q 'HUNT_REVIEW_PROVIDER_2' "$log" \
    || ! grep -Eq 'REVIEWERS|outside' "$log"; then
    record_failure "Hunt accepted a numbered review override beyond REVIEWERS"
  elif [ "$after" != "$before" ] || [ -e "$marker" ]; then
    record_failure "Hunt review index rejection mutated state or launched a provider"
  else
    printf 'ok: Hunt review provider indices are bounded by REVIEWERS\n'
  fi
}

run_awr_valid_v2_no_fallback() {
  local repo before after status log marker
  repo=$(make_repo "awr-v2-no-fallback") || {
    record_failure "awr v2 no-fallback fixture setup"
    return
  }
  sed -n '1p' "$repo/ledger.tsv" > "$repo/ledger.tsv.empty"
  mv "$repo/ledger.tsv.empty" "$repo/ledger.tsv"
  install_fake_providers "$repo"
  log="$SANDBOX_ROOT/awr-v2-no-fallback.log"
  marker="$SANDBOX_ROOT/awr-v2-no-fallback.provider-launched"
  before=$(state_digest "$repo")
  run_bounded "$repo" "$log" \
    env \
      "PATH=$repo/.test-bin:$PATH" \
      "PROVIDER_LAUNCH_LOG=$marker" \
      HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=claude SIDE_POLL_SEC=bad \
      bash ./awr-side.sh
  status=$?
  after=$(state_digest "$repo")
  if [ "$status" -ne 2 ] \
    || ! grep -q 'must be nonnegative integers' "$log"; then
    record_failure "valid AwR v2 did not reach normal sidecar validation"
  elif [ "$after" != "$before" ] || [ -e "$marker" ]; then
    record_failure "valid AwR v2 mutated state or launched a provider"
  else
    printf 'ok: valid AwR v2 cannot fall through to legacy side commands\n'
  fi
}

run_awr_role_isolation() {
  local repo before after status log marker
  repo=$(make_repo "awr-v2-role-isolation") || {
    record_failure "awr v2 role isolation fixture setup"
    return
  }
  sed -n '1p' "$repo/ledger.tsv" > "$repo/ledger.tsv.empty"
  mv "$repo/ledger.tsv.empty" "$repo/ledger.tsv"
  install_fake_providers "$repo"
  log="$SANDBOX_ROOT/awr-v2-role-isolation.log"
  marker="$SANDBOX_ROOT/awr-v2-role-isolation.provider-launched"
  before=$(state_digest "$repo")
  run_bounded "$repo" "$log" \
    env \
      "PATH=$repo/.test-bin:$PATH" \
      "PROVIDER_LAUNCH_LOG=$marker" \
      HISTORY_RUNTIME_ABI=v2 \
      AWR_PROVIDER=claude AWR_MODEL=sonnet AWR_REASONING_EFFORT=high \
      AWR_RESEARCH_PROVIDER=claude \
      AWR_RESEARCH_MODEL= AWR_RESEARCH_REASONING_EFFORT= \
      SIDE_POLL_SEC=bad \
      bash ./awr-side.sh
  status=$?
  after=$(state_digest "$repo")
  if [ "$status" -ne 2 ] || ! grep -q 'must be nonnegative integers' "$log"; then
    record_failure "AwR role provider inherited incompatible base overrides"
  elif [ "$after" != "$before" ] || [ -e "$marker" ]; then
    record_failure "AwR role isolation mutated state or launched a provider"
  else
    printf 'ok: AwR role provider can clear base model and reasoning overrides\n'
  fi
}

run_awr_agy_catalog_dedup_case() {
  local name=$1 expected_catalog_calls=$2
  shift 2
  local repo before after status marker log catalog_dir catalog catalog_calls
  repo=$(make_repo "awr-v2-agy-catalog-$name") || {
    record_failure "AwR agy catalog $name fixture setup"
    return
  }
  sed -n '1p' "$repo/ledger.tsv" > "$repo/ledger.tsv.empty"
  mv "$repo/ledger.tsv.empty" "$repo/ledger.tsv"
  install_fake_providers "$repo"
  marker="$SANDBOX_ROOT/awr-v2-agy-catalog-$name.provider-launched"
  log="$SANDBOX_ROOT/awr-v2-agy-catalog-$name.log"
  catalog_dir="$SANDBOX_ROOT/awr-v2-agy-catalog-$name.config"
  catalog="$catalog_dir/agy-catalog-count"
  mkdir -p "$catalog_dir"
  : > "$catalog_dir/agy-catalog-count.enabled"
  before=$(state_digest "$repo")
  run_bounded "$repo" "$log" \
    env \
      "PATH=$repo/.test-bin:$PATH" \
      "PROVIDER_LAUNCH_LOG=$marker" \
      "XDG_CONFIG_HOME=$catalog_dir" \
      HISTORY_RUNTIME_ABI=v2 \
      AWR_PROVIDER=agy AWR_MODEL=gemini/fixture-model \
      SIDE_POLL_SEC=bad \
      "$@" bash ./awr-side.sh
  status=$?
  after=$(state_digest "$repo")
  catalog_calls=0
  if [ -f "$catalog" ]; then
    catalog_calls=$(wc -l < "$catalog" | tr -d '[:space:]')
  fi
  if [ "$status" -ne 2 ] \
    || ! grep -q 'no provider-native exact output-token cap' "$log"; then
    record_failure "AwR agy catalog $name did not reject unsupported output cap"
  elif [ "$after" != "$before" ] || [ -e "$marker" ]; then
    record_failure "AwR agy catalog $name mutated protected state or launched a provider"
  elif [ "$catalog_calls" -ne "$expected_catalog_calls" ]; then
    record_failure "AwR agy catalog $name made $catalog_calls catalog calls instead of $expected_catalog_calls"
  else
    printf 'ok: AwR agy catalog %s made %s startup diagnostics\n' \
      "$name" "$expected_catalog_calls"
  fi
}

# Setness is intentional: even an explicitly empty v2 control is migration
# intent and cannot be silently treated as unset by v1.
run_hunt_rejection v1-set-empty-provider \
  HISTORY_RUNTIME_ABI=v1 HUNT_PROVIDER=
run_hunt_rejection v2-mixed-empty-contained \
  HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=codex CONTAINED_AGENT_CMD_JSON=
run_hunt_rejection v2-ineligible-provider \
  HISTORY_RUNTIME_ABI=v2 HUNT_PROVIDER=opencode
run_hunt_rejection unknown-abi \
  HISTORY_RUNTIME_ABI=v3
run_hunt_unset_regression
run_hunt_external_legacy_regression
run_hunt_review_isolation
run_hunt_review_index_bound

run_awr_rejection v1-set-empty-provider \
  HISTORY_RUNTIME_ABI=v1 AWR_PROVIDER=
run_awr_rejection v2-mixed-empty-legacy \
  HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=codex SIDE_CMD=
run_awr_rejection v2-unknown-provider \
  HISTORY_RUNTIME_ABI=v2 AWR_PROVIDER=unknown
run_awr_valid_v2_no_fallback
run_awr_role_isolation
run_awr_agy_catalog_dedup_case inherited 0
run_awr_agy_catalog_dedup_case distinct-model 0 \
  AWR_RESEARCH_MODEL=gemini-3.6-flash-high

if [ "$FAILURES" -ne 0 ]; then
  printf 'failed: portable runtime ABI smoke (%s cases)\n' "$FAILURES" >&2
  exit 1
fi
printf 'ok: portable runtime ABI smoke\n'
