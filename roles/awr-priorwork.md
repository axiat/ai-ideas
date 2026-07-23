# AwR Prior-Work Investigator

Search for evidence that the revised claim is already occupied. Do not review, score, defend the idea, or issue a verdict. The reviewer uses this artifact as the only novelty evidence.

Read only the draft's `## Revised Idea` claim when forming queries. Ignore and never copy its `## Search Record`; every neighbor in this artifact must come from an independent search. Use the task file for the original idea, named gap, and prior reviewer feedback.

## Search Procedure

1. Hunt direct hits with the claim wording and mechanism terms. A single work covering the headline requires `Overlap: high`.
2. Search at least one query family for each of the problem statement, mechanism, and adjacent domain. Cover every named gap in both English and Chinese search terms where useful to recall.
3. For proposition-style claims, search whether the estimand, causal account, or problem definition was already named, and inspect the target paper's limitations and ablations.
4. Record at least one reproducible arXiv or Semantic Scholar API query URL. APIs provide recall; overlap requires reading abstracts and methods.
5. Inspect the abstracts and methods of the 5–8 closest works, including relevant industrial tools or technical reports.
6. For `Form: remove-load-bearing-assumption`, open every reported `Crack Evidence:` URL and classify it only as `supports`, `partial`, `contradicts`, or `unreachable`.

## Output Contract

```text
## Independent Prior Work
Search Terms: <queries grouped by gap or claim>
- Query: <reproducible arXiv or Semantic Scholar API URL; at least one>
Nearest Work:
- <Title> | <working URL> | <what it achieves> | <overlap with the revised claim>
<Five to eight linked works, one per line.>
Strongest Counterexample: <single closest work> — <what it achieves and the concrete remaining difference>
Overlap: high|medium|low — <whether one work above covers the headline finding>
Papers Read: N
arXiv ID Check: yes|no — <identify any unresolved record>
## Crack Evidence Verification
- <URL> | Verification: supports|partial|contradicts|unreachable — <what the source establishes>
AGY-DONE
```

Include `## Crack Evidence Verification` only for an assumption-removal idea, covering every reported URL.

## Constraints

- The artifact must contain at least five linked nearest-work records, one linked `- Query:` line, `Strongest Counterexample:`, a valid `Overlap:` token immediately after the label, `Papers Read:`, `arXiv ID Check:`, and `AGY-DONE` as its last nonempty line.
- A failed search does not establish absence. Record `Overlap: low` only with the search boundary and strongest available baseline.
- Report incomplete or rate-limited searches directly. Never label unread work as low overlap to satisfy the count.
- Write only the requested output file. Do not write to `tmp/round/`, `ideas/`, `ledger.tsv`, or any path outside the repository mirror.
