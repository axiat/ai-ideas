#!/usr/bin/env bash
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
MODE=${1:-default}
case "$MODE" in
  default|overlap-commentary|missing-occupant) ;;
  *) printf 'usage: runtime_abi_smoke.sh [default|overlap-commentary|missing-occupant]\n' >&2; exit 64 ;;
esac
TEMP_BASE=${TMPDIR:-/tmp}
TEMP_BASE=${TEMP_BASE%/}
SANDBOX_ROOT=$(mktemp -d "$TEMP_BASE/ai-ideas-runtime.XXXXXX")

cleanup() {
  case "$SANDBOX_ROOT" in
    "$TEMP_BASE"/ai-ideas-runtime.*) rm -rf -- "$SANDBOX_ROOT" ;;
    *) printf 'Refusing to remove unexpected path: %s\n' "$SANDBOX_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

REPO="$SANDBOX_ROOT/repo"
PATCH_FILE="$SANDBOX_ROOT/current.diff"
BEFORE_LEDGER="$SANDBOX_ROOT/ledger.before.tsv"
HEAD_COMMIT=$(git -C "$ROOT" rev-parse HEAD)

git clone -q --no-hardlinks "$ROOT" "$REPO"
git -C "$REPO" checkout -q --detach "$HEAD_COMMIT"
git -C "$ROOT" diff --binary HEAD -- > "$PATCH_FILE"
if [ -s "$PATCH_FILE" ]; then
  git -C "$REPO" apply --binary "$PATCH_FILE"
fi
cp "$ROOT/tests/fake_agent.sh" "$REPO/tests/fake_agent.sh"
chmod 755 "$REPO/tests/fake_agent.sh"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'mkdir -p tmp' \
  "printf '%s\\n' 'publication-no-op' >> tmp/publication.noop" > "$REPO/publish.sh"
chmod 755 "$REPO/publish.sh"

prepare_awr_case() {
  awk -F'\t' '
    NR == 1 { print; next }
    !found && $2 == "hunt" && $5 == "accept-w-rev" {
      print
      found = 1
      exit
    }
    END { if (!found) exit 1 }
  ' "$BEFORE_LEDGER" > "$REPO/ledger.tsv"
  rm -f "$REPO/tmp/ledger.good"
  rm -rf "$REPO/tmp/awr-side" "$REPO/tmp/awr-side.lock"
}

run_awr_case() {
  local status=$1 mode=${2:-awr-$1} candidate final="" task last
  prepare_awr_case
  (
    cd "$REPO"
    SIDE_CMD=tests/fake_agent.sh \
    SIDE_RESEARCH_CMD=tests/fake_agent.sh \
    SIDE_PRIORWORK_CMD=tests/fake_agent.sh \
    SIDE_JUDGE_CMD=tests/fake_agent.sh \
    SIDE_POLL_SEC=0 \
    SIDE_MAX_ROUNDS=1 \
    SIDE_MAX_BAD=1 \
    SIDE_GAP_SEC=0 \
    SIDE_GAP_MIN_SEC=0 \
    SIDE_GAP_MAX_SEC=0 \
    SIDE_COOLDOWN_SEC=0 \
    FAKE_AGENT_MODE="$mode" \
    bash ./awr-side.sh
  )

  for candidate in "$REPO/tmp/awr-side/awr/"*.md; do
    [ -e "$candidate" ] || continue
    case "$candidate" in
      *.task.md|*.draft.md|*.priorwork.md|*.judge.md) continue ;;
    esac
    [ -z "$final" ] || {
      printf 'multiple AwR final artifacts: %s and %s\n' "$final" "$candidate" >&2
      return 1
    }
    final=$candidate
  done
  [ -n "$final" ] && [ -s "$final" ]
  grep -qxF '## Revised Idea' "$final"
  grep -q '^Strongest Counterexample:' "$final"
  grep -qxF "Decision: $([ "$status" = ready ] && printf 'SA-possible' || printf 'not-ready')" "$final"
  grep -qxF "Status: $status" "$final"
  grep -q '^Outcome: ' "$final"
  last=$(grep -v '^[[:space:]]*$' "$final" | tail -1)
  [ "$last" = 'AGY-DONE' ]

  if [ "$status" = not-ready ]; then
    task=${final%.md}.task.md
    grep -qxF -- '- Defect: Add a latency control that separates gating overhead from skipped world-model inference.' "$final"
    grep -qxF '## Reviewer Feedback' "$task"
    grep -qxF 'Round: 1' "$task"
  fi
  printf 'ok: AwR %s ABI smoke\n' "$status"
}

run_awr_reject_case() {
  local mode=$1 phase=$2 candidate
  prepare_awr_case
  if (
    cd "$REPO"
    SIDE_CMD=tests/fake_agent.sh \
    SIDE_RESEARCH_CMD=tests/fake_agent.sh \
    SIDE_PRIORWORK_CMD=tests/fake_agent.sh \
    SIDE_JUDGE_CMD=tests/fake_agent.sh \
    SIDE_POLL_SEC=0 \
    SIDE_MAX_ROUNDS=1 \
    SIDE_MAX_BAD=1 \
    SIDE_GAP_SEC=0 \
    SIDE_GAP_MIN_SEC=0 \
    SIDE_GAP_MAX_SEC=0 \
    SIDE_COOLDOWN_SEC=0 \
    FAKE_AGENT_MODE="$mode" \
    bash ./awr-side.sh
  ); then
    printf 'AwR unexpectedly accepted invalid %s output (%s)\n' "$phase" "$mode" >&2
    return 1
  fi

  for candidate in "$REPO/tmp/awr-side/awr/"*.md; do
    [ -e "$candidate" ] || continue
    case "$candidate" in
      *.task.md|*.draft.md|*.priorwork.md|*.judge.md) continue ;;
    esac
    printf 'AwR created a terminal artifact from invalid %s output: %s\n' "$phase" "$candidate" >&2
    return 1
  done
  printf 'ok: AwR rejects %s ABI violation\n' "$phase"
}

run_awr_agy_case() {
  local stub="$SANDBOX_ROOT/bin/agy" log="$SANDBOX_ROOT/agy.args" candidate final=""
  mkdir -p "$SANDBOX_ROOT/bin"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -eu' \
    ': "${AGY_STUB_LOG:?}" "${FAKE_AGENT_BIN:?}"' \
    'prompt=' \
    'while [ "$#" -gt 0 ]; do' \
    '  printf "%s\\n" "$1" >> "$AGY_STUB_LOG"' \
    '  if [ "$1" = "-p" ]; then' \
    '    shift' \
    '    [ "$#" -gt 0 ]' \
    '    prompt=$1' \
    '  fi' \
    '  shift' \
    'done' \
    '[ -n "$prompt" ]' \
    'exec "$FAKE_AGENT_BIN" "$prompt"' > "$stub"
  chmod 755 "$stub"

  prepare_awr_case
  (
    cd "$REPO"
    PATH="$SANDBOX_ROOT/bin:$PATH" \
    AGY_STUB_LOG="$log" \
    FAKE_AGENT_BIN="$REPO/tests/fake_agent.sh" \
    SIDE_CMD=agy \
    SIDE_POLL_SEC=0 \
    SIDE_MAX_ROUNDS=1 \
    SIDE_MAX_BAD=1 \
    SIDE_GAP_SEC=0 \
    SIDE_GAP_MIN_SEC=0 \
    SIDE_GAP_MAX_SEC=0 \
    SIDE_COOLDOWN_SEC=0 \
    FAKE_AGENT_MODE=awr-ready \
    bash ./awr-side.sh
  )

  for candidate in "$REPO/tmp/awr-side/awr/"*.md; do
    [ -e "$candidate" ] || continue
    case "$candidate" in
      *.task.md|*.draft.md|*.priorwork.md|*.judge.md) continue ;;
    esac
    [ -z "$final" ] || return 1
    final=$candidate
  done
  [ -n "$final" ]
  grep -qxF 'Status: ready' "$final"
  [ "$(grep -cxF -- '--model' "$log")" -eq 3 ]
  [ "$(grep -cxF -- '--add-dir' "$log")" -eq 3 ]
  [ "$(grep -cxF -- '--print-timeout' "$log")" -eq 3 ]
  [ "$(grep -cxF -- '-p' "$log")" -eq 3 ]
  printf 'ok: explicit SIDE_CMD=agy built-in ABI smoke\n'
}

write_awr_alias_fixture() {
  printf 'ledger_row\tlegacy_key\n2\tabc123def456\n' > "$REPO/awr-state-aliases.tsv"
}

run_awr_legacy_terminal_case() {
  local old="$REPO/tmp/awr-side/awr/abc123def456.md"
  local stable="$REPO/tmp/awr-side/awr/r000002.md"
  prepare_awr_case
  write_awr_alias_fixture
  mkdir -p "$(dirname "$old")"
  printf '%s\n' '# Historical terminal result' > "$old"
  (
    cd "$REPO"
    SIDE_CMD=false SIDE_POLL_SEC=0 SIDE_MAX_ROUNDS=1 SIDE_MAX_BAD=1 \
    SIDE_GAP_SEC=0 SIDE_GAP_MIN_SEC=0 SIDE_GAP_MAX_SEC=0 SIDE_COOLDOWN_SEC=0 \
    bash ./awr-side.sh
  )
  [ -s "$stable" ]
  [ -s "$old" ]
  printf 'ok: AwR preserves terminal state across stable-key migration\n'
}

run_awr_legacy_partial_case() {
  local base="$REPO/tmp/awr-side/awr/abc123def456"
  local stable="$REPO/tmp/awr-side/awr/r000002"
  prepare_awr_case
  write_awr_alias_fixture
  mkdir -p "$(dirname "$base")"
  printf '%s\n' \
    '# Historical AwR task' \
    'Date: 2026-07-01' \
    '## Historical reviewer record' \
    '- Historical defect: Preserve the recovered latency control.' > "$base.task.md"
  printf '%s\n' 'stale draft without the artifact ABI' > "$base.draft.md"
  printf '%s\n' 'stale prior work without the artifact ABI' > "$base.priorwork.md"
  cp "$base.task.md" "$stable.task.md"  # Simulate interruption after the compatibility copy.
  (
    cd "$REPO"
    SIDE_CMD=tests/fake_agent.sh \
    SIDE_RESEARCH_CMD=tests/fake_agent.sh \
    SIDE_PRIORWORK_CMD=tests/fake_agent.sh \
    SIDE_JUDGE_CMD=tests/fake_agent.sh \
    SIDE_POLL_SEC=0 SIDE_MAX_ROUNDS=1 SIDE_MAX_BAD=1 \
    SIDE_GAP_SEC=0 SIDE_GAP_MIN_SEC=0 SIDE_GAP_MAX_SEC=0 SIDE_COOLDOWN_SEC=0 \
    FAKE_AGENT_MODE=awr-ready \
    bash ./awr-side.sh
  )
  [ -s "$stable.md" ]
  grep -qxF 'Status: not-ready' "$stable.md"
  grep -qxF '## Reviewer Feedback' "$stable.task.md"
  grep -qxF 'Round: 1' "$stable.task.md"
  grep -qxF -- '- Defect: Preserve the recovered latency control.' "$stable.task.md"
  grep -qxF '## Revised Idea' "$stable.draft.md"
  if grep -qF 'stale prior work without the artifact ABI' "$stable.md"; then
    printf 'AwR terminal artifact reused invalid migrated prior work\n' >&2
    return 1
  fi
  [ -s "$base.task.md" ]
  printf 'ok: AwR upgrades partial legacy state without reusing invalid caches\n'
}

run_compact_review_contract_case() {
  python3 - "$REPO" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from lib import history_runtime

ballot = {
    "candidate_id": "I1",
    "verdict": "strong-accept",
    "major_count": 0,
    "reason": "Independent evidence supports a clear-accept contribution.",
}
valid = "\n".join(
    [
        "# I1",
        "Verdict: strong-accept",
        "CRITICAL: 0",
        "MAJOR: 0",
        "Headline: Bounded sparse control remains decisive.",
        "Occupation: No single occupant covers the full claim.",
        "Experiment: 128-episode one-H100 comparison with kill thresholds.",
        "Estimand: Control success and latency under sparse updates.",
        "Payoff: Lower inference cost without control collapse.",
        "Feasibility: One researcher and one H100.",
        "History: complete_no_match within the sealed watermark.",
        "Reason: Independent evidence supports a clear-accept contribution.",
        "",
    ]
).encode("utf-8")
history_runtime._validate_compact_review(valid, ballot)

invalid_cases = [
    valid.replace(b"# I1\n", b"# I2\n"),
    valid.replace(b"MAJOR: 0\n", b"MAJOR: 2\n"),
    valid.replace(b"Verdict: strong-accept\n", b"Verdict: reject\n"),
    b"# I1\nVerdict: strong-accept\n",
]
for raw in invalid_cases:
    try:
        history_runtime._validate_compact_review(raw, ballot)
    except history_runtime.RuntimeContractError:
        continue
    raise SystemExit("compact review accepted an invalid artifact")
print("ok: Strong Accept compact review contract")
PY
}

assert_history_cutover_surface() {
  grep -q '^history_sync()' "$REPO/hunt.sh"
  grep -q '^run_contained_stage()' "$REPO/hunt.sh"
  grep -q '^history_compare_targets()' "$REPO/hunt.sh"
  grep -q '^history_seal_resume_attempt()' "$REPO/hunt.sh"
  grep -q '^history_materialize_ledger()' "$REPO/hunt.sh"
  grep -q 'lib/history_runtime.py' "$REPO/hunt.sh"
  grep -q 'lib/history_archive.py' "$REPO/hunt.sh"
  if grep -Eq 'META_EVERY|META_MIN_REJECTS|Read roles/meta\.md and follow it|Read roles/generate\.md and follow it' \
    "$REPO/hunt.sh"; then
    printf 'legacy meta/generate external path remains in hunt.sh\n' >&2
    return 1
  fi
  test ! -e "$REPO/roles/bounded-generate.md"
  test ! -e "$REPO/roles/bounded-meta.md"
  test ! -e "$REPO/roles/bounded-review.md"
  printf 'ok: history cutover surface\n'
}

cp "$REPO/ledger.tsv" "$BEFORE_LEDGER"
run_compact_review_contract_case
assert_history_cutover_surface

# Full contained hunt path with fake backends lives in
# tests/history_runtime_smoke.sh. This smoke keeps AwR ABI coverage and the
# compact review/cutover surface checks above.
if [ "$MODE" = default ]; then
  run_awr_case ready
  run_awr_case ready awr-no-crack
  run_awr_case not-ready
  run_awr_reject_case awr-four-neighbors prior-work-neighbor-count
  run_awr_reject_case awr-crack-url-count prior-work-section-scope
  run_awr_reject_case awr-non-api-query prior-work-query-domain
  run_awr_reject_case awr-api-host-prefix prior-work-query-host-boundary
  run_awr_reject_case awr-api-path-prefix prior-work-query-path-boundary
  run_awr_reject_case awr-api-bare-host prior-work-query-endpoint
  run_awr_reject_case awr-query-in-neighbors prior-work-query-neighbor-separation
  run_awr_reject_case awr-reversed-sections prior-work-section-order
  run_awr_reject_case awr-invalid-verification prior-work
  run_awr_reject_case awr-mixed-verification prior-work-mixed-token
  run_awr_reject_case awr-mixed-decision judge
  run_awr_agy_case
  run_awr_legacy_terminal_case
  run_awr_legacy_partial_case
fi
printf 'ok: runtime ABI smoke (%s)\n' "$MODE"
