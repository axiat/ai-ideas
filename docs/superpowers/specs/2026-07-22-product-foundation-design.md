# Product Foundation Design

## Goal

Establish `ai-ideas` as a coherent, operator-facing product without changing its core research protocol or historical conclusions. The repository must present a clear product boundary, expose a stable runtime artifact ABI, preserve an auditable ledger and report history, and default to a backend that does not invoke Claude automatically.

## Product Boundary

`ai-ideas` is an auditable research-idea discovery harness for embodied AI. Independent workers generate candidates, search prior work, and review evidence. Deterministic orchestration aggregates the lowest vote, records every candidate, and publishes only results that pass the configured acceptance gates.

The product-foundation rollout covers every tracked human-readable file, including `s1_report_20260720.md`. It includes source comments, operator messages, prompts, fixtures, historical reports, calibration records, and every prose field in `ledger.tsv`. Ignored runtime artifacts under `tmp/` are operational state rather than repository content and remain untouched.

The product remains named `ai-ideas`. The rollout does not invent a license, claim topic independence, add a packaging system, or implement roadmap features that do not exist.

## Repository Entry and Documentation

`README.md` becomes the product entry with this order:

1. Product name, one-sentence value proposition, and hero image.
2. Independent roles, unanimous acceptance, and auditable artifacts.
3. Prerequisites and a minimal Codex-based quick start.
4. Pipeline and artifact flow.
5. Backend configuration and explicit Claude opt-in.
6. Calibration, recovery, and trust boundaries.
7. A compact documentation index and current project status.

Detailed operational material moves into focused operator documents under `docs/` where that makes the README easier to scan. Runtime protocol files such as `PROGRAM.md`, `brainstorming_policy.md`, and `roles/*.md` stay at their existing paths because scripts consume them directly. Existing design drafts and development history remain as historical project records and are curated in place.

## Hero Asset

The project includes `assets/ai-ideas-hero.png`, generated as a wide README header. It depicts candidate ideas flowing through visually separated generation, prior-work, and review lanes into an evidence gate, then into a report and ledger. The style is precise, modern, and technical, with a restrained embodied-AI motif. It contains no text, logos, badges, watermark, or decorative UI chrome.

## Runtime Content Contract

All newly produced artifacts use the stable field names and headings defined below. Producers and consumers change atomically so every parser reads the field emitted by its producer.

The coordinated product contract includes:

- `hunt.sh`, `awr-side.sh`, calibration scripts, and their parsed labels.
- `PROGRAM.md`, `brainstorming_policy.md`, `rubric.md`, entry prompts, and every role prompt.
- Prescreen, prior-work, review, report, AwR, and calibration fixture formats.
- Operator logs, error messages, source comments, test descriptions, and workflow comments.
- The eleven theme values used by policy parsing, generation validation, ledger history, and theme-frequency accounting.

Stable machine tokens keep their exact spelling: `strong-accept`, `accept-w-rev`, `reject`, `low`, `medium`, `high`, `unknown`, `novelty-dead`, `evidence-incomplete`, `design-fixable`, `ceiling-limited`, `hunt`, and `weekly`. The 29 legacy unknown-overlap labels map one-to-one to `unknown`; their rows and semantics do not change. Dates, URLs, paper identifiers, model names, commands, counts, thresholds, and table values do not change.

## Backend Policy

Codex is the default trusted backend in `hunt.sh`, `awr-side.sh`, `litwatch.sh`, calibration, and end-to-end retrieval examples. Default commands use `approval_policy=never` and the repository's existing workspace and network boundaries. The AwR and litwatch entry points keep explicit provider adapters for compatibility, but those adapters are never an automatic fallback.

Claude remains supported only through an explicitly supplied command such as `AGENT_CMD`, `BACK_CMD`, `SIDE_CMD`, `PANEL_CMD`, or `E2E_CMD`. No script, test, example marked as default, fallback, hook, worker, or orchestration path may start Claude without that explicit selection.

Provider-specific paths keep honest trust-boundary descriptions. Documentation does not present prompt instructions, allowlists, mirrors, or path guards as adversarial security.

## Ledger and Historical Integrity

The working-tree ledger is the source input. The curation preserves all 531 data rows, including the 111 uncommitted rows copied into the feature worktree.

The historical seven-column and eight-column row shapes remain unchanged. Curation changes the theme, idea, and reason text, plus the 29 legacy unknown-overlap labels that become `unknown`. It does not otherwise normalize old rows, reorder history, reconcile frozen reports with later corrections, or alter verdicts and evidence classifications.

AwR operational identity is the append-only physical ledger row, independent of mutable prose. `awr-state-aliases.tsv` freezes the compatibility mapping from every existing eligible row to its prior state key. Migration copies compatible terminal and partial artifacts onto stable row keys, preserves shared state for duplicate ideas, upgrades feedback records to the current ABI, and validates cached drafts and prior-work evidence before reuse.

The theme vocabulary is a one-to-one mapping shared by policy, parsers, fixtures, and every ledger row. In canonical policy order, the product values are:

1. `World Models - Architecture`
2. `World Models - Training Objectives`
3. `VLA - Architecture`
4. `VLA - Training Paradigms`
5. `Action Representation`
6. `Data Engines`
7. `Evaluation and Diagnostics`
8. `Efficiency and Systems`
9. `Safety and Robustness`
10. `Cross-Domain Transfer`
11. `Human-Robot Interaction and Deployment`

Historical reports retain their factual distinctions, including frozen ballots, later corrections, calibration caveats, and supported versus falsified S1 claims.

## Verification

Completion requires evidence from all of these gates:

- No Han characters in tracked human-readable files or `s1_report_20260720.md`.
- No obsolete Chinese artifact labels in producers, parsers, prompts, or fixtures.
- Ledger row count remains 531; field-count distribution remains 216 seven-column rows and 315 eight-column rows.
- Ledger date, source, verdict, category, URL, technical-token, labeled-quantity, and numeric-operator projections remain unchanged. Themes match the documented mapping, and the 29 legacy unknown-overlap labels become `unknown` in the same rows.
- Every calibration fixture retains the same `I<n>` identifiers and expectation assertions.
- `git diff --check`, shell syntax checks, Python parsing, and deterministic litwatch tests pass.
- Fake-agent tests exercise the `hunt.sh` and AwR artifact ABI without invoking an external model.
- AwR compatibility aliases cover every existing `accept-w-rev` row and preserve terminal and partial restart behavior.
- The generated hero is inspected at full resolution and at README scale.
- README links, commands, paths, and stated defaults match the repository.

Three independent Codex reviewers audit the completed branch: product usability and information architecture, operator-facing copy and shared writing-style compliance, and runtime/schema correctness. Severe findings are fixed and the relevant gates rerun before completion.

## Delivery

All changes stay on `feat/product-foundation` in the isolated worktree. The original `main` checkout and its dirty files remain unchanged. No push, pull request, merge, or remote publication occurs without a separate request.
