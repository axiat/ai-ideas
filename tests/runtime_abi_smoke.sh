#!/usr/bin/env bash
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
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
grep -q '^Papers Read: 5$' "$REPO/tmp/round/priorwork.md"
EXPECTED_VERDICT=$(printf 'I1\tstrong-accept\t0\tIndependent evidence supports a clear-accept contribution under the stated experiment.')
grep -qxF "$EXPECTED_VERDICT" "$REPO/tmp/round/rev/1/verdict.tsv"

AFTER_LINES=$(wc -l < "$REPO/ledger.tsv" | tr -d ' ')
[ "$AFTER_LINES" -eq $((BEFORE_LINES + 1)) ]
cmp -s "$BEFORE_LEDGER" <(head -n "$BEFORE_LINES" "$REPO/ledger.tsv")
awk -F'\t' '
  END {
    if (NF != 8) exit 1
    if ($2 != "hunt") exit 1
    if ($3 != "World Models - Architecture") exit 1
    if ($5 != "strong-accept") exit 1
    if ($7 != "low") exit 1
    if ($8 != "-") exit 1
  }
' "$REPO/ledger.tsv"
EXPECTED_LEDGER_ROW=$(printf '%s\thunt\tWorld Models - Architecture\tConstraint-Driven Sparse World Models\tstrong-accept\tIndependent evidence supports a clear-accept contribution under the stated experiment.\tlow\t-' "$TODAY")
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

grep -q '^publication-no-op$' "$REPO/tmp/publication.noop"
printf 'ok: runtime ABI smoke\n'
