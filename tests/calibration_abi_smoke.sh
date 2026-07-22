#!/usr/bin/env bash
set -eu

fake_backend() {
  mode=$CALIB_FAKE_MODE
  if [ -f tmp/out/ideas.md ]; then
    mkdir -p tmp/out
    case "$mode" in
      panel-valid)
        printf 'I1\taccept-w-rev\t1\tThe fixture retains one bounded major finding.\n' > tmp/out/verdict.tsv
        printf '%s\n' \
          '## I1' \
          'A complete fixture review.' \
          'not suspected published counterpart: Prefix Noise' \
          'suspected published counterpart:Suffix Noise' \
          'suspected published counterpart: Fixture Paper' > tmp/out/review.md
        ;;
      panel-malformed)
        printf 'I1\taccept-w-rev\tone\n' > tmp/out/verdict.tsv
        printf '%s\n' '## I1' 'A malformed fixture review.' > tmp/out/review.md
        ;;
      panel-empty-id-extra)
        printf 'I1\treject\t0\tThe fixture is directly occupied.\n' > tmp/out/verdict.tsv
        printf '\tstrong-accept\t0\tAn empty ID is not a blank line.\n' >> tmp/out/verdict.tsv
        ;;
      panel-missing-review)
        printf 'I1\taccept-w-rev\t1\tThe required review block is absent.\n' > tmp/out/verdict.tsv
        ;;
      panel-reject)
        printf 'I1\treject\t0\tThe fixture is directly occupied.\n' > tmp/out/verdict.tsv
        ;;
      *)
        printf 'unknown panel fake mode: %s\n' "$mode" >&2
        return 64
        ;;
    esac
    return 0
  fi

  if [ -f tmp/round/ideas.md ]; then
    mkdir -p tmp/round
    overlap='Overlap: high — Fixture Paper occupies the headline.'
    if [ "$mode" = "e2e-commentary" ]; then
      overlap='Overlap: unknown; high appears only in commentary'
    elif [ "$mode" != "e2e-valid" ]; then
      printf 'unknown e2e fake mode: %s\n' "$mode" >&2
      return 64
    fi
    printf '%s\n' \
      '## I1' \
      'Search Terms: fixture direct hit; fixture mechanism; adjacent fixture domain' \
      '- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=fixture-direct-hit' \
      'Nearest Work:' \
      '- Fixture Paper | https://arxiv.org/abs/2507.10543 | Direct hit | Occupies the headline.' \
      '- Neighbor Two | https://example.com/two | Neighbor | Leaves no relevant distinction.' \
      '- Neighbor Three | https://example.com/three | Neighbor | Leaves no relevant distinction.' \
      '- Neighbor Four | https://example.com/four | Neighbor | Leaves no relevant distinction.' \
      '- Neighbor Five | https://example.com/five | Neighbor | Leaves no relevant distinction.' \
      'Strongest Counterexample: Fixture Paper — It occupies the headline.' \
      "$overlap" \
      'Papers Read: 5' \
      'arXiv ID Check: yes' > tmp/round/priorwork.md
    return 0
  fi

  printf 'fake backend could not identify the calibration surface\n' >&2
  return 64
}

if [ -n "${CALIB_FAKE_MODE:-}" ]; then
  fake_backend
  exit $?
fi

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TEMP_BASE=${TMPDIR:-/tmp}
TEMP_BASE=${TEMP_BASE%/}
SANDBOX_ROOT=$(mktemp -d "$TEMP_BASE/ai-ideas-calibration.XXXXXX")

cleanup() {
  case "$SANDBOX_ROOT" in
    "$TEMP_BASE"/ai-ideas-calibration.*) rm -rf -- "$SANDBOX_ROOT" ;;
    *) printf 'Refusing to remove unexpected path: %s\n' "$SANDBOX_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

failures=0
record_failure() {
  printf 'not ok: %s\n' "$1" >&2
  failures=$((failures + 1))
}

REPO="$SANDBOX_ROOT/repo"
git clone -q --no-hardlinks "$ROOT" "$REPO"
rm -rf "$REPO/calib"
cp -R "$ROOT/calib" "$REPO/calib"
cp "$ROOT/tests/calibration_abi_smoke.sh" "$REPO/tests/"
cp "$ROOT/tests/verify_product_contract.py" "$REPO/tests/"
chmod 755 "$REPO/tests/calibration_abi_smoke.sh"

if ! (
  cd "$REPO"
  python3 tests/verify_product_contract.py fixtures > "$SANDBOX_ROOT/fixtures-baseline.out"
); then
  record_failure 'calibration evidence baseline'
fi
NUMERIC_FIXTURE="$REPO/calib/cases/neg-axiom-cosplay/ideas.md"
if ! grep -qF '10ms' "$NUMERIC_FIXTURE"; then
  record_failure 'numeric-unit fixture contains 10ms'
else
  sed 's/10ms/11ms/' "$NUMERIC_FIXTURE" > "$NUMERIC_FIXTURE.mutated"
  mv "$NUMERIC_FIXTURE.mutated" "$NUMERIC_FIXTURE"
  if (
    cd "$REPO"
    python3 tests/verify_product_contract.py fixtures > "$SANDBOX_ROOT/fixtures-numeric-mutation.out" 2>&1
  ); then
    record_failure 'numeric-unit evidence mutation rejection'
  fi
  cp "$ROOT/calib/cases/neg-axiom-cosplay/ideas.md" "$NUMERIC_FIXTURE"
fi

CASE="$REPO/test-fixtures/calibration-case"
mkdir -p "$CASE"
printf '%s\n' \
  '## I1' \
  'One-Sentence Story: Fixture calibration idea' \
  'Theme: Evaluation and Diagnostics' \
  'Form: new mechanism or new problem' \
  'Summary: A fixture used only for offline contract validation.' \
  'Minimal Falsification Experiment: Compare one fixture baseline on one held-out case using 1×H100.' \
  'Why It May Be Novel: Independent research decides.' > "$CASE/ideas.md"
printf '%s\n' \
  '## I1' \
  'Search Terms: fixture direct hit' \
  '- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=fixture-direct-hit' \
  'Nearest Work:' \
  '- Fixture Paper | https://arxiv.org/abs/2507.10543 | Direct hit | Occupies the headline.' \
  'Strongest Counterexample: Fixture Paper — It occupies the headline.' \
  'Overlap: high — Fixture Paper occupies the headline.' \
  'Papers Read: 5' \
  'arXiv ID Check: yes' > "$CASE/priorwork.md"
printf '%s\n' 'min_vote>=accept-w-rev' > "$CASE/expect"
printf '%s\n' 'overlap=high' 'url_contains=2507.10543' > "$CASE/e2e.expect"

if ! (
  cd "$REPO"
  CALIB_FAKE_MODE=panel-valid \
  PANEL_CMD=tests/calibration_abi_smoke.sh \
  ./calib/run_panel.sh "$CASE" 2 > "$SANDBOX_ROOT/panel-valid.out"
); then
  record_failure 'valid panel execution'
fi
EXPECTED_AGGREGATE=$(printf 'I1\taccept-w-rev,accept-w-rev\taccept-w-rev')
if [ "$(tail -n 1 "$REPO/tmp/calib/calibration-case/aggregate.tsv" 2>/dev/null || true)" != "$EXPECTED_AGGREGATE" ]; then
  record_failure 'valid panel aggregate ABI'
fi
marker_count=$(grep -cF 'suspected published counterpart: Fixture Paper' "$SANDBOX_ROOT/panel-valid.out" 2>/dev/null || true)
if [ "$marker_count" -ne 1 ]; then
  record_failure 'one-time suspected-counterpart marker aggregation'
fi
if grep -qE 'Prefix Noise|Suffix Noise' "$SANDBOX_ROOT/panel-valid.out"; then
  record_failure 'prefixed or unspaced-suffix marker exclusion'
fi

if (
  cd "$REPO"
  CALIB_FAKE_MODE=panel-malformed \
  PANEL_CMD=tests/calibration_abi_smoke.sh \
  ./calib/run_panel.sh "$CASE" 1 > "$SANDBOX_ROOT/panel-malformed.out" 2>&1
); then
  record_failure 'malformed verdict rejection'
fi

if (
  cd "$REPO"
  CALIB_FAKE_MODE=panel-empty-id-extra \
  PANEL_CMD=tests/calibration_abi_smoke.sh \
  ./calib/run_panel.sh "$CASE" 1 > "$SANDBOX_ROOT/panel-empty-id-extra.out" 2>&1
); then
  record_failure 'nonblank verdict row with an empty ID'
fi

if (
  cd "$REPO"
  CALIB_FAKE_MODE=panel-missing-review \
  PANEL_CMD=tests/calibration_abi_smoke.sh \
  ./calib/run_panel.sh "$CASE" 1 > "$SANDBOX_ROOT/panel-missing-review.out" 2>&1
); then
  record_failure 'accepted verdict review-block requirement'
fi

if ! (
  cd "$REPO"
  CALIB_FAKE_MODE=panel-reject \
  PANEL_CMD=tests/calibration_abi_smoke.sh \
  ./calib/run_panel.sh "$CASE" 1 > "$SANDBOX_ROOT/panel-reject.out"
); then
  record_failure 'reject-only output without review.md'
fi

if ! (
  cd "$REPO"
  rm -f tmp/calib/summary.tsv
  CALIB_FAKE_MODE=panel-valid \
  PANEL_CMD=tests/calibration_abi_smoke.sh \
  ./calib/run_all.sh 1 "$CASE" > "$SANDBOX_ROOT/run-all.out"
); then
  record_failure 'run_all grading execution'
fi
if ! awk -F'\t' '
    $2=="calibration-case" && $3==1 && $4=="tests/calibration_abi_smoke.sh" && $5=="pass" { ok++ }
    END { exit !(ok==1) }
  ' "$REPO/tmp/calib/summary.tsv" 2>/dev/null; then
  record_failure 'run_all recorded PANEL_CMD and passing grade'
fi

if ! (
  cd "$REPO"
  CALIB_FAKE_MODE=e2e-valid \
  E2E_CMD=tests/calibration_abi_smoke.sh \
  E2E_MIN_LINKS=5 \
  ./calib/run_e2e.sh "$CASE" > "$SANDBOX_ROOT/e2e-valid.out"
); then
  record_failure 'anchored exact Overlap enum acceptance'
fi

if (
  cd "$REPO"
  CALIB_FAKE_MODE=e2e-commentary \
  E2E_CMD=tests/calibration_abi_smoke.sh \
  E2E_MIN_LINKS=5 \
  ./calib/run_e2e.sh "$CASE" > "$SANDBOX_ROOT/e2e-commentary.out" 2>&1
); then
  record_failure 'commentary-only overlap rejection'
fi

if [ "$failures" -ne 0 ]; then
  printf 'failed: calibration ABI smoke (%s cases)\n' "$failures" >&2
  exit 1
fi
printf 'ok: calibration ABI smoke\n'
