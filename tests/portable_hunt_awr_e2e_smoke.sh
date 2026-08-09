#!/usr/bin/env bash
# Offline RED contract for the wired HISTORY_RUNTIME_ABI=v2 product paths.
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TEMP_BASE=${TMPDIR:-/tmp}
TEMP_BASE=${TEMP_BASE%/}
CASE_ROOT=$(mktemp -d "$TEMP_BASE/ai-ideas-portable-e2e.XXXXXX")
FAILURES=0

cleanup() {
  case "$CASE_ROOT" in
    "$TEMP_BASE"/ai-ideas-portable-e2e.*) rm -rf -- "$CASE_ROOT" ;;
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
        status = process.wait(timeout=30)
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

write_awr_ledger() {
  local path=$1
  printf '%s\n' \
    $'date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory' \
    $'2026-07-23\thunt\tWorld Models - Architecture\tConfidence-gated latent updates.\taccept-w-rev\tVerify prior work and repair the experiment.\tlow\tnear-sa' \
    > "$path"
}

check_hunt_result() {
  local repo=$1 provider_log=$2 runs=$3 cli_log=$4
  python3 - "$repo" "$provider_log" "$runs" "$cli_log" <<'PY'
import hashlib
import json
import pathlib
import sqlite3
import sys

repo = pathlib.Path(sys.argv[1])
provider_log = pathlib.Path(sys.argv[2])
runs = pathlib.Path(sys.argv[3])
cli_log = pathlib.Path(sys.argv[4])
records = [
    json.loads(line)
    for line in provider_log.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
stages = [(item["stage"], item["provider"]) for item in records]
if stages.count(("generate", "claude")) != 1:
    raise SystemExit(f"generate provider mismatch: {stages}")
if not any(stage == "history-compare" for stage, _ in stages):
    raise SystemExit(f"history comparator was not portable: {stages}")
if any(provider != "claude" for stage, provider in stages if stage == "history-compare"):
    raise SystemExit(f"history comparator provider leaked: {stages}")
if stages.count(("review", "claude")) != 1:
    raise SystemExit(f"review seat provider mismatch: {stages}")
if any(stage not in {"generate", "history-compare", "review"} for stage, _ in stages):
    raise SystemExit(f"external stage entered portable path: {stages}")

calls = [
    line.split("\t", 1)[0]
    for line in (repo / "tmp/round/stages.tsv").read_text(
        encoding="utf-8"
    ).splitlines()
    if line
]
if calls != ["select", "prescreen", "research", "report"]:
    raise SystemExit(f"legacy external stage sequence changed: {calls}")

cli_calls = [
    line
    for line in cli_log.read_text(encoding="utf-8").splitlines()
    if line
]
required_cli = {"init", "plan"}
missing_cli = sorted(required_cli - set(cli_calls))
if missing_cli:
    raise SystemExit(
        f"Hunt v2 bypassed audit-v2 CLI commands {missing_cli}: {cli_calls}"
    )
if cli_calls.index("init") > cli_calls.index("plan"):
    raise SystemExit(f"audit-v2 plan preceded init: {cli_calls}")
if any(command in cli_calls for command in ("run", "resume", "evaluate")):
    raise SystemExit(
        f"unbudgetable fresh Hunt started hard/offline audit work: {cli_calls}"
    )

database = repo / ".ai-ideas/history.sqlite3"
with sqlite3.connect(database) as connection:
    task_count = connection.execute(
        "SELECT count(*) FROM audit_logical_tasks"
    ).fetchone()[0]
    attempt_count = connection.execute(
        "SELECT count(*) FROM audit_task_attempts"
    ).fetchone()[0]
    release_count = connection.execute(
        "SELECT count(*) FROM audit_semantic_release_authorizations_v2"
    ).fetchone()[0]
if task_count != 0:
    raise SystemExit("unbudgetable fresh Hunt created hard audit tasks")
if attempt_count != 0:
    raise SystemExit("unbudgetable fresh Hunt started hard audit attempts")
if release_count != 0:
    raise SystemExit("shadow/unqualified no-match gained production release authority")

archives = [path for path in runs.iterdir() if (path / "round").is_dir()]
if len(archives) != 1:
    raise SystemExit(f"expected one archived round, got {archives}")
round_root = archives[0] / "round"
batch = json.loads(
    (round_root / "history/batch/batch.json").read_text(
        encoding="utf-8"
    )
)
direction = batch.get("direction")
if (
    batch.get("schema_version") != 2
    or not isinstance(direction, dict)
    or direction.get("direction_id") != "dynamic-spatial-memory-vla-v1"
    or not isinstance(direction.get("sha256"), str)
):
    raise SystemExit(f"portable Hunt lost the hard direction identity: {batch}")
shadow_plans = []
for path in round_root.rglob("*.json"):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        continue
    if isinstance(value, dict) and value.get("schema_version") == (
        "history-audit-shadow-plan-v1"
    ):
        shadow_plans.append((path, value))
if len(shadow_plans) != 1:
    raise SystemExit(f"fresh Hunt shadow plan coverage changed: {shadow_plans}")
_, shadow_plan = shadow_plans[0]
if shadow_plan.get("status") != "producer_unavailable" or shadow_plan.get(
    "reason_code"
) != "unbudgetable_provider":
    raise SystemExit("fresh Hunt omitted explicit unbudgetable producer status")
if shadow_plan.get("observation_scope") != "l1_shadow" or not isinstance(
    shadow_plan.get("l1_observation_sha256"), str
):
    raise SystemExit("fresh Hunt omitted sealed L1 shadow observation")
if (
    shadow_plan.get("batch_sha256") != batch.get("batch_sha256")
    or shadow_plan.get("direction") != direction
):
    raise SystemExit("fresh Hunt shadow plan is replayable across direction batches")
if (
    shadow_plan.get("hard_complete_work_created") is not False
    or shadow_plan.get("production_no_match_authorized") is not False
    or shadow_plan.get("authority") != "shadow-only"
):
    raise SystemExit("fresh Hunt shadow plan gained hard authority")
profiles = shadow_plan.get("execution_request_profiles")
profile_fields = {
    "surface",
    "provider",
    "requested_model",
    "requested_reasoning",
    "effective_model",
    "effective_reasoning",
    "default_probe_revision",
    "model_catalog_probe_revision",
    "model_catalog_sha256",
    "max_output_tokens",
    "output_token_cap_binding",
    "output_token_cap_semantics",
    "execution_request_profile_hash",
}
if (
    not isinstance(profiles, list)
    or len(profiles) != 2
    or any(not isinstance(item, dict) or set(item) != profile_fields for item in profiles)
    or [item["provider"] for item in profiles] != ["claude", "claude"]
    or any(item["surface"] != "hunt" for item in profiles)
):
    raise SystemExit(f"fresh Hunt did not bind base/review profiles: {profiles}")
shadow_material = dict(shadow_plan)
shadow_sha = shadow_material.pop("plan_sha256", None)
shadow_raw = (
    json.dumps(
        shadow_material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
).encode("utf-8")
if shadow_sha != hashlib.sha256(
    b"history-audit-shadow-plan-v1\0" + shadow_raw
).hexdigest():
    raise SystemExit("fresh Hunt shadow plan hash changed")
comparison = json.loads(
    (round_root / "history/observations/comparison-index.json").read_text(
        encoding="utf-8"
    )
)
if set(comparison) != {
    "schema_version",
    "execution_boundary",
    "targets",
    "comparison_index_sha256",
}:
    raise SystemExit(f"comparison v2 schema is not closed: {comparison.keys()}")
if comparison.get("execution_boundary") != "portable-mirror-v1":
    raise SystemExit("comparison index omitted portable execution boundary")
if comparison.get("schema_version") != 2:
    raise SystemExit("comparison index did not version the v2 closed schema")
comparison_material = dict(comparison)
comparison_sha = comparison_material.pop("comparison_index_sha256", None)
comparison_raw = (
    json.dumps(
        comparison_material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
).encode("utf-8")
if comparison_sha != hashlib.sha256(
    b"history-runtime-comparison-index-v2\0" + comparison_raw
).hexdigest():
    raise SystemExit("comparison v2 index used the wrong hash domain")
if not comparison["targets"]:
    raise SystemExit("comparison v2 index has no targets")
public_stage_fields = {
    "schema_version",
    "execution_boundary",
    "stage",
    "seat_id",
    "provider",
    "provider_validation",
    "authority",
    "execution_request_profile_hash",
    "max_output_tokens",
    "output_token_cap_binding",
    "output_token_cap_semantics",
    "serialized_prompt_sha256",
    "role_sha256",
    "input_sha256s",
    "provider_request_sha256",
    "provider_request_binding_sha256",
    "response_schema_sha256",
    "preflight",
    "completion",
    "outputs",
}


def assert_public_stage(stage_record, label):
    if set(stage_record) != public_stage_fields:
        raise SystemExit(f"{label} public stage schema changed")
    if stage_record.get("schema_version") != "portable-stage-public-v1":
        raise SystemExit(f"{label} public stage version changed")
    if stage_record.get("execution_boundary") != "portable-mirror-v1":
        raise SystemExit(f"{label} stage masqueraded as contained-v1")
    encoded = json.dumps(stage_record, sort_keys=True)
    for forbidden in (
        '"prepared"',
        '"executable_path"',
        '"serialized_prompt"',
        '"provider_request"',
        str(round_root),
    ):
        if forbidden in encoded:
            raise SystemExit(f"{label} public stage leaked private state")
    relative_paths = [
        stage_record["preflight"]["path"],
        stage_record["completion"]["path"],
        stage_record["completion"]["model_envelope_path"],
        *[item["path"] for item in stage_record["outputs"].values()],
    ]
    if any(path.startswith("/") or ".." in path.split("/") for path in relative_paths):
        raise SystemExit(f"{label} public stage path escaped its index root")


for target in comparison["targets"]:
    if set(target) != {
        "candidate_id",
        "observation_path",
        "observation_sha256",
        "statuses",
        "portable_stages",
    }:
        raise SystemExit(f"comparison v2 target schema changed: {target.keys()}")
    if not target["portable_stages"]:
        raise SystemExit("comparison v2 target omitted portable stage records")
    for stage_record in target["portable_stages"]:
        assert_public_stage(stage_record, "comparison")
def has_profile_hash(value):
    if isinstance(value, dict):
        if isinstance(value.get("execution_request_profile_hash"), str):
            return True
        return any(has_profile_hash(item) for item in value.values())
    if isinstance(value, list):
        return any(has_profile_hash(item) for item in value)
    return False
if not has_profile_hash(comparison):
    raise SystemExit("comparison index omitted capability profile binding")
review_indices = list(round_root.glob("history/review-attempts/*/review-index.json"))
if len(review_indices) != 1:
    raise SystemExit("review index coverage is not one bounded attempt")
review = json.loads(review_indices[0].read_text(encoding="utf-8"))
if set(review) != {
    "schema_version",
    "execution_boundary",
    "review_plan_sha256",
    "entries",
    "review_index_sha256",
}:
    raise SystemExit(f"review v2 schema is not closed: {review.keys()}")
if review.get("execution_boundary") != "portable-mirror-v1":
    raise SystemExit("review index omitted portable execution boundary")
if review.get("schema_version") != 2:
    raise SystemExit("review index did not version the v2 closed schema")
review_material = dict(review)
review_sha = review_material.pop("review_index_sha256", None)
review_raw = (
    json.dumps(
        review_material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
).encode("utf-8")
if review_sha != hashlib.sha256(
    b"history-runtime-review-index-v2\0" + review_raw
).hexdigest():
    raise SystemExit("review v2 index reused the contained-v1 hash domain")
if not review["entries"]:
    raise SystemExit("review v2 index has no portable entries")
for entry in review["entries"]:
    if set(entry) != {"candidate_id", "seat_id", "stage"}:
        raise SystemExit(f"review v2 entry schema changed: {entry.keys()}")
    assert_public_stage(entry["stage"], "review")
if not has_profile_hash(review):
    raise SystemExit("review index omitted capability profile binding")
completions = list(round_root.rglob("completion.json"))
if len(completions) < 4:
    raise SystemExit(f"portable stage receipt coverage is incomplete: {completions}")
for path in completions:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("execution_boundary") != "portable-mirror-v1":
        raise SystemExit(f"completion boundary changed: {path}")
PY
}

run_hunt_v2() {
  local repo log provider_log cli_log runs home status
  repo=$(make_repo hunt-v2) || { fail 'Hunt fixture setup'; return; }
  install_fake_providers "$repo"
  instrument_audit_cli "$repo"
  install_noop_publish "$repo"
  write_hunt_ledger "$repo/ledger.tsv"
  mkdir -p "$repo/tmp"
  : > "$repo/tmp/near-sa-queue.tsv"
  log="$CASE_ROOT/hunt-v2.log"
  provider_log="$CASE_ROOT/hunt-v2.providers.jsonl"
  cli_log="$CASE_ROOT/hunt-v2.audit-cli.calls"
  runs="$CASE_ROOT/hunt-v2-runs"
  home="$CASE_ROOT/hunt-v2-home"
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
      HISTORY_RUNTIME_ABI=v2 \
      RESEARCH_DIRECTION_FILE=directions/dynamic-spatial-memory-vla-v1.json \
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
      ROUND_LIMIT=1 \
      "RUNS_DIR=$runs" \
      bash ./hunt.sh
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "Hunt v2 bounded round exited $status"
    sed -n '1,180p' "$log" >&2
    return
  fi
  if ! check_hunt_result "$repo" "$provider_log" "$runs" "$cli_log"; then
    fail 'Hunt v2 stage routing or receipts'
    return
  fi
  printf 'ok: Hunt v2 portable internal stages and legacy external stages\n'
}

run_terminal_failure_skips_cooldown() {
  local repo log sleep_log home runs status failures
  repo=$(make_repo hunt-terminal-failure) || {
    fail 'terminal Hunt failure fixture setup'
    return
  }
  install_fake_providers "$repo"
  instrument_audit_cli "$repo"
  write_hunt_ledger "$repo/ledger.tsv"
  mkdir -p "$repo/tmp"
  : > "$repo/tmp/near-sa-queue.tsv"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'printf "%s\\n" "$*" >> "$FAKE_SLEEP_LOG"' \
    > "$repo/.test-bin/sleep"
  chmod 755 "$repo/.test-bin/sleep"
  log="$CASE_ROOT/hunt-terminal-failure.log"
  sleep_log="$CASE_ROOT/hunt-terminal-failure.sleep.log"
  home="$CASE_ROOT/hunt-terminal-failure-home"
  runs="$CASE_ROOT/hunt-terminal-failure-runs"
  mkdir -p "$home" "$runs"
  run_bounded "$repo" "$log" \
    env \
      "HOME=$home" \
      "CODEX_HOME=$home/codex-config" \
      "EXPECTED_PROVIDER_HOME=$home" \
      "EXPECTED_PROVIDER_CODEX_HOME=$home/codex-config" \
      "PATH=$repo/.test-bin:$PATH" \
      "FAKE_SLEEP_LOG=$sleep_log" \
      FAKE_PORTABLE_STAGE_MODE=malformed \
      HISTORY_RUNTIME_ABI=v2 \
      HUNT_PROVIDER=claude \
      "AGENT_CMD=$repo/tests/fake_agent.sh" \
      HISTORY_NEAR_SA=tmp/near-sa-queue.tsv \
      RESUME_FRONT=0 \
      THEME_MIN_LOW=0 \
      RESEARCH_RETRY=0 \
      ROUND_LIMIT=1 \
      MAX_FAILS=12 \
      FAIL_SLEEP_MIN=1 \
      SA_TARGET=0 \
      "RUNS_DIR=$runs" \
      bash ./hunt.sh
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "terminal Hunt failure exited $status"
    sed -n '1,180p' "$log" >&2
    return
  fi
  failures=$(rg -c 'Round failed at generate' "$log" || true)
  if [ "$failures" -ne 1 ]; then
    fail "terminal Hunt failure count changed: $failures"
    sed -n '1,180p' "$log" >&2
    return
  fi
  if ! rg -q 'Reached ROUND_LIMIT=1' "$log"; then
    fail 'terminal Hunt failure did not reach ROUND_LIMIT=1'
    sed -n '1,180p' "$log" >&2
    return
  fi
  if [ -e "$sleep_log" ]; then
    fail 'terminal Hunt failure invoked cooldown sleep'
    return
  fi
  printf 'ok: terminal Hunt failure skips cooldown\n'
}

run_retryable_failure_cooldown_case() {
  local name=$1 round_limit=$2 max_fails=$3 expected_status=$4
  local repo log sleep_log home runs status failures sleeps
  repo=$(make_repo "hunt-retry-cooldown-$name") || {
    fail "$name Hunt cooldown fixture setup"
    return
  }
  install_fake_providers "$repo"
  instrument_audit_cli "$repo"
  write_hunt_ledger "$repo/ledger.tsv"
  mkdir -p "$repo/tmp"
  : > "$repo/tmp/near-sa-queue.tsv"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'printf "%s\\n" "$*" >> "$FAKE_SLEEP_LOG"' \
    > "$repo/.test-bin/sleep"
  chmod 755 "$repo/.test-bin/sleep"
  log="$CASE_ROOT/hunt-retry-cooldown-$name.log"
  sleep_log="$CASE_ROOT/hunt-retry-cooldown-$name.sleep.log"
  home="$CASE_ROOT/hunt-retry-cooldown-$name-home"
  runs="$CASE_ROOT/hunt-retry-cooldown-$name-runs"
  mkdir -p "$home" "$runs"
  run_bounded "$repo" "$log" \
    env \
      "HOME=$home" \
      "CODEX_HOME=$home/codex-config" \
      "EXPECTED_PROVIDER_HOME=$home" \
      "EXPECTED_PROVIDER_CODEX_HOME=$home/codex-config" \
      "PATH=$repo/.test-bin:$PATH" \
      "FAKE_SLEEP_LOG=$sleep_log" \
      FAKE_PORTABLE_STAGE_MODE=malformed \
      HISTORY_RUNTIME_ABI=v2 \
      HUNT_PROVIDER=claude \
      "AGENT_CMD=$repo/tests/fake_agent.sh" \
      HISTORY_NEAR_SA=tmp/near-sa-queue.tsv \
      RESUME_FRONT=0 \
      THEME_MIN_LOW=0 \
      RESEARCH_RETRY=0 \
      "ROUND_LIMIT=$round_limit" \
      "MAX_FAILS=$max_fails" \
      FAIL_SLEEP_MIN=1 \
      SA_TARGET=0 \
      "RUNS_DIR=$runs" \
      bash ./hunt.sh
  status=$?
  if [ "$status" -ne "$expected_status" ]; then
    fail "$name Hunt cooldown exited $status"
    sed -n '1,180p' "$log" >&2
    return
  fi
  failures=$(rg -c 'Round failed at generate' "$log" || true)
  failures=${failures:-0}
  if [ "$failures" -ne 2 ]; then
    fail "$name Hunt failure count changed: $failures"
    sed -n '1,180p' "$log" >&2
    return
  fi
  sleeps=$(cat "$sleep_log" 2>/dev/null || true)
  if [ "$sleeps" != 60 ]; then
    fail "$name Hunt cooldown calls changed: ${sleeps:-none}"
    return
  fi
  if [ "$round_limit" -gt 0 ]; then
    if ! rg -q "Reached ROUND_LIMIT=$round_limit" "$log"; then
      fail "$name Hunt did not reach ROUND_LIMIT=$round_limit"
      sed -n '1,180p' "$log" >&2
      return
    fi
  elif rg -q 'Reached ROUND_LIMIT=' "$log"; then
    fail "$name Hunt unexpectedly reported a bounded round limit"
    return
  fi
  printf 'ok: %s Hunt retryable failure retains one cooldown\n' "$name"
}

run_retryable_failures_retain_cooldown() {
  run_retryable_failure_cooldown_case bounded 2 12 0
  run_retryable_failure_cooldown_case unlimited 0 2 1
}

check_awr_result() {
  local repo=$1 provider_log=$2 profile=${3:-mixed}
  python3 - "$repo" "$provider_log" "$profile" <<'PY'
import json
import pathlib
import sys

repo = pathlib.Path(sys.argv[1])
provider_log = pathlib.Path(sys.argv[2])
profile = sys.argv[3]
records = [
    json.loads(line)
    for line in provider_log.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
observed = [(item["stage"], item["provider"]) for item in records]
if profile == "mixed":
    expected = [
        ("awr-research", "claude"),
        ("awr-priorwork", "claude"),
        ("awr-judge", "claude"),
    ]
elif profile == "all-agy":
    expected = [
        ("awr-research", "agy"),
        ("awr-priorwork", "agy"),
        ("awr-judge", "agy"),
    ]
else:
    raise SystemExit(f"unknown AwR test profile: {profile}")
if observed != expected:
    raise SystemExit(f"AwR role/provider isolation changed: {observed}")
if profile == "all-agy":
    for item in records:
        if item.get("legacy_file_wording_seen") is not True:
            raise SystemExit(
                f"agy did not observe legacy role file wording: {item}"
            )
        if item.get("transport_instructions_valid") is not True:
            raise SystemExit(
                f"agy did not receive the portable stdout override: {item}"
            )
        if item.get("structured_transport_valid") is not True:
            raise SystemExit(
                f"agy did not receive one matching inline schema: {item}"
            )

outdir = repo / "tmp/awr-side/awr"
finals = [
    path
    for path in outdir.glob("*.md")
    if not path.name.endswith(
        (".task.md", ".draft.md", ".priorwork.md", ".judge.md")
    )
]
if len(finals) != 1:
    raise SystemExit(f"AwR terminal artifact coverage changed: {finals}")
text = finals[0].read_text(encoding="utf-8")
for required in (
    "Status: ready",
    "## Revised Idea",
    "## Independent Prior Work",
    "Decision: SA-possible",
):
    if required not in text:
        raise SystemExit(f"AwR validator-visible output omitted {required}")
completions = list((repo / "tmp/awr-side").rglob("completion.json"))
if len(completions) != 3:
    raise SystemExit(f"AwR completion coverage changed: {completions}")
for path in completions:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("execution_boundary") != "portable-mirror-v1":
        raise SystemExit(f"AwR completion boundary changed: {path}")
PY
}

run_awr_v2() {
  local repo log provider_log home status
  repo=$(make_repo awr-v2) || { fail 'AwR fixture setup'; return; }
  install_fake_providers "$repo"
  write_awr_ledger "$repo/ledger.tsv"
  rm -f "$repo/tmp/ledger.good"
  log="$CASE_ROOT/awr-v2.log"
  provider_log="$CASE_ROOT/awr-v2.providers.jsonl"
  home="$CASE_ROOT/awr-v2-home"
  mkdir -p "$home"
  run_bounded "$repo" "$log" \
    env \
      "HOME=$home" \
      "CODEX_HOME=$home/codex-config" \
      "EXPECTED_PROVIDER_HOME=$home" \
      "EXPECTED_PROVIDER_CODEX_HOME=$home/codex-config" \
      "PATH=$repo/.test-bin:$PATH" \
      "FAKE_PORTABLE_STAGE_LOG=$provider_log" \
      FAKE_PORTABLE_STAGE_MODE=mirror-audit \
      HISTORY_RUNTIME_ABI=v2 \
      AWR_PROVIDER=claude \
      AWR_RESEARCH_PROVIDER=claude \
      AWR_PRIORWORK_PROVIDER=claude \
      AWR_JUDGE_PROVIDER=claude \
      SIDE_POLL_SEC=0 \
      SIDE_MAX_ROUNDS=1 \
      SIDE_MAX_BAD=1 \
      SIDE_GAP_SEC=0 \
      SIDE_GAP_MIN_SEC=0 \
      SIDE_GAP_MAX_SEC=0 \
      SIDE_COOLDOWN_SEC=0 \
      bash ./awr-side.sh
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "AwR v2 bounded scan exited $status"
    sed -n '1,180p' "$log" >&2
    return
  fi
  if ! check_awr_result "$repo" "$provider_log" mixed; then
    fail 'AwR v2 role routing, validators, or receipts'
    return
  fi
  printf 'ok: AwR v2 portable role isolation\n'
}

run_awr_v2_all_agy() {
  local repo log provider_log home status
  repo=$(make_repo awr-v2-all-agy) || {
    fail 'all-agy AwR fixture setup'
    return
  }
  install_fake_providers "$repo"
  write_awr_ledger "$repo/ledger.tsv"
  rm -f "$repo/tmp/ledger.good"
  log="$CASE_ROOT/awr-v2-all-agy.log"
  provider_log="$CASE_ROOT/awr-v2-all-agy.providers.jsonl"
  home="$CASE_ROOT/awr-v2-all-agy-home"
  mkdir -p "$home"
  run_bounded "$repo" "$log" \
    env \
      "HOME=$home" \
      "CODEX_HOME=$home/codex-config" \
      "EXPECTED_PROVIDER_HOME=$home" \
      "EXPECTED_PROVIDER_CODEX_HOME=$home/codex-config" \
      "PATH=$repo/.test-bin:$PATH" \
      "FAKE_PORTABLE_STAGE_LOG=$provider_log" \
      FAKE_PORTABLE_STAGE_MODE=mirror-audit \
      HISTORY_RUNTIME_ABI=v2 \
      AWR_PROVIDER=claude \
      AWR_RESEARCH_PROVIDER=claude \
      AWR_PRIORWORK_PROVIDER=claude \
      AWR_JUDGE_PROVIDER=claude \
      SIDE_POLL_SEC=0 \
      SIDE_MAX_ROUNDS=1 \
      SIDE_MAX_BAD=1 \
      SIDE_GAP_SEC=0 \
      SIDE_GAP_MIN_SEC=0 \
      SIDE_GAP_MAX_SEC=0 \
      SIDE_COOLDOWN_SEC=0 \
      bash ./awr-side.sh
  status=$?
  if [ "$status" -ne 0 ]; then
    fail "all-agy AwR v2 bounded scan exited $status"
    sed -n '1,180p' "$log" >&2
    return
  fi
  if ! check_awr_result "$repo" "$provider_log" mixed; then
    fail 'all-agy AwR stdout override, validators, or receipts'
    return
  fi
  printf 'ok: AwR v2 agy portable stdout override\n'
}

run_v1_regressions() {
  if ! bash "$ROOT/tests/generation_contract_smoke.sh" \
      > "$CASE_ROOT/v1-generation.log" 2>&1; then
    fail 'v1 generation contract changed'
  else
    printf 'ok: v1 generation contract unchanged\n'
  fi
  if ! bash "$ROOT/tests/runtime_abi_smoke.sh" \
      > "$CASE_ROOT/v1-runtime.log" 2>&1; then
    fail 'v1 runtime behavior changed'
  else
    printf 'ok: v1 runtime behavior unchanged\n'
  fi
}

run_hunt_v2
run_terminal_failure_skips_cooldown
run_retryable_failures_retain_cooldown
run_awr_v2
run_awr_v2_all_agy
run_v1_regressions

if [ "$FAILURES" -ne 0 ]; then
  printf 'failed: portable Hunt/AwR e2e smoke (%s failures)\n' \
    "$FAILURES" >&2
  exit 1
fi
printf 'ok: portable Hunt/AwR e2e smoke\n'
