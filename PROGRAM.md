# PROGRAM — Idea Research Loop Protocol

All entry points, including scheduled routines and active loops, follow this
protocol. Only a human may modify this file or the immutable inputs.

## Fixed contracts

1. `rubric.md` defines review procedure and
   `brainstorming_policy.md` defines idea forms, divergence rules, theme
   vocabulary, and verdict calibration. Agents may neither relax nor tighten
   either contract.
2. `.ai-ideas/history.sqlite3` is the sole history authority. `ledger.tsv`,
   `tmp/ledger.good`, search indexes, and near-SA views are replayable
   projections. Startup imports the operator TSV in one crash-safe epoch only
   when its durable bootstrap marker is absent; database-file existence alone
   does not prove migration completion. Later runs validate the sealed epoch
   and reconcile both TSV targets from the database.
3. Verdict is the sole success metric. The orchestrator takes the lowest valid
   vote across independent reviewer seats, so Strong Accept requires
   unanimity. No generator, selector, prescreener, researcher, or individual
   reviewer controls the final verdict.
4. Every prospective Strong Accept needs directed external prior-work
   evidence: 5–8 close works with links, `Strongest Counterexample:`, at least
   one reproducible arXiv or Semantic Scholar `- Query:`, `Papers Read:`, a
   closed `Overlap:` value, and an executable
   `Minimal Falsification Experiment:`. An assumption-removal candidate also
   needs at least two supported crack-evidence verifications.
5. A sealed prescreen kill or reviewed candidate creates one canonical row.
   Candidates beyond `SHORT_MAX` create no row because they receive no review.
   In enforcement mode, a nonpermanent internal-history result creates neither
   a research task nor a row; its sealed target remains an auditable
   `history_abstain`.
6. Generation's declared input surface contains only its role, the bounded
   generation brief, generation policy, and optional bounded research context.
   Its mirror omits the database, ledger, indexes, run archives, failure queue,
   and Git history. One confirmed parent may occupy the shared
   evolution/recheck slot. All candidates receive fresh external research and
   review.
7. Generation, comparison, and review use registered provider command
   intents and direct-host portable mirrors; the runtime retains same-user
   host authority. The portable ABI uses sealed manifests, exact
   declared-input allowlists, fresh mirrors, durable preflight receipts, and
   validated completion receipts. Reviewer mirrors form the exact
   candidate × seat product and omit sibling output.
8. Selector, prescreen, external prior-work research, and report assembly run
   in disposable mirrors containing only their declared roles and inputs. The
   host validates and atomically copies only their declared outputs.
9. Agents write no canonical state, run no publication command, and receive no
   stopping condition. The host owns database commits, projections, archives,
   report placement, and `publish.sh`.
10. During the loop, do not request human confirmation. Only the entry point
    defines the stopping condition; never lower the bar or stop early.
11. `RESEARCH_DIRECTION_FILE`, when set, names a repository-relative closed
    direction contract. The host canonicalizes the contract before any agent
    invocation. Every raw candidate must provide exact `Direction Axis`,
    `Target Failure`, and `Direction Evidence` fields. An independent selector
    classifies every candidate; malformed coverage, selector failure, or one
    `out-of-scope` verdict rejects the complete batch before history retrieval,
    prescreen, prior-work research, review, or ledger mutation.

## Startup

`hunt.sh` performs all authority checks before an agent can start:

1. Canonicalize `RESEARCH_DIRECTION_FILE` when present and write its startup
   identity. This precedes audit initialization, database synchronization,
   provider launch, and resume selection. Resume later supplies that identity
   through `expected-direction`; directed and undirected identities must match
   exactly.
2. Load the retrieval policy. Enforcement requires a matching sealed
   production calibration capability and trust root; synthetic test
   authorities are rejected by production entrypoints.
3. When the durable bootstrap marker is absent, import `ledger.tsv` once in a
   sealed epoch. The legacy near-SA snapshot participates only when the
   operator explicitly sets `HISTORY_NEAR_SA`; an invalid, missing, stale,
   symlinked, or semantically mismatched snapshot fails the epoch before an
   agent starts.
4. Reconcile pending ledger projection outbox work to `ledger.tsv` and
   `tmp/ledger.good`, including recovery after either target rename or receipt.
5. Recover and validate the published search generation.
6. Build `generation-brief.json` from bounded theme counts, structured failure
   counts, the selected divergence lens, and at most one eligible parent.

## Round protocol

The production path is:

```text
brief
-> internal generation (direct-host portable)
-> immutable candidate batch
-> directed selector classification and all-in-scope gate, when configured
-> model-free history retrieval
-> undirected selector ranking or reuse of directed ranking, then prescreen
-> sealed target selection
-> internal history comparison (direct-host portable)
-> eligible external prior-work research
-> sealed independent review matrix
-> deterministic aggregation
-> atomic DB commit
-> replayable TSV projection
-> decision archive
-> report and publication
```

### Directed candidate gate

```text
canonical direction snapshot
-> internal generation with direction_constraint.json
-> exact candidate-field validation
-> immutable schema-v2 batch with direction identity
-> selector advisory ranking plus direction.tsv classification
-> all in-scope gate
-> history retrieval, prescreen, research, review, commit
```

Generation receives the canonical `direction_constraint.json`; the selector
mirror receives the same bytes at
`tmp/round/history/direction-constraint.json`. It writes advisory `select.tsv`
and ordered `direction.tsv` rows with `in-scope` or `out-of-scope` plus bounded
evidence. The classifier is an independent model judgment enforced by a
fail-closed host; it is not a proof of natural-language meaning.

One absent, malformed, reordered, or `out-of-scope` direction row archives the
batch as `rejected:direction` before retrieval and research. Rejection leaves
the backend-failure budget unchanged and uses the short no-hit retry. The
batch manifest uses schema-v2 with `direction: null` or the exact canonical
identity. Resume validates that identity through `expected-direction`.

Direction mode sets `theme_min_low=0` because low-inventory balancing can force
off-direction candidates. Form, falsification, evidence, novelty, review, and
acceptance rules remain active. An undirected invocation keeps broad generation,
existing selector fallback, theme quota, frozen-batch compatibility, and resume
behavior.

### Candidate freeze and observation

Internal generation writes `ideas.tsv` and `ideas.md`. The host freezes their
exact bytes, one canonical candidate artifact per id, and a batch manifest
before any downstream decision. Duplicate and failure-pattern retrieval run
for every frozen candidate; evolution retrieval runs only for a validated
declared parent. Retrieval is model-free and writes ranked traces and bounded
packs.

Selection ranks the frozen batch without killing. Prescreen may kill only a
single-work direct hit with a reproducible query and occupying URL. Invalid or
missing kill evidence fails open to keep. The host seals selector and
prescreen bytes, applies deterministic priority and `SHORT_MAX`, and
materializes byte-bound full, kill, keep, and shortlist views.

### History modes

`shadow` is the shipped default. Internal comparison runs only for complete
packs and archives packs, comparisons, receipts, preflights, and completions.
No history summary enters external research or review. Retrieval status cannot
suppress, reorder, retry, reclassify, or otherwise change the frozen
downstream reference path.

`enforcement` is available only under matching production calibration.
Complete receipts are required for every selected target. A permanent
`complete_match` or `complete_no_match` result permits the target to continue;
only receipt-addressed match evidence may be mounted as a bounded history
summary. `partial`, `backend_failed`, `budget_exceeded`, `uncertain`, and
`conflicting_evidence` become `history_abstain`. A budget failure launches no
comparator. Internal no-match is scoped repository evidence, never an academic
novelty claim or an automatic verdict.

### Research, review, and commit

External research receives only the eligible candidate view, its role, and an
enforcement summary when one exists. It still performs the complete external
occupation search and produces the evidence fields in the fixed contract.

The host seals one review plan from the frozen candidate, exact prior-work
block, review contract, comparison receipt set, optional enforcement summary,
and reviewer command prefixes. Each candidate × seat review runs in a fresh
ABI-specific mirror. Aggregation validates every completion and ballot, applies
the MAJOR cap and mechanical evidence gates, assigns the lowest vote, overlap,
and non-SA category, and constructs one closed delta.

One transaction commits the round rows, vote vectors, near-SA observations,
lineage facts, and projection outbox work. Projection materialization publishes
one immutable database snapshot to both TSV targets. Shell code neither appends
canonical rows nor copies one TSV over the other.

### Resume, archive, and publication

Resume accepts only a sealed state whose policy mode and hash, source
watermark, index generation, candidate content hash, pack hash, comparator
version, adapter version, and preflight hash replay against current state.
After validation, a resumed attempt mints a distinct run ID. Its canonical
receipt binds the sealed resume digest, the previous run ID, and, when
available, the verified failure-archive receipt and tree hashes. Frozen front
artifacts remain unchanged; reviews and aggregate verdicts are always fresh.

Every round archive preserves the generation brief, frozen batch, retrieval
traces and packs, comparisons and receipts, policy authority references,
internal-stage preflights and completions, review plan and matrix,
aggregation, commit receipt, and projection receipt. Publication builds and
fsyncs a fresh sibling tree, seals its exact file manifest and authority
reference, then renames it atomically. Reuse requires the archived tree, source
attempt, and current authority inputs to verify. A failure archive never
transitions into a decision archive; a resumed attempt uses its new run ID.
A Strong Accept cannot reach report assembly or publication until the decision
archive succeeds.

For a committed Strong Accept, `roles/report.md` receives host-materialized
compatibility views in a disposable mirror. The host places the single
validated report under `ideas/` and then runs `publish.sh`. The loop stops only
when the daily `SA_TARGET` is reached or an explicit entry-point round bound
expires.

## `ledger.tsv`

The stable export has eight tab-separated columns:

```text
date	source	theme	idea	verdict	reason	overlap	category
```

- `date`: `YYYY-MM-DD`
- `source`: `weekly` or `hunt`
- `theme`: one policy theme
- `idea`: one-sentence story
- `verdict`: `strong-accept`, `accept-w-rev`, or `reject`
- `reason`: one sentence; a prescreen kill begins with
  `Prescreen direct hit:`
- `overlap`: `high`, `medium`, `low`, or `unknown`
- `category`: `novelty-dead`, `evidence-incomplete`, `design-fixable`,
  `ceiling-limited`, or `-`

Strong Accept uses category `-`. A direct hit or high overlap is
`novelty-dead`; a unanimous Strong Accept reduced only by a hard evidence gate
is `evidence-incomplete`; Accept with Revisions plus low overlap is
`design-fixable`; other Accept with Revisions rows are `ceiling-limited`.
Historical seven-column rows remain valid and project category `-`.
