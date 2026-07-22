#!/usr/bin/env bash
set -eu

[ "$#" -eq 1 ] || {
  printf 'usage: fake_agent.sh <prompt>\n' >&2
  exit 64
}

prompt=${1:-}
FAKE_AGENT_MODE=${FAKE_AGENT_MODE:-default}
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
      'Summary: Gate latent-dynamics updates with model confidence so redundant control transitions consume no world-model inference. Compare the gated controller with the strongest dense-update baseline.' \
      'Minimal Falsification Experiment: Compare dense and event-triggered latent updates on 128 held-out manipulation episodes using one H100; kill the idea if success drops by more than two percentage points or latency improves by less than thirty percent.' \
      'Why It May Be Novel: The hypothesis is that confidence-gated latent updates preserve closed-loop control while fixed-rate world models dominate the nearest work; independent research must verify it.' \
      'Assumption to Remove: Dense temporal updates are required for reliable closed-loop world-model control.' \
      'Why It Can Be Removed Now: Event-triggered latent updates can preserve task-relevant state while skipping redundant transitions.' \
      'Forcing Constraint: Deployment latency and energy budgets prohibit dense inference at every control tick.' \
      'Crack Evidence: https://example.com/crack-one | Confidence-triggered observation updates preserve stable control in the fixture evidence.' \
      'Crack Evidence: https://example.com/crack-two | Bounded latent drift permits redundant transitions to be skipped in the fixture evidence.' > tmp/round/ideas.md
    ;;
  *roles/select.md*)
    record_call select
    printf 'I1\t1\tThe proposition removes a load-bearing fixed-rate assumption.\tA successful repair would support a clear-accept efficiency claim.\tThe experiment names a dense baseline, 128 episodes, and explicit kill thresholds.\tOne researcher can run the comparison on one H100.\n' > tmp/round/select.tsv
    ;;
  *roles/prescreen.md*)
    record_call prescreen
    if [ "$FAKE_AGENT_MODE" = "missing-occupant" ]; then
      # Intentionally invalid kill: a distracting URL cannot replace Occupant:.
      printf '%s\n' \
        '## I1' \
        '- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=event-triggered-world-model-control' \
        'Reference: https://example.com/distracting-ordinary-url' \
        'Decision: kill' > tmp/round/prescreen.md
    else
      printf '%s\n' \
        '## I1' \
        '- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=event-triggered-world-model-control' \
        'Decision: keep' > tmp/round/prescreen.md
    fi
    ;;
  *roles/research.md*)
    record_call research
    overlap_line='Overlap: low — None of the five nearest works occupies confidence-gated latent updates for closed-loop world-model control.'
    if [ "$FAKE_AGENT_MODE" = "overlap-commentary" ]; then
      # Intentionally invalid token: commentary must not supply an overlap value.
      overlap_line='Overlap: unknown; high appears only in commentary'
    fi
    printf '%s\n' \
      '## I1' \
      'Search Terms: event-triggered world-model updates; sparse latent dynamics; adaptive compute for control' \
      '- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=event-triggered-latent-dynamics' \
      'Nearest Work:' \
      '- Neighbor One | https://example.com/paper-one | Uses dense latent updates | Does not test event-triggered control.' \
      '- Neighbor Two | https://example.com/paper-two | Studies sparse perception | Does not change world-model update schedules.' \
      '- Neighbor Three | https://example.com/paper-three | Compresses dynamics models | Retains fixed-rate inference.' \
      '- Neighbor Four | https://example.com/paper-four | Uses adaptive compute | Omits closed-loop robotic control.' \
      '- Neighbor Five | https://example.com/paper-five | Gates observations | Does not gate latent-dynamics updates.' \
      'Strongest Counterexample: Neighbor Four is the closest adaptive-compute result, but it does not test closed-loop world-model control.' \
      "$overlap_line" \
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
      '### 1. First impression' \
      '- Paper type: Novel Method' \
      '- One-sentence story: Confidence-gated latent updates preserve control while reducing world-model inference.' \
      '### 2. Fatal-flaws audit (early gate)' \
      'No CRITICAL or MAJOR flaw remains in the supplied evidence.' \
      '### 3. Lifecycle and capability match' \
      'The minimal experiment fits one researcher and one H100.' \
      '### 4. Five-dimension radar' \
      'Faster and Cheaper have the strongest evidence; the remaining dimensions are neutral.' \
      '### 5. Paradigm-shift probe' \
      'The work challenges the fixed-rate latent-update assumption.' \
      '### 6. Feasibility' \
      'Compute, data, engineering, and timeline risks are low for the stated experiment.' \
      '### 7. Integrity gate result' \
      '- Gate 1 through 8: pass' \
      '### 8. Verdict' \
      '**Strong Accept**' > tmp/round/rev/1/review.md
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
    {
      printf '%s\n' \
        '# Idea Hunt Report' \
        '' \
        '## Key Literature' \
        '- Five directly relevant works were inspected; none occupied the headline claim.' \
        '' \
        '## Qualified Ideas'
      awk '
        /^## I1$/ { print "### I1"; copy = 1; next }
        copy && /^## I[0-9]+$/ { exit }
        copy { print }
      ' tmp/round/ideas.md
      printf '%s\n' \
        'The single independent reviewer returned Strong Accept.' \
        '' \
        '### Reviewer 1 Full Review'
      sed -n '/^## I1$/,$p' tmp/round/rev/1/review.md | sed '1d'
      printf '%s\n' '' '### Directed Prior Work'
      sed -n '/^## I1$/,$p' tmp/round/priorwork.md
      printf '%s\n' '' '## Rejected Ideas' 'None.' '' '## Metadata' "Review Date: ${report_date}"
    } > "$report_path"
    printf '%s\n' "$report_path" > tmp/fake-report.path
    ;;
  *)
    printf 'fake_agent.sh: unknown prompt: %s\n' "$prompt" >&2
    exit 64
    ;;
esac
