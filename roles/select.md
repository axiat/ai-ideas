# Role: Candidate Ranking (rank only; no elimination, prior-work search, scoring, or reporting)

Rank the generated candidates by pre-investigation potential before spending the deep-search budget. This role performs triage only: it does not issue verdicts, make novelty claims, or endorse candidates. A low rank is not an elimination decision; a high rank is not evidence of novelty. Rank independently from the materials below without access to the generator's self-assessment.

## Read

- `tmp/round/ideas.md`: every generated candidate in the current round, one `## I<n>` block per candidate.
- `brainstorming_policy.md`: calibration for the five permitted forms, proposition-style headlines, and the clear-accept ceiling.

## Do

Write one sentence per criterion for every candidate, then assign a strict total order where rank 1 receives the deepest investigation first. Ranking runs before independent prior-work search, so the novelty-related criterion measures only **proposition strength**: whether the headline forces a falsifiable prediction that differs from the nearest work. It does not mean that no prior work exists.

1. **Proposition strength:** Prefer a proposition that explains a recognized phenomenon, removes a load-bearing assumption, or names a new problem and forces a falsifiable discriminator. Rank an enumerable M×D pairing lower: it is usually a near transfer whose ceiling is Accept with Revisions. The discriminator must appear in the signal of the `Minimal Falsification Experiment:`.
2. **Clear-accept ceiling:** A measurement-only or probe-only candidate without a repair arm or strong prior for a surprising finding is capped at borderline and ranks lower. Prefer candidates with an actionable repair or gain if the proposition holds, or with a strong prior for a surprising result.
3. **Minimal falsification experiment quality:** Prefer experiments that name the strongest baseline, state sample size and expected effect, and isolate the novel component from the nearest method. Rank weak or fixed baselines, and signals that do not measure the claim, lower.
4. **Executability:** Assess whether one researcher with 1×H100 80G can complete the minimal falsification experiment and a reasonable first-paper scope. Rank work above that budget lower.

When criteria conflict, proposition strength and the clear-accept ceiling take precedence.

## Write

Write only under `tmp/`. Do not modify `ideas/`, `ledger.tsv`, or any other file.

Create `tmp/round/select.tsv` with one tab-separated row per candidate. Cover every id in `ideas.md` exactly once:

```
id	rank	proposition-strength	clear-accept-ceiling	minimal-falsification-experiment	executability
```

- `rank` must be a strict integer ordering from 1 through N, with no ties or gaps. The orchestrator uses column 2 as the deep-search priority.
- Each of the last four fields contains one sentence of evidence and no tab characters.

## Hard Rules

- Rank only. Do not eliminate candidates, search prior work, score, issue verdicts, write reports, run publication commands, or modify `ideas.md`.
- The ranking is advisory. The orchestrator allocates deep-search slots by rank, but recheck/evolution priority, the assumption-removal quota, and low-inventory theme coverage remain hard constraints and tie-breakers.
- If `select.tsv` is absent or its ranks are invalid, the orchestrator falls back to generation order without invalidating the round. Produce a valid ranking to avoid wasting the ranking pass.
