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
for section in 1 2 3 4 5 6 7 8; do
  grep -q "^### ${section}\." "$REPO/tmp/round/rev/1/review.md"
done
grep -qxF '| # | Flaw | Severity | Defense |' "$REPO/tmp/round/rev/1/review.md"
grep -qxF "| Aspect | User's input | Assessment |" "$REPO/tmp/round/rev/1/review.md"
grep -qxF '| Dimension | Score 1-10 | Evidence | Lift suggestion |' "$REPO/tmp/round/rev/1/review.md"
grep -qxF '| Probe | Yes or No | Rationale |' "$REPO/tmp/round/rev/1/review.md"
grep -qxF '| Risk | Level | Mitigation |' "$REPO/tmp/round/rev/1/review.md"
awk -F'|' '
  function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
  $0 == "### 3. Lifecycle and capability match" { section=3; next }
  $0 == "### 4. Five-dimension radar" { section=4; next }
  $0 == "### 5. Paradigm-shift probe" { section=5; next }
  $0 == "### 6. Feasibility" { section=6; next }
  $0 ~ /^### [1-8]\./ { section=0; next }
  section == 3 && /^\|/ {
    key=trim($2)
    if (key == "Idea category" || key == "Lifecycle" || key == "Weekly effective hours" || key == "Fit") lifecycle[key]++
  }
  section == 4 && /^\|/ {
    key=trim($2); score=trim($3)
    if (key == "Higher" || key == "Faster" || key == "Stronger" || key == "Cheaper" || key == "Broader") {
      if (score !~ /^([1-9]|10)$/) exit 1
      dimensions[key]++
    }
  }
  section == 5 && /^\|/ {
    key=trim($2); answer=trim($3)
    if (key == "First Principles" || key == "Elephant in the Room" || key == "Technology Cycle" || key == "Hamming\047s Rule") {
      if (answer != "Yes" && answer != "No") exit 1
      probes[key]++
    }
  }
  section == 6 && /^\|/ {
    key=trim($2)
    if (key == "Compute" || key == "Data" || key == "Engineering" || key == "Timeline") risks[key]++
  }
  END {
    if (lifecycle["Idea category"] != 1 || lifecycle["Lifecycle"] != 1 || lifecycle["Weekly effective hours"] != 1 || lifecycle["Fit"] != 1) exit 1
    if (dimensions["Higher"] != 1 || dimensions["Faster"] != 1 || dimensions["Stronger"] != 1 || dimensions["Cheaper"] != 1 || dimensions["Broader"] != 1) exit 1
    if (probes["First Principles"] != 1 || probes["Elephant in the Room"] != 1 || probes["Technology Cycle"] != 1 || probes["Hamming\047s Rule"] != 1) exit 1
    if (risks["Compute"] != 1 || risks["Data"] != 1 || risks["Engineering"] != 1 || risks["Timeline"] != 1) exit 1
  }
' "$REPO/tmp/round/rev/1/review.md"
awk '
  $0 == "Top three actions to take first:" { actions=1; next }
  actions && /^[1-3]\. / {
    count++
    if (substr($0, 1, 1) != count) exit 1
  }
  END { exit !(count == 3) }
' "$REPO/tmp/round/rev/1/review.md"

EXPECTED_OVERLAP=low
if [ "$MODE" = "overlap-commentary" ]; then
  EXPECTED_OVERLAP=unknown
  grep -qxF 'Overlap: unknown; high appears only in commentary' "$REPO/tmp/round/priorwork.md"
fi
if [ "$MODE" = "missing-occupant" ]; then
  grep -qxF 'Decision: kill' "$REPO/tmp/round/prescreen.md"
  ! grep -q '^Occupant:' "$REPO/tmp/round/prescreen.md"
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
printf 'ok: runtime ABI smoke (%s)\n' "$MODE"
