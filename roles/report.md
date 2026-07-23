# Role: Report Assembly (no review or verdict changes)

The pipeline has fixed every verdict. Transfer and format source material without adding, rewriting, or aggregating review conclusions.

## Read

- `tmp/round/accepted.tsv`: qualified ideas as `id<TAB>one-sentence story`.
- `tmp/round/ideas.md`: idea text.
- `tmp/round/priorwork.md`: prior-work records and literature.
- `tmp/round/rev/1/review.md`: Reviewer 1's full reviews.
- `tmp/round/rejects.tsv`: rejected ideas as `id<TAB>one-sentence story<TAB>reason`.
- `tmp/round/meta.txt`: attempt count, review date, and reviewer count.

## Write

Create `ideas/YYYY-MM-DD_hunt.md`, using the date from `meta.txt`. Add `-2`, `-3`, and so on for repeated reports on the same date. Use this structure:

1. **Key Literature:** Transfer only literature facts and links from `priorwork.md` that directly concern qualified ideas. Do not calculate new differences or rewrite arms from different tables, datasets, or experiments as a matched contrast. Omit numbers when the input contains no reliable value.
2. **Qualified Ideas:** Take ids only from `accepted.tsv`. For each, transfer the idea text, then write the sole panel-wide verdict sentence from the reviewer count in `meta.txt`: “The single independent reviewer returned Strong Accept.” for one reviewer, or “All <reviewer count> independent reviewers returned Strong Accept.” for more than one. Add a `Reviewer 1 Full Review` heading, then copy that id's block from `rev/1/review.md`, starting after its `## I<n>` heading and ending immediately before the next `## I<n>` or end of file. Preserve the body as one contiguous verbatim span. Do not edit, reorder, indent, quote, or wrap it in a code fence. Finish with the idea's directed prior-work record from `priorwork.md`.
3. **Rejected Ideas:** Transfer the one-sentence story and reason directly from `rejects.tsv`.
4. **Metadata:** Transfer the attempt count and review date.

## Evidence Boundary

- The unanimous-verdict sentence above is the only permitted panel-wide review conclusion.
- `rev/1/review.md` represents Reviewer 1 only. Do not infer “all,” “unanimous,” “consistent,” or any panel-wide CRITICAL/MAJOR count from it. With one reviewer, use the singular panel sentence above; any other reviewer-specific count may appear only inside the verbatim Reviewer 1 block.
- Do not transfer a full review block for any id absent from `accepted.tsv`.
- Literature facts must come from `priorwork.md`. Do not add prior-work research, arithmetic, comparisons, or review.

Preserve verdicts and scores verbatim. Write only under `ideas/` and do not run publication commands; the orchestrator owns publication.
