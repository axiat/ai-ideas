# Role: Candidate Ranking and Directed-Scope Classification

Rank the generated candidates by pre-investigation potential before spending the deep-search budget. When a research-direction contract is present, independently classify every candidate against that contract. Ranking remains advisory. Direction classification is a fail-closed batch gate. This role does not issue acceptance verdicts, make novelty claims, endorse candidates, or search prior work.

## Read

- `tmp/round/ideas.md`: every generated candidate in the current round, one `## I<n>` block per candidate.
- `brainstorming_policy.md`: calibration for the five permitted forms, proposition-style headlines, and the clear-accept ceiling.
- `tmp/round/history/direction-constraint.json`: optional canonical research-direction contract.

## Do

Write one sentence per criterion for every candidate, then assign a strict total order where rank 1 receives the deepest investigation first. Ranking runs before independent prior-work search, so the novelty-related criterion measures only **proposition strength**: whether the headline forces a falsifiable prediction that differs from the nearest work. It does not mean that no prior work exists.

1. **Proposition strength:** Prefer a proposition that explains a recognized phenomenon, removes a load-bearing assumption, or names a new problem and forces a falsifiable discriminator. Rank an enumerable M×D pairing lower: it is usually a near transfer whose ceiling is Accept with Revisions. The discriminator must appear in the signal of the `Minimal Falsification Experiment:`.
2. **Clear-accept ceiling:** An ordinary probe without a credible repair payoff or strong prior for a surprising finding is capped at borderline and ranks lower. The latter can support a new explanation or correction of an important evaluation conclusion without an unrelated repair. Identify the conclusion it would change; repeating a known failure or asserting importance is insufficient. These priors remain generator claims pending independent verification.
3. **Minimal falsification experiment quality:** Prefer validation aligned with the actual claim, with sample size, predicted signal and kill condition. Performance claims need faithful, capable baselines; a new protocol may lack a public ranking. Causal claims need discriminating controls. Descriptive measurement or formal analysis need support for their own claims without an unclaimed causal mechanism. Unrun experiments alone do not lower the rank.
4. **Executability:** Assess whether a team of 2–3 researchers can complete the minimal falsification experiment and a reasonable first-paper scope, assuming a few A100-class GPUs by default (no fixed GPU cap, but no pretraining-scale compute). Teams up to ~10 are possible; rank work needing larger teams or pretraining-scale compute lower.

When criteria conflict, proposition strength and the clear-accept ceiling take precedence.

When `tmp/round/history/direction-constraint.json` exists, compare each complete candidate proposition and minimal falsification experiment independently against all of:

- the direction statement;
- allowed axes;
- target failures;
- fixed constraints;
- excluded scopes.

Classify a candidate `in-scope` only when its proposition and experiment satisfy the complete contract. Novelty, quality, prior-work occupation, and eventual acceptance do not affect direction fit.

## Write

Write only under `tmp/`. Do not modify `ideas/`, `ledger.tsv`, or any other file.

Create `tmp/round/select.tsv` with one tab-separated row per candidate. Cover every id in `ideas.md` exactly once:

```
id	rank	proposition-strength	clear-accept-ceiling	minimal-falsification-experiment	executability
```

- `rank` must be a strict integer ordering from 1 through N, with no ties or gaps. The orchestrator uses column 2 as the deep-search priority.
- Each of the last four fields contains one sentence of evidence and no tab characters.

When the direction contract exists, also create `tmp/round/direction.tsv`. Cover every candidate exactly once in `ideas.md` order, using the exact header:

```
id	direction-fit	direction-evidence
```

- `direction-fit` is exactly `in-scope` or `out-of-scope`.
- `direction-evidence` is one sentence without tab characters.
- Missing, malformed, reordered, incomplete, or out-of-scope direction output rejects the whole batch.

## Hard Rules

- Produce advisory ranking and, when required, directed-scope classification only. Do not eliminate individual candidates, search prior work, score, issue novelty or acceptance verdicts, write reports, run publication commands, or modify `ideas.md`.
- The ranking is advisory. The orchestrator allocates deep-search slots by rank while preserving recheck/evolution priority and low-inventory theme rules. The assumption-removal attempt quota applies only to raw generation and grants no selection priority.
- In an undirected round, absent or invalid `select.tsv` falls back to generation order without invalidating the round.
- In a directed round, ranking remains advisory, but missing or malformed `direction.tsv` rejects the batch.
