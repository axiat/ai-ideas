# PROGRAM — Idea Research Loop Protocol

All entry points, including scheduled routines and active loops, follow this protocol. Only a human may modify this file or the immutable inputs below; agents read them without alteration.

## Immutable Inputs

1. `rubric.md` — the review procedure. It is read-only and may not be skipped or reinterpreted.
2. `brainstorming_policy.md` — divergence rules, valid idea forms, and verdict calibration. It is read-only; agents may neither relax nor tighten the Strong Accept standard.
3. Verdict is the sole success metric. Keep if and only if Strong Accept. No single agent decides a verdict: the orchestrator (`hunt.sh`) aggregates N independent reviewers by taking the lowest vote, so Strong Accept requires unanimity.
4. Grading evidence is mandatory. Every prospective Strong Accept needs the policy's directed prior-work record: the 5–8 closest papers with links, a `Strongest Counterexample:` line, at least one reproducible arXiv or Semantic Scholar API `- Query:` record, and a `Minimal Falsification Experiment:` specifying data × compute × expected signal. `Form: remove-load-bearing-assumption` also requires a `Crack Evidence Verification` section with at least two `supports` results. Missing evidence prevents the grade. Prior-work research runs in an independent process. Reviewers accept novelty only from that evidence and feasibility only from the falsification experiment, never from the generator's claims.
5. The `ledger.tsv` schema is fixed below. Every generated idea receives one row regardless of verdict. The ledger is append-only; historical rows are immutable. Only the orchestrator may write it.
6. Generation begins by reading `ledger.tsv`; a new idea may not substantially duplicate any prior row, including rejected rows. One exception slot per round is shared by evolution and recheck:
   - Evolution may repair only an `accept-w-rev` row with `overlap=low` whose failure was experimental-design related. A novelty-capped or occupied idea cannot evolve.
   - Recheck may resubmit unchanged either an `accept-w-rev` row with weak prior-work evidence or a `reject` row with `category=evidence-incomplete`, where unanimous SA votes were reduced solely by a hard evidence gate. Start the block with `Recheck:` and its eligibility condition. A story gets at most one recheck.
   - Both paths undergo a fresh full prior-work search and review and inherit no votes. Reject eligibility is mechanical from column 8: `novelty-dead` (`direct-hit`, `overlap=high`, or CRITICAL) is permanent; `evidence-incomplete` permits one recheck, after which another failure becomes permanent. Mark evolution lineage with `Evolved from:`.
7. Roles remain isolated against collusion. Generation, prior-work research, and scoring are separate processes with no shared context; prompts live under `roles/`. The generator neither judges novelty nor scores candidates. Reviewers default to Reject, cannot communicate, and do not know the stopping condition.
8. Agent writes are limited to `tmp/` (gitignored drafts) and `ideas/` (report role only). Agents may not modify `ledger.tsv` or other files and may not run `git`, `gh`, or publication commands. The orchestrator owns accounting and runs accepted output through `./publish.sh`, which uses a feature branch and pull request.
9. One round generates about 10 candidates, independently ranks them, applies prescreen pruning, and then completes prior-work research → scoring → accounting. A prescreen kill is recorded as `reject`; it may not disappear silently. Survivors beyond `SHORT_MAX` are truncated before deep research, receive no ledger row because they received no review, and may be generated again later. No other idea already written to the round artifacts may be abandoned midway.
10. During the loop, do not ask a human for confirmation. Only the entry point defines the stopping condition; never lower the bar or stop early before it is met.

## Loop

`hunt.sh` launches independent processes in this order for each round:

0. **Failure distillation** (`roles/meta.md`; every `META_EVERY` rounds when enough `reject` and `accept-w-rev` rows exist; fallible): summarize fatal patterns, ceiling patterns, and evolution candidates in `tmp/deathlist.md`. Failure is logged and never blocks the round.
1. **Generation** (`roles/generate.md`): read the policy, ledger, and failure list; write about 10 unscreened candidates to `tmp/round/`. Respect the policy's divergence, cross-round theme anti-collapse, and five valid forms. An assumption-removal candidate carries its structured fields and an `Assumption-Removal Attempt:` marker. Avoid existing ledger stories, death patterns, and saturated templates. Label every idea with a policy theme and include a `Minimal Falsification Experiment:` that names the strongest baseline, sample size, and expected effect. `hunt.sh` injects a randomly selected divergence lens.
1.4 **Selection** (`roles/select.md`; isolated context; cheap and fallible; rank only): rank the complete divergent set by proposition strength, clear-accept ceiling, falsification quality, and executability; write `tmp/round/select.tsv`. The orchestrator uses rank to prioritize deep-research slots. Missing or invalid output falls back to generation order and does not invalidate the round. Selection is triage: it issues no verdict, performs no prior-work search, and grants no endorsement. Recheck/evolution limits, the assumption-removal quota, and low-inventory themes remain hard constraints or tie-breakers.
1.5 **Prescreen** (`roles/prescreen.md`; cheap and fallible; kill only): kill only a direct hit where one paper occupies the headline claim. `Decision: kill` requires the occupying link; the orchestrator immediately records `reject` with `overlap=high`. `Decision: keep` makes no novelty claim. Up to `SHORT_MAX` prioritized survivors proceed to deep research.
2. **Deep prior-work research** (`roles/research.md`): search adversarially, hunting direct hits first and then expanding across problem wording, mechanism, and adjacent-domain queries. For each idea, read the abstracts and methods of the 5–8 closest papers and produce independent evidence with `Papers Read:`, `Strongest Counterexample:`, and at least one reproducible API `- Query:` URL. A `Form: remove-load-bearing-assumption` block also verifies every self-reported `Crack Evidence:` URL and records `supports`, `partial`, `contradicts`, or `unreachable` under `Crack Evidence Verification`.
3. **Scoring** (`roles/review.md`; run N times): each reviewer independently completes `rubric.md`, applies policy calibration, defaults to Reject, and emits a verdict.
4. **Aggregation and accounting** (orchestrator): take the lowest of N votes for every idea; unanimity is required for Strong Accept. Append each idea to `ledger.tsv` with the prior-work `overlap` value and the mechanically assigned non-SA `category`.
5. If any idea receives unanimous Strong Accept, `roles/report.md` writes the report under `ideas/` and the orchestrator runs `./publish.sh`. Stop only when the day's accepted count reaches the entry point's `SA_TARGET` (default 1); otherwise begin another round. Prior-work research may be extended incrementally.

## `ledger.tsv`

Tab-separated, eight columns:

```
date	source	theme	idea	verdict	reason	overlap	category
```

- `date`: `YYYY-MM-DD`
- `source`: `weekly` or `hunt`
- `theme`: one value from the policy theme vocabulary; used for cross-round anti-collapse accounting
- `idea`: one-sentence story
- `verdict`: `strong-accept`, `accept-w-rev`, or `reject`
- `reason`: one sentence containing the rejection cause or the kept idea's core value; a prescreen kill begins with `Prescreen direct hit:`
- `overlap`: `high`, `medium`, `low`, or `unknown`; the prior-work overlap judgment. Evolution parent eligibility uses this field, and a legacy row without column 7 is treated as `unknown`.
- `category`: `novelty-dead`, `evidence-incomplete`, `design-fixable`, `ceiling-limited`, or `-`. Strong Accept rows use `-`; legacy seven-column rows omit category and are treated as `-`. The orchestrator assigns a non-SA category mechanically from the lowest vote before hard-gate reduction, hard-gate status, and overlap (see `classify_nonsa` in `hunt.sh`):
  - `novelty-dead`: the headline is occupied (`overlap=high`) or a CRITICAL defect exists.
  - `evidence-incomplete`: unanimous Strong Accept was reduced only by a hard evidence gate.
  - `design-fixable`: `accept-w-rev` with `overlap=low`, indicating an experimental-design defect that may be repaired.
  - `ceiling-limited`: `accept-w-rev` capped by prior work (`overlap` is not `low`).
