#!/usr/bin/env bash
# Run the bounded idea-hunt protocol.
#
# SQLite is the only history authority.  ledger.tsv and tmp/ledger.good are
# replayable projections reconciled by the history runtime.  Generation,
# internal-history comparison, and every review seat run through the contained
# stage ABI; selector, prescreen, external prior-work research, report assembly,
# and publication retain their existing process boundaries.
#
# Usage:
#   ./hunt.sh [failure retry delay in minutes; default: 150]
#
# Main controls:
#   AGENT_CMD / FRONT_CMD / BACK_CMD
#       Legacy external command strings for selector, prescreen, research, and
#       report.  They are parsed as argv without eval or a shell.
#   CONTAINED_AGENT_CMD_JSON
#       Canonical absolute JSON argv for generation and comparison.  The
#       default is the registered Codex xhigh prefix.
#   CONTAINED_REV_CMD_<N>_JSON
#       Optional canonical absolute JSON argv override for review seat N.
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
set -u

cd "$(dirname "$0")" || exit 2
git config core.hooksPath .githooks

AGENT_CMD=${AGENT_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write}
FRONT_CMD=${FRONT_CMD:-$AGENT_CMD}
BACK_CMD=${BACK_CMD:-$AGENT_CMD}
FAIL_SLEEP_MIN=${FAIL_SLEEP_MIN:-${1:-150}}
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

LOG=hunt.log
RD=tmp/round
LOCK=tmp/hunt.lock
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

is_uint() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

validate_config() {
  local name value
  for name in \
    FAIL_SLEEP_MIN NO_HIT_SLEEP_MIN_LO NO_HIT_SLEEP_MIN_HI MAX_FAILS \
    REVIEWERS MIN_READ SA_TARGET ROUND_LIMIT EMPTY_MAX PRIOR_MIN_LINKS \
    PRIOR_MIN_API RESEARCH_RETRY SHORT_MAX THEME_MIN_LOW AXIOM_MIN_CRACKS
  do
    value=${!name}
    is_uint "$value" || {
      log "$name must be a nonnegative integer: $value"
      exit 2
    }
  done
  [ "$MAX_FAILS" -ge 1 ] || { log "MAX_FAILS must be at least 1"; exit 2; }
  [ "$REVIEWERS" -ge 1 ] || { log "REVIEWERS must be at least 1"; exit 2; }
  [ "$EMPTY_MAX" -ge 1 ] || { log "EMPTY_MAX must be at least 1"; exit 2; }
  [ "$SHORT_MAX" -ge 1 ] || { log "SHORT_MAX must be at least 1"; exit 2; }
  [ "$AXIOM_MIN_CRACKS" -ge 1 ] || {
    log "AXIOM_MIN_CRACKS must be at least 1"
    exit 2
  }
  case "$ALLOW_ZERO_NO_HIT_SLEEP" in
    0|1) ;;
    *) log "ALLOW_ZERO_NO_HIT_SLEEP must be 0 or 1"; exit 2 ;;
  esac
  case "$RESUME_FRONT" in
    0|1) ;;
    *) log "RESUME_FRONT must be 0 or 1"; exit 2 ;;
  esac
  if [ "$NO_HIT_SLEEP_MIN_LO" -gt "$NO_HIT_SLEEP_MIN_HI" ]; then
    log "NO_HIT_SLEEP_MIN_LO cannot exceed NO_HIT_SLEEP_MIN_HI"
    exit 2
  fi
  if [ "$ALLOW_ZERO_NO_HIT_SLEEP" != 1 ] \
     && { [ "$NO_HIT_SLEEP_MIN_LO" -lt 1 ] \
       || [ "$NO_HIT_SLEEP_MIN_HI" -lt 1 ]; }; then
    log "No-hit sleeps must be positive outside explicit test configuration"
    exit 2
  fi
  case "$RUNS_DIR" in
    ''|/) log "RUNS_DIR must name a bounded directory"; exit 2 ;;
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

history_freeze_batch() {
  python3 lib/history_runtime.py freeze-batch \
    --tsv "$1" \
    --markdown "$2" \
    --output-root "$3" \
    --brief "$4"
}

history_observe_round() {
  history_runtime_authorized observe-round \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$1" \
    --artifact-root "$2"
}

history_seal_selection() {
  python3 lib/history_runtime.py seal-selection \
    --batch "$1" \
    --round-observation "$2" \
    --brief "$3" \
    --selector "$4" \
    --prescreen "$5" \
    --short-max "$SHORT_MAX" \
    --theme-min-low "$THEME_MIN_LOW" \
    --output "$6"
}

history_materialize_selection() {
  python3 lib/history_runtime.py materialize-selection \
    --batch "$1" \
    --selection "$2" \
    --output-root "$3"
}

history_compare_shortlist() {
  history_runtime_authorized compare-targets \
    --db "$HISTORY_DB" \
    --policy "$HISTORY_POLICY" \
    --batch "$1" \
    --artifact-root "$2" \
    --selection "$3" \
    --command "$4"
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
    --resume "$1"
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
      cp "$RD/ideas.md" "$mirror/tmp/round/ideas.md"
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
  local stage=$1 mirror=$2
  python3 - "$stage" "$mirror" "$RD" "$today" <<'PY'
import os
import pathlib
import stat
import sys

stage = sys.argv[1]
mirror = pathlib.Path(sys.argv[2]).resolve()
round_root = pathlib.Path(sys.argv[3]).resolve()
today = sys.argv[4]

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
        raise SystemExit(f"{stage} output is not a bounded regular file")
    raw = path.read_bytes()
    if len(raw) != status.st_size or b"\0" in raw or b"\r" in raw:
        raise SystemExit(f"{stage} output contains invalid bytes")
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
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

if stage in outputs:
    relative, destination, maximum, required = outputs[stage]
    raw = read_regular(mirror / relative, maximum, required)
    atomic_write(destination, raw)
elif stage == "report":
    candidates = sorted((mirror / "ideas").glob("*.md"))
    if len(candidates) != 1:
        raise SystemExit("report must create exactly one markdown artifact")
    raw = read_regular(candidates[0], 1024 * 1024, True)
    destination = pathlib.Path("ideas") / f"{today}_hunt.md"
    suffix = 2
    while destination.exists():
        destination = pathlib.Path("ideas") / f"{today}_hunt-{suffix}.md"
        suffix += 1
    atomic_write(destination, raw)
    print(destination)
else:
    raise SystemExit("unknown external output stage")
PY
}

run_external_stage() {
  local command_string=$1 prompt=$2 stage=$3
  local -a argv=()
  local item rc started ended mirror
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
    *) log "External mirror path is outside its bound"; return 2 ;;
  esac
  if ! prepare_external_mirror "$stage" "$mirror"; then
    rm -rf "$mirror"
    return 2
  fi
  mkdir -p "$RD/logs"
  input_manifest="$RD/logs/$stage.input-manifest.json"
  if ! external_input_manifest seal "$mirror" "$input_manifest"; then
    rm -rf "$mirror"
    return 2
  fi
  started=$(date '+%F %T')
  log "Starting external stage [$stage] in a disposable mirror"
  if (
    cd "$mirror" \
      && PWD="$mirror" OLDPWD="$mirror" "${argv[@]}" "$prompt"
  ) > "$RD/logs/$stage.log" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  cat "$RD/logs/$stage.log" >> "$LOG"
  if [ "$rc" -eq 0 ]; then
    if ! external_input_manifest verify "$mirror" "$input_manifest" \
      >> "$RD/logs/$stage.log" 2>&1 \
      || ! copy_external_output "$stage" "$mirror" \
      >> "$RD/logs/$stage.log" 2>&1; then
      rc=2
    fi
  fi
  rm -rf "$mirror"
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

themes_ok() {
  local tsv=${1:-$RD/ideas.tsv} id story theme
  while IFS=$'\t' read -r id story theme; do
    [ -n "$id" ] || continue
    grep -F -- "$theme" brainstorming_policy.md >/dev/null || {
      log "Theme gate: $id uses an unknown theme: $theme"
      return 1
    }
  done < "$tsv"
}

axiom_ok() {
  local markdown=$1 tsv=$2 id story theme block field value urls
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
    [ "$urls" -ge "$AXIOM_MIN_CRACKS" ] || {
      log "Assumption-removal gate: $id has too few Crack Evidence rows"
      return 1
    }
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
  local id story theme block count
  while IFS=$'\t' read -r id story theme; do
    [ -n "$id" ] || continue
    is_axiom_idea "$id" "$RD/ideas.md" || continue
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
  local reason=$1 destination
  local -a archive_args
  [ -n "${run_id:-}" ] && [ "$run_id" != "-" ] || return 0
  case "$run_id" in
    *[!A-Za-z0-9._-]*) log "Invalid run id: $run_id"; return 1 ;;
  esac
  destination="$RUNS_DIR/$run_id"
  archive_args=(
    --source-root "$RD"
    --destination "$destination"
    --run-id "$run_id"
    --round "$round"
    --date "$today"
    --policy-mode "${policy_mode:--}"
    --reason "$reason"
    --policy "$HISTORY_POLICY"
    --startup "$RD/history/startup.json"
    --state-root "$HISTORY_STATE_ROOT"
  )
  if [ -f "$RD/history/materialize-ledger.json" ]; then
    archive_args+=(
      --projection "$RD/history/materialize-ledger.json"
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

fail_round() {
  local stage=$1
  archive_round "failed:$stage" || true
  fails=$((fails + 1))
  log "Round failed at $stage (${fails}/${MAX_FAILS})"
  [ "$fails" -lt "$MAX_FAILS" ] || return 1
  sleep_minutes "$FAIL_SLEEP_MIN"
}

validate_config
mkdir -p tmp "$HISTORY_STATE_ROOT"

if ! mkdir "$LOCK" 2>/dev/null; then
  other=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
    log "Another hunt.sh instance is running (pid $other)"
    exit 2
  fi
  case "$LOCK" in
    tmp/hunt.lock) rm -rf "$LOCK" ;;
    *) log "Refusing to remove unexpected lock path: $LOCK"; exit 2 ;;
  esac
  mkdir "$LOCK" 2>/dev/null || { log "Cannot acquire hunt lock"; exit 2; }
fi
printf '%s\n' "$$" > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

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
if ! history_sync "$startup_root/generation-brief.json" "" \
  > "$startup_root/startup.json"; then
  log "History startup failed before agent invocation"
  exit 2
fi
policy_mode=$(history_policy_mode "$startup_root/startup.json") || exit 2
log "History runtime ready in $policy_mode mode"

today=$(date +%F)
if [ "$SA_TARGET" -gt 0 ] && [ "$(sa_today)" -ge "$SA_TARGET" ]; then
  if [ "$(reports_today)" -gt 0 ]; then
    ./publish.sh >> "$LOG" 2>&1 || {
      log "Publication recovery failed"
      exit 2
    }
  fi
  log "Daily Strong Accept target is already satisfied"
  exit 0
fi

fails=0
round=0
run_id=-
resume_candidate=0
if [ "$RESUME_FRONT" = 1 ] && [ -s "$RD/history/resume-state.json" ]; then
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
    lens=$(pick_lens)
    if ! history_build_brief "$RD/history/generation-brief.json" "$lens" \
      > "$RD/history/startup.json"; then
      log "Round history preflight failed"
      exit 2
    fi
    policy_mode=$(history_policy_mode "$RD/history/startup.json") || exit 2

    contained_json=${CONTAINED_AGENT_CMD_JSON:-}
    [ -n "$contained_json" ] \
      || contained_json=$(default_contained_command_json) \
      || exit 2
    contained_json=$(normalize_command_json "$contained_json") || exit 2

    generation_inputs=(
      --input "generation_brief.json=$RD/history/generation-brief.json"
      --input "generation_policy.md=brainstorming_policy.md"
    )
    if [ -s research_context.md ]; then
      generation_inputs+=(--input "research_context.md=research_context.md")
    fi
    if ! run_contained_stage \
      generate generate \
      "$RD/history/generate-output" \
      "$RD/history/generate-manifest.json" \
      "$contained_json" \
      "${generation_inputs[@]}" \
      > "$RD/history/generate-stage.json"; then
      fail_round generate || exit 1
      continue
    fi
    if [ ! -s "$RD/history/generate-output/ideas.tsv" ] \
       || [ ! -s "$RD/history/generate-output/ideas.md" ] \
       || ! themes_ok "$RD/history/generate-output/ideas.tsv" \
       || ! axiom_ok \
         "$RD/history/generate-output/ideas.md" \
         "$RD/history/generate-output/ideas.tsv"; then
      fail_round generation-contract || exit 1
      continue
    fi

    cp "$RD/history/generate-output/ideas.tsv" "$RD/ideas.tsv"
    cp "$RD/history/generate-output/ideas.md" "$RD/ideas.md"
    cp "$RD/history/generate-output/ideas.tsv" "$RD/ideas.all.tsv"
    cp "$RD/history/generate-output/ideas.md" "$RD/ideas.all.md"
    if ! history_freeze_batch \
      "$RD/history/generate-output/ideas.tsv" \
      "$RD/history/generate-output/ideas.md" \
      "$RD/history/batch" \
      "$RD/history/generation-brief.json" \
      > "$RD/history/freeze-batch.json"; then
      fail_round freeze-batch || exit 1
      continue
    fi
    if ! history_observe_round \
      "$RD/history/batch/batch.json" \
      "$RD/history/observations" \
      > "$RD/history/observe-round.json"; then
      fail_round history-observation || exit 1
      continue
    fi

    : > "$RD/select.tsv"
    if ! run_external_stage \
      "$FRONT_CMD" \
      "Read roles/select.md and follow it" \
      select; then
      log "Selector failed; sealed selection will use generation order"
      : > "$RD/select.tsv"
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
      cp "$RD/history/views/$view" "$RD/$view"
    done

    if ! history_compare_targets \
      "$RD/history/batch/batch.json" \
      "$RD/history/observations" \
      "$RD/history/selection.json" \
      "$contained_json" \
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
    cp "$RD/history/research-view/ideas.tsv" "$RD/ideas.tsv"
    cp "$RD/history/research-view/ideas.md" "$RD/ideas.md"

    : > "$RD/priorwork.md"
    if [ -s "$RD/ideas.tsv" ]; then
      research_ok=0
      research_try=0
      while [ "$research_try" -le "$RESEARCH_RETRY" ]; do
        : > "$RD/priorwork.md"
        if run_external_stage \
          "$FRONT_CMD" \
          "Read roles/research.md and follow it" \
          research \
          && priorwork_ok \
          && cracks_ok; then
          research_ok=1
          break
        fi
        research_try=$((research_try + 1))
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

  contained_json=${CONTAINED_AGENT_CMD_JSON:-}
  [ -n "$contained_json" ] \
    || contained_json=$(default_contained_command_json) \
    || exit 2
  contained_json=$(normalize_command_json "$contained_json") || exit 2
  reviewer_args=()
  for seat in $(seq 1 "$REVIEWERS"); do
    variable="CONTAINED_REV_CMD_${seat}_JSON"
    reviewer_json=${!variable:-$contained_json}
    reviewer_json=$(normalize_command_json "$reviewer_json") || exit 2
    reviewer_args+=(--reviewer-command "${seat}=${reviewer_json}")
  done

  review_attempt_number=1
  while :; do
    review_attempt=$(printf \
      '%s/history/review-attempts/%03d' \
      "$RD" "$review_attempt_number")
    [ -e "$review_attempt" ] || break
    review_attempt_number=$((review_attempt_number + 1))
  done
  mkdir -p "$review_attempt"
  if ! history_seal_review_plan \
    "$RD/history/batch/batch.json" \
    "$RD/history/selection.json" \
    "$RD/history/observations" \
    "$RD/history/observations/comparison-index.json" \
    "$RD/priorwork.md" \
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

  if ! archive_round decision; then
    if [ "$sa_count" -gt 0 ]; then
      printf '%s\trun_id=%s\tsa_count=%s\n' \
        "$(date '+%F %T')" "$run_id" "$sa_count" > "$HALT_MARK"
      log "Strong Accept decision archive failed; publication is blocked"
      exit 2
    fi
    log "Round archive failed"
  fi

  if [ "$sa_count" -gt 0 ]; then
    before_reports=$(reports_today)
    if ! run_external_stage \
      "$BACK_CMD" \
      "Read roles/report.md and follow it" \
      report \
      1; then
      fail_round report || exit 1
      continue
    fi
    if [ "$(reports_today)" -le "$before_reports" ]; then
      log "Report stage created no report"
      exit 2
    fi
    ./publish.sh >> "$LOG" 2>&1 || {
      log "publish.sh failed"
      exit 2
    }
    archive_round published || true
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
