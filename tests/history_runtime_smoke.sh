#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# Keep the protocol-shape check ahead of the slower Python suite.  A legacy
# hunt must fail here instead of looking green because only the library tests
# passed (or looking red because a nested sandbox is unavailable).
for helper in \
  history_startup \
  history_freeze_batch \
  history_observe_round \
  history_seal_selection \
  history_compare_targets \
  history_materialize_research \
  history_seal_resume \
  history_seal_review_plan \
  history_run_review_matrix \
  history_build_aggregation \
  history_commit_round \
  history_materialize_ledger
do
  grep -q "^${helper}()" "$ROOT/hunt.sh" || {
    printf 'history_runtime_smoke: missing %s runtime cutover helper\n' "$helper" >&2
    exit 1
  }
done

if grep -Eq 'META_EVERY|META_MIN_REJECTS|Read roles/meta\.md and follow it' "$ROOT/hunt.sh"; then
  printf 'history_runtime_smoke: routine meta path remains\n' >&2
  exit 1
fi

if grep -Eq 'cp[[:space:]]+(ledger\.tsv|"?\\$LEDGER_GOOD"?)' "$ROOT/hunt.sh"; then
  printf 'history_runtime_smoke: hunt still uses cp-based ledger ownership\n' >&2
  exit 1
fi

relative_observation_failed=0
if ! python3 - "$ROOT/lib/history_runtime.py" <<'PY'
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
body = source.split("def observe_frozen_batch(", 1)[1].split(
    "def _contained_comparator_runner(", 1
)[0]
if (
    "root = pathlib.Path(artifact_root)" in body
    or '"batch_path": str(pathlib.Path(batch_path))' in body
):
    raise SystemExit(1)
PY
then
  relative_observation_failed=1
fi

if [ "${HISTORY_RUNTIME_SKIP_PYTHON:-0}" != 1 ]; then
  python3 "$ROOT/tests/history_runtime_smoke.py"
fi

case_root=$(mktemp -d "${TMPDIR:-/tmp}/history-hunt-cutover.XXXXXX")
upstream_pid=
upstream_stop=
upstream_control=
loopback_port=

cleanup() {
  if [ -n "$upstream_pid" ] && kill -0 "$upstream_pid" 2>/dev/null; then
    [ -n "$upstream_stop" ] && : > "$upstream_stop"
    wait "$upstream_pid" 2>/dev/null || true
  fi
  rm -rf "$case_root"
}
trap cleanup EXIT HUP INT TERM

copy_repo() {
  local destination=$1
  mkdir -p "$destination"
  (
    cd "$ROOT"
    tar \
      --exclude=.git \
      --exclude=.worktrees \
      --exclude=.ai-ideas \
      --exclude=tmp \
      --exclude=ideas \
      -cf - .
  ) | tar -xf - -C "$destination"
  git -C "$destination" init -q
  git -C "$destination" config user.name history-runtime-smoke
  git -C "$destination" config user.email history-runtime-smoke@example.invalid
  git -C "$destination" add .
  git -C "$destination" commit -qm baseline
  mkdir -p "$destination/tmp" "$destination/ideas"
  chmod 755 "$destination/tests/fake_stage_agent.py"
}

contained_json() {
  python3 - "$1" <<'PY'
import json
import os
import sys
print(json.dumps([os.path.realpath(sys.argv[1])], separators=(",", ":")))
PY
}

contained_codex_json() {
  python3 - "$1" <<'PY'
import json
import os
import sys

print(
    json.dumps(
        [
            os.path.realpath(sys.argv[1]),
            "-m",
            "fake-model",
            "-c",
            "model_reasoning_effort=xhigh",
        ],
        separators=(",", ":"),
    )
)
PY
}

configure_loopback_codex() {
  local repository=$1 executable=$2 port=$3 fake_home=$4
  mkdir -p "$fake_home/.codex"
  python3 - "$repository" "$executable" "$port" "$fake_home" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import sys

repository = pathlib.Path(sys.argv[1]).resolve()
executable = pathlib.Path(sys.argv[2]).resolve()
port = int(sys.argv[3])
fake_home = pathlib.Path(sys.argv[4]).resolve()

stage_path = repository / "lib" / "history_stage.py"
source = stage_path.read_text(encoding="utf-8")
replacement = (
    "CODEX_UPSTREAM = {\n"
    '    "scheme": "http",\n'
    '    "host": "127.0.0.1",\n'
    f'    "port": {port},\n'
    '    "path": "/v1/responses",\n'
    "}"
)
source, count = re.subn(
    r"CODEX_UPSTREAM = \{\n(?:    .*\n)+?\}",
    replacement,
    source,
    count=1,
)
if count != 1:
    raise SystemExit("could not bind disposable runtime to loopback")
stage_path.write_text(source, encoding="utf-8")

sys.path.insert(0, str(repository))
from lib import history_projection
from lib import history_stage

policy = history_projection.load_policy(
    repository / "history" / "retrieval-policy-v1.json"
)
adapter_artifacts = {
    "executable": {
        "sha256": hashlib.sha256(
            (repository / "lib" / "history_stage_adapter.py").read_bytes()
        ).hexdigest()
    },
    "canonicalizer": {
        "sha256": hashlib.sha256(
            (repository / "lib" / "history_stage_proxy.py").read_bytes()
        ).hexdigest()
    },
}
identity = {
    "model": "fake-model",
    "reasoning_setting": "model_reasoning_effort=xhigh",
}
profile_sha256 = hashlib.sha256(
    history_stage._codex_profile_bytes(
        hashlib.sha256(executable.read_bytes()).hexdigest(),
        identity,
        policy,
        adapter_artifacts,
    )
).hexdigest()
capability_id = hashlib.sha256(
    b"history-codex-capability-v2\0"
    + profile_sha256.encode("ascii")
).hexdigest()
(repository / "history" / "codex-adapter-capabilities-v2.json").write_text(
    json.dumps(
        {
            "schema_version": 2,
            "capabilities": [
                {
                    "capability_id": capability_id,
                    "codex_cli_version": history_stage.CODEX_CLI_VERSION,
                    "profile_sha256": profile_sha256,
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n",
    encoding="utf-8",
)
auth = {
    "auth_mode": "chatgpt",
    "tokens": {
        "id_token": "offline-id",
        "access_token": "offline-access",
        "refresh_token": "offline-refresh",
        "account_id": "offline-account",
    },
    "last_refresh": "offline-fixture",
}
auth_path = fake_home / ".codex" / "auth.json"
auth_path.write_text(
    json.dumps(auth, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
os.chmod(auth_path, 0o600)
PY
}

start_loopback_upstream() {
  local repository=$1
  local port_file=$case_root/upstream.port
  local request_file=$case_root/upstream.requests.json
  local upstream_log=$case_root/upstream.log
  upstream_stop=$case_root/upstream.stop
  upstream_control=$case_root/upstream.control.json
  rm -f \
    "$port_file" \
    "$request_file" \
    "$upstream_stop" \
    "$upstream_control"
  set_loopback_behavior complete_no_match valid
  PYTHONPATH="$repository" python3 - \
    "$port_file" \
    "$upstream_stop" \
    "$request_file" \
    "$upstream_control" \
    > "$upstream_log" 2>&1 <<'PY' &
import json
import pathlib
import sys
import time

from tests.history_stage_proxy_smoke import FakeUpstream
from tests.history_stage_proxy_smoke import valid_sse

port_path = pathlib.Path(sys.argv[1])
stop_path = pathlib.Path(sys.argv[2])
request_path = pathlib.Path(sys.argv[3])
control_path = pathlib.Path(sys.argv[4])
observed_behaviors = []

def read_behavior():
    behavior = json.loads(control_path.read_text(encoding="utf-8"))
    if (
        set(behavior) != {"comparison_status", "review_mode"}
        or behavior["comparison_status"]
        not in {"complete_no_match", "uncertain"}
        or behavior["review_mode"] not in {"valid", "invalid"}
    ):
        raise ValueError("invalid loopback behavior")
    return behavior

def envelope(request, behavior):
    prompt_text = request["input"][-1]["content"][0]["text"]
    invocation = json.loads(prompt_text)
    stage = invocation["stage"]
    if stage == "generate":
        artifacts = [
            {
                "artifact_kind": "generation-ideas-markdown",
                "content": (
                    "Assumption-Removal Attempt: incomplete — fixture; "
                    "blocked by: evidence\n\n"
                    "## I1\n"
                    "One-Sentence Story: Bounded Test Idea\n"
                    "Theme: Evaluation and Diagnostics\n"
                    "Form: new mechanism or new problem\n"
                    "Summary: Exercise the bounded stage contract.\n"
                    "Minimal Falsification Experiment: Compare against the "
                    "strongest fixture baseline on 128 episodes using one "
                    "H100; kill the idea if the expected bounded signal is "
                    "absent.\n"
                    "Why It May Be Novel: Downstream research must test "
                    "occupation.\n"
                ),
            },
            {
                "artifact_kind": "generation-ideas-tsv",
                "content": (
                    "I1\tBounded Test Idea\t"
                    "Evaluation and Diagnostics\n"
                ),
            },
        ]
    elif stage == "history-compare":
        pack = invocation["retrieval_payload"]
        status = behavior["comparison_status"]
        relations = []
        for lineage in pack["lineages"]:
            match = lineage["matches"][0]
            relations.append(
                {
                    "relation": (
                        "uncertain"
                        if status == "uncertain"
                        else "distinct"
                    ),
                    "candidate_id": match["candidate_id"],
                    "lineage_id": match["lineage_id"],
                    "facet": match["facet"],
                    "evidence_id": match["evidence_id"],
                    "material_difference": (
                        "The supplied propositions differ."
                    ),
                    "confidence": 0.8,
                }
            )
        comparison = {
            "status": status,
            "comparator_version": "history-comparator-v1",
            "relations": relations,
            "expansion_request": (
                {
                    "record_ids": [
                        pack["lineages"][0]["matches"][0][
                            "candidate_id"
                        ]
                    ]
                }
                if (
                    status == "uncertain"
                    and pack["lineages"]
                    and pack["expansion_round"]
                    < pack["hard_limits"]["max_expansion_rounds"]
                )
                else None
            ),
        }
        artifacts = [
            {
                "artifact_kind": "history-comparison-json",
                "content": json.dumps(
                    comparison,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            }
        ]
    elif stage == "review":
        candidate_id = invocation["candidate"]["candidate_id"]
        if behavior["review_mode"] == "invalid":
            return {
                "schema_version": 1,
                "stage": stage,
                "artifacts": [
                    {
                        "artifact_kind": "review-markdown",
                        "content": "invalid review fixture\n",
                    }
                ],
            }
        reason = "The bounded fixture leaves one major issue."
        artifacts = [
            {
                "artifact_kind": "review-markdown",
                "content": (
                    f"# {candidate_id}\n"
                    "Verdict: accept-w-rev\n"
                    "CRITICAL: 0\n"
                    "MAJOR: 1\n"
                    "Headline: The bounded candidate remains plausible.\n"
                    "Occupation: The supplied prior work leaves one gap.\n"
                    "Experiment: The falsification is bounded but incomplete.\n"
                    "Estimand: The supplied estimand is aligned.\n"
                    "Payoff: One attributable payoff remains possible.\n"
                    "Feasibility: One researcher and one H100 suffice.\n"
                    "History: unavailable\n"
                    f"Reason: {reason}\n"
                ),
            },
            {
                "artifact_kind": "review-verdict-tsv",
                "content": (
                    f"{candidate_id}\taccept-w-rev\t1\t{reason}\n"
                ),
            },
        ]
    else:
        raise ValueError(f"unexpected routine stage: {stage}")
    return {
        "schema_version": 1,
        "stage": stage,
        "artifacts": artifacts,
    }

def response_factory(request):
    behavior = read_behavior()
    observed_behaviors.append(dict(behavior))
    output = json.dumps(
        envelope(request, behavior),
        sort_keys=True,
        separators=(",", ":"),
    )
    return valid_sse(request, output_text=output)

with FakeUpstream(response_factory) as upstream:
    port_path.write_text(f"{upstream.port}\n", encoding="ascii")
    while not stop_path.exists():
        time.sleep(0.02)
    request_path.write_text(
        json.dumps(
            [
                dict(
                    {
                    "authorization": item["authorization"],
                    "account_id": item["account_id"],
                    "path": item["path"],
                    "stage": json.loads(
                        json.loads(item["raw"])["input"][-1]["content"][0][
                            "text"
                        ]
                    )["stage"],
                    },
                    **behavior,
                )
                for item, behavior in zip(
                    upstream.requests, observed_behaviors
                )
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
PY
  upstream_pid=$!
  attempt=0
  while [ ! -s "$port_file" ]; do
    kill -0 "$upstream_pid" 2>/dev/null || {
      printf 'history_runtime_smoke: loopback upstream failed to start\n' >&2
      cat "$upstream_log" >&2
      exit 1
    }
    attempt=$((attempt + 1))
    [ "$attempt" -lt 200 ] || {
      printf 'history_runtime_smoke: loopback upstream start timed out\n' >&2
      exit 1
    }
    sleep 0.02
  done
  loopback_port=$(cat "$port_file")
}

set_loopback_behavior() {
  local comparison_status=$1 review_mode=$2
  python3 - \
    "$upstream_control" \
    "$comparison_status" \
    "$review_mode" <<'PY'
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
temporary = path.with_name(path.name + ".tmp")
temporary.write_text(
    json.dumps(
        {
            "comparison_status": sys.argv[2],
            "review_mode": sys.argv[3],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n",
    encoding="utf-8",
)
os.replace(temporary, path)
PY
}

stop_loopback_upstream() {
  [ -n "$upstream_pid" ] || return 0
  : > "$upstream_stop"
  wait "$upstream_pid"
  upstream_pid=
}

# Production enforcement authority must be checked before storage or any
# external/contained backend.  The fake paths are supplied only as sentinels:
# observing a call is a contract failure.
enforcement_repo="$case_root/enforcement"
copy_repo "$enforcement_repo"
python3 - "$enforcement_repo/history/retrieval-policy-v1.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["mode"] = "enforcement"
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
PY
enforcement_log="$case_root/enforcement.log"
if (
  cd "$enforcement_repo"
  AGENT_CMD="$enforcement_repo/tests/fake_agent.sh" \
  FRONT_CMD="$enforcement_repo/tests/fake_agent.sh" \
  BACK_CMD="$enforcement_repo/tests/fake_agent.sh" \
  CONTAINED_AGENT_CMD_JSON="$(contained_json "$enforcement_repo/tests/fake_stage_agent.py")" \
  REVIEWERS=1 \
  RESUME_FRONT=0 \
  THEME_MIN_LOW=0 \
  ROUND_LIMIT=1 \
  FAIL_SLEEP_MIN=0 \
  NO_HIT_SLEEP_MIN_LO=0 \
  NO_HIT_SLEEP_MIN_HI=0 \
  ALLOW_ZERO_NO_HIT_SLEEP=1 \
  RUNS_DIR="$case_root/enforcement-runs" \
  bash ./hunt.sh
) > "$enforcement_log" 2>&1; then
  printf 'history_runtime_smoke: enforcement started without production calibration\n' >&2
  exit 1
fi
grep -q 'History startup failed before agent invocation' "$enforcement_log" || {
  cat "$enforcement_log" >&2
  exit 1
}
test ! -e "$enforcement_repo/tmp/fake-agent.calls"
test ! -d "$enforcement_repo/tmp/round/history/generate-output"

# A repository fixture is never a production backend, even in shadow mode.
# Private temp-confined wrappers in history_runtime_smoke.py exercise the full
# frozen-batch A/B and enforcement failure matrix with that fixture.
shadow_repo="$case_root/shadow"
copy_repo "$shadow_repo"
shadow_log="$case_root/shadow.log"
if (
  cd "$shadow_repo"
  AGENT_CMD="$shadow_repo/tests/fake_agent.sh" \
  FRONT_CMD="$shadow_repo/tests/fake_agent.sh" \
  BACK_CMD="$shadow_repo/tests/fake_agent.sh" \
  CONTAINED_AGENT_CMD_JSON="$(contained_json "$shadow_repo/tests/fake_stage_agent.py")" \
  REVIEWERS=1 \
  RESUME_FRONT=0 \
  THEME_MIN_LOW=0 \
  ROUND_LIMIT=1 \
  FAIL_SLEEP_MIN=0 \
  NO_HIT_SLEEP_MIN_LO=0 \
  NO_HIT_SLEEP_MIN_HI=0 \
  ALLOW_ZERO_NO_HIT_SLEEP=1 \
  MAX_FAILS=1 \
  RUNS_DIR="$case_root/shadow-runs" \
  bash ./hunt.sh
) > "$shadow_log" 2>&1; then
  printf 'history_runtime_smoke: production hunt accepted a fixture backend\n' >&2
  exit 1
fi
fresh_start_failed=0
if ! grep -q 'registered fixture backend is unavailable in production' \
  "$shadow_log"; then
  fresh_start_failed=1
fi
test ! -e "$shadow_repo/tmp/fake-agent.calls"

# The successful harness supplies an explicit empty legacy snapshot so a
# fresh-start regression does not prevent exercising the rest of the cutover.
fixture_repo="$case_root/fixture-rejection"
copy_repo "$fixture_repo"
: > "$fixture_repo/tmp/near-sa-queue.tsv"
fixture_log="$case_root/fixture-rejection.log"
if (
  cd "$fixture_repo"
  AGENT_CMD="$fixture_repo/tests/fake_agent.sh" \
  FRONT_CMD="$fixture_repo/tests/fake_agent.sh" \
  BACK_CMD="$fixture_repo/tests/fake_agent.sh" \
  HISTORY_NEAR_SA=tmp/near-sa-queue.tsv \
  CONTAINED_AGENT_CMD_JSON="$(contained_json "$fixture_repo/tests/fake_stage_agent.py")" \
  REVIEWERS=1 \
  RESUME_FRONT=0 \
  THEME_MIN_LOW=0 \
  ROUND_LIMIT=1 \
  FAIL_SLEEP_MIN=0 \
  NO_HIT_SLEEP_MIN_LO=0 \
  NO_HIT_SLEEP_MIN_HI=0 \
  ALLOW_ZERO_NO_HIT_SLEEP=1 \
  MAX_FAILS=1 \
  RUNS_DIR="$case_root/fixture-rejection-runs" \
  bash ./hunt.sh
) > "$fixture_log" 2>&1; then
  printf 'history_runtime_smoke: production hunt accepted a fixture backend\n' >&2
  exit 1
fi
grep -q 'registered fixture backend is unavailable in production' \
  "$fixture_log" || {
  cat "$fixture_log" >&2
  exit 1
}
test ! -e "$fixture_repo/tmp/fake-agent.calls"
test -s "$fixture_repo/.ai-ideas/history.sqlite3"
cmp "$fixture_repo/ledger.tsv" "$fixture_repo/tmp/ledger.good"

success_repo="$case_root/success"
copy_repo "$success_repo"
python3 - "$success_repo/ledger.tsv" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.write_text(
    "date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
    "2026-07-23\thunt\tEvaluation and Diagnostics\t"
    "A short historical fixture.\treject\t"
    "The bounded fixture is occupied.\thigh\tnovelty-dead\n",
    encoding="utf-8",
)
PY
: > "$success_repo/tmp/near-sa-queue.tsv"
fake_codex_dir="$case_root/fake-codex"
fake_home="$case_root/fake-home"
success_runs="$case_root/success-runs"
mkdir -p "$fake_codex_dir" "$fake_home" "$success_runs"
cp "$ROOT/tests/fake_agent.sh" "$fake_codex_dir/codex"
chmod 755 "$fake_codex_dir/codex"
start_loopback_upstream "$success_repo"
configure_loopback_codex \
  "$success_repo" \
  "$fake_codex_dir/codex" \
  "$loopback_port" \
  "$fake_home"
codex_json=$(contained_codex_json "$fake_codex_dir/codex")

run_success_round() {
  local label=$1 resume_front=$2 comparator_status=$3
  set_loopback_behavior "$comparator_status" valid
  if ! (
    cd "$success_repo"
    HOME="$fake_home" \
    AGENT_CMD="$success_repo/tests/fake_agent.sh" \
    FRONT_CMD="$success_repo/tests/fake_agent.sh" \
    BACK_CMD="$success_repo/tests/fake_agent.sh" \
    HISTORY_NEAR_SA=tmp/near-sa-queue.tsv \
    CONTAINED_AGENT_CMD_JSON="$codex_json" \
    REVIEWERS=1 \
    RESUME_FRONT="$resume_front" \
    THEME_MIN_LOW=0 \
    RESEARCH_RETRY=0 \
    FAIL_SLEEP_MIN=0 \
    NO_HIT_SLEEP_MIN_LO=0 \
    NO_HIT_SLEEP_MIN_HI=0 \
    ALLOW_ZERO_NO_HIT_SLEEP=1 \
    MAX_FAILS=1 \
    SA_TARGET=0 \
    ROUND_LIMIT=1 \
    RUNS_DIR="$success_runs" \
    FAKE_AGENT_MODE=history-summary-forbidden \
    bash ./hunt.sh
  ) > "$case_root/$label.log" 2>&1; then
    printf 'history_runtime_smoke: %s failed\n' "$label" >&2
    cat "$case_root/$label.log" >&2
    return 1
  fi
}

run_front_failure() {
  local label=$1 resume_front=$2 comparator_status=$3
  set_loopback_behavior "$comparator_status" invalid
  if (
    cd "$success_repo"
    HOME="$fake_home" \
    AGENT_CMD="$success_repo/tests/fake_agent.sh" \
    FRONT_CMD="$success_repo/tests/fake_agent.sh" \
    BACK_CMD="$success_repo/tests/fake_agent.sh" \
    HISTORY_NEAR_SA=tmp/near-sa-queue.tsv \
    CONTAINED_AGENT_CMD_JSON="$codex_json" \
    REVIEWERS=1 \
    RESUME_FRONT="$resume_front" \
    THEME_MIN_LOW=0 \
    RESEARCH_RETRY=0 \
    FAIL_SLEEP_MIN=0 \
    NO_HIT_SLEEP_MIN_LO=0 \
    NO_HIT_SLEEP_MIN_HI=0 \
    ALLOW_ZERO_NO_HIT_SLEEP=1 \
    MAX_FAILS=1 \
    SA_TARGET=0 \
    ROUND_LIMIT=1 \
    RUNS_DIR="$success_runs" \
    FAKE_AGENT_MODE=history-summary-forbidden \
    bash ./hunt.sh
  ) > "$case_root/$label.log" 2>&1; then
    printf 'history_runtime_smoke: %s unexpectedly succeeded\n' \
      "$label" >&2
    return 1
  fi
  grep -q 'Round failed at review' "$case_root/$label.log" || {
    printf 'history_runtime_smoke: %s failed before review\n' \
      "$label" >&2
    cat "$case_root/$label.log" >&2
    return 1
  }
}

ledger_before=$(wc -l < "$success_repo/ledger.tsv" | tr -d ' ')
run_success_round shadow-reference 0 uncertain
set -- "$success_runs"/*
[ "$#" -eq 1 ] && [ -d "$1/round" ] || {
  printf 'history_runtime_smoke: reference round was not archived\n' >&2
  cat "$case_root/shadow-reference.log" >&2
  exit 1
}
reference_archive=$1

run_success_round shadow-observed 0 complete_no_match
observed_archive=
archive_count=0
for candidate_archive in "$success_runs"/*; do
  [ -d "$candidate_archive/round" ] || continue
  archive_count=$((archive_count + 1))
  [ "$candidate_archive" = "$reference_archive" ] \
    || observed_archive=$candidate_archive
done
[ "$archive_count" -eq 2 ] && [ -n "$observed_archive" ] || {
  printf 'history_runtime_smoke: observed replay was not archived\n' >&2
  cat "$case_root/shadow-observed.log" >&2
  exit 1
}

for relative in \
  ideas.all.tsv \
  ideas.all.md \
  select.tsv \
  prescreen.md \
  ideas.tsv \
  ideas.md \
  priorwork.md \
  history/research-view/ideas.tsv \
  history/research-view/ideas.md \
  history/review-attempts/001/report-view/ideas.md \
  history/review-attempts/001/report-view/priorwork.md \
  history/review-attempts/001/report-view/rev/1/review.md
do
  cmp \
    "$reference_archive/round/$relative" \
    "$observed_archive/round/$relative" \
    || {
      printf 'history_runtime_smoke: shadow replay drifted at %s\n' \
        "$relative" >&2
      exit 1
    }
done

for archive in "$reference_archive" "$observed_archive"; do
  for required in \
    manifest.tsv \
    round/history/batch/batch.json \
    round/history/selection.json \
    round/history/observations/comparison-index.json \
    round/history/resume-state.json \
    round/history/review-attempts/001/review-plan.json \
    round/history/review-attempts/001/review-index.json \
    round/history/review-attempts/001/aggregation.json \
    round/history/review-attempts/001/commit-round.json \
    round/history/review-attempts/001/report-view/accepted.tsv \
    round/history/materialize-ledger.json \
    round/history/archive-receipt.json \
    round/history/archive-authority/retrieval-policy.json \
    round/history/archive-authority/startup.json \
    round/history/archive-authority/materialize-ledger.json \
    round/history/archive-authority/authority-reference.json \
    round/history/archive-authority/ledger-target-receipts/ledger.tsv.json \
    round/history/archive-authority/ledger-target-receipts/tmp__ledger.good.json
  do
    [ -f "$archive/$required" ] || {
      printf 'history_runtime_smoke: archive omitted %s\n' "$required" >&2
      exit 1
    }
  done
done

python3 - "$success_repo" "$reference_archive" "$observed_archive" <<'PY'
import json
import pathlib
import sys

repository = pathlib.Path(sys.argv[1])
archives = tuple(pathlib.Path(item) for item in sys.argv[2:])
sys.path.insert(0, str(repository / "lib"))
import history_archive

reference = archives[0] / "round"
observed = archives[1] / "round"
batches = []
for archive, root in zip(archives, (reference, observed)):
    history_archive.verify_archive(
        root,
        run_id=archive.name,
        round_number=1,
        policy_mode="shadow",
        reason="decision",
    )
    batch = json.loads((root / "history/batch/batch.json").read_text())
    if batch.get("schema_version") != 1:
        raise SystemExit("invalid archived frozen batch")
    batches.append(batch)
    authority = json.loads(
        (
            root
            / "history/archive-authority/authority-reference.json"
        ).read_text()
    )
    capability_path = (
        root
        / "history/archive-authority/calibration-capability.json"
    )
    if (authority.get("capability") is None) == capability_path.exists():
        raise SystemExit("archive capability authority is inconsistent")
    for path in root.rglob("*"):
        if path.is_file() and any(
            secret in path.read_bytes()
            for secret in (
                b"offline-access",
                b"offline-refresh",
                b"offline-account",
            )
        ):
            raise SystemExit("archive copied private provider credentials")
if [
    item["content_sha256"]
    for item in batches[0]["candidates"]
] != [
    item["content_sha256"]
    for item in batches[1]["candidates"]
]:
    raise SystemExit("shadow A/B did not freeze identical candidates")
if (reference / "ideas.all.tsv").read_bytes() != (
    observed / "ideas.all.tsv"
).read_bytes():
    raise SystemExit("frozen candidate TSV changed")
if (reference / "ideas.all.md").read_bytes() != (
    observed / "ideas.all.md"
).read_bytes():
    raise SystemExit("frozen candidate markdown changed")

reference_selection = json.loads(
    (reference / "history/selection.json").read_text()
)
observed_selection = json.loads(
    (observed / "history/selection.json").read_text()
)
for field in ("short_max", "theme_min_low", "targets"):
    if reference_selection[field] != observed_selection[field]:
        raise SystemExit(f"shadow selection decision drifted at {field}")

comparisons = [
    json.loads(
        (
            root
            / "history/observations/comparison-index.json"
        ).read_text()
    )
    for root in (reference, observed)
]
status_sets = [
    {
        status
        for target in comparison["targets"]
        for status in target["statuses"]
    }
    for comparison in comparisons
]
if status_sets != [{"uncertain"}, {"complete_no_match"}]:
    raise SystemExit("shadow A/B comparison statuses drifted")
for prompt_root in (
    observed / "history/research-view",
    observed / "history/review-attempts/001/review-stages",
):
    for path in prompt_root.rglob("*"):
        if path.is_file() and b"complete_no_match" in path.read_bytes():
            raise SystemExit(
                "complete comparator evidence entered a downstream prompt"
            )
PY

ledger_after_two=$(wc -l < "$success_repo/ledger.tsv" | tr -d ' ')
[ "$ledger_after_two" -eq $((ledger_before + 2)) ] || {
  printf 'history_runtime_smoke: shadow A/B ledger delta is not two rows\n' >&2
  exit 1
}
cmp "$success_repo/ledger.tsv" "$success_repo/tmp/ledger.good"
test ! -s "$success_repo/tmp/near-sa-queue.tsv"
python3 - \
  "$success_repo/.ai-ideas/history.sqlite3" \
  "$reference_archive" \
  "$observed_archive" <<'PY'
import json
import pathlib
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    count = connection.execute(
        "SELECT COUNT(*) FROM near_sa_observations"
    ).fetchone()[0]
finally:
    connection.close()
if count != 0:
    raise SystemExit("non-SA fake ballot created a near-SA observation")
for archive in map(pathlib.Path, sys.argv[2:]):
    aggregation = json.loads(
        (
            archive
            / "round/history/review-attempts/001/aggregation.json"
        ).read_text()
    )
    if aggregation.get("near_sa_observations") != []:
        raise SystemExit("archive disagrees with canonical near-SA state")
PY

run_front_failure \
  shadow-resume-failure-1 \
  0 \
  complete_no_match
[ "$(wc -l < "$success_repo/ledger.tsv" | tr -d ' ')" \
  -eq "$ledger_after_two" ] || {
  printf 'history_runtime_smoke: failed front attempt changed ledger\n' >&2
  exit 1
}

failed_archive_1=
archive_count=0
for candidate_archive in "$success_runs"/*; do
  [ -d "$candidate_archive/round" ] || continue
  archive_count=$((archive_count + 1))
  case "$candidate_archive" in
    "$reference_archive"|"$observed_archive") ;;
    *) failed_archive_1=$candidate_archive ;;
  esac
done
[ "$archive_count" -eq 3 ] && [ -n "$failed_archive_1" ] || {
  printf 'history_runtime_smoke: first failed resume source was not archived\n' >&2
  exit 1
}

(
  cd "$success_repo"
  HOME="$fake_home" python3 lib/history_runtime.py validate-resume \
    --policy history/retrieval-policy-v1.json \
    --resume tmp/round/history/resume-state.json \
    > "$case_root/validate-resume-1.json"
)

run_front_failure \
  shadow-resume-failure-2 \
  1 \
  complete_no_match
[ "$(wc -l < "$success_repo/ledger.tsv" | tr -d ' ')" \
  -eq "$ledger_after_two" ] || {
  printf 'history_runtime_smoke: repeated front failure changed ledger\n' >&2
  exit 1
}

failed_archive_2=
archive_count=0
for candidate_archive in "$success_runs"/*; do
  [ -d "$candidate_archive/round" ] || continue
  archive_count=$((archive_count + 1))
  case "$candidate_archive" in
    "$reference_archive"|"$observed_archive"|"$failed_archive_1") ;;
    *) failed_archive_2=$candidate_archive ;;
  esac
done
[ "$archive_count" -eq 4 ] && [ -n "$failed_archive_2" ] || {
  printf 'history_runtime_smoke: repeated failed resume reused an archive id\n' >&2
  exit 1
}

(
  cd "$success_repo"
  HOME="$fake_home" python3 lib/history_runtime.py validate-resume \
    --policy history/retrieval-policy-v1.json \
    --resume tmp/round/history/resume-state.json \
    > "$case_root/validate-resume-2.json"
)

run_success_round \
  shadow-resume-success \
  1 \
  complete_no_match
ledger_after_resume=$(wc -l < "$success_repo/ledger.tsv" | tr -d ' ')
[ "$ledger_after_resume" -eq $((ledger_after_two + 1)) ] || {
  printf 'history_runtime_smoke: sealed resume committed an invalid delta\n' >&2
  exit 1
}
cmp "$success_repo/ledger.tsv" "$success_repo/tmp/ledger.good"

decision_archive=
archive_count=0
for candidate_archive in "$success_runs"/*; do
  [ -d "$candidate_archive/round" ] || continue
  archive_count=$((archive_count + 1))
  case "$candidate_archive" in
    "$reference_archive"|"$observed_archive"|"$failed_archive_1"|"$failed_archive_2") ;;
    *) decision_archive=$candidate_archive ;;
  esac
done
[ "$archive_count" -eq 5 ] && [ -n "$decision_archive" ] || {
  printf 'history_runtime_smoke: resumed decision reused a failure archive\n' >&2
  exit 1
}

python3 - \
  "$success_repo" \
  "$failed_archive_1" \
  "$failed_archive_2" \
  "$decision_archive" <<'PY'
import hashlib
import json
import pathlib
import sys

repository = pathlib.Path(sys.argv[1])
failed_1, failed_2, decision = map(pathlib.Path, sys.argv[2:])
sys.path.insert(0, str(repository / "lib"))
import history_archive

receipts = []
for archive in (failed_1, failed_2, decision):
    receipt_path = (
        archive / "round/history/archive-receipt.json"
    )
    receipt = json.loads(receipt_path.read_text())
    history_archive.verify_archive(
        archive / "round",
        run_id=archive.name,
        round_number=1,
    )
    receipts.append((receipt, receipt_path))
if [item[0]["archive_class"] for item in receipts] != [
    "failure",
    "failure",
    "decision",
]:
    raise SystemExit("resume archive lifecycle classes drifted")
if len({failed_1.name, failed_2.name, decision.name}) != 3:
    raise SystemExit("resume attempt run IDs collided")

for current, prior, prior_receipt in (
    (failed_2, failed_1, receipts[0]),
    (decision, failed_2, receipts[1]),
):
    lineage_path = (
        current
        / "round/history/resume-attempts"
        / f"{current.name}.json"
    )
    lineage = json.loads(lineage_path.read_text())
    if (
        lineage.get("run_id") != current.name
        or lineage.get("resumed_from_run_id") != prior.name
    ):
        raise SystemExit("resume lineage run IDs are invalid")
    prior_binding = lineage.get("prior_failure_archive")
    expected_receipt_sha = hashlib.sha256(
        prior_receipt[1].read_bytes()
    ).hexdigest()
    if (
        not isinstance(prior_binding, dict)
        or prior_binding.get("archive_receipt_sha256")
        != expected_receipt_sha
        or prior_binding.get("archive_tree_sha256")
        != prior_receipt[0]["tree_sha256"]
    ):
        raise SystemExit("resume lineage omitted prior failure authority")
PY

stop_loopback_upstream
python3 - "$case_root/upstream.requests.json" <<'PY'
import json
import pathlib
import sys

requests = json.loads(pathlib.Path(sys.argv[1]).read_text())
stages = [item["stage"] for item in requests]
if stages.count("generate") != 3:
    raise SystemExit("sealed resume reran or skipped generation")
if stages.count("history-compare") < 1:
    raise SystemExit("observed replay never invoked the comparator")
if stages.count("review") != 5:
    raise SystemExit("review matrix or sealed resume invocation count drifted")
comparison_behaviors = {
    item["comparison_status"]
    for item in requests
    if item["stage"] == "history-compare"
}
if comparison_behaviors != {"uncertain", "complete_no_match"}:
    raise SystemExit("loopback comparator control did not exercise A/B")
review_modes = [
    item["review_mode"]
    for item in requests
    if item["stage"] == "review"
]
if review_modes.count("invalid") != 2 or review_modes.count("valid") != 3:
    raise SystemExit("loopback review failure sequence drifted")
if any(
    item["path"] != "/v1/responses"
    or item["authorization"] != "Bearer offline-access"
    or item["account_id"] != "offline-account"
    for item in requests
):
    raise SystemExit("canonical exchange escaped the local fixture contract")
PY

[ "$fresh_start_failed" -eq 0 ] || {
  printf 'history_runtime_smoke: fresh shadow startup requires a missing optional near-SA snapshot\n' >&2
  cat "$shadow_log" >&2
  exit 1
}
[ "$relative_observation_failed" -eq 0 ] || {
  printf 'history_runtime_smoke: relative observation paths cannot be replayed by seal-selection\n' >&2
  exit 1
}

printf 'ok: bounded history runtime contract\n'
