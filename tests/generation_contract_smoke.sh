#!/usr/bin/env bash
# Drive shipped hunt axiom_ok on representative generation markdown.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

AXIOM_MIN_CRACKS=${AXIOM_MIN_CRACKS:-2}
log() { printf '%s\n' "$*"; }

# Shell functions under test (same definitions hunt.sh uses).
eval "$(sed -n '/^is_axiom_idea()/,/^}/p; /^axiom_ok()/,/^}/p' hunt.sh)"

SCRATCH="${GENERATION_CONTRACT_SCRATCH:-$(mktemp -d "${TMPDIR:-/tmp}/gen-contract.XXXXXX")}"
mkdir -p "$SCRATCH"
cleanup() { :; }
trap cleanup EXIT

fail=0
assert_exit() {
  local want=$1 name=$2
  shift 2
  set +e
  "$@" >"$SCRATCH/${name}.out" 2>&1
  local got=$?
  set -e
  if [ "$got" -ne "$want" ]; then
    printf 'FAIL %s: expected exit %s got %s\n' "$name" "$want" "$got" >&2
    cat "$SCRATCH/${name}.out" >&2 || true
    fail=1
  else
    printf 'ok %s (exit %s)\n' "$name" "$got"
  fi
}

# Pre-fix live shape: incomplete marker + Form=axiom + non-URL placeholders.
cat >"$SCRATCH/pre-fix.md" <<'EOF'
Assumption-Removal Attempt: incomplete — I1; blocked by: crack evidence links unavailable in this invocation

## I1
One-Sentence Story: Binary terminal success is treated as the decisive benchmark quantity.
Theme: Evaluation and Diagnostics
Form: remove-load-bearing-assumption
Assumption to Remove: Terminal success is a sufficient and complete measure of embodied policy quality.
Why It Can Be Removed Now: Per-step rollouts now routinely expose force and intervention traces.
Forcing Constraint: Deployment safety budgets impose hard limits on dangerous recovery events per hour.
Crack Evidence: <URL unavailable> | Independent verification pending.
Crack Evidence: <URL unavailable> | Independent verification pending.
Summary: Endpoint success may be the wrong quantity for embodied comparisons.
Minimal Falsification Experiment: Baseline success metric; 20 checkpoints; 1xH100; kill if ranks stay identical.
Why It May Be Novel: Tests whether current benchmark practice optimizes the wrong quantity.
EOF
printf 'I1\tBinary terminal success is treated as the decisive benchmark quantity.\tEvaluation and Diagnostics\n' \
  >"$SCRATCH/pre-fix.tsv"

# After incomplete-exempt alignment this shape must pass generation-contract.
assert_exit 0 pre-fix-incomplete-exempt \
  axiom_ok "$SCRATCH/pre-fix.md" "$SCRATCH/pre-fix.tsv"

# Complete shape with real URLs must pass.
cat >"$SCRATCH/complete-urls.md" <<'EOF'
Assumption-Removal Attempt: complete I1

## I1
One-Sentence Story: Binary terminal success is treated as the decisive benchmark quantity.
Theme: Evaluation and Diagnostics
Form: remove-load-bearing-assumption
Assumption to Remove: Terminal success is a sufficient and complete measure of embodied policy quality.
Why It Can Be Removed Now: Per-step rollouts now routinely expose force and intervention traces.
Forcing Constraint: Deployment safety budgets impose hard limits on dangerous recovery events per hour.
Crack Evidence: https://arxiv.org/abs/1705.08292 — Adaptive methods can underperform tuned SGD.
Crack Evidence: https://arxiv.org/abs/2306.09782 — Simpler optimizers can fine-tune large models.
Summary: Endpoint success may be the wrong quantity for embodied comparisons.
Minimal Falsification Experiment: Baseline success metric; 20 checkpoints; 1xH100; kill if ranks stay identical.
Why It May Be Novel: Tests whether current benchmark practice optimizes the wrong quantity.
EOF
cp "$SCRATCH/pre-fix.tsv" "$SCRATCH/complete-urls.tsv"
assert_exit 0 complete-with-urls \
  axiom_ok "$SCRATCH/complete-urls.md" "$SCRATCH/complete-urls.tsv"

# Complete marker + placeholder URLs must still fail (AXIOM_MIN_CRACKS not weakened).
cat >"$SCRATCH/complete-placeholder.md" <<'EOF'
Assumption-Removal Attempt: complete I1

## I1
One-Sentence Story: Binary terminal success is treated as the decisive benchmark quantity.
Theme: Evaluation and Diagnostics
Form: remove-load-bearing-assumption
Assumption to Remove: Terminal success is a sufficient and complete measure of embodied policy quality.
Why It Can Be Removed Now: Per-step rollouts now routinely expose force and intervention traces.
Forcing Constraint: Deployment safety budgets impose hard limits on dangerous recovery events per hour.
Crack Evidence: <URL unavailable> | Independent verification pending.
Crack Evidence: <URL unavailable> | Independent verification pending.
Summary: Endpoint success may be the wrong quantity for embodied comparisons.
Minimal Falsification Experiment: Baseline success metric; 20 checkpoints; 1xH100; kill if ranks stay identical.
Why It May Be Novel: Tests whether current benchmark practice optimizes the wrong quantity.
EOF
cp "$SCRATCH/pre-fix.tsv" "$SCRATCH/complete-placeholder.tsv"
assert_exit 1 complete-placeholder-rejected \
  axiom_ok "$SCRATCH/complete-placeholder.md" "$SCRATCH/complete-placeholder.tsv"

# Live generate-output fixture when present (same incomplete exempt rule).
if [ -s tmp/round/history/generate-output/ideas.md ] \
  && [ -s tmp/round/history/generate-output/ideas.tsv ]; then
  assert_exit 0 live-generate-output \
    axiom_ok \
      tmp/round/history/generate-output/ideas.md \
      tmp/round/history/generate-output/ideas.tsv
fi

# cracks_ok: incomplete Form=axiom without real URLs must not fail research.
eval "$(sed -n '/^cracks_ok()/,/^}/p' hunt.sh)"
RD="$SCRATCH/cracks-rd"
mkdir -p "$RD"
cp "$SCRATCH/pre-fix.md" "$RD/ideas.md"
cp "$SCRATCH/pre-fix.tsv" "$RD/ideas.tsv"
# priorwork may omit crack verification for incomplete hollow cracks
cat >"$RD/priorwork.md" <<'EOF'
## I1
Nearest Work:
- https://example.com/a — Neighbor A.
- https://example.com/b — Neighbor B.
- https://example.com/c — Neighbor C.
- https://example.com/d — Neighbor D.
- https://example.com/e — Neighbor E.
- Query: https://export.arxiv.org/api/query?search_query=all:fixture&start=0&max_results=5
Strongest Counterexample: https://example.com/a — Closest occupied mechanism.
Papers Read: 5
Overlap: medium
EOF
assert_exit 0 cracks-incomplete-exempt cracks_ok

# Complete Form=axiom still requires verification lines with real URLs.
cp "$SCRATCH/complete-urls.md" "$RD/ideas.md"
cp "$SCRATCH/complete-urls.tsv" "$RD/ideas.tsv"
assert_exit 1 cracks-complete-missing-verification cracks_ok

if [ "$fail" -ne 0 ]; then
  printf 'generation_contract_smoke: FAILED\n' >&2
  exit 1
fi
printf 'generation_contract_smoke: ok\n'
