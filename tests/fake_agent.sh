#!/usr/bin/env bash
set -eu

[ "$#" -eq 1 ] || {
  printf 'usage: fake_agent.sh <prompt>\n' >&2
  exit 64
}

prompt=${1:-}
mkdir -p tmp/round

record_call() {
  printf '%s\n' "$1" >> tmp/fake-agent.calls
}

case "$prompt" in
  *roles/meta.md*)
    record_call meta
    mkdir -p tmp
    printf '%s\n' \
      '# Failure Patterns' \
      '' \
      '## Fatal Patterns' \
      '' \
      '## Ceiling Patterns' \
      '' \
      '## Evolution Candidates' > tmp/deathlist.md
    ;;
  *roles/generate.md*)
    if grep -qx 'generate' tmp/fake-agent.calls 2>/dev/null; then
      printf 'fake_agent.sh: refusing a second generation pass\n' >&2
      exit 65
    fi
    record_call generate
    printf 'I1\tConstraint-Driven Sparse World Models\tWorld Models - Architecture\n' > tmp/round/ideas.tsv
    printf '%s\n' \
      'Assumption-Removal Attempt: complete I1' \
      '' \
      '## I1' \
      'One-Sentence Story: Constraint-Driven Sparse World Models' \
      'Theme: World Models - Architecture' \
      'Form: remove-load-bearing-assumption' \
      'Assumption to Remove: Dense temporal updates are required for reliable closed-loop world-model control.' \
      'Why It Can Be Removed Now: Event-triggered latent updates can preserve task-relevant state while skipping redundant transitions.' \
      'Forcing Constraint: Deployment latency and energy budgets prohibit dense inference at every control tick.' \
      'Crack Evidence: https://example.com/crack-one' \
      'Crack Evidence: https://example.com/crack-two' \
      'Minimal Falsification Experiment: Compare dense and event-triggered latent updates on 128 held-out manipulation episodes using one H100; kill the idea if success drops by more than two percentage points or latency improves by less than thirty percent.' \
      'Core Claim: Event-triggered latent updates can match dense world-model control while materially reducing inference cost.' \
      'Why Now: Modern latent state estimators expose confidence signals that can drive update decisions.' \
      'First Exploration: Measure control success, latent drift, latency, and energy against the strongest dense-update baseline.' > tmp/round/ideas.md
    ;;
  *roles/select.md*)
    record_call select
    printf 'I1\t1\tA decisive claim with a cheap falsification path.\n' > tmp/round/select.tsv
    ;;
  *roles/prescreen.md*)
    record_call prescreen
    printf '%s\n' \
      '## I1' \
      '- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=event-triggered-world-model-control' \
      'Decision: keep' > tmp/round/prescreen.md
    ;;
  *roles/research.md*)
    record_call research
    printf '%s\n' \
      '## I1' \
      'Search Terms: event-triggered world-model updates; sparse latent dynamics; adaptive compute for control' \
      '- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=event-triggered-latent-dynamics' \
      'Nearest Work:' \
      '- Neighbor One | https://example.com/paper-one | Uses dense latent updates and does not test event-triggered control.' \
      '- Neighbor Two | https://example.com/paper-two | Studies sparse perception without changing world-model update schedules.' \
      '- Neighbor Three | https://example.com/paper-three | Compresses dynamics models but retains fixed-rate inference.' \
      '- Neighbor Four | https://example.com/paper-four | Uses adaptive compute outside closed-loop robotic control.' \
      '- Neighbor Five | https://example.com/paper-five | Gates observations rather than latent dynamics updates.' \
      'Strongest Counterexample: Neighbor Four is the closest adaptive-compute result, but it does not test closed-loop world-model control.' \
      'Overlap: low' \
      'Papers Read: 5' \
      'arXiv ID Check: yes' \
      '## Crack Evidence Verification' \
      '- https://example.com/crack-one | Verification: supports — Reports stable control under confidence-triggered observation updates.' \
      '- https://example.com/crack-two | Verification: supports — Shows redundant latent transitions can be skipped under bounded drift.' > tmp/round/priorwork.md
    ;;
  *roles/review.md*)
    record_call review
    mkdir -p tmp/round/rev/1
    printf 'I1\tstrong-accept\t0\tIndependent evidence supports a clear-accept contribution under the stated experiment.\n' > tmp/round/rev/1/verdict.tsv
    printf '%s\n' \
      '## I1' \
      'The claim is distinct, falsifiable, and feasible on the stated compute budget.' > tmp/round/rev/1/review.md
    ;;
  *roles/report.md*)
    record_call report
    report_date=$(awk -F': ' '$1=="Review Date"{print $2; exit}' tmp/round/meta.txt 2>/dev/null || true)
    [ -n "$report_date" ] || report_date=$(date +%F)
    report_path="ideas/${report_date}_hunt.md"
    suffix=2
    while [ -e "$report_path" ]; do
      report_path="ideas/${report_date}_hunt-${suffix}.md"
      suffix=$((suffix + 1))
    done
    mkdir -p ideas
    printf '%s\n' \
      '# Idea Hunt Report' \
      '' \
      '## Key Literature' \
      '- Five directly relevant works were inspected; none occupied the headline claim.' \
      '' \
      '## Accepted Idea' \
      'Constraint-Driven Sparse World Models' \
      '' \
      'The single reviewer returned strong-accept.' \
      '' \
      '## Rejected Ideas' \
      'None.' \
      '' \
      '## Metadata' \
      "Review Date: ${report_date}" > "$report_path"
    printf '%s\n' "$report_path" > tmp/fake-report.path
    ;;
  *)
    printf 'fake_agent.sh: unknown prompt: %s\n' "$prompt" >&2
    exit 64
    ;;
esac
