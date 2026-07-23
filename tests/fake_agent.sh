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

awr_base() {
  local task
  for task in tmp/awr-side/awr/r*.task.md; do
    [ -e "$task" ] || continue
    printf '%s\n' "${task%.task.md}"
    return 0
  done
  for task in tmp/awr-side/awr/*.task.md; do
    [ -e "$task" ] || continue
    printf '%s\n' "${task%.task.md}"
    return 0
  done
  printf 'fake_agent.sh: AwR task file not found\n' >&2
  return 1
}

case "$prompt" in
  *roles/awr-priorwork.md*)
    base=$(awr_base)
    crack_heading='## Crack Evidence Verification'
    verification_one='- https://example.com/crack-one | Verification: supports — Stable control survives confidence-triggered updates.'
    verification_two='- https://example.com/crack-two | Verification: supports — Bounded latent drift permits skipped transitions.'
    if [ "$FAKE_AGENT_MODE" = "awr-invalid-verification" ]; then
      verification_one='- https://example.com/crack-one | Verification: maybe — The outcome token is intentionally invalid.'
    elif [ "$FAKE_AGENT_MODE" = "awr-mixed-verification" ]; then
      verification_one='- https://example.com/crack-one | Verification: maybe; Verification: supports — The second token must not mask the first.'
    elif [ "$FAKE_AGENT_MODE" = "awr-no-crack" ]; then
      crack_heading=
      verification_one=
      verification_two=
    fi
    query='- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=confidence-gated-latent-updates'
    query_before=$query
    query_inside=
    neighbor_three='- Neighbor Three | https://example.com/awr-paper-three | Compresses a dense world model.'
    neighbor_four='- Neighbor Four | https://example.com/awr-paper-four | Studies adaptive compute outside robot control.'
    neighbor_five='- Neighbor Five | https://example.com/awr-paper-five | Uses event triggers without confidence gating.'
    counterexample_before=
    counterexample_after='Strongest Counterexample: Neighbor Four is the closest adaptive-compute result, but it omits closed-loop world-model control.'
    case "$FAKE_AGENT_MODE" in
      awr-four-neighbors) neighbor_five= ;;
      awr-crack-url-count) neighbor_three=; neighbor_four=; neighbor_five= ;;
      awr-non-api-query) query_before='- Query: https://example.com/search?q=confidence-gated-latent-updates' ;;
      awr-api-host-prefix) query_before='- Query: https://api.semanticscholar.org.evil/graph/v1/paper/search?query=x' ;;
      awr-api-path-prefix) query_before='- Query: https://export.arxiv.org/api/queryevil?search_query=x' ;;
      awr-api-bare-host) query_before='- Query: https://api.semanticscholar.org' ;;
      awr-query-in-neighbors) neighbor_five=; query_before=; query_inside=$query ;;
      awr-reversed-sections) counterexample_before=$counterexample_after; counterexample_after= ;;
    esac
    printf '%s\n' \
      '## Independent Prior Work' \
      'Search Terms: confidence-gated latent updates; event-triggered world models' \
      "$query_before" \
      "$counterexample_before" \
      'Nearest Work:' \
      "$query_inside" \
      '- Neighbor One | https://example.com/awr-paper-one | Uses fixed-rate latent updates.' \
      '- Neighbor Two | https://example.com/awr-paper-two | Gates observations instead of latent dynamics.' \
      "$neighbor_three" \
      "$neighbor_four" \
      "$neighbor_five" \
      "$counterexample_after" \
      'Overlap: low — None of the five works occupies confidence-gated latent updates for closed-loop control.' \
      'Papers Read: 5' \
      'arXiv ID Check: yes' \
      "$crack_heading" \
      "$verification_one" \
      "$verification_two" \
      'AGY-DONE' > "${base}.priorwork.new.md"
    ;;
  *roles/awr-judge.md*)
    base=$(awr_base)
    case "$FAKE_AGENT_MODE" in
      awr-not-ready)
        printf '%s\n' \
          'Decision: not-ready' \
          '- Defect: Add a latency control that separates gating overhead from skipped world-model inference.' \
          'AGY-DONE' > "${base}.judge.md"
        ;;
      awr-mixed-decision)
        printf '%s\n' \
          'Decision: SA-possible' \
          'Decision: not-ready' \
          '- Defect: This mixed decision is intentionally invalid.' \
          'AGY-DONE' > "${base}.judge.md"
        ;;
      *)
        printf '%s\n' \
          'Decision: SA-possible' \
          'AGY-DONE' > "${base}.judge.md"
        ;;
    esac
    ;;
  *roles/awr.md*)
    base=$(awr_base)
    printf '%s\n' \
      '## Revised Idea' \
      'Confidence-gated latent updates skip redundant world-model inference while preserving closed-loop control.' \
      'Minimal Falsification Experiment: Compare dense and confidence-gated updates on 128 held-out episodes using one H100; reject the claim if success drops by more than two points or latency improves by less than thirty percent.' \
      '## Search Record' \
      '- Neighbor One | https://example.com/awr-paper-one | Fixed-rate latent updates.' \
      '- Neighbor Two | https://example.com/awr-paper-two | Observation gating.' \
      '- Neighbor Three | https://example.com/awr-paper-three | Dense world-model compression.' \
      '## Response' \
      'The revision isolates confidence gating and names a decisive dense-update comparison.' \
      'AGY-DONE' > "${base}.new.md"
    ;;
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
      'Execution Budget: One researcher at 20 effective hours per week with one H100.' \
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
    case "$FAKE_AGENT_MODE" in
      ballot-short) printf 'I1\tstrong-accept\n' > tmp/round/rev/1/verdict.tsv ;;
      ballot-extra) printf 'I1\tstrong-accept\t0\tValid reason.\textra\n' > tmp/round/rev/1/verdict.tsv ;;
      ballot-nonnumeric-major) printf 'I1\tstrong-accept\tmany\tValid reason.\n' > tmp/round/rev/1/verdict.tsv ;;
      ballot-empty-reason) printf 'I1\tstrong-accept\t0\t\n' > tmp/round/rev/1/verdict.tsv ;;
      ballot-duplicate) printf 'I1\tstrong-accept\t0\tFirst reason.\nI1\treject\t0\tSecond reason.\n' > tmp/round/rev/1/verdict.tsv ;;
      *) printf 'I1\tstrong-accept\t0\tIndependent evidence supports a clear-accept contribution under the stated experiment.\n' > tmp/round/rev/1/verdict.tsv ;;
    esac
    printf '%s\n' \
      '## I1' \
      '### 1. First impression' \
      '- Paper type: Novel Method' \
      '- One-sentence story: Confidence-gated latent updates preserve control while reducing world-model inference.' \
      '### 2. Fatal-flaws audit (early gate)' \
      '| # | Flaw | Severity | Defense |' \
      '|---|---|---|---|' \
      '| - | None identified in the supplied evidence. | - | No defense required. |' \
      '### 3. Lifecycle and capability match' \
      "| Aspect | User's input | Assessment |" \
      '|---|---|---|' \
      '| Idea category | Innovative Technique | Matches a bounded method contribution. |' \
      '| Lifecycle | 3 months | Fits the pilot and first-paper scope. |' \
      '| Weekly effective hours | 20 | Sufficient for the stated experiment. |' \
      '| Fit | One researcher and one H100 | Green |' \
      '### 4. Five-dimension radar' \
      '| Dimension | Score 1-10 | Evidence | Lift suggestion |' \
      '|---|---|---|---|' \
      '| Higher | 6 | The kill threshold bounds control-success loss at two points. | Report success confidence intervals. |' \
      '| Faster | 9 | The decisive experiment requires at least 30 percent lower latency. | Profile each control stage. |' \
      '| Stronger | 6 | Two crack-evidence checks support stable control under skipped updates. | Add drift stress tests. |' \
      '| Cheaper | 8 | Skipped latent updates directly reduce inference demand. | Report energy per episode. |' \
      '| Broader | 5 | Evidence covers one manipulation setting. | Defer broader claims until cross-task evidence exists. |' \
      '### 5. Paradigm-shift probe' \
      '| Probe | Yes or No | Rationale |' \
      '|---|---|---|' \
      '| First Principles | Yes | It tests whether fixed-rate latent updates are necessary. |' \
      '| Elephant in the Room | No | The evidence does not establish a field-wide avoidance pattern. |' \
      '| Technology Cycle | Yes | Confidence estimates make event-triggered updates executable. |' \
      "| Hamming's Rule | Yes | Reliable sparse updates would materially reduce deployment cost. |" \
      'Disruptive potential: possible.' \
      '### 6. Feasibility' \
      '| Risk | Level | Mitigation |' \
      '|---|---|---|' \
      '| Compute | Low | Run the 128-episode comparison on the stated one H100. |' \
      '| Data | Low | Use the stated held-out manipulation episodes. |' \
      '| Engineering | Low | Limit the first paper to the confidence gate and dense baseline. |' \
      '| Timeline | Low | Kill the idea when either explicit threshold fails. |' \
      '### 7. Integrity gate result' \
      '- Gate 1 through 8: pass' \
      '### 8. Verdict' \
      '**Strong Accept**' \
      'Top three actions to take first:' \
      '1. Implement the confidence gate and dense baseline under one profiler.' \
      '2. Run the 128-episode falsification experiment with fixed seeds.' \
      '3. Report success, latency, and energy against the stated kill thresholds.' > tmp/round/rev/1/review.md
    ;;
  *roles/report.md*)
    record_call report
    rounds_attempted=$(awk -F': ' '$1=="Rounds Attempted"{print $2; exit}' tmp/round/meta.txt 2>/dev/null || true)
    report_date=$(awk -F': ' '$1=="Review Date"{print $2; exit}' tmp/round/meta.txt 2>/dev/null || true)
    [ -n "$rounds_attempted" ] || rounds_attempted=unknown
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
      printf '%s\n' '' '## Rejected Ideas' 'None.' '' '## Metadata' \
        "Rounds Attempted: ${rounds_attempted}" "Review Date: ${report_date}"
    } > "$report_path"
    printf '%s\n' "$report_path" > tmp/fake-report.path
    ;;
  *)
    printf 'fake_agent.sh: unknown prompt: %s\n' "$prompt" >&2
    exit 64
    ;;
esac
