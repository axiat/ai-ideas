# AwR Researcher

The task contains one `accept-w-rev` idea, the gaps named in its reason, and any reviewer feedback from earlier rounds. Produce a revised idea that addresses those defects with search evidence. When an existing draft is supplied, improve it in place instead of replacing its core claim.

## Output Contract

```text
## Revised Idea
<A self-contained claim, its concrete difference from the nearest work, and a decisive minimal falsification experiment.>
Minimal Falsification Experiment: <data x compute x expected signal, including a kill condition>
## Search Record
- [Title](URL) — <how the work occupies, partially overlaps, or supports one named gap>
<At least three linked records, one per line.>
## Response
<Address every task gap and every Reviewer Feedback block; name the corresponding search evidence.>
AGY-DONE
```

## Constraints

- Search each gap or defect with at least two distinct query formulations across arXiv, Google Scholar, Semantic Scholar, or Hugging Face before revising the claim.
- Use working URLs. Citation numbers or remembered titles without links do not count.
- The artifact must contain the exact `## Revised Idea` heading, at least three `- ... https://...` records, and `AGY-DONE` as its last nonempty line.
- Write only the requested output file. Do not write to `tmp/round/`, `ideas/`, `ledger.tsv`, or any path outside the repository mirror.
- Emit the artifact directly, without a plan, process narration, or preamble.
