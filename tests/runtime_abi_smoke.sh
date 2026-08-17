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
    'expected_repo=$PWD' \
    '[ "$#" -eq 8 ]' \
    '[ "$1" = "--model" ]' \
    '[ "$2" = "gemini-3.6-flash-high" ]' \
    '[ "$3" = "--add-dir" ]' \
    '[ "$4" = "$expected_repo" ]' \
    '[ "$5" = "--print-timeout" ]' \
    '[ "$6" = "10m" ]' \
    '[ "$7" = "-p" ]' \
    'prompt=$8' \
    'printf "%s\\n" "$@" >> "$AGY_STUB_LOG"' \
    'case "$prompt" in' \
    '  "Repository root (absolute path): $expected_repo."*) ;;' \
    '  *) exit 1 ;;' \
    'esac' \
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
  local outcome old stable status decision before after candidate
  for outcome in ready not-ready; do
    old="$REPO/tmp/awr-side/awr/abc123def456.md"
    stable="$REPO/tmp/awr-side/awr/r000002.md"
    case "$outcome" in
      ready)
        status='达标(裁判判 SA-可能,第 2 轮)'
        decision='SA-可能'
        ;;
      not-ready)
        status='未达标(3 轮反馈用尽;末节裁判意见针对修订前草稿,缺陷已回灌并修订)'
        decision='还不行'
        ;;
    esac
    prepare_awr_case
    write_awr_alias_fixture
    mkdir -p "$(dirname "$old")"
    printf '%s\n' \
      '# AwR 复活成品 abc123def456' \
      "- 状态: $status" \
      '- 原始 idea: 兼容迁移必须保留完整历史成品。' \
      '- 过程档: abc123def456.task.md(含历轮反馈)' \
      '' \
      '## 修订版 idea' \
      '保留旧成品，同时为 stable row 写入可信 terminal 标记。' \
      '## 检索记录' \
      '- https://example.com/legacy-one' \
      '- https://example.com/legacy-two' \
      '- https://example.com/legacy-three' \
      '## 回应' \
      '迁移不应重新调用 agent。' \
      'AGY-DONE' \
      '' \
      '---' \
      '## 最后裁判意见' \
      "判定: $decision" \
      'AGY-DONE' > "$old"
    before=$(shasum -a 256 "$old")
    for candidate in first second; do
      (
        cd "$REPO"
        SIDE_CMD=false SIDE_POLL_SEC=0 SIDE_MAX_ROUNDS=1 SIDE_MAX_BAD=1 \
        SIDE_GAP_SEC=0 SIDE_GAP_MIN_SEC=0 SIDE_GAP_MAX_SEC=0 SIDE_COOLDOWN_SEC=0 \
        bash ./awr-side.sh
      )
      [ "$(grep -cve '^[[:space:]]*$' "$stable")" -eq 1 ]
      grep -qxF '# Historical terminal result' "$stable"
      [ ! -e "$REPO/tmp/awr-side/awr/r000002.final.bad1" ]
      [ ! -e "$REPO/tmp/awr-side/awr/r000002.task.md" ]
    done
    after=$(shasum -a 256 "$old")
    [ "$before" = "$after" ]
  done
  printf 'ok: AwR canonicalizes complete legacy terminals without agent calls\n'
}

run_awr_truncated_legacy_terminal_case() {
  local old="$REPO/tmp/awr-side/awr/abc123def456.md"
  local stable="$REPO/tmp/awr-side/awr/r000002.md"
  local before
  prepare_awr_case
  write_awr_alias_fixture
  mkdir -p "$(dirname "$old")"
  printf '%s\n' \
    '# AwR 复活成品 abc123def456' \
    '- 状态: 达标(裁判判 SA-可能,第 1 轮)' > "$old"
  before=$(shasum -a 256 "$old")
  (
    cd "$REPO"
    SIDE_CMD=false SIDE_POLL_SEC=0 SIDE_MAX_ROUNDS=1 SIDE_MAX_BAD=0 \
    SIDE_GAP_SEC=0 SIDE_GAP_MIN_SEC=0 SIDE_GAP_MAX_SEC=0 SIDE_COOLDOWN_SEC=0 \
    bash ./awr-side.sh
  )
  [ ! -e "$stable" ]
  [ ! -e "$REPO/tmp/awr-side/awr/r000002.final.bad1" ]
  [ ! -e "$REPO/tmp/awr-side/awr/r000002.task.md" ]
  [ "$before" = "$(shasum -a 256 "$old")" ]
  printf '%s\n' '# malformed legacy terminal' 'partial bytes' > "$old"
  before=$(shasum -a 256 "$old")
  (
    cd "$REPO"
    SIDE_CMD=false SIDE_POLL_SEC=0 SIDE_MAX_ROUNDS=1 SIDE_MAX_BAD=0 \
    SIDE_GAP_SEC=0 SIDE_GAP_MIN_SEC=0 SIDE_GAP_MAX_SEC=0 SIDE_COOLDOWN_SEC=0 \
    bash ./awr-side.sh
  )
  [ ! -e "$stable" ]
  [ ! -e "$REPO/tmp/awr-side/awr/r000002.final.bad1" ]
  [ "$before" = "$(shasum -a 256 "$old")" ]
  printf '%s\n' \
    '# AwR 复活成品 abc123def456' \
    '- 状态: 达标(裁判判 SA-可能,第 1 轮)' \
    '- 原始 idea: 伪造的分隔顺序不能成为 terminal。' \
    '- 过程档: abc123def456.task.md(含历轮反馈)' \
    '## 修订版 idea' '内容' \
    '## 检索记录' \
    '- https://example.com/one' '- https://example.com/two' '- https://example.com/three' \
    '## 回应' '内容' 'AGY-DONE' '---' '---' \
    '## 最后裁判意见' '判定: SA-可能' 'AGY-DONE' 'AGY-DONE' > "$old"
  (
    cd "$REPO"
    SIDE_CMD=false SIDE_POLL_SEC=0 SIDE_MAX_ROUNDS=1 SIDE_MAX_BAD=0 \
    SIDE_GAP_SEC=0 SIDE_GAP_MIN_SEC=0 SIDE_GAP_MAX_SEC=0 SIDE_COOLDOWN_SEC=0 \
    bash ./awr-side.sh
  )
  [ ! -e "$stable" ]
  printf '%s\n' \
    '# AwR 复活成品 abc123def456' \
    '- 状态: 达标(裁判判 SA-可能,第 1 轮)' \
    '- 原始 idea: 裁判结论必须来自裁判小节。' \
    '- 过程档: abc123def456.task.md(含历轮反馈)' \
    '## 修订版 idea' '内容' \
    '## 检索记录' \
    '- https://example.com/one' '- https://example.com/two' '- https://example.com/three' \
    '## 回应' '判定: SA-可能' 'AGY-DONE' '---' \
    '## 最后裁判意见' '判定: 还不行' 'AGY-DONE' > "$old"
  (
    cd "$REPO"
    SIDE_CMD=false SIDE_POLL_SEC=0 SIDE_MAX_ROUNDS=1 SIDE_MAX_BAD=0 \
    SIDE_GAP_SEC=0 SIDE_GAP_MIN_SEC=0 SIDE_GAP_MAX_SEC=0 SIDE_COOLDOWN_SEC=0 \
    bash ./awr-side.sh
  )
  [ ! -e "$stable" ]
  printf 'ok: AwR rejects truncated legacy terminals\n'
}

run_awr_newer_stable_work_case() {
  local out="$REPO/tmp/awr-side/awr"
  local old="$out/abc123def456.md"
  local stable="$out/r000002.md"
  local draft="$out/r000002.draft.md"
  local before
  prepare_awr_case
  write_awr_alias_fixture
  mkdir -p "$out"
  printf '%s\n' \
    '# AwR 复活成品 abc123def456' \
    '- 状态: 达标(裁判判 SA-可能,第 1 轮)' \
    '- 原始 idea: 旧裁判认为可接受。' \
    '- 过程档: abc123def456.task.md(含历轮反馈)' \
    '## 修订版 idea' \
    '旧修订内容。' \
    '## 检索记录' \
    '- https://example.com/legacy-one' \
    '- https://example.com/legacy-two' \
    '- https://example.com/legacy-three' \
    '## 回应' \
    '旧回应。' \
    'AGY-DONE' \
    '---' \
    '## 最后裁判意见' \
    '判定: SA-可能' \
    'AGY-DONE' > "$old"
  printf '%s\n' \
    '## Revised Idea' \
    'A newer stable revision must remain resumable.' \
    '## Search Record' \
    '- https://example.com/current-one' \
    '- https://example.com/current-two' \
    '- https://example.com/current-three' \
    'AGY-DONE' > "$draft"
  python3 - "$old" "$draft" <<'PY'
import os
import sys

os.utime(sys.argv[1], ns=(1_700_000_000_000_000_000,) * 2)
os.utime(sys.argv[2], ns=(1_700_000_001_000_000_000,) * 2)
PY
  before=$(shasum -a 256 "$old")
  (
    cd "$REPO"
    SIDE_CMD=false SIDE_POLL_SEC=0 SIDE_MAX_ROUNDS=1 SIDE_MAX_BAD=0 \
    SIDE_GAP_SEC=0 SIDE_GAP_MIN_SEC=0 SIDE_GAP_MAX_SEC=0 SIDE_COOLDOWN_SEC=0 \
    bash ./awr-side.sh
  )
  [ ! -e "$stable" ]
  grep -qxF '## Revised Idea' "$draft"
  [ "$before" = "$(shasum -a 256 "$old")" ]
  printf 'ok: AwR preserves newer stable work without a judge\n'
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

run_nondefault_mode_case() {
  local helper="$SANDBOX_ROOT/priorwork-helper.sh"
  rm -rf "$REPO/tmp/round"
  mkdir -p "$REPO/tmp/round"
  case "$MODE" in
    overlap-commentary)
      printf 'I1\t1\tFixture story\tWorld Models - Architecture\n' \
        > "$REPO/tmp/round/ideas.tsv"
      (
        cd "$REPO"
        FAKE_AGENT_MODE="$MODE" \
          tests/fake_agent.sh 'Read roles/research.md and follow it'
      )
      grep -qxF 'Overlap: unknown; high appears only in commentary' \
        "$REPO/tmp/round/priorwork.md"
      awk '
        /^priorwork_ok\(\) \{/ { copy=1 }
        /^cracks_ok\(\) \{/ { exit }
        copy { print }
      ' "$REPO/hunt.sh" > "$helper"
      if (
        cd "$REPO"
        RD=tmp/round
        PRIOR_MIN_LINKS=5
        PRIOR_MIN_API=1
        STRUCTURED_API_RE='^- Query: https?://'
        log() { :; }
        . "$helper"
        priorwork_ok
      ); then
        printf 'overlap-commentary fixture passed the current prior-work helper\n' >&2
        return 1
      fi
      ;;
    missing-occupant)
      (
        cd "$REPO"
        FAKE_AGENT_MODE="$MODE" \
          tests/fake_agent.sh 'Read roles/prescreen.md and follow it'
      )
      python3 - "$REPO" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from lib import history_runtime

text = (root / "tmp/round/prescreen.md").read_text(encoding="utf-8")
if "Decision: kill" not in text or "Occupant:" in text:
    raise SystemExit("missing-occupant fixture did not execute")
result = history_runtime._prescreen_result(text, "I1")
if result != {"decision": "keep", "evidence": None}:
    raise SystemExit("missing-occupant fixture bypassed the current prescreen helper")
PY
      ;;
    *) return 0 ;;
  esac
  printf 'ok: runtime ABI %s case\n' "$MODE"
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
  run_awr_reject_case awr-empty-counterexample prior-work-empty-counterexample
  run_awr_reject_case awr-invalid-verification prior-work
  run_awr_reject_case awr-mixed-verification prior-work-mixed-token
  run_awr_reject_case awr-verification-missing-url prior-work-verification-url
  run_awr_reject_case awr-verification-missing-description prior-work-verification-description
  run_awr_reject_case awr-duplicate-verification-heading prior-work-verification-heading
  run_awr_reject_case awr-verification-without-heading prior-work-verification-section
  run_awr_reject_case awr-mixed-decision judge
  run_awr_agy_case
  run_awr_legacy_terminal_case
  run_awr_truncated_legacy_terminal_case
  run_awr_newer_stable_work_case
  run_awr_legacy_partial_case
else
  run_nondefault_mode_case
fi
printf 'ok: runtime ABI smoke (%s)\n' "$MODE"
