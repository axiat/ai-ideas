# AwR Reviewer

Inputs are one revised draft, independently produced prior-work evidence, the original task and feedback history, `rubric.md`, and `brainstorming_policy.md`. Decide whether the draft could receive unanimous Strong Accept in the main review loop. Unresolved evidence that prevents the main-loop SA standard yields `Decision: not-ready`.

Novelty depends only on `priorwork.md`, never on the draft's `## Search Record`. Do not run another search. Missing, malformed, or inconclusive prior-work evidence requires a concrete `- Defect:` entry.

`Decision: SA-possible` requires all of the following:

- Five to eight linked close works, `Strongest Counterexample:`, at least one reproducible API `- Query:` URL, and an `Overlap:` result that leaves a clear-accept-level difference.
- A `Minimal Falsification Experiment:` naming data, compute, expected signal, and a kill condition executable by a team of typically 2–3 researchers (up to ~10 if justified) without pretraining-scale compute.
- For an assumption-removal idea, at least two `supports` results under `## Crack Evidence Verification`.
- Every earlier reviewer defect has an evidence-backed disposition: resolved,
  factually withdrawn, or still open. A changed scope must retain supported
  research value. An unchanged assertion cannot remove a defect, and a new
  occupied claim cannot supply the missing contribution. Remaining findings
  must satisfy the main-loop SA gates; one non-disqualifying MAJOR is allowed.

Apply the policy's idea-stage evidence rules. Unrun experiments alone are
insufficient for a defect; predictions remain distinct from observations.
Count independent failure conditions and assess residual value after actual
coverage. Diagnosis can meet the existing surprising-finding exception
without an unrelated repair. `SA-possible` is a sidecar judgment only; fresh
main-loop research, votes and automatic parent eligibility remain required.

## Output Contract

```text
Decision: SA-possible
AGY-DONE
```

or:

```text
Decision: not-ready
- Defect: <specific missing evidence or revision and its acceptance condition>
AGY-DONE
```

A not-ready decision requires at least one actionable `- Defect:` line. Use exactly one decision line. `AGY-DONE` must be the last nonempty line. Write only the requested output file; do not write to `tmp/round/`, `ideas/`, `ledger.tsv`, or any path outside the repository mirror.
