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

cp "$REPO/ledger.tsv" "$BEFORE_LEDGER"
BEFORE_LINES=$(wc -l < "$BEFORE_LEDGER" | tr -d ' ')
TODAY=$(date +%F)
EXISTING_SA=$(awk -F'\t' -v d="$TODAY" '$1==d && $2=="hunt" && $5=="strong-accept"{n++} END{print n+0}' "$REPO/ledger.tsv")
SA_TARGET=$((EXISTING_SA + 1))
REPORTS_BEFORE=0
for report in "$REPO/ideas/${TODAY}"_hunt*.md; do
  [ -e "$report" ] || continue
  REPORTS_BEFORE=$((REPORTS_BEFORE + 1))
done

(
  cd "$REPO"
  AGENT_CMD=tests/fake_agent.sh \
  FRONT_CMD=tests/fake_agent.sh \
  BACK_CMD=tests/fake_agent.sh \
  REV_CMD_1=tests/fake_agent.sh \
  FAKE_AGENT_MODE="$MODE" \
  REVIEWERS=1 \
  RESUME_FRONT=0 \
  META_EVERY=1 \
  META_MIN_REJECTS=0 \
  THEME_MIN_LOW=0 \
  RESEARCH_RETRY=0 \
  FAIL_SLEEP_MIN=0 \
  NO_HIT_SLEEP_MIN_LO=0 \
  NO_HIT_SLEEP_MIN_HI=0 \
  ALLOW_ZERO_NO_HIT_SLEEP=1 \
  EMPTY_MAX=1 \
  MAX_FAILS=1 \
  SA_TARGET="$SA_TARGET" \
  RUNS_DIR="$SANDBOX_ROOT/runs" \
  bash ./hunt.sh
)

[ -s "$REPO/tmp/round/ideas.tsv" ]
grep -q '^Summary: ' "$REPO/tmp/round/ideas.md"
grep -q '^Why It May Be Novel: ' "$REPO/tmp/round/ideas.md"
awk -F'\t' 'NF==6 && $1=="I1" && $2==1{ok++} END{exit !(ok==1)}' "$REPO/tmp/round/select.tsv"
grep -q '^Papers Read: 5$' "$REPO/tmp/round/priorwork.md"
EXPECTED_VERDICT=$(printf 'I1\tstrong-accept\t0\tIndependent evidence supports a clear-accept contribution under the stated experiment.')
grep -qxF "$EXPECTED_VERDICT" "$REPO/tmp/round/rev/1/verdict.tsv"
REVIEW_PATH="$REPO/tmp/round/rev/1/review.md"
while IFS= read -r required_review_line; do
  grep -qxF -- "$required_review_line" "$REVIEW_PATH"
done <<'REVIEW_CONTRACT'
### 1. First impression
### 2. Fatal-flaws audit (early gate)
| # | Flaw | Severity | Defense |
| - | None identified in the supplied evidence. | - | No defense required. |
### 3. Lifecycle and capability match
| Aspect | User's input | Assessment |
| Idea category | Innovative Technique | Matches a bounded method contribution. |
| Lifecycle | 3 months | Fits the pilot and first-paper scope. |
| Weekly effective hours | 20 | Sufficient for the stated experiment. |
| Fit | One researcher and one H100 | Green |
### 4. Five-dimension radar
| Dimension | Score 1-10 | Evidence | Lift suggestion |
| Higher | 6 | The kill threshold bounds control-success loss at two points. | Report success confidence intervals. |
| Faster | 9 | The decisive experiment requires at least 30 percent lower latency. | Profile each control stage. |
| Stronger | 6 | Two crack-evidence checks support stable control under skipped updates. | Add drift stress tests. |
| Cheaper | 8 | Skipped latent updates directly reduce inference demand. | Report energy per episode. |
| Broader | 5 | Evidence covers one manipulation setting. | Defer broader claims until cross-task evidence exists. |
### 5. Paradigm-shift probe
| Probe | Yes or No | Rationale |
| First Principles | Yes | It tests whether fixed-rate latent updates are necessary. |
| Elephant in the Room | No | The evidence does not establish a field-wide avoidance pattern. |
| Technology Cycle | Yes | Confidence estimates make event-triggered updates executable. |
| Hamming's Rule | Yes | Reliable sparse updates would materially reduce deployment cost. |
Disruptive potential: possible.
### 6. Feasibility
| Risk | Level | Mitigation |
| Compute | Low | Run the 128-episode comparison on the stated one H100. |
| Data | Low | Use the stated held-out manipulation episodes. |
| Engineering | Low | Limit the first paper to the confidence gate and dense baseline. |
| Timeline | Low | Kill the idea when either explicit threshold fails. |
### 7. Integrity gate result
- Gate 1 through 8: pass
### 8. Verdict
**Strong Accept**
Top three actions to take first:
1. Implement the confidence gate and dense baseline under one profiler.
2. Run the 128-episode falsification experiment with fixed seeds.
3. Report success, latency, and energy against the stated kill thresholds.
REVIEW_CONTRACT
awk -F'|' '
  function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
  $0 == "### 3. Lifecycle and capability match" { section=3; next }
  $0 == "### 4. Five-dimension radar" { section=4; next }
  $0 == "### 5. Paradigm-shift probe" { section=5; next }
  $0 == "### 6. Feasibility" { section=6; next }
  $0 ~ /^### [1-8]\./ { section=0; next }
  section == 3 && /^\|/ {
    key=trim($2)
    if (key == "Idea category" || key == "Lifecycle" || key == "Weekly effective hours" || key == "Fit") {
      lifecycle[key]++
      lifecycle_order[++lifecycle_count]=key
    }
  }
  section == 4 && /^\|/ {
    key=trim($2); score=trim($3)
    if (key == "Higher" || key == "Faster" || key == "Stronger" || key == "Cheaper" || key == "Broader") {
      if (score !~ /^([1-9]|10)$/) exit 1
      dimensions[key]++
      dimension_order[++dimension_count]=key
    }
  }
  section == 5 && /^\|/ {
    key=trim($2); answer=trim($3)
    if (key == "First Principles" || key == "Elephant in the Room" || key == "Technology Cycle" || key == "Hamming\047s Rule") {
      if (answer != "Yes" && answer != "No") exit 1
      probes[key]++
      probe_order[++probe_count]=key
    }
  }
  section == 6 && /^\|/ {
    key=trim($2)
    if (key == "Compute" || key == "Data" || key == "Engineering" || key == "Timeline") {
      risks[key]++
      risk_order[++risk_count]=key
    }
  }
  END {
    if (lifecycle["Idea category"] != 1 || lifecycle["Lifecycle"] != 1 || lifecycle["Weekly effective hours"] != 1 || lifecycle["Fit"] != 1) exit 1
    if (lifecycle_order[1] != "Idea category" || lifecycle_order[2] != "Lifecycle" || lifecycle_order[3] != "Weekly effective hours" || lifecycle_order[4] != "Fit") exit 1
    if (dimensions["Higher"] != 1 || dimensions["Faster"] != 1 || dimensions["Stronger"] != 1 || dimensions["Cheaper"] != 1 || dimensions["Broader"] != 1) exit 1
    if (dimension_order[1] != "Higher" || dimension_order[2] != "Faster" || dimension_order[3] != "Stronger" || dimension_order[4] != "Cheaper" || dimension_order[5] != "Broader") exit 1
    if (probes["First Principles"] != 1 || probes["Elephant in the Room"] != 1 || probes["Technology Cycle"] != 1 || probes["Hamming\047s Rule"] != 1) exit 1
    if (probe_order[1] != "First Principles" || probe_order[2] != "Elephant in the Room" || probe_order[3] != "Technology Cycle" || probe_order[4] != "Hamming\047s Rule") exit 1
    if (risks["Compute"] != 1 || risks["Data"] != 1 || risks["Engineering"] != 1 || risks["Timeline"] != 1) exit 1
    if (risk_order[1] != "Compute" || risk_order[2] != "Data" || risk_order[3] != "Engineering" || risk_order[4] != "Timeline") exit 1
  }
' "$REVIEW_PATH"
awk '
  $0 == "Top three actions to take first:" { actions=1; next }
  actions && /^[1-3]\. / {
    count++
    if (substr($0, 1, 1) != count) exit 1
  }
  END { exit !(count == 3) }
' "$REVIEW_PATH"

EXPECTED_OVERLAP=low
if [ "$MODE" = "overlap-commentary" ]; then
  EXPECTED_OVERLAP=unknown
  grep -qxF 'Overlap: unknown; high appears only in commentary' "$REPO/tmp/round/priorwork.md"
fi
if [ "$MODE" = "missing-occupant" ]; then
  grep -qxF 'Decision: kill' "$REPO/tmp/round/prescreen.md"
  if grep -q '^Occupant:' "$REPO/tmp/round/prescreen.md"; then
    printf 'invalid missing-occupant fixture unexpectedly contained Occupant evidence\n' >&2
    exit 1
  fi
  [ ! -s "$REPO/tmp/round/kills.tsv" ]
  awk -F'\t' '$3=="failopen" && $4=="prescreen"{ok++} END{exit !(ok>=1)}' "$REPO/tmp/hunt.metrics.tsv"
fi

AFTER_LINES=$(wc -l < "$REPO/ledger.tsv" | tr -d ' ')
[ "$AFTER_LINES" -eq $((BEFORE_LINES + 1)) ]
cmp -s "$BEFORE_LEDGER" <(head -n "$BEFORE_LINES" "$REPO/ledger.tsv")
awk -F'\t' '
  END {
    if (NF != 8) exit 1
    if ($2 != "hunt") exit 1
    if ($3 != "World Models - Architecture") exit 1
    if ($5 != "strong-accept") exit 1
    if ($7 != expected_overlap) exit 1
    if ($8 != "-") exit 1
  }
' expected_overlap="$EXPECTED_OVERLAP" "$REPO/ledger.tsv"
EXPECTED_LEDGER_ROW=$(printf '%s\thunt\tWorld Models - Architecture\tConstraint-Driven Sparse World Models\tstrong-accept\tIndependent evidence supports a clear-accept contribution under the stated experiment.\t%s\t-' "$TODAY" "$EXPECTED_OVERLAP")
[ "$(tail -n 1 "$REPO/ledger.tsv")" = "$EXPECTED_LEDGER_ROW" ]

CALLS=$(tr '\n' ' ' < "$REPO/tmp/fake-agent.calls")
[ "$CALLS" = 'meta generate select prescreen research review report ' ]

[ -s "$REPO/tmp/fake-report.path" ]
REPORT_REL=$(sed -n '1p' "$REPO/tmp/fake-report.path")
case "$REPORT_REL" in ideas/"$TODAY"_hunt*.md) ;; *) exit 1 ;; esac
[ -s "$REPO/$REPORT_REL" ]
REPORTS_AFTER=0
for report in "$REPO/ideas/${TODAY}"_hunt*.md; do
  [ -e "$report" ] || continue
  REPORTS_AFTER=$((REPORTS_AFTER + 1))
done
[ "$REPORTS_AFTER" -eq $((REPORTS_BEFORE + 1)) ]
if rg -n --pcre2 '\p{Script=Han}' "$REPO/$REPORT_REL"; then
  exit 1
fi
grep -qxF 'The single independent reviewer returned Strong Accept.' "$REPO/$REPORT_REL"

IDEA_SOURCE="$SANDBOX_ROOT/idea.source"
IDEA_REPORT="$SANDBOX_ROOT/idea.report"
awk '
  $0 == "## I1" { copy=1; next }
  copy && /^## I[0-9]+$/ { exit }
  copy { print }
' "$REPO/tmp/round/ideas.md" > "$IDEA_SOURCE"
awk '
  $0 == "### I1" { copy=1; next }
  copy && $0 == "The single independent reviewer returned Strong Accept." { exit }
  copy { print }
' "$REPO/$REPORT_REL" > "$IDEA_REPORT"
cmp -s "$IDEA_SOURCE" "$IDEA_REPORT"

REVIEW_SOURCE="$SANDBOX_ROOT/review.source"
REVIEW_REPORT="$SANDBOX_ROOT/review.report"
awk '
  $0 == "## I1" { copy=1; next }
  copy && /^## I[0-9]+$/ { exit }
  copy { print }
' "$REPO/tmp/round/rev/1/review.md" > "$REVIEW_SOURCE"
awk '
  $0 == "### Reviewer 1 Full Review" { copy=1; next }
  copy && $0 == "### Directed Prior Work" {
    if (have && held != "") print held
    exit
  }
  copy {
    if (have) print held
    held=$0
    have=1
  }
' "$REPO/$REPORT_REL" > "$REVIEW_REPORT"
cmp -s "$REVIEW_SOURCE" "$REVIEW_REPORT"

PRIORWORK_SOURCE="$SANDBOX_ROOT/priorwork.source"
PRIORWORK_REPORT="$SANDBOX_ROOT/priorwork.report"
awk '
  $0 == "## I1" { copy=1 }
  copy && $0 != "## I1" && /^## I[0-9]+$/ { exit }
  copy { print }
' "$REPO/tmp/round/priorwork.md" > "$PRIORWORK_SOURCE"
awk '
  $0 == "### Directed Prior Work" { copy=1; next }
  copy && $0 == "## Rejected Ideas" {
    if (have && held != "") print held
    exit
  }
  copy {
    if (have) print held
    held=$0
    have=1
  }
' "$REPO/$REPORT_REL" > "$PRIORWORK_REPORT"
cmp -s "$PRIORWORK_SOURCE" "$PRIORWORK_REPORT"

METADATA_SOURCE="$SANDBOX_ROOT/metadata.source"
METADATA_REPORT="$SANDBOX_ROOT/metadata.report"
awk '/^(Rounds Attempted|Review Date): / { print }' "$REPO/tmp/round/meta.txt" > "$METADATA_SOURCE"
awk '$0 == "## Metadata" { copy=1; next } copy { print }' "$REPO/$REPORT_REL" > "$METADATA_REPORT"
cmp -s "$METADATA_SOURCE" "$METADATA_REPORT"

grep -q '^publication-no-op$' "$REPO/tmp/publication.noop"
if [ "$MODE" = default ]; then
  run_awr_case ready
  run_awr_case ready awr-no-crack
  run_awr_case not-ready
  run_awr_reject_case awr-invalid-verification prior-work
  run_awr_reject_case awr-mixed-verification prior-work-mixed-token
  run_awr_reject_case awr-mixed-decision judge
  run_awr_agy_case
fi
printf 'ok: runtime ABI smoke (%s)\n' "$MODE"
