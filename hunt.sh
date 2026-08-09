#!/usr/bin/env bash
# Run the bounded idea-hunt protocol.
#
# SQLite is the only history authority.  ledger.tsv and tmp/ledger.good are
# replayable projections reconciled by the history runtime. Generation,
# internal-history comparison, and every review seat use contained-v1 or
# portable-v2. Selector, prescreen, external prior-work research, report
# assembly, and publication retain their existing process boundaries.
#
# Usage:
#   ./hunt.sh [failure retry delay in minutes; default: 150]
#
# Main controls:
#   HISTORY_RUNTIME_ABI=v1|v2
#       V1 is the compatibility default. V2 selects provider-neutral portable
#       execution for generation, history comparison, and review.
#   HUNT_PROVIDER / HUNT_MODEL / HUNT_REASONING_EFFORT
#       V2 base provider and optional exact overrides. Provider/model/reasoning
#       values omitted from the environment preserve the CLI's current defaults.
#   HUNT_REVIEW_PROVIDER_<N> / MODEL_<N> / REASONING_EFFORT_<N>
#       Optional v2 review-seat overrides.
#   AGENT_CMD / FRONT_CMD / BACK_CMD
#       External command strings for selector, prescreen, research, and report.
#       They are parsed as argv without eval or a shell and never enter v2
#       internal stages.
#   CONTAINED_AGENT_CMD_JSON
#       V1 canonical absolute JSON argv for generation and comparison.
#   CONTAINED_REV_CMD_<N>_JSON
#       Optional v1 canonical absolute JSON argv override for review seat N.
#   HISTORY_CALIBRATION_CAPABILITY / HISTORY_PRODUCTION_TRUST_ROOT
#       Production enforcement authority.  Shadow mode needs neither.
#   REVIEWERS, MIN_READ, SHORT_MAX, THEME_MIN_LOW, AXIOM_MIN_CRACKS
#       Review and mechanical-gate bounds.
#   SA_TARGET
#       Daily Strong Accept target.  Zero means unbounded.
#   ROUND_LIMIT
#       Optional process round bound.  Zero means unbounded.
#   RESUME_FRONT
#       Reuse only a runtime-validated sealed front state.
#   RESEARCH_DIRECTION_FILE
#       Optional repository-relative closed research-direction contract.
set -u

cd "$(dirname "$0")" || exit 2
# shellcheck source=lib/mirror_pre.sh
. lib/mirror_pre.sh

ACTIVE_EXTERNAL_MIRROR=

runtime_variable_is_set() {
  declare -p "$1" >/dev/null 2>&1
}

hunt_provider_diagnostic() {
  local label=$1 provider=$2 model=$3 reasoning=$4 output
  local -a command=(
    python3 -B lib/history_audit_cli.py provider-command
    --surface hunt
    --provider "$provider"
  )
  [ -z "$model" ] || command+=(--model "$model")
  [ -z "$reasoning" ] || command+=(--reasoning "$reasoning")
  if ! output=$("${command[@]}" 2>&1); then
    printf 'hunt.sh: invalid %s=%s: %s\n' "$label" "$provider" "$output" >&2
    return 1
  fi
  printf '%s\n' "$output"
}

hunt_write_provider_profile() {
  local output=$1 provider=$2 model=$3 reasoning=$4 temporary
  local -a command=(
    python3 -B lib/history_audit_cli.py provider-command
    --surface hunt
    --provider "$provider"
  )
  [ -z "$model" ] || command+=(--model "$model")
  [ -z "$reasoning" ] || command+=(--reasoning "$reasoning")
  mkdir -p "$(dirname "$output")" || return 1
  temporary="${output}.tmp.$$"
  if ! "${command[@]}" > "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  chmod 600 "$temporary" || { rm -f "$temporary"; return 1; }
  mv -f "$temporary" "$output"
}

hunt_write_base_profile() {
  hunt_write_provider_profile \
    "$1" \
    "${HUNT_PROVIDER:-codex}" \
    "${HUNT_MODEL:-}" \
    "${HUNT_REASONING_EFFORT:-}"
}

hunt_write_review_profile() {
  local seat=$1 output=$2 provider model reasoning provider_changed=0
  local provider_name="HUNT_REVIEW_PROVIDER_${seat}"
  local model_name="HUNT_REVIEW_MODEL_${seat}"
  local reasoning_name="HUNT_REVIEW_REASONING_EFFORT_${seat}"
  local base_provider=${HUNT_PROVIDER:-codex}
  if runtime_variable_is_set "$provider_name" && [ -n "${!provider_name}" ]; then
    provider=${!provider_name}
  else
    provider=$base_provider
  fi
  [ "$provider" = "$base_provider" ] || provider_changed=1
  if runtime_variable_is_set "$model_name"; then
    model=${!model_name}
  elif [ "$provider_changed" -eq 1 ]; then
    model=
  else
    model=${HUNT_MODEL:-}
  fi
  if runtime_variable_is_set "$reasoning_name"; then
    reasoning=${!reasoning_name}
  elif [ "$provider_changed" -eq 1 ]; then
    reasoning=
  else
    reasoning=${HUNT_REASONING_EFFORT:-}
  fi
  hunt_write_provider_profile "$output" "$provider" "$model" "$reasoning"
}

hunt_runtime_preflight() {
  local abi=v1 name index indices="" provider model reasoning diagnostic
  local base_provider base_model base_reasoning provider_changed reviewer_limit
  local provider_name model_name reasoning_name
  if runtime_variable_is_set HISTORY_RUNTIME_ABI; then
    abi=$HISTORY_RUNTIME_ABI
  fi
  case "$abi" in
    v1)
      HUNT_RUNTIME_ABI=v1
      for name in HUNT_PROVIDER HUNT_MODEL HUNT_REASONING_EFFORT; do
        if runtime_variable_is_set "$name"; then
          printf 'hunt.sh: %s is valid only with HISTORY_RUNTIME_ABI=v2\n' \
            "$name" >&2
          return 2
        fi
      done
      while IFS= read -r name; do
        case "$name" in
          HUNT_REVIEW_PROVIDER_*|HUNT_REVIEW_MODEL_*|HUNT_REVIEW_REASONING_EFFORT_*)
            printf 'hunt.sh: %s is valid only with HISTORY_RUNTIME_ABI=v2\n' \
              "$name" >&2
            return 2
            ;;
        esac
      done < <(compgen -A variable HUNT_REVIEW_)
      return 0
      ;;
    v2) ;;
    *)
      printf 'hunt.sh: HISTORY_RUNTIME_ABI must be v1 or v2: %s\n' "$abi" >&2
      return 2
      ;;
  esac

  if runtime_variable_is_set CONTAINED_AGENT_CMD_JSON; then
    printf 'hunt.sh: CONTAINED_AGENT_CMD_JSON cannot be mixed with v2 provider controls\n' >&2
    return 2
  fi
  while IFS= read -r name; do
    case "$name" in
      CONTAINED_REV_CMD_*_JSON)
        printf 'hunt.sh: %s cannot be mixed with v2 provider controls\n' \
          "$name" >&2
        return 2
        ;;
    esac
  done < <(compgen -A variable CONTAINED_REV_CMD_)

  base_provider=${HUNT_PROVIDER:-codex}
  base_model=${HUNT_MODEL:-}
  base_reasoning=${HUNT_REASONING_EFFORT:-}
  reviewer_limit=${REVIEWERS:-3}
  case "$reviewer_limit" in
    ''|0|*[!0-9]*)
      printf 'hunt.sh: REVIEWERS must be a positive integer for v2 preflight: %s\n' \
        "$reviewer_limit" >&2
      return 2
      ;;
  esac
  diagnostic=$(hunt_provider_diagnostic \
    HUNT_PROVIDER "$base_provider" "$base_model" "$base_reasoning") \
    || return 2

  while IFS= read -r name; do
    case "$name" in
      HUNT_REVIEW_PROVIDER_*|HUNT_REVIEW_MODEL_*|HUNT_REVIEW_REASONING_EFFORT_*)
        index=${name##*_}
        case "$index" in
          ''|0|*[!0-9]*)
            printf 'hunt.sh: invalid numbered review provider control: %s\n' \
              "$name" >&2
            return 2
            ;;
        esac
        if [ "$index" -gt "$reviewer_limit" ]; then
          printf 'hunt.sh: %s is outside REVIEWERS=%s\n' \
            "$name" "$reviewer_limit" >&2
          return 2
        fi
        case " $indices " in
          *" $index "*) ;;
          *) indices="$indices $index" ;;
        esac
        ;;
    esac
  done < <(compgen -A variable HUNT_REVIEW_)

  for index in $indices; do
    provider_name=HUNT_REVIEW_PROVIDER_$index
    model_name=HUNT_REVIEW_MODEL_$index
    reasoning_name=HUNT_REVIEW_REASONING_EFFORT_$index
    if runtime_variable_is_set "$provider_name" && [ -n "${!provider_name}" ]; then
      provider=${!provider_name}
    else
      provider=$base_provider
    fi
    provider_changed=0
    [ "$provider" = "$base_provider" ] || provider_changed=1
    if runtime_variable_is_set "$model_name"; then
      model=${!model_name}
    elif [ "$provider_changed" -eq 1 ]; then
      model=
    else
      model=$base_model
    fi
    if runtime_variable_is_set "$reasoning_name"; then
      reasoning=${!reasoning_name}
    elif [ "$provider_changed" -eq 1 ]; then
      reasoning=
    else
      reasoning=$base_reasoning
    fi
    hunt_provider_diagnostic "$provider_name" "$provider" "$model" "$reasoning" \
      >/dev/null || return 2
  done

  HUNT_RUNTIME_ABI=v2
  return 0
}

hunt_runtime_preflight || exit 2
git config core.hooksPath .githooks

AGENT_CMD=${AGENT_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write}
FRONT_CMD=${FRONT_CMD:-$AGENT_CMD}
BACK_CMD=${BACK_CMD:-$AGENT_CMD}
FAIL_SLEEP_MIN=${FAIL_SLEEP_MIN:-${1:-150}}
# Contract/format failures should not burn the long operational cooldown.
CONTRACT_FAIL_SLEEP_MIN=${CONTRACT_FAIL_SLEEP_MIN:-1}
NO_HIT_SLEEP_MIN_LO=${NO_HIT_SLEEP_MIN_LO:-1}
NO_HIT_SLEEP_MIN_HI=${NO_HIT_SLEEP_MIN_HI:-8}
ALLOW_ZERO_NO_HIT_SLEEP=${ALLOW_ZERO_NO_HIT_SLEEP:-0}
MAX_FAILS=${MAX_FAILS:-12}
REVIEWERS=${REVIEWERS:-3}
MIN_READ=${MIN_READ:-5}
SA_TARGET=${SA_TARGET:-1}
ROUND_LIMIT=${ROUND_LIMIT:-0}
EMPTY_MAX=${EMPTY_MAX:-3}
PRIOR_MIN_LINKS=${PRIOR_MIN_LINKS:-5}
PRIOR_MIN_API=${PRIOR_MIN_API:-1}
RESEARCH_RETRY=${RESEARCH_RETRY:-1}
SHORT_MAX=${SHORT_MAX:-3}
THEME_MIN_LOW=${THEME_MIN_LOW:-2}
AXIOM_MIN_CRACKS=${AXIOM_MIN_CRACKS:-2}
RESUME_FRONT=${RESUME_FRONT:-1}
RESEARCH_DIRECTION_FILE=${RESEARCH_DIRECTION_FILE:-}

LOG=hunt.log
RD=tmp/round
LOCK=tmp/hunt.lock
LOCK_STATUS=
LOCK_HOLDER_PID=
ARCHIVE_SOURCE=
RECOVERY_ARCHIVE_DIR=
RECOVERY_RUN_ID=
RECOVERY_ROUND=
RECOVERY_DATE=
RECOVERY_REASON=
RECOVERY_OUTCOME=
RECOVERY_REPORT_PATH=
RECOVERY_CURRENT_DECISION_COMPLETE=0
HALT_MARK=tmp/HALTED-ARCHIVE-FAIL
HISTORY_DB=.ai-ideas/history.sqlite3
HISTORY_STATE_ROOT=.ai-ideas
HISTORY_POLICY=history/retrieval-policy-v1.json
HISTORY_LEDGER_GOOD=tmp/ledger.good
HISTORY_NEAR_SA=${HISTORY_NEAR_SA:-}
HISTORY_REVIEW_CONTRACT=history/review-contract-v1.md
HISTORY_CALIBRATION_CAPABILITY=${HISTORY_CALIBRATION_CAPABILITY:-}
HISTORY_PRODUCTION_TRUST_ROOT=${HISTORY_PRODUCTION_TRUST_ROOT:-}
RUNS_DIR=${RUNS_DIR:-$HOME/.ai-ideas-runs/$(basename "$PWD")}
STRUCTURED_API_RE='^- Query:[[:space:]]*https?://(export\.arxiv\.org/api/query\?[^[:space:]]+|api\.semanticscholar\.org/graph/v1/[A-Za-z0-9._~%/:+-]+\?[^[:space:]]+)[[:space:]]*$'

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"
}

copy_mutable_round_view() {
  local source=$1 destination=$2 temporary="${2}.tmp.$$"
  if ! cp "$source" "$temporary" || ! chmod 600 "$temporary" \
     || ! mv -f "$temporary" "$destination"; then
    rm -f "$temporary"
    return 1
  fi
}

is_uint() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

validate_config() {
  local name value
  for name in \
    FAIL_SLEEP_MIN CONTRACT_FAIL_SLEEP_MIN NO_HIT_SLEEP_MIN_LO NO_HIT_SLEEP_MIN_HI MAX_FAILS \
    REVIEWERS MIN_READ SA_TARGET ROUND_LIMIT EMPTY_MAX PRIOR_MIN_LINKS \
    PRIOR_MIN_API RESEARCH_RETRY SHORT_MAX THEME_MIN_LOW AXIOM_MIN_CRACKS
  do
    value=${!name}
    is_uint "$value" || {
      printf 'hunt.sh: %s must be a nonnegative integer: %s\n' \
        "$name" "$value" >&2
      exit 2
    }
  done
  [ "$MAX_FAILS" -ge 1 ] || {
    printf 'hunt.sh: MAX_FAILS must be at least 1\n' >&2; exit 2;
  }
  [ "$REVIEWERS" -ge 1 ] || {
    printf 'hunt.sh: REVIEWERS must be at least 1\n' >&2; exit 2;
  }
  [ "$EMPTY_MAX" -ge 1 ] || {
    printf 'hunt.sh: EMPTY_MAX must be at least 1\n' >&2; exit 2;
  }
  [ "$SHORT_MAX" -ge 1 ] || {
    printf 'hunt.sh: SHORT_MAX must be at least 1\n' >&2; exit 2;
  }
  [ "$AXIOM_MIN_CRACKS" -ge 1 ] || {
    printf 'hunt.sh: AXIOM_MIN_CRACKS must be at least 1\n' >&2
    exit 2
  }
  case "$ALLOW_ZERO_NO_HIT_SLEEP" in
    0|1) ;;
    *) printf 'hunt.sh: ALLOW_ZERO_NO_HIT_SLEEP must be 0 or 1\n' >&2; exit 2 ;;
  esac
  case "$RESUME_FRONT" in
    0|1) ;;
    *) printf 'hunt.sh: RESUME_FRONT must be 0 or 1\n' >&2; exit 2 ;;
  esac
  if [ "$NO_HIT_SLEEP_MIN_LO" -gt "$NO_HIT_SLEEP_MIN_HI" ]; then
    printf 'hunt.sh: NO_HIT_SLEEP_MIN_LO cannot exceed NO_HIT_SLEEP_MIN_HI\n' >&2
    exit 2
  fi
  if [ "$ALLOW_ZERO_NO_HIT_SLEEP" != 1 ] \
     && { [ "$NO_HIT_SLEEP_MIN_LO" -lt 1 ] \
       || [ "$NO_HIT_SLEEP_MIN_HI" -lt 1 ]; }; then
    printf '%s\n' \
      'hunt.sh: No-hit sleeps must be positive outside explicit test configuration' \
      >&2
    exit 2
  fi
  case "$RUNS_DIR" in
    ''|/) printf 'hunt.sh: RUNS_DIR must name a bounded directory\n' >&2; exit 2 ;;
  esac
}

sleep_minutes() {
  local minutes=$1
  log "Retrying in ${minutes} minutes"
  sleep "$((minutes * 60))"
}

random_no_hit_sleep_min() {
  printf '%s\n' \
    "$((NO_HIT_SLEEP_MIN_LO + RANDOM % (NO_HIT_SLEEP_MIN_HI - NO_HIT_SLEEP_MIN_LO + 1)))"
}

acquire_hunt_lock() {
  local status owner
  if [ -d "$LOCK" ]; then
    owner=$(cat "$LOCK/pid" 2>/dev/null || true)
    log "Legacy hunt lock directory is present (pid ${owner:-unknown}); remove it only after verifying no legacy hunt is running"
    return 1
  fi
  LOCK_STATUS="${LOCK}.status.$$.$RANDOM"
  rm -f "$LOCK_STATUS"
  mkfifo "$LOCK_STATUS" || {
    log "Cannot create hunt lock handshake"
    return 1
  }
  exec 9<> "$LOCK_STATUS" || {
    rm -f "$LOCK_STATUS"
    log "Cannot open hunt lock handshake"
    return 1
  }
  python3 - "$LOCK" "$LOCK_STATUS" "$$" <<'PY' &
import fcntl
import os
import pathlib
import secrets
import signal
import stat
import sys
import time

lock_path = pathlib.Path(sys.argv[1])
status_path = pathlib.Path(sys.argv[2])
parent_pid = int(sys.argv[3])
token = secrets.token_hex(16)

def publish_status(value):
    with status_path.open("w", encoding="utf-8") as stream:
        stream.write(value + "\n")
        stream.flush()

def safe_lock_state(descriptor):
    state = os.fstat(descriptor)
    return (
        stat.S_ISREG(state.st_mode)
        and state.st_nlink == 1
        and state.st_uid == os.geteuid()
    )

def raise_exit(*_):
    raise SystemExit(0)

try:
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    if not safe_lock_state(descriptor):
        raise OSError("unsafe lock file identity")
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if not safe_lock_state(descriptor):
        raise OSError("lock file identity changed")
except BlockingIOError:
    publish_status("busy")
    raise SystemExit(2)
except OSError:
    publish_status("error")
    raise SystemExit(2)

owner = f"pid={parent_pid} token={token}\n".encode("ascii")
os.ftruncate(descriptor, 0)
os.lseek(descriptor, 0, os.SEEK_SET)
os.write(descriptor, owner)
os.fsync(descriptor)
publish_status("locked")
signal.signal(signal.SIGTERM, raise_exit)

while os.getppid() == parent_pid:
    time.sleep(0.25)
PY
  LOCK_HOLDER_PID=$!
  if ! IFS= read -r status <&9; then
    status=error
  fi
  exec 9>&-
  rm -f "$LOCK_STATUS"
  case "$status" in
    locked) return 0 ;;
    busy)
      owner=$(cat "$LOCK" 2>/dev/null || true)
      log "Another hunt.sh instance is running (${owner:-owner unavailable})"
      wait "$LOCK_HOLDER_PID" 2>/dev/null || true
      LOCK_HOLDER_PID=
      return 1
      ;;
    *)
      log "Cannot acquire hunt lock safely"
      kill "$LOCK_HOLDER_PID" 2>/dev/null || true
      wait "$LOCK_HOLDER_PID" 2>/dev/null || true
      LOCK_HOLDER_PID=
      return 1
      ;;
  esac
}

pick_lens() {
  local n total
  total=$(awk '
    /^## Divergence Lenses/ {inside=1; next}
    /^## / {inside=0}
    inside && /^- / {count++}
    END {print count+0}
  ' brainstorming_policy.md)
  [ "$total" -gt 0 ] || { printf '\n'; return 0; }
  n=$((RANDOM % (total + 3) + 1))
  [ "$n" -le "$total" ] || { printf '\n'; return 0; }
  awk -v wanted="$n" '
    /^## Divergence Lenses/ {inside=1; next}
    /^## / {inside=0}
    inside && /^- / {
      seen++
      if (seen == wanted) {
        sub(/^- /, "")
        print
        exit
      }
    }
  ' brainstorming_policy.md
}

# Convert a legacy external command string to NUL-separated argv without shell
# evaluation.  This keeps existing FRONT_CMD/BACK_CMD configuration while
# making command substitution, redirection, and control operators inert.
external_command_argv() {
  python3 - "$1" <<'PY'
import os
import shlex
import shutil
import sys

try:
    argv = shlex.split(sys.argv[1], posix=True)
except ValueError as exc:
    raise SystemExit(f"invalid external command: {exc}")
if not argv or any(not item or any(c in item for c in "\x00\r\n") for item in argv):
    raise SystemExit("external command contains an invalid argument")
if any(item in {";", "&&", "||", "|", "&", "<", ">"} for item in argv):
    raise SystemExit("external command contains a shell control operator")
resolved = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
if not resolved:
    raise SystemExit("external command executable is unavailable")
argv[0] = os.path.realpath(resolved)
sys.stdout.buffer.write(b"\0".join(item.encode() for item in argv) + b"\0")
PY
}

# Contained commands are accepted only as closed JSON argv with an absolute
# executable.  The normalized bytes are passed unchanged to every runtime
# entrypoint that seals the command prefix.
normalize_command_json() {
  python3 - "$1" <<'PY'
import json
import os
import sys

try:
    argv = json.loads(sys.argv[1])
except ValueError as exc:
    raise SystemExit(f"contained command is not JSON: {exc}")
if (
    not isinstance(argv, list)
    or not argv
    or any(
        not isinstance(item, str)
        or not item
        or any(c in item for c in "\x00\r\n")
        for item in argv
    )
):
    raise SystemExit("contained command must be a nonempty string argv")
if not os.path.isabs(argv[0]):
    raise SystemExit("contained command executable must be absolute")
resolved = os.path.realpath(argv[0])
if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
    raise SystemExit("contained command executable is unavailable")
argv[0] = resolved
print(json.dumps(argv, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
PY
}

default_contained_command_json() {
  local codex_path
  codex_path=$(command -v codex) || {
    log "Codex executable is unavailable; set CONTAINED_AGENT_CMD_JSON"
    return 1
  }
  python3 - "$codex_path" <<'PY'
import json
import os
import sys

print(json.dumps(
    [
        os.path.realpath(sys.argv[1]),
        "-m",
        "gpt-5.3-codex-spark",
        "-c",
        "model_reasoning_effort=xhigh",
    ],
    separators=(",", ":"),
))
PY
}

history_runtime_authorized() {
  local command=$1
  local -a command_line
  shift
  command_line=(python3 lib/history_runtime.py "$command" "$@")
  [ -n "$HISTORY_CALIBRATION_CAPABILITY" ] \
    && command_line+=(
      --calibration-capability "$HISTORY_CALIBRATION_CAPABILITY"
    )
  [ -n "$HISTORY_PRODUCTION_TRUST_ROOT" ] \
    && command_line+=(
      --production-trust-root "$HISTORY_PRODUCTION_TRUST_ROOT"
    )
  "${command_line[@]}"
}

history_audit_init() {
  [ "$HUNT_RUNTIME_ABI" = v2 ] || return 0
  python3 -B lib/history_audit_cli.py init \
    --db "$HISTORY_DB" \
    --cas-root "$HISTORY_STATE_ROOT/audit-cas"
}

history_audit_plan_shadow_round() {
  local batch=$1 selection=$2 observation_root=$3 output_root=$4 profile_root=$5
  local candidate_id candidate_path observation_path seat candidate_list
  local -a profile_args=(
    --execution-request-profile "$profile_root/base.json"
  )
  [ "$HUNT_RUNTIME_ABI" = v2 ] || return 0
  mkdir -p "$output_root/candidates" || return 1
  for seat in $(seq 1 "$REVIEWERS"); do
    if ! hunt_write_review_profile \
      "$seat" "$profile_root/review-${seat}.json"; then
      return 1
    fi
    profile_args+=(
      --execution-request-profile "$profile_root/review-${seat}.json"
    )
  done
  candidate_list="$output_root/candidates.tsv"
  if ! python3 -B - \
    "$batch" "$selection" "$observation_root" "$output_root/candidates" \
    > "$candidate_list" <<'PY'
import json
import pathlib
import sys

from lib import history_audit_plan
from lib import history_runtime

batch_path, selection_path, observation_root, candidate_root = sys.argv[1:]
batch = json.loads(pathlib.Path(batch_path).read_text(encoding="utf-8"))
history_runtime.verify_frozen_batch(batch)
selection = json.loads(pathlib.Path(selection_path).read_text(encoding="utf-8"))
selected = {
    item["candidate_id"]
    for item in selection.get("targets", [])
    if item.get("disposition") == "shortlist"
}
root = pathlib.Path(candidate_root)
root.mkdir(parents=True, exist_ok=True)
for source_order, descriptor in enumerate(batch["candidates"]):
    candidate_id = descriptor["candidate_id"]
    if candidate_id not in selected:
        continue
    candidate = {
        "candidate_id": candidate_id,
        "candidate_hash": "",
        "raw_artifact_sha": descriptor["content_sha256"],
        "source_order": source_order,
    }
    candidate["candidate_hash"] = history_audit_plan.runtime_candidate_hash(
        candidate
    )
    raw = (
        json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    candidate_path = root / f"{candidate_id}.json"
    candidate_path.write_bytes(raw)
    observation_path = (
        pathlib.Path(observation_root)
        / candidate_id
        / "build-observation.json"
    )
    print(candidate_id, candidate_path, observation_path, sep="\t")
PY
  then
    return 1
  fi
  while IFS=$'\t' read -r candidate_id candidate_path observation_path; do
    [ -n "$candidate_id" ] || continue
    python3 -B lib/history_audit_cli.py plan \
      --db "$HISTORY_DB" \
      --candidate "$candidate_path" \
      --batch "$batch" \
      --intent duplicate_search \
      --output "$output_root/${candidate_id}.json" \
      --l1-observation "$observation_path" \
      "${profile_args[@]}" \
      > "$output_root/${candidate_id}.stdout" \
      || return 1
  done < "$candidate_list"
}

history_startup() {
  local brief=$1 lens=${2:-}
  local -a startup_args=(
    --db "$HISTORY_DB"
    --ledger ledger.tsv
    --ledger-good "$HISTORY_LEDGER_GOOD"
    --state-root "$HISTORY_STATE_ROOT"
    --policy "$HISTORY_POLICY"
    --brief "$brief"
    --divergence-lens "$lens"
  )
  if [ -n "$HISTORY_NEAR_SA" ]; then
    startup_args+=(--near-sa "$HISTORY_NEAR_SA")
  fi
  history_runtime_authorized startup "${startup_args[@]}"
}

history_sync() {
  history_startup "$@"
}

history_build_brief() {
  history_startup "$@"
}

history_reconcile_ledger() {
  python3 lib/history_cli.py --db "$HISTORY_DB" reconcile-ledger \
    --ledger ledger.tsv \
    --ledger-good "$HISTORY_LEDGER_GOOD" \
    --state-root "$HISTORY_STATE_ROOT"
}

history_policy_mode() {
  python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], "rb") as handle:
    value = json.load(handle)
print(value["policy_mode"])
PY
}

run_contained_stage() {
  local stage=$1 seat=$2 output_root=$3 manifest=$4 command_json=$5
  shift 5
  history_runtime_authorized run-stage \
    --stage "$stage" \
    --seat "$seat" \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --output-root "$output_root" \
    --manifest "$manifest" \
    --command "$command_json" \
    "$@"
}

run_portable_generate_stage() {
  local profile=$1 output_root=$2 state_root=$3 prompt_path=$4
  shift 4
  printf '%s\n' '{"schema_version":1,"stage":"generate"}' \
    > "$prompt_path" || return 1
  chmod 600 "$prompt_path" || return 1
  python3 -B lib/portable_stage.py run \
    --provider-request-profile "$profile" \
    --stage generate \
    --seat generate \
    --serialized-prompt "$prompt_path" \
    --output-root "$output_root" \
    --state-root "$state_root" \
    "$@"
}

history_freeze_batch() {
  local -a freeze_args=(
    --tsv "$1"
    --markdown "$2"
    --output-root "$3"
    --brief "$4"
    --expected-direction "$6"
  )
  if [ -n "${5:-}" ]; then
    freeze_args+=(--direction "$5")
  fi
  python3 lib/history_runtime.py freeze-batch \
    "${freeze_args[@]}"
}

history_publish_round_direction() {
  local mode=$1
  local -a publish_args=(
    publish-round
    --startup-identity "$startup_root/direction-identity.json"
    --output-identity "$RD/history/direction-identity.json"
  )
  if [ "$direction_active" -eq 1 ]; then
    publish_args+=(
      --startup-contract "$startup_root/direction-constraint.json"
      --output-contract "$RD/history/direction-constraint.json"
    )
  fi
  [ "$mode" = fresh ] || publish_args+=(--resume)
  python3 lib/direction_contract.py "${publish_args[@]}"
}

history_observe_round() {
  history_runtime_authorized observe-round \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$1" \
    --artifact-root "$2"
}

history_seal_selection() {
  local theme_min_low=$THEME_MIN_LOW
  [ "${direction_active:-0}" -eq 0 ] || theme_min_low=0
  python3 lib/history_runtime.py seal-selection \
    --batch "$1" \
    --round-observation "$2" \
    --brief "$3" \
    --selector "$4" \
    --prescreen "$5" \
    --short-max "$SHORT_MAX" \
    --theme-min-low "$theme_min_low" \
    --output "$6"
}

history_materialize_selection() {
  python3 lib/history_runtime.py materialize-selection \
    --batch "$1" \
    --selection "$2" \
    --output-root "$3"
}

history_compare_shortlist() {
  local -a compare_args=(
    compare-targets
    --db "$HISTORY_DB"
    --policy "$HISTORY_POLICY"
    --batch "$1"
    --artifact-root "$2"
    --selection "$3"
  )
  if [ "$HUNT_RUNTIME_ABI" = v2 ]; then
    compare_args+=(
      --executor portable-v2
      --provider-request-profile "$4"
    )
  else
    compare_args+=(--command "$4")
  fi
  history_runtime_authorized "${compare_args[@]}"
}

history_compare_targets() {
  history_compare_shortlist "$@"
}

history_publish_summaries() {
  history_runtime_authorized publish-summaries \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$1" \
    --selection "$2" \
    --artifact-root "$3"
}

history_materialize_research() {
  history_runtime_authorized materialize-research \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$1" \
    --selection "$2" \
    --comparison-index "$3" \
    --artifact-root "$4" \
    --output-root "$5"
}

history_seal_resume() {
  history_runtime_authorized seal-resume \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$1" \
    --selection "$2" \
    --artifact-root "$3" \
    --comparison-index "$4" \
    --prior-work "$5" \
    --output "$6"
}

history_seal_resume_attempt() {
  local resume=$1 run_id=$2 resumed_from=$3 output=$4
  local -a attempt_args=(
    --policy "$HISTORY_POLICY"
    --resume "$resume"
    --run-id "$run_id"
    --resumed-from "$resumed_from"
    --output "$output"
  )
  if [ -n "${5:-}" ]; then
    attempt_args+=(--prior-archive "$5")
  fi
  history_runtime_authorized seal-resume-attempt \
    "${attempt_args[@]}"
}

history_receipts_ok() {
  history_runtime_authorized validate-resume \
    --policy "$HISTORY_POLICY" \
    --resume "$1" \
    --expected-direction "$startup_root/direction-identity.json"
}

validate_direction_verdicts() {
  python3 lib/history_runtime.py validate-direction-gate \
    --contract "$RD/history/direction-constraint.json" \
    --expected-direction "$startup_root/direction-identity.json" \
    --batch "$RD/history/batch/batch.json" \
    --verdicts "$RD/direction.tsv" \
    --output "$RD/history/direction-gate.json"
}

history_seal_review_plan() {
  local batch=$1 selection=$2 observation_root=$3 comparison=$4
  local prior=$5 output=$6
  shift 6
  history_runtime_authorized seal-review-plan \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$batch" \
    --selection "$selection" \
    --comparison-index "$comparison" \
    --artifact-root "$observation_root" \
    --prior-work "$prior" \
    --review-contract "$HISTORY_REVIEW_CONTRACT" \
    --round-date "$today" \
    --min-read "$MIN_READ" \
    --axiom-min-cracks "$AXIOM_MIN_CRACKS" \
    --output "$output" \
    "$@"
}

history_run_review_matrix() {
  local batch=$1 plan=$2 stage_root=$3 output=$4
  shift 4
  history_runtime_authorized run-review-matrix \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$batch" \
    --review-plan "$plan" \
    --stage-root "$stage_root" \
    --output "$output" \
    "$@"
}

history_build_aggregation() {
  history_runtime_authorized build-aggregation \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$1" \
    --review-plan "$2" \
    --review-index "$3" \
    --output "$4"
}

history_materialize_report() {
  history_runtime_authorized materialize-report \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$1" \
    --research-view "$2" \
    --review-plan "$3" \
    --review-index "$4" \
    --aggregation "$5" \
    --round-number "$6" \
    --output-root "$7"
}

history_append_rows() {
  history_runtime_authorized commit-round \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$1" \
    --selection "$2" \
    --comparison-index "$3" \
    --review-plan "$4" \
    --review-index "$5" \
    --aggregation "$6"
}

history_commit_round() {
  history_append_rows "$@"
}

history_materialize_ledger() {
  history_reconcile_ledger
}

prepare_external_mirror() {
  local stage=$1 mirror=$2 id story theme summary report_view
  mkdir -p "$mirror/roles" "$mirror/tmp/round" "$mirror/ideas"
  git -C "$mirror" init -q
  case "$stage" in
    select)
      cp roles/select.md "$mirror/roles/select.md"
      cp brainstorming_policy.md "$mirror/brainstorming_policy.md"
      cp "$RD/history/batch/sources/ideas.md" \
        "$mirror/tmp/round/ideas.md"
      if [ "$direction_active" -eq 1 ]; then
        mkdir -p "$mirror/tmp/round/history"
        if ! python3 lib/history_runtime.py copy-direction \
          --contract "$RD/history/direction-constraint.json" \
          --round-identity "$RD/history/direction-identity.json" \
          --expected-direction "$startup_root/direction-identity.json" \
          --batch "$RD/history/batch/batch.json" \
          --output \
            "$mirror/tmp/round/history/direction-constraint.json" \
          > "$RD/history/copy-direction.json"; then
          log "Direction snapshot changed before selector copy"
          return 2
        fi
      fi
      ;;
    prescreen)
      cp roles/prescreen.md "$mirror/roles/prescreen.md"
      cp "$RD/ideas.all.md" "$mirror/tmp/round/ideas.all.md"
      cp "$RD/ideas.all.tsv" "$mirror/tmp/round/ideas.all.tsv"
      ;;
    research)
      cp roles/research.md "$mirror/roles/research.md"
      cp "$RD/ideas.md" "$mirror/tmp/round/ideas.md"
      cp "$RD/ideas.tsv" "$mirror/tmp/round/ideas.tsv"
      if [ -s tmp/litwatch/index.jsonl ]; then
        mkdir -p "$mirror/tmp/litwatch"
        cp tmp/litwatch/index.jsonl "$mirror/tmp/litwatch/index.jsonl"
      fi
      while IFS=$'\t' read -r id story theme; do
        [ -n "$id" ] || continue
        summary="$RD/history/research-view/history-summaries/$id.json"
        [ -s "$summary" ] || continue
        mkdir -p \
          "$mirror/tmp/round/history/research-view/history-summaries"
        cp "$summary" \
          "$mirror/tmp/round/history/research-view/history-summaries/$id.json"
      done < "$RD/ideas.tsv"
      ;;
    report)
      report_view=${CURRENT_REPORT_VIEW:-}
      [ -n "$report_view" ] && [ -d "$report_view" ] || {
        log "Verified report view is unavailable"
        return 2
      }
      cp roles/report.md "$mirror/roles/report.md"
      cp "$report_view/accepted.tsv" \
        "$mirror/tmp/round/accepted.tsv"
      cp "$report_view/ideas.md" \
        "$mirror/tmp/round/ideas.md"
      cp "$report_view/priorwork.md" \
        "$mirror/tmp/round/priorwork.md"
      cp "$report_view/rejects.tsv" \
        "$mirror/tmp/round/rejects.tsv"
      cp "$report_view/meta.txt" \
        "$mirror/tmp/round/meta.txt"
      mkdir -p "$mirror/tmp/round/rev/1"
      cp "$report_view/rev/1/review.md" \
        "$mirror/tmp/round/rev/1/review.md"
      ;;
    *)
      log "Unknown external stage mirror: $stage"
      return 2
      ;;
  esac
  find "$mirror/roles" "$mirror/tmp" -type f -exec chmod 444 {} \;
  if [ -f "$mirror/brainstorming_policy.md" ]; then
    chmod 444 "$mirror/brainstorming_policy.md"
  fi
}

external_input_manifest() {
  local operation=$1 mirror=$2 manifest=$3
  python3 - "$operation" "$mirror" "$manifest" <<'PY'
import hashlib
import json
import pathlib
import stat
import sys

operation = sys.argv[1]
root = pathlib.Path(sys.argv[2]).resolve()
manifest_path = pathlib.Path(sys.argv[3])

def capture(relative):
    path = root / relative
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        raise SystemExit(f"external input is not regular: {relative}")
    raw = path.read_bytes()
    if len(raw) != status.st_size:
        raise SystemExit(f"external input changed during capture: {relative}")
    return hashlib.sha256(raw).hexdigest()

if operation == "seal":
    paths = []
    for relative in ("brainstorming_policy.md",):
        if (root / relative).exists():
            paths.append(relative)
    for base in ("roles", "tmp"):
        for path in sorted((root / base).rglob("*")):
            if path.is_file():
                paths.append(str(path.relative_to(root)))
    value = {
        "schema_version": 1,
        "inputs": [
            {"path": relative, "sha256": capture(relative)}
            for relative in paths
        ],
    }
    manifest_path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    )
elif operation == "verify":
    value = json.loads(manifest_path.read_text())
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "inputs"}
        or value["schema_version"] != 1
        or not isinstance(value["inputs"], list)
    ):
        raise SystemExit("external input manifest is invalid")
    for item in value["inputs"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or capture(item["path"]) != item["sha256"]
        ):
            raise SystemExit("external stage changed a declared input")
else:
    raise SystemExit("unknown external input-manifest operation")
PY
}

copy_external_output() {
  local stage=$1 mirror=$2 diagnostic=${3:-}
  python3 - \
    "$stage" "$mirror" "$RD" "$today" "$diagnostic" \
    "$run_id" "$RUNS_DIR" <<'PY'
import base64
import hashlib
import json
import os
import pathlib
import stat
import sys

stage = sys.argv[1]
mirror = pathlib.Path(sys.argv[2]).resolve()
round_root = pathlib.Path(sys.argv[3]).resolve()
today = sys.argv[4]
diagnostic = sys.argv[5]
run_id = sys.argv[6]
runs_root = pathlib.Path(sys.argv[7]).resolve()

outputs = {
    "select": ("tmp/round/select.tsv", round_root / "select.tsv", 65536, False),
    "prescreen": (
        "tmp/round/prescreen.md",
        round_root / "prescreen.md",
        65536,
        False,
    ),
    "research": (
        "tmp/round/priorwork.md",
        round_root / "priorwork.md",
        1024 * 1024,
        True,
    ),
}

def read_regular(path, maximum, required):
    try:
        status = path.lstat()
    except FileNotFoundError:
        if required:
            raise SystemExit(f"{stage} omitted its declared output")
        return b""
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or status.st_size > maximum
    ):
        raise SystemExit(
            f"{stage} output is not a bounded regular file "
            f"(mode={status.st_mode:#o} nlink={status.st_nlink} "
            f"size={status.st_size} max={maximum})"
        )
    raw = path.read_bytes()
    if len(raw) != status.st_size:
        raise SystemExit(f"{stage} output size changed during read")
    if b"\0" in raw:
        raise SystemExit(f"{stage} output contains NUL bytes")
    # Agents may emit CRLF; normalize so host gates see LF-only text.
    if b"\r" in raw:
        raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{stage} output is not UTF-8") from exc
    return raw

def atomic_write(destination, raw):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.name + f".tmp-{os.getpid()}"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_new_binding(destination, raw):
    archive = destination.parent
    archive_state = archive.lstat()
    if (
        not stat.S_ISDIR(archive_state.st_mode)
        or stat.S_ISLNK(archive_state.st_mode)
        or archive.name != run_id
        or archive.parent != runs_root
    ):
        raise SystemExit("report binding archive is unsafe")
    temporary = archive / f".report-binding.tmp-{os.getpid()}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination, follow_symlinks=False)
        temporary.unlink()
        directory = os.open(archive, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

if diagnostic:
    if stage != "research":
        raise SystemExit("diagnostic copy is only valid for research")
    name = pathlib.PurePath(diagnostic)
    if name.name != diagnostic or not diagnostic.startswith("priorwork.try"):
        raise SystemExit("invalid research diagnostic name")
    raw = read_regular(
        mirror / "tmp/round/priorwork.md",
        1024 * 1024,
        False,
    )
    if raw:
        atomic_write(round_root / "logs" / diagnostic, raw)
    raise SystemExit(0)

if stage == "select":
    relative, destination, maximum, required = outputs[stage]
    raw = read_regular(mirror / relative, maximum, required)
    atomic_write(destination, raw)
    direction_contract = (
        mirror / "tmp/round/history/direction-constraint.json"
    )
    if direction_contract.exists():
        direction_raw = read_regular(
            mirror / "tmp/round/direction.tsv",
            65536,
            True,
        )
        atomic_write(round_root / "direction.tsv", direction_raw)
elif stage in outputs:
    relative, destination, maximum, required = outputs[stage]
    raw = read_regular(mirror / relative, maximum, required)
    atomic_write(destination, raw)
elif stage == "report":
    candidates = sorted((mirror / "ideas").glob("*.md"))
    if len(candidates) != 1:
        raise SystemExit("report must create exactly one markdown artifact")
    binding_destination = runs_root / run_id / "report-binding.json"
    try:
        binding_destination.lstat()
    except FileNotFoundError:
        pass
    else:
        raise SystemExit("report binding already exists")
    raw = read_regular(candidates[0], 1024 * 1024, True)
    destination = pathlib.Path("ideas") / f"{today}_hunt.md"
    suffix = 2
    while destination.exists():
        destination = pathlib.Path("ideas") / f"{today}_hunt-{suffix}.md"
        suffix += 1
    binding = {
        "report_content_base64": base64.b64encode(raw).decode("ascii"),
        "report_path": destination.as_posix(),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "run_id": run_id,
        "schema_version": 1,
    }
    binding_raw = (
        json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    write_new_binding(binding_destination, binding_raw)
    atomic_write(destination, raw)
    print(destination)
else:
    raise SystemExit("unknown external output stage")
PY
}

external_stage_prompt() {
  local stage=$1 mirror=$2 role_prompt=$3 target
  case "$stage" in
    select) target="tmp/round/select.tsv (and tmp/round/direction.tsv only when the role requires it)" ;;
    prescreen) target="tmp/round/prescreen.md" ;;
    research) target="tmp/round/priorwork.md" ;;
    report) target="exactly one new ideas/*.md file" ;;
    *) return 2 ;;
  esac
  printf '%s %s %s' \
    "$(mirror_pre "$mirror" "$target")" \
    "$role_prompt" \
    "Execute the full task immediately without asking for confirmation or waiting for a reply. If you delegate work to subagents, the parent agent must collect their results and create the complete declared artifact before this provider process exits. A chat response or subagent response is not the artifact."
}

run_external_stage() {
  local command_string=$1 prompt=$2 stage=$3 attempt_tag=${4:-}
  local -a argv=()
  local item rc started ended mirror input_manifest stage_log compatibility_log final_prompt
  stage_log="$RD/logs/$stage.log"
  input_manifest="$RD/logs/$stage.input-manifest.json"
  if [ -n "$attempt_tag" ]; then
    stage_log="$RD/logs/$stage.try${attempt_tag}.log"
    input_manifest="$RD/logs/$stage.try${attempt_tag}.input-manifest.json"
  fi
  compatibility_log="$RD/logs/$stage.log"
  while IFS= read -r -d '' item; do
    argv+=("$item")
  done < <(external_command_argv "$command_string")
  [ "${#argv[@]}" -gt 0 ] || {
    log "External stage $stage has no executable argv"
    return 2
  }
  mirror=$(mktemp -d "${TMPDIR:-/tmp}/hunt-${stage}.XXXXXX") || return 2
  case "$mirror" in
    "${TMPDIR:-/tmp}"/hunt-"$stage".*) ;;
    *)
      log "External mirror path is outside its bound"
      rm -rf "$mirror"
      return 2
      ;;
  esac
  ACTIVE_EXTERNAL_MIRROR=$mirror
  if ! prepare_external_mirror "$stage" "$mirror"; then
    rm -rf "$mirror"
    ACTIVE_EXTERNAL_MIRROR=
    return 2
  fi
  mkdir -p "$RD/logs"
  if ! external_input_manifest seal "$mirror" "$input_manifest"; then
    rm -rf "$mirror"
    ACTIVE_EXTERNAL_MIRROR=
    return 2
  fi
  final_prompt=$(external_stage_prompt "$stage" "$mirror" "$prompt") || {
    rm -rf "$mirror"
    ACTIVE_EXTERNAL_MIRROR=
    return 2
  }
  started=$(date '+%F %T')
  log "Starting external stage [$stage] in a disposable mirror"
  # Wrapper root overrides keep every documented backend in the disposable mirror.
  if (
    cd "$mirror" \
      && PWD="$mirror" OLDPWD="$mirror" \
        GROK_REPO="$mirror" CLAUDE_REPO="$mirror" AGY_REPO="$mirror" \
        "${argv[@]}" "$final_prompt"
  ) > "$stage_log" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  cat "$stage_log" >> "$LOG"
  if [ "$rc" -ne 0 ]; then
    log "External stage [$stage] agent exit rc=$rc"
  fi
  if [ "$rc" -eq 0 ]; then
    if ! external_input_manifest verify "$mirror" "$input_manifest" \
      >> "$stage_log" 2>&1; then
      log "External stage [$stage] input-manifest verify failed"
      tail -n 20 "$stage_log" >> "$LOG" 2>/dev/null || true
      rc=2
    elif ! copy_external_output "$stage" "$mirror" \
      >> "$stage_log" 2>&1; then
      log "External stage [$stage] output copy failed"
      tail -n 20 "$stage_log" >> "$LOG" 2>/dev/null || true
      rc=2
    fi
  fi
  if [ "$stage" = research ] && [ "$rc" -ne 0 ] && [ -n "$attempt_tag" ]; then
    copy_external_output \
      research "$mirror" "priorwork.try${attempt_tag}.diagnostic.md" \
      >> "$stage_log" 2>&1 || true
  fi
  if ! rm -rf "$mirror"; then
    log "External stage [$stage] mirror cleanup failed"
    rc=2
  fi
  ACTIVE_EXTERNAL_MIRROR=
  if [ -n "$attempt_tag" ]; then
    cp "$stage_log" "$compatibility_log" 2>/dev/null || true
  fi
  ended=$(date '+%F %T')
  printf '%s\t%s\t%s\t%s\n' "$stage" "$started" "$ended" "$rc" \
    >> "$RD/stages.tsv"
  return "$rc"
}

is_axiom_idea() {
  local id=$1 source=$2
  awk -v id="$id" '
    $1=="##" && $2==id {inside=1; next}
    $1=="##" && $2~/^I[0-9]+$/ {if (inside) exit}
    inside
  ' "$source" 2>/dev/null \
    | grep -q '^Form:[[:space:]]*remove-load-bearing-assumption[[:space:]]*$'
}

theme_in_vocabulary() {
  local wanted=$1
  awk -v wanted="$wanted" '
    /^## Theme Vocabulary[[:space:]]*$/ {inside=1; next}
    inside && /^## / {exit}
    inside && NF {
      count=split($0, values, /[[:space:]]*\/[[:space:]]*/)
      for (i=1; i<=count; i++) {
        value=values[i]
        sub(/^[[:space:]]+/, "", value)
        sub(/[[:space:]]+$/, "", value)
        if (value == wanted) found=1
      }
    }
    END {exit !found}
  ' brainstorming_policy.md
}

themes_ok() {
  local tsv=${1:-$RD/ideas.tsv} id story theme
  while IFS=$'\t' read -r id story theme; do
    [ -n "$id" ] || continue
    theme=$(printf '%s' "$theme" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    theme_in_vocabulary "$theme" || {
      log "Theme gate: $id uses an unknown theme: $theme"
      return 1
    }
  done < "$tsv"
}

axiom_ok() {
  # Complete Form=axiom rows need structured fields + AXIOM_MIN_CRACKS real
  # http(s) Crack Evidence URLs. An incomplete assumption-removal marker
  # satisfies the policy attempt quota; Form=axiom rows in that batch may
  # keep honest non-URL placeholders and must not fail generation-contract.
  local markdown=$1 tsv=$2 id story theme block field value urls
  local incomplete_marker=0
  if grep -qE '^Assumption-Removal Attempt: incomplete ' "$markdown"; then
    incomplete_marker=1
  fi
  while IFS=$'\t' read -r id story theme; do
    [ -n "$id" ] || continue
    is_axiom_idea "$id" "$markdown" || continue
    block=$(awk -v id="$id" '
      $1=="##" && $2==id {inside=1; next}
      $1=="##" && $2~/^I[0-9]+$/ {if (inside) exit}
      inside
    ' "$markdown")
    for field in \
      'Assumption to Remove' \
      'Why It Can Be Removed Now' \
      'Forcing Constraint'
    do
      value=$(printf '%s\n' "$block" \
        | sed -n "s/^${field}:[[:space:]]*//p" | head -1)
      [ "$(printf '%s' "$value" | wc -c | tr -d ' ')" -ge 12 ] || {
        log "Assumption-removal gate: $id lacks $field"
        return 1
      }
    done
    urls=$(printf '%s\n' "$block" \
      | grep -cE '^Crack Evidence:.*https?://' || true)
    if [ "$urls" -ge "$AXIOM_MIN_CRACKS" ]; then
      continue
    fi
    if [ "$incomplete_marker" -eq 1 ]; then
      log "Assumption-removal incomplete exempt: $id ($urls real Crack Evidence URL rows)"
      continue
    fi
    log "Assumption-removal gate: $id has too few Crack Evidence rows"
    return 1
  done < "$tsv"
}

priorwork_ok() {
  local id story theme block links api
  while IFS=$'\t' read -r id story theme; do
    [ -n "$id" ] || continue
    block=$(awk -v id="$id" '
      $1=="##" && $2==id {inside=1; next}
      $1=="##" && $2~/^I[0-9]+$/ {if (inside) exit}
      inside
    ' "$RD/priorwork.md")
    [ -n "$block" ] || {
      log "Research gate: priorwork.md lacks $id"
      return 1
    }
    links=$(printf '%s\n' "$block" | awk '
      /^Nearest Work:/ {inside=1; next}
      /^Strongest Counterexample:/ {inside=0; strongest++; next}
      inside && /^- / && $0 !~ /^- Query:/ && /https?:\/\// {links++}
      END {
        if (strongest != 1) exit 2
        print links+0
      }
    ') || {
      log "Research gate: $id has an invalid nearest-work section"
      return 1
    }
    [ "$links" -ge "$PRIOR_MIN_LINKS" ] || {
      log "Research gate: $id has too few linked works"
      return 1
    }
    if [ "$PRIOR_MIN_API" -gt 0 ]; then
      api=$(printf '%s\n' "$block" \
        | grep -cE "$STRUCTURED_API_RE" || true)
      [ "$api" -ge "$PRIOR_MIN_API" ] || {
        log "Research gate: $id has too few reproducible API queries"
        return 1
      }
    fi
    printf '%s\n' "$block" | grep -q '^Papers Read:' || {
      log "Research gate: $id lacks Papers Read"
      return 1
    }
    printf '%s\n' "$block" | grep -qE '^Overlap: (high|medium|low)' || {
      log "Research gate: $id lacks a closed Overlap value"
      return 1
    }
  done < "$RD/ideas.tsv"
}

cracks_ok() {
  # Complete Form=axiom rows (real http(s) Crack Evidence in the idea)
  # need Crack Evidence Verification with AXIOM_MIN_CRACKS real URL lines.
  # Hollow / incomplete axiom attempts (placeholders, no real URLs) skip this
  # gate — research-view may drop the incomplete marker, so exemption is keyed
  # off the idea's own Crack Evidence URL count, not the marker alone.
  local id story theme block count idea_block idea_urls
  while IFS=$'\t' read -r id story theme; do
    [ -n "$id" ] || continue
    is_axiom_idea "$id" "$RD/ideas.md" || continue
    idea_block=$(awk -v id="$id" '
      $1=="##" && $2==id {inside=1; next}
      $1=="##" && $2~/^I[0-9]+$/ {if (inside) exit}
      inside
    ' "$RD/ideas.md")
    idea_urls=$(printf '%s\n' "$idea_block" \
      | grep -cE '^Crack Evidence:.*https?://' || true)
    if [ "$idea_urls" -lt "$AXIOM_MIN_CRACKS" ]; then
      log "Crack verification incomplete exempt: $id ($idea_urls real Crack Evidence URL rows)"
      continue
    fi
    block=$(awk -v id="$id" '
      $1=="##" && $2==id {inside=1; next}
      $1=="##" && $2~/^I[0-9]+$/ {if (inside) exit}
      inside
    ' "$RD/priorwork.md")
    printf '%s\n' "$block" \
      | grep -q '^## Crack Evidence Verification$' || {
        log "Research gate: $id lacks Crack Evidence Verification"
        return 1
      }
    count=$(printf '%s\n' "$block" \
      | grep -cE '^- https?://.* \| Verification: (supports|partial|contradicts|unreachable) ' \
      || true)
    [ "$count" -ge "$AXIOM_MIN_CRACKS" ] || {
      log "Research gate: $id has too few crack verifications"
      return 1
    }
  done < "$RD/ideas.tsv"
}

# Retained as a compatibility validator for archived pre-cutover review blocks.
# New reviews use the compact sealed review contract and are gated by Python.
archive_round() {
  local reason=$1 source_root=${2:-$RD} destination
  local -a archive_args
  [ -n "${run_id:-}" ] && [ "$run_id" != "-" ] || return 0
  case "$run_id" in
    *[!A-Za-z0-9._-]*) log "Invalid run id: $run_id"; return 1 ;;
  esac
  destination="$RUNS_DIR/$run_id"
  archive_args=(
    --source-root "$source_root"
    --destination "$destination"
    --run-id "$run_id"
    --round "$round"
    --date "$today"
    --policy-mode "${policy_mode:--}"
    --reason "$reason"
    --policy "$HISTORY_POLICY"
    --startup "$source_root/history/startup.json"
    --state-root "$HISTORY_STATE_ROOT"
  )
  if [ -f "$source_root/history/materialize-ledger.json" ]; then
    archive_args+=(
      --projection "$source_root/history/materialize-ledger.json"
    )
  fi
  if [ -n "$HISTORY_CALIBRATION_CAPABILITY" ]; then
    archive_args+=(
      --capability "$HISTORY_CALIBRATION_CAPABILITY"
    )
  fi
  python3 lib/history_archive.py "${archive_args[@]}"
}

sa_today() {
  awk -F'\t' -v date="$today" '
    $1==date && $2=="hunt" && $5=="strong-accept" {count++}
    END {print count+0}
  ' ledger.tsv
}

reports_today() {
  local path count=0
  for path in "ideas/${today}"_hunt*.md; do
    [ -e "$path" ] || continue
    count=$((count + 1))
  done
  printf '%s\n' "$count"
}

seal_decision_outcome() {
  local count=$1
  python3 - "$RD/history/decision-outcome.tsv" "$count" <<'PY'
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    count = int(sys.argv[2])
except ValueError as exc:
    raise SystemExit("decision outcome count is invalid") from exc
if count < 0:
    raise SystemExit("decision outcome count is invalid")
outcome = "strong-accept" if count else "no-strong-accept"
raw = f"{outcome}\t{count}\n".encode("ascii")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags, 0o444)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(raw)
    handle.flush()
    os.fsync(handle.fileno())
PY
}

verify_pending_report_binding() {
  local archive=$1 expected_run_id=$2 expected_date=$3
  python3 - "$archive" "$expected_run_id" "$expected_date" <<'PY'
import base64
import binascii
import hashlib
import json
import os
import pathlib
import re
import stat
import sys

archive = pathlib.Path(sys.argv[1])
expected_run_id = sys.argv[2]
expected_date = sys.argv[3]
binding_path = archive / "report-binding.json"
try:
    archive_state = archive.lstat()
except OSError as exc:
    raise SystemExit(f"report binding archive is unavailable: {exc}")
if (
    not stat.S_ISDIR(archive_state.st_mode)
    or stat.S_ISLNK(archive_state.st_mode)
    or archive.name != expected_run_id
):
    raise SystemExit("report binding archive is unsafe")
try:
    binding_state = binding_path.lstat()
except FileNotFoundError:
    raise SystemExit(3)
if (
    not stat.S_ISREG(binding_state.st_mode)
    or binding_state.st_nlink != 1
    or binding_state.st_size > 2 * 1024 * 1024
):
    raise SystemExit("report binding is unsafe")
raw = binding_path.read_bytes()
if len(raw) != binding_state.st_size:
    raise SystemExit("report binding size changed during read")
try:
    binding = json.loads(raw)
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit("report binding is malformed") from exc
if (
    not isinstance(binding, dict)
    or set(binding) != {
        "schema_version", "run_id", "report_path", "report_sha256",
        "report_content_base64",
    }
    or binding.get("schema_version") != 1
    or binding.get("run_id") != expected_run_id
    or not isinstance(binding.get("report_path"), str)
    or not isinstance(binding.get("report_sha256"), str)
    or not isinstance(binding.get("report_content_base64"), str)
    or re.fullmatch(r"[0-9a-f]{64}", binding["report_sha256"]) is None
):
    raise SystemExit("report binding fields are invalid")
canonical = (
    json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n"
).encode("utf-8")
if raw != canonical:
    raise SystemExit("report binding is not canonical")
try:
    sealed_report_raw = base64.b64decode(
        binding["report_content_base64"], validate=True
    )
except (ValueError, TypeError, binascii.Error) as exc:
    raise SystemExit("bound report content is invalid") from exc
if (
    len(sealed_report_raw) > 1024 * 1024
    or hashlib.sha256(sealed_report_raw).hexdigest()
    != binding["report_sha256"]
):
    raise SystemExit("bound report content does not match its hash")
report_pattern = re.compile(
    rf"ideas/{re.escape(expected_date)}_hunt(?:-(?:[2-9]|[1-9][0-9]+))?\.md"
)
if report_pattern.fullmatch(binding["report_path"]) is None:
    raise SystemExit("report binding path does not match the archived date")
report = pathlib.Path(binding["report_path"])
try:
    report_state = report.lstat()
except FileNotFoundError:
    parent_state = report.parent.lstat()
    if (
        not stat.S_ISDIR(parent_state.st_mode)
        or stat.S_ISLNK(parent_state.st_mode)
    ):
        raise SystemExit("bound report parent is unsafe")
    temporary = report.with_name(f".{report.name}.recovery-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(sealed_report_raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, report, follow_symlinks=False)
        temporary.unlink()
        directory = os.open(report.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    report_state = report.lstat()
except OSError as exc:
    raise SystemExit(f"bound report is unavailable: {exc}")
if (
    not stat.S_ISREG(report_state.st_mode)
    or report_state.st_nlink != 1
    or report_state.st_size > 1024 * 1024
):
    raise SystemExit("bound report is unsafe")
report_raw = report.read_bytes()
if len(report_raw) != report_state.st_size:
    raise SystemExit("bound report size changed during read")
if report_raw != sealed_report_raw:
    raise SystemExit("bound report does not match sealed content")
print(binding["report_path"])
PY
}

snapshot_archive_source() {
  local source=$1 destination=$2
  rm -rf "$destination"
  mkdir -p "$destination" || return 1
  cp -R "$source"/. "$destination"/ || {
    rm -rf "$destination"
    return 1
  }
}

restore_archive_source() {
  local archive=$1 destination=$2
  snapshot_archive_source "$archive/round" "$destination" || return 1
  rm -f "$destination/history/archive-receipt.json"
  rm -rf "$destination/history/archive-authority"
}

verify_pending_recovery_archive() {
  local archive=$1
  python3 - "$archive" <<'PY'
import datetime
import pathlib
import stat
import sys

from lib.history_archive import ArchiveError, verify_archive

archive = pathlib.Path(sys.argv[1])
manifest = archive / "manifest.tsv"
try:
    archive_state = archive.lstat()
    round_state = (archive / "round").lstat()
    manifest_state = manifest.lstat()
except OSError as exc:
    raise SystemExit(f"recovery archive metadata is unavailable: {exc}")
if (
    not stat.S_ISDIR(archive_state.st_mode)
    or stat.S_ISLNK(archive_state.st_mode)
    or not stat.S_ISDIR(round_state.st_mode)
    or stat.S_ISLNK(round_state.st_mode)
    or not stat.S_ISREG(manifest_state.st_mode)
    or manifest_state.st_nlink != 1
):
    raise SystemExit("recovery archive paths are unsafe")
fields = {}
for line in manifest.read_text(encoding="utf-8").splitlines():
    parts = line.split("\t")
    if len(parts) != 2 or parts[0] in fields or not parts[1]:
        raise SystemExit("recovery archive manifest is malformed")
    fields[parts[0]] = parts[1]
expected = {
    "run_id", "round", "date", "policy_mode", "reason", "archived_at"
}
if set(fields) != expected:
    raise SystemExit("recovery archive manifest fields are invalid")
try:
    archived_date = datetime.date.fromisoformat(fields["date"])
except ValueError as exc:
    raise SystemExit("recovery archive date is invalid") from exc
if archived_date.isoformat() != fields["date"]:
    raise SystemExit("recovery archive date is invalid")
if fields["reason"] != "decision":
    raise SystemExit(3)
if archive.name != fields["run_id"]:
    raise SystemExit("recovery archive run identity is inconsistent")
try:
    round_number = int(fields["round"])
except ValueError as exc:
    raise SystemExit("recovery archive round is invalid") from exc
if round_number < 1:
    raise SystemExit("recovery archive round is invalid")
try:
    receipt = verify_archive(
        archive / "round",
        run_id=fields["run_id"],
        round_number=round_number,
        policy_mode=fields["policy_mode"],
        reason="decision",
    )
except ArchiveError as exc:
    raise SystemExit(f"recovery archive verification failed: {exc}") from exc
if receipt.get("created_reason") not in {"decision", "published"}:
    raise SystemExit("recovery archive lifecycle is invalid")
outcome_path = archive / "round/history/decision-outcome.tsv"
outcome = "compatibility-unknown"
if outcome_path.exists():
    outcome_state = outcome_path.lstat()
    if not stat.S_ISREG(outcome_state.st_mode) or outcome_state.st_nlink != 1:
        raise SystemExit("recovery decision outcome is unsafe")
    parts = outcome_path.read_text(encoding="ascii").rstrip("\n").split("\t")
    if len(parts) != 2 or not parts[1].isdigit():
        raise SystemExit("recovery decision outcome is invalid")
    count = int(parts[1])
    outcome = parts[0]
    if (
        (outcome == "strong-accept" and count < 1)
        or (outcome == "no-strong-accept" and count != 0)
        or outcome not in {"strong-accept", "no-strong-accept"}
    ):
        raise SystemExit("recovery decision outcome is inconsistent")
print("\t".join((
    fields["run_id"], fields["round"], fields["date"],
    fields["reason"], outcome,
)))
PY
}

find_pending_archive_report_view() {
  local archive view candidate metadata outcome rc saw_report_view
  local candidate_run_id candidate_round candidate_date candidate_reason
  local current_round_run_id
  RECOVERY_ARCHIVE_DIR=
  RECOVERY_RUN_ID=
  RECOVERY_ROUND=
  RECOVERY_DATE=
  RECOVERY_REASON=
  RECOVERY_OUTCOME=
  RECOVERY_REPORT_PATH=
  RECOVERY_CURRENT_DECISION_COMPLETE=0
  CURRENT_REPORT_VIEW=
  current_round_run_id=$(cat "$RD/history/run-id" 2>/dev/null || true)
  for archive in "$RUNS_DIR"/*; do
    [ -e "$archive" ] || continue
    if metadata=$(verify_pending_recovery_archive "$archive" 2>> "$LOG"); then
      :
    else
      rc=$?
      [ "$rc" -eq 3 ] && continue
      log "Invalid pending recovery archive: $archive"
      return 2
    fi
    IFS=$'\t' read -r \
      candidate_run_id candidate_round candidate_date candidate_reason outcome \
      <<< "$metadata"
    if [ "$outcome" = no-strong-accept ]; then
      if [ "$candidate_run_id" = "$current_round_run_id" ]; then
        RECOVERY_CURRENT_DECISION_COMPLETE=1
      fi
      continue
    fi
    candidate=
    saw_report_view=0
    for view in "$archive"/round/history/review-attempts/*/report-view; do
      [ -f "$view/accepted.tsv" ] || continue
      saw_report_view=1
      [ -s "$view/accepted.tsv" ] || continue
      candidate=$view
    done
    if [ -z "$candidate" ]; then
      if [ "$outcome" = compatibility-unknown ] \
         && [ "$saw_report_view" -eq 1 ]; then
        # Pre-marker decision archives with only empty accepted.tsv files are
        # completed no-Strong-Accept decisions, not pending publication.
        if [ "$candidate_run_id" = "$current_round_run_id" ]; then
          RECOVERY_CURRENT_DECISION_COMPLETE=1
        fi
        continue
      fi
      log "Pending Strong Accept archive lacks its report view: $archive"
      return 2
    fi
    RECOVERY_ARCHIVE_DIR=$archive
    RECOVERY_RUN_ID=$candidate_run_id
    RECOVERY_ROUND=$candidate_round
    RECOVERY_DATE=$candidate_date
    RECOVERY_REASON=$candidate_reason
    RECOVERY_OUTCOME=strong-accept
    CURRENT_REPORT_VIEW=$candidate
  done
  [ -n "$RECOVERY_ARCHIVE_DIR" ]
}

refresh_published_archive() {
  local source=$ARCHIVE_SOURCE
  if [ -z "$source" ] && [ -n "$RECOVERY_ARCHIVE_DIR" ]; then
    source="tmp/archive-source.recovery.$$"
    restore_archive_source "$RECOVERY_ARCHIVE_DIR" "$source" || {
      log "Cannot reconstruct the committed archive source"
      return 1
    }
  fi
  if [ -z "$source" ]; then
    log "Published archive refresh has no immutable decision source"
    return 1
  fi
  if ! archive_round published "$source"; then
    log "Published archive refresh failed"
    [ "$source" = "$ARCHIVE_SOURCE" ] || rm -rf "$source"
    return 1
  fi
  [ "$source" = "$ARCHIVE_SOURCE" ] || rm -rf "$source"
}

publish_hunt_for_date() {
  local publication_date=$1 shim rc
  shim=$(mktemp -d "${TMPDIR:-/tmp}/hunt-publish-date.XXXXXX") || return 2
  if ! python3 - "$shim/date" <<'PY'
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
script = b'''#!/usr/bin/env bash
if [ "${1:-}" = "+%F" ]; then
  printf '%s\\n' "$HUNT_PUBLICATION_DATE"
else
  exec /bin/date "$@"
fi
'''
descriptor = os.open(
    path,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o700,
)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(script)
    handle.flush()
    os.fsync(handle.fileno())
PY
  then
    rm -rf "$shim"
    return 2
  fi
  if HUNT_PUBLICATION_DATE="$publication_date" \
     PATH="$shim:$PATH" ./publish.sh >> "$LOG" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  rm -rf "$shim"
  return "$rc"
}

publish_existing_strong_accept_report() {
  publish_hunt_for_date "$today" || {
    log "Publication recovery failed"
    return 2
  }
  refresh_published_archive || return 2
  log "Recovered publication and archive lifecycle for committed Strong Accept"
}

finalize_strong_accept() {
  while :; do
    if [ -n "${RUNS_DIR:-}" ] \
       && { [ -e "$RUNS_DIR/$run_id/report-binding.json" ] \
            || [ -L "$RUNS_DIR/$run_id/report-binding.json" ]; }; then
      if ! RECOVERY_REPORT_PATH=$(verify_pending_report_binding \
        "$RUNS_DIR/$run_id" "$run_id" "$today" 2>> "$LOG"); then
        log "Committed Strong Accept report binding is invalid"
        return 2
      fi
      if publish_hunt_for_date "$today"; then
        refresh_published_archive || return 2
        fails=0
        return 0
      fi
      log "publish.sh failed"
    else
      if run_external_stage \
        "$BACK_CMD" \
        "Read roles/report.md and follow it" \
        report \
        1; then
        if ! RECOVERY_REPORT_PATH=$(verify_pending_report_binding \
          "$RUNS_DIR/$run_id" "$run_id" "$today" 2>> "$LOG"); then
          log "Report stage did not seal an exact run-bound report"
          return 2
        fi
        continue
      fi
      log "Report stage failed after committed Strong Accept"
    fi
    fails=$((fails + 1))
    log "Committed Strong Accept report failure (${fails}/${MAX_FAILS})"
    [ "$fails" -lt "$MAX_FAILS" ] || return 1
    sleep_minutes "$FAIL_SLEEP_MIN"
  done
}

fail_round() {
  local stage=$1
  local error_class=${2:-execution}
  local sleep_for=$FAIL_SLEEP_MIN
  archive_round "failed:$stage" || true
  fails=$((fails + 1))
  log "Round failed at $stage (${fails}/${MAX_FAILS}) class=$error_class"
  [ "$fails" -lt "$MAX_FAILS" ] || return 1
  if [ "$error_class" = contract ]; then
    sleep_for=$CONTRACT_FAIL_SLEEP_MIN
  fi
  if [ "$ROUND_LIMIT" -eq 0 ] || [ "$round" -lt "$ROUND_LIMIT" ]; then
    sleep_minutes "$sleep_for"
  fi
}

# Capture portable-stage stderr class tag: portable-stage: CODE [class] ...
portable_error_class_from_text() {
  local text=$1
  case "$text" in
    *' [contract]'*|*'[contract] '*) printf 'contract\n' ;;
    *) printf 'execution\n' ;;
  esac
}

reject_direction_round() {
  local delay
  if ! archive_round "rejected:direction"; then
    log "Direction rejection archive failed"
    return 1
  fi
  log "Direction gate rejected the candidate batch"
  if [ "$ROUND_LIMIT" -eq 0 ] || [ "$round" -lt "$ROUND_LIMIT" ]; then
    delay=$(random_no_hit_sleep_min)
    sleep_minutes "$delay"
  fi
}

validate_config
mkdir -p tmp "$HISTORY_STATE_ROOT"

acquire_hunt_lock || exit 2
trap '
  case "$ACTIVE_EXTERNAL_MIRROR" in
    "${TMPDIR:-/tmp}"/hunt-*) rm -rf "$ACTIVE_EXTERNAL_MIRROR" ;;
  esac
  [ -z "$ARCHIVE_SOURCE" ] || rm -rf "$ARCHIVE_SOURCE"
  rm -f "$LOCK_STATUS"
  if [ -n "$LOCK_HOLDER_PID" ]; then
    kill "$LOCK_HOLDER_PID" 2>/dev/null || true
    wait "$LOCK_HOLDER_PID" 2>/dev/null || true
  fi
' EXIT

mkdir -p "$RUNS_DIR" || { log "Cannot create archive directory"; exit 2; }
[ ! -e "$HALT_MARK" ] || {
  log "Archive-integrity sentinel exists: $HALT_MARK"
  exit 2
}

# Validate enforcement authority, migrate once, recover projections, and
# reconcile both TSV targets before any agent process can start.
startup_root=tmp/history-startup
rm -rf "$startup_root"
mkdir -p "$startup_root"
direction_snapshot_args=(
  snapshot
  --repo-root "$PWD"
  --identity-output "$startup_root/direction-identity.json"
)
if [ -n "$RESEARCH_DIRECTION_FILE" ]; then
  direction_snapshot_args+=(
    --source "$RESEARCH_DIRECTION_FILE"
    --output "$startup_root/direction-constraint.json"
  )
fi
if ! python3 lib/direction_contract.py \
  "${direction_snapshot_args[@]}"; then
  log "Research direction snapshot failed before history startup"
  exit 2
fi
direction_active=0
if [ -f "$startup_root/direction-constraint.json" ]; then
  direction_active=1
fi
if [ "$HUNT_RUNTIME_ABI" = v2 ] \
   && ! history_audit_init > "$startup_root/audit-init.json"; then
  log "History audit-v2 initialization failed before agent invocation"
  exit 2
fi
if ! history_sync "$startup_root/generation-brief.json" "" \
  > "$startup_root/startup.json"; then
  log "History startup failed before agent invocation"
  exit 2
fi
policy_mode=$(history_policy_mode "$startup_root/startup.json") || exit 2
log "History runtime ready in $policy_mode mode"

today=$(date +%F)
fails=0
round=0
run_id=-
resume_candidate=0
recovered_report=0
while :; do
  if find_pending_archive_report_view; then
    :
  else
    recovery_rc=$?
    [ "$recovery_rc" -eq 1 ] && break
    exit "$recovery_rc"
  fi
  run_id=$RECOVERY_RUN_ID
  round=$RECOVERY_ROUND
  today=$RECOVERY_DATE
  if RECOVERY_REPORT_PATH=$(verify_pending_report_binding \
    "$RECOVERY_ARCHIVE_DIR" "$run_id" "$today" 2>> "$LOG"); then
    log "Recovering publication for pending Strong Accept archive $run_id"
    publish_existing_strong_accept_report || exit $?
  else
    recovery_rc=$?
    if [ "$recovery_rc" -eq 3 ]; then
      log "Recovering missing report for pending Strong Accept archive $run_id"
      finalize_strong_accept || exit $?
    else
      log "Pending Strong Accept report binding is invalid: $run_id"
      exit 2
    fi
  fi
  recovered_report=1
done

today=$(date +%F)
today_sa=$(sa_today)
if [ "$SA_TARGET" -gt 0 ] && [ "$today_sa" -ge "$SA_TARGET" ]; then
  if [ "$recovered_report" -eq 0 ]; then
    publish_hunt_for_date "$today" || {
      log "Publication recovery failed"
      exit 2
    }
  fi
  log "Daily Strong Accept target is already satisfied"
  exit 0
fi
if [ "$recovered_report" -eq 1 ]; then
  round=0
  run_id=-
  RECOVERY_ARCHIVE_DIR=
  RECOVERY_RUN_ID=
  RECOVERY_ROUND=
  RECOVERY_DATE=
  RECOVERY_REASON=
  RECOVERY_OUTCOME=
  RECOVERY_REPORT_PATH=
  CURRENT_REPORT_VIEW=
fi

if [ "$recovered_report" -eq 0 ] \
   && [ "$RECOVERY_CURRENT_DECISION_COMPLETE" -eq 0 ] \
   && [ "$RESUME_FRONT" = 1 ] \
   && [ -s "$RD/history/resume-state.json" ]; then
  if history_receipts_ok "$RD/history/resume-state.json" \
    > "$startup_root/resume-validation.json"; then
    resume_candidate=1
    log "Validated sealed front state for resume"
  else
    log "Discarding stale or incomplete front state"
  fi
fi

while :; do
  if [ "$ROUND_LIMIT" -gt 0 ] && [ "$round" -ge "$ROUND_LIMIT" ]; then
    log "Reached ROUND_LIMIT=$ROUND_LIMIT"
    break
  fi
  round=$((round + 1))
  today=$(date +%F)
  if [ "$resume_candidate" = 1 ]; then
    resume_candidate=0
    prior_run_id=$(cat "$RD/history/run-id" 2>/dev/null || true)
    [ -n "$prior_run_id" ] || {
      log "Sealed resume is missing its prior run id"
      exit 2
    }
    run_id="$(date +%Y%m%dT%H%M%S)-p$$-r${round}"
    while [ "$run_id" = "$prior_run_id" ] || [ -e "$RUNS_DIR/$run_id" ]; do
      run_id="$(date +%Y%m%dT%H%M%S)-p$$-r${round}-$RANDOM"
    done
    printf '%s\n' "$run_id" > "$RD/history/run-id"
    if [ ! -e "$RD/history/direction-identity.json" ] \
       && [ "$direction_active" -eq 0 ]; then
      if ! history_publish_round_direction fresh; then
        log "Legacy undirected round identity publication failed"
        exit 2
      fi
    elif ! history_publish_round_direction resume; then
      log "Resumed round direction snapshot changed"
      exit 2
    fi
    mkdir -p "$RD/history/resume-attempts"
    prior_archive=
    if [ -d "$RUNS_DIR/$prior_run_id/round" ]; then
      prior_archive="$RUNS_DIR/$prior_run_id"
    fi
    if [ -n "$prior_archive" ]; then
      if ! history_seal_resume_attempt \
        "$RD/history/resume-state.json" \
        "$run_id" \
        "$prior_run_id" \
        "$RD/history/resume-attempts/${run_id}.json" \
        "$prior_archive" \
        > "$RD/history/resume-attempts/${run_id}.seal.json"; then
        log "Resume attempt receipt failed"
        exit 2
      fi
    elif ! history_seal_resume_attempt \
      "$RD/history/resume-state.json" \
      "$run_id" \
      "$prior_run_id" \
      "$RD/history/resume-attempts/${run_id}.json" \
      > "$RD/history/resume-attempts/${run_id}.seal.json"; then
      log "Resume attempt receipt failed"
      exit 2
    fi
    if [ -f "$RD/history/startup.json" ]; then
      policy_mode=$(history_policy_mode "$RD/history/startup.json") || exit 2
    else
      policy_mode=$(history_policy_mode "$startup_root/startup.json") || exit 2
    fi
    log "Resuming sealed front artifacts for $run_id (from $prior_run_id)"
  else
    rm -rf "$RD"
    mkdir -p "$RD/history" "$RD/logs"
    : > "$RD/stages.tsv"
    run_id="$(date +%Y%m%dT%H%M%S)-p$$-r${round}"
    printf '%s\n' "$run_id" > "$RD/history/run-id"
    if ! history_publish_round_direction fresh; then
      log "Round direction snapshot publication failed"
      exit 2
    fi
    lens=$(pick_lens)
    if ! history_build_brief "$RD/history/generation-brief.json" "$lens" \
      > "$RD/history/startup.json"; then
      log "Round history preflight failed"
      exit 2
    fi
    policy_mode=$(history_policy_mode "$RD/history/startup.json") || exit 2

    contained_json=
    internal_profile=
    if [ "$HUNT_RUNTIME_ABI" = v2 ]; then
      internal_profile="$RD/history/provider-profiles/base.json"
      if ! hunt_write_base_profile "$internal_profile"; then
        log "Portable base provider profile creation failed"
        exit 2
      fi
    else
      contained_json=${CONTAINED_AGENT_CMD_JSON:-}
      [ -n "$contained_json" ] \
        || contained_json=$(default_contained_command_json) \
        || exit 2
      contained_json=$(normalize_command_json "$contained_json") || exit 2
      internal_profile=$contained_json
    fi

    generation_inputs=(
      --input "generation_brief.json=$RD/history/generation-brief.json"
      --input "generation_policy.md=brainstorming_policy.md"
    )
    if [ "$direction_active" -eq 1 ]; then
      generation_inputs+=(
        --input \
          "direction_constraint.json=$RD/history/direction-constraint.json"
      )
    fi
    if [ -s research_context.md ]; then
      generation_inputs+=(--input "research_context.md=research_context.md")
    fi
    if [ "$HUNT_RUNTIME_ABI" = v2 ]; then
      portable_err=$RD/history/generate-portable.err
      if ! run_portable_generate_stage \
        "$internal_profile" \
        "$RD/history/generate-output" \
        "$RD/history/generate-attempt" \
        "$RD/history/generate-prompt.json" \
        "${generation_inputs[@]}" \
        > "$RD/history/generate-stage.json" \
        2> "$portable_err"
      then
        portable_msg=$(cat "$portable_err" 2>/dev/null || true)
        [ -n "$portable_msg" ] && log "$portable_msg"
        fail_round generate "$(portable_error_class_from_text "$portable_msg")" || exit 1
        continue
      fi
    else
      run_contained_stage \
        generate generate \
        "$RD/history/generate-output" \
        "$RD/history/generate-manifest.json" \
        "$contained_json" \
        "${generation_inputs[@]}" \
        > "$RD/history/generate-stage.json"
      if [ "$?" -ne 0 ]; then
        fail_round generate || exit 1
        continue
      fi
    fi
    if [ ! -s "$RD/history/generate-output/ideas.tsv" ] \
       || [ ! -s "$RD/history/generate-output/ideas.md" ] \
       || ! themes_ok "$RD/history/generate-output/ideas.tsv" \
       || ! axiom_ok \
         "$RD/history/generate-output/ideas.md" \
         "$RD/history/generate-output/ideas.tsv"; then
      fail_round generation-contract contract || exit 1
      continue
    fi

    copy_mutable_round_view \
      "$RD/history/generate-output/ideas.tsv" "$RD/ideas.tsv" || exit 2
    copy_mutable_round_view \
      "$RD/history/generate-output/ideas.md" "$RD/ideas.md" || exit 2
    copy_mutable_round_view \
      "$RD/history/generate-output/ideas.tsv" "$RD/ideas.all.tsv" || exit 2
    copy_mutable_round_view \
      "$RD/history/generate-output/ideas.md" "$RD/ideas.all.md" || exit 2
    round_direction=
    if [ "$direction_active" -eq 1 ]; then
      round_direction="$RD/history/direction-constraint.json"
    fi
    if ! history_freeze_batch \
      "$RD/history/generate-output/ideas.tsv" \
      "$RD/history/generate-output/ideas.md" \
      "$RD/history/batch" \
      "$RD/history/generation-brief.json" \
      "$round_direction" \
      "$startup_root/direction-identity.json" \
      > "$RD/history/freeze-batch.json"; then
      fail_round freeze-batch || exit 1
      continue
    fi
    if [ "$direction_active" -eq 1 ]; then
      : > "$RD/select.tsv"
      if ! run_external_stage \
        "$FRONT_CMD" \
        "Read roles/select.md and follow it" \
        select; then
        reject_direction_round || exit 1
        continue
      fi
      if ! validate_direction_verdicts \
        > "$RD/history/validate-direction.json"; then
        reject_direction_round || exit 1
        continue
      fi
    fi
    if ! history_observe_round \
      "$RD/history/batch/batch.json" \
      "$RD/history/observations" \
      > "$RD/history/observe-round.json"; then
      fail_round history-observation || exit 1
      continue
    fi

    if [ "$direction_active" -eq 0 ]; then
      : > "$RD/select.tsv"
      if ! run_external_stage \
        "$FRONT_CMD" \
        "Read roles/select.md and follow it" \
        select; then
        log "Selector failed; sealed selection will use generation order"
        : > "$RD/select.tsv"
      fi
    fi
    : > "$RD/prescreen.md"
    if ! run_external_stage \
      "$FRONT_CMD" \
      "Read roles/prescreen.md and follow it" \
      prescreen; then
      fail_round prescreen || exit 1
      continue
    fi
    if ! history_seal_selection \
      "$RD/history/batch/batch.json" \
      "$RD/history/observations/round-observation.json" \
      "$RD/history/generation-brief.json" \
      "$RD/select.tsv" \
      "$RD/prescreen.md" \
      "$RD/history/selection.json" \
      > "$RD/history/seal-selection.json"; then
      fail_round selection-contract || exit 1
      continue
    fi
    if [ "$HUNT_RUNTIME_ABI" = v2 ] \
       && ! history_audit_plan_shadow_round \
         "$RD/history/batch/batch.json" \
         "$RD/history/selection.json" \
         "$RD/history/observations" \
         "$RD/history/audit-shadow" \
         "$RD/history/provider-profiles"; then
      fail_round audit-shadow-plan || exit 1
      continue
    fi
    if ! history_materialize_selection \
      "$RD/history/batch/batch.json" \
      "$RD/history/selection.json" \
      "$RD/history/views" \
      > "$RD/history/materialize-selection.json"; then
      fail_round selection-views || exit 1
      continue
    fi
    for view in \
      ideas.all.tsv ideas.all.md kills.tsv keeps.tsv ideas.tsv ideas.md
    do
      copy_mutable_round_view "$RD/history/views/$view" "$RD/$view" \
        || exit 2
    done

    if ! history_compare_targets \
      "$RD/history/batch/batch.json" \
      "$RD/history/observations" \
      "$RD/history/selection.json" \
      "$internal_profile" \
      > "$RD/history/compare-targets.json"; then
      fail_round history-comparison || exit 1
      continue
    fi
    if [ "$policy_mode" = enforcement ]; then
      if ! history_publish_summaries \
        "$RD/history/batch/batch.json" \
        "$RD/history/selection.json" \
        "$RD/history/observations" \
        > "$RD/history/publish-summaries.json"; then
        fail_round history-summary || exit 1
        continue
      fi
    fi
    if ! history_materialize_research \
      "$RD/history/batch/batch.json" \
      "$RD/history/selection.json" \
      "$RD/history/observations/comparison-index.json" \
      "$RD/history/observations" \
      "$RD/history/research-view" \
      > "$RD/history/materialize-research.json"; then
      fail_round research-view || exit 1
      continue
    fi
    copy_mutable_round_view \
      "$RD/history/research-view/ideas.tsv" "$RD/ideas.tsv" || exit 2
    copy_mutable_round_view \
      "$RD/history/research-view/ideas.md" "$RD/ideas.md" || exit 2

    : > "$RD/priorwork.md"
    if [ -s "$RD/ideas.tsv" ]; then
      research_ok=0
      research_try=0
      while [ "$research_try" -le "$RESEARCH_RETRY" ]; do
        : > "$RD/priorwork.md"
        if ! run_external_stage \
          "$FRONT_CMD" \
          "Read roles/research.md and follow it" \
          research "$research_try"; then
          log "Research stage agent/copy failed (try $research_try)"
          research_try=$((research_try + 1))
          continue
        fi
        if ! priorwork_ok; then
          log "Research stage priorwork_ok failed (try $research_try)"
          cp "$RD/priorwork.md" \
            "$RD/logs/priorwork.try${research_try}.diagnostic.md" \
            2>/dev/null || true
          research_try=$((research_try + 1))
          continue
        fi
        if ! cracks_ok; then
          log "Research stage cracks_ok failed (try $research_try)"
          cp "$RD/priorwork.md" \
            "$RD/logs/priorwork.try${research_try}.diagnostic.md" \
            2>/dev/null || true
          research_try=$((research_try + 1))
          continue
        fi
        research_ok=1
        break
      done
    else
      research_ok=1
      log "No history-eligible shortlist target requires external research"
    fi
    if [ "$research_ok" -ne 1 ]; then
      fail_round research || exit 1
      continue
    fi
    if ! history_seal_resume \
      "$RD/history/batch/batch.json" \
      "$RD/history/selection.json" \
      "$RD/history/observations" \
      "$RD/history/observations/comparison-index.json" \
      "$RD/priorwork.md" \
      "$RD/history/resume-state.json" \
      > "$RD/history/seal-resume.json"; then
      fail_round resume-seal || exit 1
      continue
    fi
  fi

  reviewer_args=()
  if [ "$HUNT_RUNTIME_ABI" = v2 ]; then
    reviewer_args+=(--executor portable-v2)
    for seat in $(seq 1 "$REVIEWERS"); do
      reviewer_profile="$RD/history/provider-profiles/review-${seat}.json"
      if ! hunt_write_review_profile "$seat" "$reviewer_profile"; then
        log "Portable review provider profile creation failed for seat $seat"
        exit 2
      fi
      reviewer_args+=(
        --reviewer-request-profile "${seat}=${reviewer_profile}"
      )
    done
  else
    contained_json=${CONTAINED_AGENT_CMD_JSON:-}
    [ -n "$contained_json" ] \
      || contained_json=$(default_contained_command_json) \
      || exit 2
    contained_json=$(normalize_command_json "$contained_json") || exit 2
    for seat in $(seq 1 "$REVIEWERS"); do
      variable="CONTAINED_REV_CMD_${seat}_JSON"
      reviewer_json=${!variable:-$contained_json}
      reviewer_json=$(normalize_command_json "$reviewer_json") || exit 2
      reviewer_args+=(--reviewer-command "${seat}=${reviewer_json}")
    done
  fi

  review_attempt_number=1
  while :; do
    review_attempt=$(printf \
      '%s/history/review-attempts/%03d' \
      "$RD" "$review_attempt_number")
    [ -e "$review_attempt" ] || break
    review_attempt_number=$((review_attempt_number + 1))
  done
  mkdir -p "$review_attempt"
  review_prior_work=$RD/priorwork.md
  if [ -s "$RD/history/resume-state.json" ]; then
    sealed_prior=$(python3 - "$RD/history/resume-state.json" <<'PY'
import json
import pathlib
import sys

resume = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
prior = resume.get("prior_work") if isinstance(resume, dict) else None
path = prior.get("path") if isinstance(prior, dict) else None
if not isinstance(path, str) or not path:
    raise SystemExit("resume state is missing sealed prior work")
print(path)
PY
) || {
      fail_round resume-prior-work || exit 1
      continue
    }
    review_prior_work=$sealed_prior
  fi
  if ! history_seal_review_plan \
    "$RD/history/batch/batch.json" \
    "$RD/history/selection.json" \
    "$RD/history/observations" \
    "$RD/history/observations/comparison-index.json" \
    "$review_prior_work" \
    "$review_attempt/review-plan.json" \
    "${reviewer_args[@]}" \
    > "$review_attempt/seal-review-plan.json"; then
    fail_round review-plan || exit 1
    continue
  fi
  if ! history_run_review_matrix \
    "$RD/history/batch/batch.json" \
    "$review_attempt/review-plan.json" \
    "$review_attempt/review-stages" \
    "$review_attempt/review-index.json" \
    "${reviewer_args[@]}" \
    > "$review_attempt/run-review-matrix.json"; then
    fail_round review || exit 1
    continue
  fi
  if ! history_build_aggregation \
    "$RD/history/batch/batch.json" \
    "$review_attempt/review-plan.json" \
    "$review_attempt/review-index.json" \
    "$review_attempt/aggregation.json" \
    > "$review_attempt/build-aggregation.json"; then
    fail_round aggregation || exit 1
    continue
  fi
  CURRENT_REPORT_VIEW="$review_attempt/report-view"
  if ! history_materialize_report \
    "$RD/history/batch/batch.json" \
    "$RD/history/research-view/research-view.json" \
    "$review_attempt/review-plan.json" \
    "$review_attempt/review-index.json" \
    "$review_attempt/aggregation.json" \
    "$round" \
    "$CURRENT_REPORT_VIEW" \
    > "$review_attempt/materialize-report.json"; then
    fail_round report-view || exit 1
    continue
  fi
  sa_count=$(awk 'END { print NR + 0 }' \
    "$CURRENT_REPORT_VIEW/accepted.tsv")
  if ! history_commit_round \
    "$RD/history/batch/batch.json" \
    "$RD/history/selection.json" \
    "$RD/history/observations/comparison-index.json" \
    "$review_attempt/review-plan.json" \
    "$review_attempt/review-index.json" \
    "$review_attempt/aggregation.json" \
    > "$review_attempt/commit-round.json"; then
    fail_round commit || exit 1
    continue
  fi
  if ! history_materialize_ledger \
    > "$RD/history/materialize-ledger.json"; then
    log "Canonical commit succeeded but ledger projection remains pending"
    exit 2
  fi
  fails=0
  if ! seal_decision_outcome "$sa_count"; then
    log "Cannot seal explicit decision outcome"
    exit 2
  fi
  if [ "$sa_count" -gt 0 ]; then
    ARCHIVE_SOURCE="tmp/archive-source.${run_id}.$$"
    if ! snapshot_archive_source "$RD" "$ARCHIVE_SOURCE"; then
      log "Cannot seal immutable Strong Accept archive source"
      exit 2
    fi
    reports_today > "$ARCHIVE_SOURCE/history/report-count-before" || {
      log "Cannot seal pre-report publication count"
      exit 2
    }
  fi

  if ! archive_round decision "${ARCHIVE_SOURCE:-$RD}"; then
    if [ "$sa_count" -gt 0 ]; then
      printf '%s\trun_id=%s\tsa_count=%s\n' \
        "$(date '+%F %T')" "$run_id" "$sa_count" > "$HALT_MARK"
      log "Strong Accept decision archive failed; publication is blocked"
      exit 2
    fi
    log "Round archive failed"
  fi

  if [ "$sa_count" -gt 0 ]; then
    finalize_strong_accept || exit $?
    rm -rf "$ARCHIVE_SOURCE"
    ARCHIVE_SOURCE=
    if [ "$SA_TARGET" -gt 0 ] && [ "$(sa_today)" -ge "$SA_TARGET" ]; then
      log "Daily Strong Accept target reached"
      break
    fi
  fi

  if [ "$ROUND_LIMIT" -gt 0 ] && [ "$round" -ge "$ROUND_LIMIT" ]; then
    log "Reached ROUND_LIMIT=$ROUND_LIMIT"
    break
  fi
  delay=$(random_no_hit_sleep_min)
  log "Round complete without reaching the daily target"
  sleep_minutes "$delay"
done
