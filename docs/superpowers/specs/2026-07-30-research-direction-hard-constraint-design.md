# Research-Direction Hard Constraint Design

Status: draft

## Goal

`hunt.sh` accepts an optional, versioned research-direction contract. When a
contract is active, every raw candidate in the generated batch must satisfy
it. Missing structural evidence, an unavailable semantic check, or one
out-of-scope candidate prevents the entire batch from entering history
comparison, prior-work research, review, or the ledger.

The initial contract covers dynamic-environment 3D spatial memory for
VLA-controlled humanoid robots. Runs without a direction contract preserve the
current broad World Models, VLA, and embodied-AI behavior.

## Invariants

| Invariant | Enforcement |
|---|---|
| A direction is an explicit run input | `RESEARCH_DIRECTION_FILE` names one repository-relative, regular JSON file. The host validates and canonicalizes it before any agent runs. |
| Every raw candidate is covered | Direction mode requires `Direction Axis`, `Target Failure`, and `Direction Evidence` in every `I<n>` block. Missing or invalid fields fail generation. |
| Generator self-attestation is insufficient | The existing selector independently classifies every candidate as `in-scope` or `out-of-scope` against the same canonical direction snapshot. |
| Direction checking fails closed | Selector failure, a missing or malformed direction verdict, an ID mismatch, or any `out-of-scope` verdict rejects the whole batch. |
| Rejected candidates consume no downstream research | Direction checking completes before history comparison, prescreen, prior-work research, review, and ledger mutation. |
| A resumed run cannot change direction | Resume is allowed only when the current canonical direction hash equals the sealed front-state direction hash, including the distinction between directed and undirected runs. |
| Direction mode does not weaken quality rules | Candidate form, falsification experiment, evidence, novelty research, review, and acceptance gates remain active. |
| Existing invocations remain compatible | With `RESEARCH_DIRECTION_FILE` unset, generation fields, selector output, theme balancing, and resume behavior retain their current contracts. |

Natural-language scope cannot be proven mechanically. The hard guarantee is
orchestration-level fail-closed behavior around an independent semantic
classification. Exact axis and failure-condition fields provide a deterministic
first gate; the selector supplies the semantic judgment.

## Direction Contract

Direction files live under `directions/` and use a closed JSON schema:

```json
{
  "schema_version": 1,
  "direction_id": "dynamic-spatial-memory-vla-v1",
  "statement": "Research persistent, queryable, dynamically correctable 3D spatial memory for 2D-vision VLA foundation models controlling humanoid robots in long-horizon tasks that combine navigation and manipulation.",
  "all_candidates_must_match": true,
  "allowed_axes": [
    {
      "id": "memory-representation-update",
      "description": "Represent, persist, invalidate, protect, and repair embodied 3D memory under scene changes."
    },
    {
      "id": "memory-to-vla-injection",
      "description": "Inject 3D memory into a frozen GR00T backbone through spatial tokens, target-pose embeddings, rendered visual prompts, or a lightweight adapter."
    },
    {
      "id": "disturbance-recovery-evaluation",
      "description": "Measure attributable gains over a 2D VLA baseline in long-horizon disturbance and recovery tasks."
    }
  ],
  "target_failures": [
    {
      "id": "out-of-view-target",
      "description": "The task target is outside the current camera view."
    },
    {
      "id": "dynamic-scene-change",
      "description": "Objects move or people alter the scene after memory formation."
    },
    {
      "id": "task-interruption-recovery",
      "description": "Execution is interrupted and must resume from persistent spatial state."
    }
  ],
  "fixed_constraints": [
    "Use GR00T N1.6 as the VLA skill foundation and preserve its pretrained backbone.",
    "Remain compatible with the EngineAI PM01-EDU or T800 platform and the existing layered controller.",
    "Treat locomotion and whole-body control as execution infrastructure.",
    "Attribute the measured task effect to the 3D-memory contribution."
  ],
  "excluded_scopes": [
    "Generic World Model, VLA, or robotics research without dynamically correctable 3D memory.",
    "Pure locomotion, gait, or whole-body-control research.",
    "Generic SLAM, mapping, or scene graphs without a measured VLA decision or task-execution effect.",
    "Static-only or single-frame perception.",
    "Training a VLA from scratch.",
    "Large-scale retraining or architectural replacement of the GR00T backbone.",
    "Reproducing a closed OminiA-style end-to-end model.",
    "System integration without a research question and falsifiable experiment."
  ]
}
```

The validator rejects unknown top-level or nested fields, duplicate IDs,
control characters, empty strings, oversized strings, an empty axis or failure
set, and `all_candidates_must_match` values other than `true`. The canonical
UTF-8 JSON is bounded to 16 KiB.

## Operator Interface

```bash
RESEARCH_DIRECTION_FILE='directions/dynamic-spatial-memory-vla-v1.json' \
  caffeinate -is ./hunt.sh
```

The path must remain under the repository, name a regular single-link file,
and resolve without symlink traversal. Inline direction prose is not an
interface: the contract is long, structured, versionable, and must have a
stable content hash.

## Runtime Flow

### Canonical snapshot

Before startup and resume selection, the host validates the requested contract,
writes a canonical snapshot under `tmp/history-startup/`, and records its
SHA-256. An undirected run uses a distinct `none` identity.

At the start of a new round, the canonical snapshot and hash are copied into
`tmp/round/history/`. Both are covered by normal round archival. The source
file may change after startup without changing the active round.

Batch freezing records either `direction: null` or the exact
`direction_id`/SHA-256 pair in `batch.json`. Selection, comparison, review,
aggregation, commit, and resume receipts already depend on the frozen batch
identity, so the direction identity remains bound throughout the round.

### Contained generation

`direction_constraint.json` becomes an optional registered input for the
contained `generate` stage. The exact canonical bytes enter the existing
serialized invocation and prompt hash.

When the input is mounted, `roles/generate.md` requires every candidate block
to include:

```text
Direction Axis: <one exact allowed_axes id>
Target Failure: <one exact target_failures id>
Direction Evidence: <one bounded sentence connecting the proposition and experiment to the contract>
```

The host markdown parser validates presence, uniqueness, length, and exact enum
membership before projecting `ideas.tsv`. The TSV remains the existing
three-column `id/story/theme` index; direction evidence remains in the
canonical candidate Markdown.

The direction contract overrides only the broad-expansion and low-inventory
theme requirements that can force unrelated candidates. `themes_ok` still
requires every candidate to use the existing policy vocabulary. The random
divergence lens remains available only as an in-direction ideation aid.

### Independent selector check

The disposable selector mirror receives the same canonical direction snapshot.
In addition to its existing `select.tsv`, the selector writes:

```text
id	direction-fit	direction-evidence
I1	in-scope	<one sentence tied to the candidate proposition and experiment>
```

`direction-fit` is exactly `in-scope` or `out-of-scope`. `direction.tsv` must
cover every generated candidate exactly once and in candidate order.

The host copies both selector outputs from the disposable mirror. In direction
mode, selector failure no longer falls back to generation order. The direction
gate rejects missing files, malformed rows, duplicate or unknown IDs, missing
evidence, and every `out-of-scope` verdict. The existing selector ranking
remains advisory after the direction gate passes.

### Rejection and retry

A semantically rejected batch is archived as `rejected:direction` and waits
using the existing short no-hit delay before the next round. It is not treated
as an infrastructure failure and does not use the default 150-minute failure
sleep. `ROUND_LIMIT` still bounds directed runs.

Contained-stage errors, malformed generator output, and other technical
failures retain `fail_round` and `MAX_FAILS` behavior.

### Resume identity

The sealed front state records the direction identity and canonical SHA-256.
Startup compares these values before accepting `resume-state.json`. A changed,
added, or removed contract discards the old front state and starts generation
again. A byte-identical canonical contract may resume even if insignificant
source formatting changed.

## Components

| Component | Responsibility |
|---|---|
| `directions/dynamic-spatial-memory-vla-v1.json` | Artifact-derived initial direction contract. |
| `lib/direction_contract.py` | Closed-schema validation, canonicalization, hashing, candidate-field validation, and selector-verdict validation. |
| `hunt.sh` | Operator interface, snapshot lifecycle, mounted input, fail-closed selector behavior, short rejection retry, theme-quota override, and resume hash comparison. |
| `lib/history_stage.py` | Registered direction input and generation Markdown validation against the mounted contract. |
| `lib/history_budget.py` | Exact optional-mount profile and prompt-budget accounting. |
| `lib/history_runtime.py` | Direction identity in frozen batches and resume validation, plus direction-aware theme-quota selection. |
| `roles/generate.md` | Required candidate direction fields and precedence over broad expansion. |
| `roles/select.md` | Independent direction classification and `direction.tsv` output. |
| `brainstorming_policy.md` | Explicit precedence rule for directed runs. |
| `README.md`, `docs/getting-started.md`, `PROGRAM.md`, `docs/architecture.md` | Operator command and runtime contract. |

No new model call or backend is introduced. The semantic gate reuses the
selector that already reads every raw candidate.

## Test Strategy

Tests use the repository's fake backends and do not invoke an external model.

1. Direction-contract unit tests reject schema drift, unsafe paths, duplicate
   IDs, invalid UTF-8/control characters, oversized input, and non-canonical
   variants while producing a stable semantic hash.
2. Generation-stage tests first fail on absent fields, unknown axis IDs,
   unknown failure IDs, duplicate fields, and empty evidence; valid directed
   Markdown passes and preserves the existing TSV shape.
3. Selector tests first fail on missing `direction.tsv`, selector failure,
   malformed rows, ID mismatch, duplicate IDs, empty evidence, and one
   `out-of-scope` verdict; a complete all-`in-scope` batch passes.
4. Resume tests prove that identical canonical contracts resume and changed,
   added, or removed contracts regenerate.
5. Compatibility tests prove that undirected fake-backend runs retain selector
   fallback, low-inventory theme balancing, current candidate format, and
   existing resume behavior.
6. Product-contract tests require the documented environment variable,
   direction file, role instructions, runtime mounts, and fail-closed gate to
   agree.

Fresh verification covers shell syntax, direction and stage tests, history
runtime smoke tests, generation contracts, product contracts, and
`git diff --check`.
