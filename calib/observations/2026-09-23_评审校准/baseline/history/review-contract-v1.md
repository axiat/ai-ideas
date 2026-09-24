# Bounded Review Contract v1

Review one candidate against its supplied prior-work evidence. Use `strong-accept`, `accept-w-rev`, or `reject`.

## Gates

- Default to Reject. Every promotion must cite supplied evidence.
- Any CRITICAL finding requires Reject. Two or more MAJOR findings forbid Strong Accept.
- A direct occupying result caps novelty unless the remaining attributable difference independently supports clear accept.
- Missing or weak prior-work coverage is MAJOR and forbids Strong Accept.
- Feasibility is judged only from the minimal falsification experiment: strongest baseline, data scale, compute, expected signal, attribution control, and kill condition. A missing or non-executable experiment is MAJOR.
- A mismatched estimand is MAJOR. A measurement-only result without an attributable repair or a strong, aligned prior is at most borderline.
- Strong Accept requires a substantial clear-accept case, approximately 6,6,8 or better, with the first-paper experiment feasible for a team of typically 2–3 researchers (up to ~10 with justification) without pretraining-scale compute.
- A transferred mechanism needs zero target-setting hits, a nontrivial adaptation forced by the setting, and an evidence-backed prospect of a new, attributable payoff sufficient for clear accept to reach Strong Accept. That prospect requires a specific causal argument grounded in relevant observations or analysis and a feasible minimal falsification experiment with a strong baseline, attribution controls, and an explicit kill condition. Existing experimental results strengthen the case but are not required; unrun experiments alone are neither MAJOR nor grounds to cap the verdict. Unsupported gain assumptions, weak causal support, or non-discriminating experiments do not satisfy this gate.
- An assumption-removal candidate needs low overlap, two directly supporting crack-evidence verifications, an external forcing constraint, and a decisive bounded experiment. Missing conditions return it to ordinary calibration.
- A verified internal-history summary can support duplication, lineage, or failure-pattern findings. It cannot establish academic novelty.

## Output

The response contains exactly one artifact, `review-markdown`. The host
derives `output/verdict.tsv` from that markdown as exactly one four-field row:

```text
candidate-id<TAB>verdict<TAB>MAJOR-count<TAB>one-sentence reason
```

The `review-markdown` response artifact is materialized as
`output/review.md` and contains:

```text
# <candidate-id>
Verdict: <verdict>
CRITICAL: <count>
MAJOR: <count>
Headline: <one sentence>
Occupation: <closest work and attributable difference>
Experiment: <bounded falsification assessment>
Estimand: <alignment assessment>
Payoff: <net-new attributable payoff>
Feasibility: <first-paper resource assessment>
History: <verified relation and evidence IDs, or scoped no-match receipt, or unavailable>
Reason: <decision>
```

Every factual claim names the supplied artifact or evidence ID that supports it. Unsupported content is omitted.
