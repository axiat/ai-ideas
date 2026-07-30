# Research-Direction Hard Constraint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task by task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, versioned research-direction contract to `hunt.sh`
so every raw candidate in a directed batch must pass deterministic structural
checks and an independent selector classification before any history
retrieval, prescreen, prior-work research, review, or ledger mutation.

**Architecture:** A repository-relative JSON contract is validated and
canonicalized before agent invocation. Its canonical identity is mounted into
contained generation, frozen into batch schema v2, checked against resume
state, and supplied to the existing selector. Generation supplies exact
axis/failure evidence fields; the selector supplies a separate per-candidate
semantic verdict. Directed selector failures, malformed coverage, or one
`out-of-scope` row reject and archive the whole batch with a short retry.
Undirected runs keep the current contracts and fallback behavior.

**Tech Stack:** Bash 3.2, Python 3.9 standard library, canonical JSON, TSV,
Markdown, `unittest`, deterministic fake backends, and Git.

## Global Constraints

- Work in `/Users/qinningxu/code/ai-ideas`. Preserve the user's existing
  uncommitted `ledger.tsv` change byte-for-byte and exclude it from every
  commit.
- Never invoke Claude or another external model while implementing or testing.
  The direction contract below is the already approved artifact extraction.
- Use `RESEARCH_DIRECTION_FILE` as the sole operator input. It must name a
  repository-relative, non-symlinked, single-link regular file.
- Canonical direction JSON is UTF-8, closed-schema, newline-terminated, and at
  most 16 KiB. Hash the canonical bytes, not source formatting.
- In direction mode, all raw candidates must carry exact direction fields and
  all selector verdicts must be `in-scope`. One violation rejects the whole
  batch.
- Place the directed selector gate after batch freezing and before
  `observe-round`, prescreen, comparison, prior-work research, review, commit,
  or ledger projection.
- Reuse the existing selector invocation. Do not add a model call, backend,
  judge stage, or second generation pass.
- Directed selector failure is fail-closed. Undirected selector failure keeps
  the current generation-order fallback.
- `rejected:direction` is a semantic rejection archive class. It does not
  increment `fails`, consume `MAX_FAILS`, or use `FAIL_SLEEP_MIN`.
- A direction-rejection archive failure exits nonzero before the round
  directory can be replaced. Rejected evidence must not be silently lost.
- Direction mode sets the effective low-inventory theme quota to zero because
  the quota can force off-direction candidates. Existing theme vocabulary,
  form, falsification, evidence, novelty, review, and acceptance gates remain.
- A direction identity is either canonical JSON `null` or exactly
  `{"direction_id": STRING, "sha256": HEX64}`. Added, removed, or changed
  identities invalidate resume; source whitespace changes do not.
- New frozen batches use schema v2 and bind `direction`. Verification must
  continue accepting archived schema-v1 batches as undirected.
- Without `RESEARCH_DIRECTION_FILE`, generation fields, selector output,
  low-inventory balancing, batch behavior, and resume behavior remain
  compatible.
- Use test-driven development: add one focused failing test, run it and confirm
  the intended failure, implement the smallest behavior, then rerun the
  focused test before each commit.
- All tracked prose remains English, minimal, bounded, and dense.

---

### Task 1: Closed Direction Contract and Verdict Validator

**Files:**

- Create: `lib/direction_contract.py`
- Create: `directions/dynamic-spatial-memory-vla-v1.json`
- Create: `tests/direction_contract_smoke.py`
- Modify: `CONTRIBUTING.md`

**Interfaces:**

```python
class DirectionContractError(ValueError):
    pass


def canonical_bytes(value):
    """Return sorted compact UTF-8 JSON with one trailing LF."""


def parse_contract_bytes(raw):
    """Return (contract, canonical_raw, identity)."""


def load_contract(source, repo_root):
    """Safely read a repository-relative contract and return the same tuple."""


def write_snapshot(source, repo_root, output_path, identity_path):
    """Atomically write a canonical contract and identity; source=None writes null identity."""


def validate_identity(value):
    """Return None or an exact direction_id/SHA-256 identity object."""


def validate_candidate_fields(values, contract, candidate_id):
    """Validate Direction Axis, Target Failure, and Direction Evidence."""


def parse_direction_verdicts(raw, candidate_ids):
    """Validate the exact header, ordered ID coverage, enums, and evidence."""


def require_all_in_scope(raw, candidate_ids):
    """Return parsed verdicts or raise when any verdict is out-of-scope."""
```

CLI:

```text
python3 lib/direction_contract.py snapshot \
  --repo-root PATH \
  --identity-output PATH \
  [--source REPO_RELATIVE_PATH --output PATH]

python3 lib/direction_contract.py validate-verdicts \
  --contract PATH \
  --ideas PATH \
  --verdicts PATH \
  --output PATH
```

`snapshot` without `--source` writes `null\n` only. With `--source`, both
`--source` and `--output` are required. `validate-verdicts` writes a canonical
receipt only when every candidate is in scope:

```json
{
  "schema_version": 1,
  "direction": {
    "direction_id": "dynamic-spatial-memory-vla-v1",
    "sha256": "50bbf68a8ee20f2635194abab2a41ee702d4ec227b5277bf1bba9f463fee0d85"
  },
  "candidate_count": 2,
  "verdicts": [
    {
      "candidate_id": "I1",
      "direction_fit": "in-scope",
      "evidence": "The proposition tests correctable 3D memory."
    },
    {
      "candidate_id": "I2",
      "direction_fit": "in-scope",
      "evidence": "The experiment isolates memory injection."
    }
  ]
}
```

- [ ] **Step 1: Write the failing contract tests**

Create `tests/direction_contract_smoke.py` with literal assertions:

```python
class DirectionContractSmoke(unittest.TestCase):
    def test_initial_contract_has_stable_canonical_identity(self):
        value, raw, identity = direction_contract.load_contract(
            "directions/dynamic-spatial-memory-vla-v1.json",
            ROOT,
        )
        self.assertEqual(value["direction_id"], "dynamic-spatial-memory-vla-v1")
        self.assertEqual(len(raw), 2072)
        self.assertEqual(
            identity,
            {
                "direction_id": "dynamic-spatial-memory-vla-v1",
                "sha256":
                    "50bbf68a8ee20f2635194abab2a41ee702d4ec227b5277bf1bba9f463fee0d85",
            },
        )
        self.assertTrue(raw.endswith(b"\n"))

    def test_source_formatting_does_not_change_identity(self):
        compact = json.dumps(self.contract, ensure_ascii=False).encode("utf-8")
        pretty = json.dumps(
            self.contract, indent=4, ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(
            direction_contract.parse_contract_bytes(compact)[1:],
            direction_contract.parse_contract_bytes(pretty)[1:],
        )

    def test_schema_is_closed_and_duplicate_json_keys_are_rejected(self):
        invalid = dict(self.contract, unexpected=True)
        with self.assertRaises(direction_contract.DirectionContractError):
            direction_contract.parse_contract_bytes(
                json.dumps(invalid).encode("utf-8")
            )
        with self.assertRaises(direction_contract.DirectionContractError):
            direction_contract.parse_contract_bytes(
                b'{"schema_version":1,"schema_version":1}'
            )

    def test_ids_controls_and_bounds_are_rejected(self):
        cases = [
            self.changed(direction_id="UPPER CASE"),
            self.changed(statement="contains\u0001control"),
            self.changed(statement="x" * 16385),
            self.with_duplicate_axis_id(),
            self.with_unknown_axis_field(),
            self.changed(all_candidates_must_match=False),
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(
                    direction_contract.DirectionContractError
                ):
                    direction_contract.parse_contract_bytes(
                        json.dumps(value).encode("utf-8")
                    )

    def test_loader_rejects_absolute_parent_symlink_and_hardlink_paths(self):
        for source in (
            str(self.repo / "direction.json"),
            "../direction.json",
            "direction-link.json",
            "direction-hardlink.json",
        ):
            with self.subTest(source=source):
                with self.assertRaises(
                    direction_contract.DirectionContractError
                ):
                    direction_contract.load_contract(source, self.repo)

    def test_candidate_fields_use_exact_contract_enums(self):
        values = {
            "Direction Axis": "memory-representation-update",
            "Target Failure": "dynamic-scene-change",
            "Direction Evidence":
                "The repair experiment attributes recovery to corrected 3D memory.",
        }
        direction_contract.validate_candidate_fields(
            values, self.contract, "I1"
        )
        for field, invalid in (
            ("Direction Axis", "memory"),
            ("Target Failure", "navigation"),
            ("Direction Evidence", ""),
        ):
            changed = dict(values, **{field: invalid})
            with self.subTest(field=field):
                with self.assertRaises(
                    direction_contract.DirectionContractError
                ):
                    direction_contract.validate_candidate_fields(
                        changed, self.contract, "I1"
                    )

    def test_identity_schema_is_closed(self):
        valid = {
            "direction_id": "dynamic-spatial-memory-vla-v1",
            "sha256":
                "50bbf68a8ee20f2635194abab2a41ee702d4ec227b5277bf1bba9f463fee0d85",
        }
        self.assertEqual(direction_contract.validate_identity(valid), valid)
        self.assertIsNone(direction_contract.validate_identity(None))
        for value in (
            {},
            dict(valid, unexpected=True),
            dict(valid, sha256="0" * 63),
            dict(valid, direction_id="UPPER CASE"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(
                    direction_contract.DirectionContractError
                ):
                    direction_contract.validate_identity(value)

    def test_direction_verdicts_require_header_order_coverage_and_scope(self):
        valid = (
            b"id\tdirection-fit\tdirection-evidence\n"
            b"I1\tin-scope\tThe proposition tests correctable 3D memory.\n"
            b"I2\tin-scope\tThe experiment isolates memory injection.\n"
        )
        parsed = direction_contract.require_all_in_scope(
            valid, ["I1", "I2"]
        )
        self.assertEqual(
            [item["candidate_id"] for item in parsed],
            ["I1", "I2"],
        )
        invalid_values = [
            valid.replace(b"I2\tin-scope", b"I2\tout-of-scope"),
            valid.replace(b"I2\t", b"I1\t"),
            valid.replace(b"I1\t", b"I2\t", 1),
            valid.replace(b"\tThe experiment isolates memory injection.", b"\t"),
            valid.replace(
                b"id\tdirection-fit\tdirection-evidence\n", b""
            ),
        ]
        for raw in invalid_values:
            with self.subTest(raw=raw):
                with self.assertRaises(
                    direction_contract.DirectionContractError
                ):
                    direction_contract.require_all_in_scope(
                        raw, ["I1", "I2"]
                    )
```

The fixture setup creates a private temporary repository root, writes a real
single-link contract, creates one symlink and one hardlink, and cleans it with
`TemporaryDirectory`.

- [ ] **Step 2: Run the contract test and verify RED**

Run:

```bash
rtk python3 tests/direction_contract_smoke.py
```

Expected: FAIL because `lib/direction_contract.py` and the initial contract do
not exist.

- [ ] **Step 3: Implement the closed schema and safe loader**

Use these exact schema rules:

- Top-level fields:
  `schema_version`, `direction_id`, `statement`,
  `all_candidates_must_match`, `allowed_axes`, `target_failures`,
  `fixed_constraints`, `excluded_scopes`.
- `schema_version` is integer `1`; booleans do not count as integers.
- `all_candidates_must_match` is the boolean `true`.
- IDs match `[a-z0-9]+(?:-[a-z0-9]+)*` and are at most 96 UTF-8 bytes.
- `statement`, descriptions, fixed constraints, and exclusions are nonempty,
  contain no Unicode control characters, and are at most 2048 UTF-8 bytes.
- `allowed_axes` and `target_failures` contain 1–16 unique IDs. Each element
  has exactly `id` and `description`.
- `fixed_constraints` and `excluded_scopes` contain 1–32 unique strings.
- Duplicate JSON object keys are rejected through `object_pairs_hook`.
- Canonical output is
  `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False) +
  "\n"` and is at most 16384 bytes.
- Candidate direction evidence is one nonempty line, contains no tab or
  control character, and is at most 2048 UTF-8 bytes.
- The validator's ideas input is the frozen batch's
  `sources/ideas.tsv`. It must contain at least one exact three-column row,
  valid unique `I[1-9][0-9]*` IDs, and no blank or header row.
- `direction.tsv` starts with the exact three-column header from the test,
  followed by one row per candidate in exact batch order. Evidence is one
  nonempty field of at most 2048 UTF-8 bytes.

For `load_contract`, reject absolute paths, empty paths, `.`/`..` components,
symlinked components, a non-directory parent, a non-regular final component,
`st_nlink != 1`, files larger than 16384 bytes, and size changes between
`fstat` and read. Open the final file with `O_NOFOLLOW` where available.

Write snapshots with a same-directory `O_EXCL` temporary file, `fsync`, atomic
replace, and parent-directory `fsync`. The startup directory is single-use, so
snapshot destinations must not pre-exist.

- [ ] **Step 4: Add the approved initial contract**

Create `directions/dynamic-spatial-memory-vla-v1.json` with this exact semantic
content. Formatting may be indented; the test pins canonical bytes and hash.

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

- [ ] **Step 5: Run the focused test and document it**

Run:

```bash
rtk python3 tests/direction_contract_smoke.py
rtk git diff --check
```

Add `rtk python3 tests/direction_contract_smoke.py` to the local validation
list in `CONTRIBUTING.md`.

- [ ] **Step 6: Commit Task 1**

```bash
rtk git add lib/direction_contract.py \
  directions/dynamic-spatial-memory-vla-v1.json \
  tests/direction_contract_smoke.py CONTRIBUTING.md
rtk git commit -m "feat: add versioned research direction contract"
```

---

### Task 2: Bind Direction Structure to Contained Generation

**Files:**

- Modify: `lib/history_budget.py`
- Modify: `lib/history_runtime.py`
- Modify: `lib/history_stage.py`
- Modify: `roles/generate.md`
- Modify: `brainstorming_policy.md`
- Modify: `tests/history_budget_smoke.py`
- Modify: `tests/history_runtime_smoke.py`
- Modify: `tests/history_stage_smoke.py`
- Modify: `tests/fake_stage_agent.py`

**Interfaces:**

- Optional generate mount:
  `direction_constraint.json`, maximum 16384 bytes.
- Parser:
  `_build_generation_tsv_from_markdown(markdown,
  direction_contract=None)`.
- Directed candidate fields:

```text
Direction Axis: <exact allowed_axes id>
Target Failure: <exact target_failures id>
Direction Evidence: <one bounded sentence>
```

- [ ] **Step 1: Add the failing mount-profile tests**

In `tests/history_budget_smoke.py`, extend
`test_generate_mount_profile_is_exact_and_bounded`:

```python
self.assertEqual(
    history_budget._STAGE_REQUIREMENTS["generate"]["optional_mounts"],
    {"research_context.md", "direction_constraint.json"},
)
```

In `tests/history_stage_smoke.py`, assert:

```python
self.assertEqual(
    history_stage._INPUT_CAPS["direction_constraint.json"],
    16384,
)
self.assertEqual(
    history_stage._STAGE_PROFILES["generate"]["optional_inputs"],
    {"research_context.md", "direction_constraint.json"},
)
```

In `tests/history_runtime_smoke.py`, assert the production manifest builder's
closed profile:

```python
self.assertEqual(
    history_runtime._INPUT_CAPS["direction_constraint.json"],
    16384,
)
self.assertEqual(
    history_runtime._STAGE_INPUTS["generate"],
    (
        {"generation_brief.json", "generation_policy.md"},
        {"research_context.md", "direction_constraint.json"},
    ),
)
```

- [ ] **Step 2: Run the mount tests and verify RED**

Run:

```bash
rtk python3 tests/history_budget_smoke.py
rtk python3 tests/history_runtime_smoke.py
rtk python3 tests/history_stage_smoke.py
```

Expected: FAIL because the new optional mount is outside all three exact
profiles.

- [ ] **Step 3: Register and parse the optional input**

Add `direction_constraint.json: 16384` to `_INPUT_CAPS` in
`history_runtime.py` and `history_stage.py`. Add it to the generate optional
input sets in `history_runtime.py`, `history_budget.py`, and
`history_stage.py`. This allows the production manifest builder to capture the
mount before the contained stage validates it. In `_parse_stage_inputs`, parse
the captured bytes with `direction_contract_lib.parse_contract_bytes`, reject
noncanonical mounted bytes, and store the validated contract in
`parsed["direction_constraint.json"]`.

The mounted file is a host canonical snapshot, so:

```python
contract, canonical_raw, _ = direction_contract_lib.parse_contract_bytes(
    captured["direction_constraint.json"]["raw"]
)
if canonical_raw != captured["direction_constraint.json"]["raw"]:
    raise StageError("direction contract is not canonical")
parsed["direction_constraint.json"] = contract
```

- Import the module as `direction_contract_lib` anywhere a function argument
  or local variable is named `direction_contract`.

- [ ] **Step 4: Add failing directed-Markdown tests**

Add one helper that returns valid existing candidate Markdown and optionally
inserts these lines:

```text
Direction Axis: memory-representation-update
Target Failure: dynamic-scene-change
Direction Evidence: The repair arm attributes recovery to corrected 3D memory.
```

Add table-driven tests:

```python
def test_directed_generation_requires_exact_candidate_fields(self):
    contract = self.direction_contract()
    invalid = {
        "missing": self.generation_markdown(direction_lines=[]),
        "unknown-axis": self.generation_markdown(
            direction_axis="generic-memory"
        ),
        "unknown-failure": self.generation_markdown(
            target_failure="navigation"
        ),
        "empty-evidence": self.generation_markdown(
            direction_evidence=""
        ),
        "duplicate-axis": self.generation_markdown(
            duplicate_direction_axis=True
        ),
    }
    for name, markdown in invalid.items():
        with self.subTest(name=name):
            with self.assertRaises(history_stage.StageError):
                history_stage._build_generation_tsv_from_markdown(
                    markdown, direction_contract=contract
                )

def test_valid_directed_generation_preserves_three_column_tsv(self):
    projected = history_stage._build_generation_tsv_from_markdown(
        self.generation_markdown(),
        direction_contract=self.direction_contract(),
    )
    self.assertEqual(
        projected,
        "I1\tConstraint-Driven Sparse World Models\t"
        "World Models - Architecture\n",
    )

def test_undirected_generation_keeps_current_candidate_shape(self):
    projected = history_stage._build_generation_tsv_from_markdown(
        self.generation_markdown(direction_lines=[])
    )
    self.assertEqual(
        projected,
        "I1\tConstraint-Driven Sparse World Models\t"
        "World Models - Architecture\n",
    )
```

Also add an actual contained-stage case proving that the optional input is
accepted by `history_runtime.build_stage_manifest`, captured in the manifest
and prompt serialization, and that tampering with the mounted snapshot fails
completion.

- [ ] **Step 5: Run the generation tests and verify RED**

Run:

```bash
rtk python3 tests/history_stage_smoke.py
```

Expected: FAIL because `_build_generation_tsv_from_markdown` does not accept or
validate a direction contract.

- [ ] **Step 6: Implement the structural gate**

When a direction contract is present:

- add the three direction labels to the per-candidate single-value parser;
- enforce presence, uniqueness, nonempty bounded values, and call
  `direction_contract.validate_candidate_fields`;
- load the mounted canonical contract in `_project_generation_tsv`;
- pass the parsed contract again from `validate_stage_outputs` when rebuilding
  the expected TSV.

When absent, do not require or interpret the three fields. Preserve the
existing host-owned three-column TSV projection.

- [ ] **Step 7: Update generation policy and fixture output**

In `roles/generate.md`:

- add `direction_constraint.json` as an optional authoritative input;
- require all three fields in every candidate when mounted;
- require every proposition and falsification experiment to stay within the
  statement, fixed constraints, and exclusions;
- state that the direction contract overrides only broad expansion,
  low-inventory theme coverage, and off-direction use of a divergence lens.

In `brainstorming_policy.md`, add one precedence rule under divergence:

```text
When `direction_constraint.json` is mounted, every raw candidate must satisfy
that contract. Its scope overrides free cross-domain expansion, low-inventory
theme coverage, and off-direction divergence-lens use; all quality, form,
evidence, falsification, and review rules remain active.
```

Update `tests/fake_stage_agent.py` so generation checks
`input/direction_constraint.json`. If present, emit an in-direction proposition
plus the exact three fields; if absent, preserve its current fixture output.

- [ ] **Step 8: Run focused stage verification**

Run:

```bash
rtk python3 tests/direction_contract_smoke.py
rtk python3 tests/history_budget_smoke.py
rtk python3 tests/history_runtime_smoke.py
rtk python3 tests/history_stage_smoke.py
rtk git diff --check
```

- [ ] **Step 9: Commit Task 2**

```bash
rtk git add lib/history_budget.py lib/history_runtime.py \
  lib/history_stage.py \
  roles/generate.md brainstorming_policy.md \
  tests/history_budget_smoke.py tests/history_runtime_smoke.py \
  tests/history_stage_smoke.py \
  tests/fake_stage_agent.py
rtk git commit -m "feat: bind research direction to generation"
```

---

### Task 3: Freeze Direction Identity and Fence Resume

**Files:**

- Modify: `lib/history_runtime.py`
- Modify: `tests/history_runtime_smoke.py`
- Modify: `tests/history_runtime_smoke.sh`

**Interfaces:**

```python
def freeze_candidate_batch(
    ideas_tsv,
    ideas_md,
    output_root,
    generation_brief=None,
    direction_contract=None,
):
    ...


def frozen_batch_direction(manifest):
    """Return None for schema v1 or the validated schema-v2 direction identity."""


def validate_resume_state(
    resume_path,
    authority=None,
    expected_direction=_DIRECTION_UNSPECIFIED,
):
    ...
```

CLI additions:

```text
freeze-batch ... [--direction PATH]
validate-resume ... [--expected-direction PATH]
```

`--expected-direction` points to canonical JSON containing either `null` or
the exact two-field identity object.

- [ ] **Step 1: Add failing batch-v2 tests**

Extend `CandidateAndObservationContract`:

```python
def test_directed_batch_v2_binds_canonical_direction_identity(self):
    result = history_runtime.freeze_candidate_batch(
        self.ideas_tsv,
        self.ideas_md,
        self.root / "directed-batch",
        direction_contract=self.direction_contract,
    )
    self.assertEqual(result["schema_version"], 2)
    self.assertEqual(
        result["direction"],
        {
            "direction_id": "dynamic-spatial-memory-vla-v1",
            "sha256":
                "50bbf68a8ee20f2635194abab2a41ee702d4ec227b5277bf1bba9f463fee0d85",
        },
    )
    history_runtime.verify_frozen_batch(result)

def test_new_undirected_batch_v2_records_null_direction(self):
    result = history_runtime.freeze_candidate_batch(
        self.ideas_tsv,
        self.ideas_md,
        self.root / "undirected-batch",
    )
    self.assertEqual(result["schema_version"], 2)
    self.assertIsNone(result["direction"])

def test_schema_v1_batch_remains_verifiable_as_undirected(self):
    manifest = self.schema_v1_batch_fixture()
    self.assertTrue(history_runtime.verify_frozen_batch(manifest))
    self.assertIsNone(history_runtime.frozen_batch_direction(manifest))

def test_batch_direction_tamper_breaks_v2_hash(self):
    manifest = self.directed_batch()
    manifest["direction"]["direction_id"] = "changed"
    with self.assertRaises(history_runtime.RuntimeContractError):
        history_runtime.verify_frozen_batch(manifest)

def test_malformed_direction_identity_fails_after_rehash(self):
    manifest = self.directed_batch()
    manifest["direction"] = {
        "direction_id": "dynamic-spatial-memory-vla-v1",
        "sha256": "0" * 63,
    }
    material = dict(manifest)
    material.pop("batch_sha256")
    manifest["batch_sha256"] = history_runtime.sha256(
        b"history-runtime-batch-v2\0"
        + history_runtime.canonical_bytes(material)
    )
    with self.assertRaises(history_runtime.RuntimeContractError):
        history_runtime.verify_frozen_batch(manifest)
```

The schema-v1 fixture must use the existing exact v1 field set and recompute
its hash with `b"history-runtime-batch-v1\0"`; do not derive it by asking the
new freeze path to emit v1. A test helper may convert a newly frozen
undirected v2 manifest before any observation is built: remove `direction`,
set `schema_version` to `1`, recompute the v1 hash, and replace only the
test-owned `batch.json`.

Update the archived-batch assertion in `tests/history_runtime_smoke.sh` to
require schema v2 with `direction is None` for newly generated undirected
batches. It currently hard-codes schema v1.

- [ ] **Step 2: Run the batch tests and verify RED**

Run:

```bash
rtk python3 tests/history_runtime_smoke.py
```

Expected: FAIL because freeze has no direction argument and emits schema v1.

- [ ] **Step 3: Implement versioned frozen batches**

New freezes emit:

```python
manifest = {
    "schema_version": 2,
    "artifact_root": str(root),
    "generation_brief_sha256": ...,
    "direction": direction_identity_or_none,
    "ideas_tsv": ...,
    "ideas_markdown": ...,
    "candidate_count": len(rows),
    "candidates": publications,
}
manifest["batch_sha256"] = sha256(
    b"history-runtime-batch-v2\0" + canonical_bytes(manifest)
)
```

Validate the `direction_contract` argument through
`direction_contract_lib.parse_contract_bytes(canonical_bytes(value))`; import
the module with that alias to avoid shadowing the argument. For schema v2,
require the exact extra `direction` field, call
`direction_contract_lib.validate_identity` even after the batch hash verifies,
and use the v2 hash domain. For schema v1, retain the current exact field set
and v1 hash domain and interpret the direction as `None`. All downstream
callers continue using `verify_frozen_batch`.

- [ ] **Step 4: Add failing resume-identity tests**

Extend `_sealed_round` and `_compared_round` to accept an optional direction
contract. Capture the return value from `freeze_candidate_batch` and include
`"direction_identity": frozen["direction"]` in the fixture state; this is the
identity used by the assertions below. Add:

```python
def test_resume_accepts_same_canonical_direction(self):
    state, resume_path = self.directed_resume()
    identity = state["direction_identity"]
    self.assertTrue(
        history_runtime.validate_resume_state(
            resume_path, expected_direction=identity
        )
    )

def test_resume_rejects_changed_added_and_removed_direction(self):
    directed_state, directed_resume = self.directed_resume()
    undirected_state, undirected_resume = self.undirected_resume()
    cases = [
        (directed_resume, None),
        (undirected_resume, directed_state["direction_identity"]),
        (
            directed_resume,
            {
                "direction_id": "dynamic-spatial-memory-vla-v1",
                "sha256": "0" * 64,
            },
        ),
    ]
    for resume_path, expected in cases:
        with self.subTest(resume_path=resume_path, expected=expected):
            with self.assertRaises(history_runtime.RuntimeContractError):
                history_runtime.validate_resume_state(
                    resume_path, expected_direction=expected
                )
```

Also prove that omitting `expected_direction` preserves existing direct API
behavior and that the CLI rejects noncanonical or malformed identity files.
Create one complete resume state bound to a hand-built schema-v1 batch: it must
validate with `expected_direction=None` and fail with a directed identity.
Use the conversion helper immediately after freeze and before
`observe_frozen_batch`, then build selection, comparison, and resume receipts
normally. This covers the legacy resume chain, not only the batch loader.

- [ ] **Step 5: Run resume tests and verify RED**

Run:

```bash
rtk python3 tests/history_runtime_smoke.py
```

Expected: FAIL because resume validation has no expected-direction fence.

- [ ] **Step 6: Implement the resume fence and CLI**

Add a private sentinel so existing Python callers that omit
`expected_direction` do not gain a new check. When supplied:

- validate `None` or the exact two-field identity object with
  `direction_contract_lib.validate_identity`;
- load and verify the resume-bound batch;
- compare `frozen_batch_direction(batch)` with the expected identity;
- raise `RuntimeContractError("resume direction identity changed")` on any
  added, removed, or changed identity.

The `freeze-batch --direction` CLI loads canonical contract bytes and passes
the parsed object. The `validate-resume --expected-direction` CLI loads
canonical identity JSON and passes it to `validate_resume_state`.

- [ ] **Step 7: Run focused runtime verification**

Run:

```bash
rtk python3 tests/direction_contract_smoke.py
rtk python3 tests/history_runtime_smoke.py
rtk bash tests/history_runtime_smoke.sh
rtk git diff --check
```

- [ ] **Step 8: Commit Task 3**

```bash
rtk git add lib/history_runtime.py tests/history_runtime_smoke.py \
  tests/history_runtime_smoke.sh
rtk git commit -m "feat: bind direction identity to batch resume"
```

---

### Task 4: Fail-Closed Selector Gate and Short Rejection Retry

**Files:**

- Modify: `hunt.sh`
- Modify: `lib/history_archive.py`
- Modify: `roles/select.md`
- Modify: `tests/fake_agent.sh`
- Modify: `tests/history_runtime_smoke.py`
- Modify: `tests/history_runtime_smoke.sh`

**Runtime order for directed rounds:**

```text
snapshot → generate → structural validation → freeze batch
         → selector + direction.tsv gate
         → observe history → prescreen → selection → comparison
         → prior-work research → review → commit
```

- [ ] **Step 1: Add failing archive-reason tests**

In the archive contract tests, add:

```python
def test_direction_rejection_is_not_a_failure_or_decision_archive(self):
    receipt = history_archive.archive_round(
        **dict(self.values, reason="rejected:direction")
    )
    self.assertEqual(receipt["archive_class"], "rejection")
    verified = history_archive.verify_archive(
        self.destination / "round",
        run_id="run-1",
        round_number=1,
        reason="rejected:direction",
    )
    self.assertEqual(verified["created_reason"], "rejected:direction")
    with self.assertRaises(history_archive.ArchiveError):
        history_archive.verified_failure_archive_binding(
            self.destination, expected_run_id="run-1"
        )

def test_direction_rejection_receipt_cannot_change_reason(self):
    history_archive.archive_round(
        **dict(self.values, reason="rejected:direction")
    )
    receipt_path = (
        self.destination
        / "round/history/archive-receipt.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["created_reason"] = "rejected:other"
    receipt_path.write_bytes(canonical(receipt))
    with self.assertRaises(history_archive.ArchiveError):
        history_archive.verify_archive(
            self.destination / "round",
            run_id="run-1",
            round_number=1,
        )
```

- [ ] **Step 2: Run the archive test and verify RED**

Run:

```bash
rtk python3 tests/history_runtime_smoke.py
```

Expected: FAIL because `_reason_class` rejects `rejected:direction`.

- [ ] **Step 3: Add the bounded rejection class**

Extend `_reason_class` with the single accepted semantic-rejection reason:

```python
if reason == "rejected:direction":
    return "rejection"
```

Do not treat a rejection archive as a decision requiring a ledger projection,
or as a failure eligible for resume-attempt failure binding. In
`verify_archive`, always require
`_reason_class(receipt["created_reason"]) == receipt["archive_class"]`.
A requested rejection additionally requires both
`archive_class == "rejection"` and
`created_reason == "rejected:direction"`; class equality alone is
insufficient.

- [ ] **Step 4: Add failing shell end-to-end cases**

Extend `tests/history_runtime_smoke.sh` with isolated fake-backend cases:

1. Directed all-in-scope:
   - set
     `RESEARCH_DIRECTION_FILE=directions/dynamic-spatial-memory-vla-v1.json`;
   - assert generation receives the mounted canonical file;
   - assert `direction.tsv` and `history/direction-gate.json` are archived;
   - assert the round reaches prescreen and comparison.
2. Directed out-of-scope:
   - set `FAKE_AGENT_MODE=direction-out-of-scope`;
   - assert the process exits successfully at `ROUND_LIMIT=1`;
   - assert archive reason `rejected:direction` and class `rejection`;
   - assert no `history/observations`, `prescreen.md`, `priorwork.md`,
     comparison index, review plan, or ledger change exists.
3. Directed missing verdict:
   - set `FAKE_AGENT_MODE=direction-missing-verdict`;
   - assert the same whole-batch rejection and no downstream artifacts.
4. Undirected selector failure:
   - set `FAKE_AGENT_MODE=selector-failure`;
   - assert the current generation-order fallback still reaches prescreen and
     selection.
5. Resume mismatch:
   - create a directed sealed front state;
   - rerun once without the direction and once with a modified copied contract;
   - assert both log `Discarding stale or incomplete front state` and generate
     a new batch; assert no review consumes the sealed front.
6. Invalid startup contract:
   - supply a repository-relative JSON file with an unknown field, then a
     symlinked contract path;
   - assert both exit before `history_sync` or any fake-agent call and leave no
     generated batch.
7. Two semantic rejections:
   - run `ROUND_LIMIT=2`, `MAX_FAILS=1`, zero test no-hit delay, and
     `FAKE_AGENT_MODE=direction-out-of-scope`;
   - assert two distinct `rejected:direction` archives, no `Round failed`
     message, and exactly one `Retrying in 0 minutes` message between rounds;
   - this proves semantic rejection does not consume the failure budget and
     the terminal round does not sleep.
8. Rejection archive failure:
   - in an isolated copied repository, replace only
     `history_archive.py`'s `__main__` dispatch with a deterministic nonzero
     exit so module imports and startup remain valid;
   - run one directed out-of-scope round and assert `hunt.sh` exits nonzero,
     logs `Direction rejection archive failed`, starts no second round, and
     leaves `tmp/round` intact for diagnosis.

Use `ROUND_LIMIT=1` for single-round cases, `FAIL_SLEEP_MIN=0`,
`NO_HIT_SLEEP_MIN_LO=0`, `NO_HIT_SLEEP_MIN_HI=0`, and
`ALLOW_ZERO_NO_HIT_SLEEP=1`. Keep all tests on the existing loopback fake
backend; no production or external model invocation is permitted.

- [ ] **Step 5: Run the shell smoke test and verify RED**

Run:

```bash
rtk bash tests/history_runtime_smoke.sh
```

Expected: FAIL because `hunt.sh` ignores `RESEARCH_DIRECTION_FILE`, the
selector emits no direction verdict, and resume does not compare direction.

- [ ] **Step 6: Add startup snapshot and round binding**

In `hunt.sh`:

- document `RESEARCH_DIRECTION_FILE` in the control header;
- set `RESEARCH_DIRECTION_FILE=${RESEARCH_DIRECTION_FILE:-}`;
- after recreating `tmp/history-startup` and before `history_sync`, run the
  snapshot CLI;
- always create
  `tmp/history-startup/direction-identity.json`;
- create `direction-constraint.json` only for a directed run;
- set `direction_active=1` from the validated snapshot, not directly from the
  environment string;
- on each new round, copy the identity and optional canonical contract into
  `tmp/round/history/`;
- mount the contract as
  `direction_constraint.json=$RD/history/direction-constraint.json`;
- pass optional `--direction` to `freeze-batch`;
- pass `--expected-direction
  "$startup_root/direction-identity.json"` to `validate-resume`.

The startup snapshot is authoritative for the process lifetime. Do not reread
the source direction file inside later rounds.

- [ ] **Step 7: Add selector input, dual output, and fail-closed gate**

For the `select` disposable mirror:

- copy `tmp/round/history/batch/sources/ideas.md` to the mirror's
  `tmp/round/ideas.md`; the selector must classify the exact frozen Markdown,
  not a mutable convenience copy;
- copy the canonical contract to
  `tmp/round/history/direction-constraint.json` when directed;
- keep `select.tsv` optional for advisory ranking;
- require and copy `direction.tsv` only when the mirrored contract exists;
- bound both outputs to 65536 bytes with the existing regular-file and UTF-8
  checks.

Add a shell wrapper that invokes:

```bash
python3 lib/direction_contract.py validate-verdicts \
  --contract "$RD/history/direction-constraint.json" \
  --ideas "$RD/history/batch/sources/ideas.tsv" \
  --verdicts "$RD/direction.tsv" \
  --output "$RD/history/direction-gate.json"
```

In a directed round, selector exit failure, output-copy failure, validator
failure, malformed coverage, or any `out-of-scope` verdict calls
`reject_direction_round` and immediately continues the main loop. The helper:

- archives with `rejected:direction` and returns failure when archival fails;
- logs one bounded rejection message;
- leaves `fails` unchanged;
- uses `random_no_hit_sleep_min` only when another round is allowed;
- performs no sleep after reaching `ROUND_LIMIT`.

Every call site exits nonzero on `reject_direction_round` failure. Do not use
`archive_round ... || true` for semantic rejection.

Only after this gate succeeds may the directed round call
`history_observe_round`.

For undirected rounds, retain the existing selector position after observation
and its empty-`select.tsv` generation-order fallback.

In `history_seal_selection`, pass effective `theme_min_low=0` only when
`direction_active=1`; otherwise pass `THEME_MIN_LOW`.

- [ ] **Step 8: Update the selector role and deterministic fixtures**

In `roles/select.md`, add the canonical contract to optional inputs. When
present:

- change the role title and hard rules from rank-only to advisory ranking plus
  directed-scope classification; keep novelty, quality, prior-work, and
  acceptance verdicts forbidden;
- independently compare every complete candidate proposition and experiment
  against the statement, allowed axes, failures, fixed constraints, and
  exclusions;
- write `tmp/round/direction.tsv`;
- use the exact literal header
  `id<TAB>direction-fit<TAB>direction-evidence`;
- emit candidates in `ideas.md` order;
- use exactly `in-scope` or `out-of-scope`;
- provide one evidence sentence without tabs;
- still write advisory `select.tsv`.

State that missing or malformed direction output rejects the batch, while the
undirected ranking fallback remains advisory.

In `tests/fake_agent.sh`:

- in the contained fake-Codex generation branch, detect
  `input/direction_constraint.json` and emit the same valid in-direction
  proposition and three structural fields as `tests/fake_stage_agent.py`;
- when the mirrored direction file exists, emit a complete default
  all-`in-scope` `direction.tsv`;
- emit one `out-of-scope` row for
  `FAKE_AGENT_MODE=direction-out-of-scope`;
- omit the file for
  `FAKE_AGENT_MODE=direction-missing-verdict`;
- exit nonzero for `FAKE_AGENT_MODE=selector-failure`.

- [ ] **Step 9: Run focused orchestration verification**

Run:

```bash
rtk bash -n hunt.sh
rtk python3 tests/direction_contract_smoke.py
rtk python3 tests/history_runtime_smoke.py
rtk bash tests/history_runtime_smoke.sh
rtk git diff --check
```

- [ ] **Step 10: Commit Task 4**

```bash
rtk git add hunt.sh lib/history_archive.py roles/select.md \
  tests/fake_agent.sh tests/history_runtime_smoke.py \
  tests/history_runtime_smoke.sh
rtk git commit -m "feat: reject off-direction candidate batches"
```

---

### Task 5: Operator Documentation and Full Contract Verification

**Files:**

- Modify: `README.md`
- Modify: `docs/getting-started.md`
- Modify: `PROGRAM.md`
- Modify: `docs/architecture.md`
- Modify: `tests/verify_product_contract.py`

- [ ] **Step 1: Add failing product-contract assertions**

Extend the runtime surface requirements so the test demands agreement across:

- `RESEARCH_DIRECTION_FILE` in `hunt.sh`, README, and getting-started;
- `directions/dynamic-spatial-memory-vla-v1.json`;
- `direction_constraint.json` in generation and selector roles;
- `Direction Axis`, `Target Failure`, and `Direction Evidence`;
- `direction.tsv`, `in-scope`, `out-of-scope`, and fail-closed batch rejection;
- frozen batch `direction` identity and expected-direction resume validation;
- `rejected:direction` and the short no-hit retry;
- directed low-inventory quota override with all other gates preserved.

Use product-contract assertions for cross-surface agreement only. Behavioral
claims remain covered by Python and shell tests.

- [ ] **Step 2: Run the product test and verify RED**

Run:

```bash
rtk python3 tests/verify_product_contract.py runtime
```

Expected: FAIL because operator and architecture documents do not yet expose
the interface and guarantees.

- [ ] **Step 3: Update the operator and architecture documents**

Document this exact operator command:

```bash
RESEARCH_DIRECTION_FILE='directions/dynamic-spatial-memory-vla-v1.json' \
  caffeinate -is ./hunt.sh
```

State these bounded guarantees:

- the file is canonicalized before any agent invocation;
- every raw candidate must pass exact structural fields and independent
  selector classification;
- one failure rejects the whole batch before history retrieval and research;
- resume requires the same canonical direction identity;
- undirected invocation preserves broad generation;
- the semantic classifier is an independent model judgment wrapped by
  fail-closed orchestration, not a proof of natural-language meaning.

In `PROGRAM.md` and `docs/architecture.md`, record the directed stage order,
optional generation mount, selector dual output, batch-v2 identity, rejection
archive, theme-quota precedence, and compatibility behavior.

- [ ] **Step 4: Run the focused product verification**

Run:

```bash
rtk python3 tests/verify_product_contract.py runtime
rtk python3 tests/verify_product_contract.py fixtures
rtk git diff --check
```

- [ ] **Step 5: Run the full repository validation**

Run in this order:

```bash
rtk bash -n hunt.sh
rtk python3 tests/direction_contract_smoke.py
rtk python3 tests/history_store_smoke.py
rtk python3 tests/history_projection_smoke.py
rtk python3 tests/history_budget_smoke.py
rtk python3 tests/history_retrieval_smoke.py
rtk python3 tests/history_retrieval_adversarial.py
rtk python3 tests/history_stage_smoke.py
rtk python3 tests/history_runtime_smoke.py
rtk bash tests/generation_contract_smoke.sh
rtk bash tests/history_runtime_smoke.sh
rtk bash tests/runtime_abi_smoke.sh
rtk bash tests/calibration_abi_smoke.sh
rtk python3 tests/verify_product_contract.py all
rtk git diff --check
```

Expected: every command exits zero. Confirm `rtk git status --short` still
shows the pre-existing `ledger.tsv` modification and no test-generated tracked
files.

- [ ] **Step 6: Commit Task 5**

```bash
rtk git add README.md docs/getting-started.md PROGRAM.md \
  docs/architecture.md tests/verify_product_contract.py
rtk git commit -m "docs: document research direction hard constraint"
```

- [ ] **Step 7: Final commit and diff audit**

```bash
rtk git status --short
rtk git log --oneline -7
rtk git diff 1a0c9b2..HEAD --stat
rtk git diff --check 1a0c9b2..HEAD
```

Expected commits after the design baseline:

```text
docs: plan research direction hard constraint
feat: add versioned research direction contract
feat: bind research direction to generation
feat: bind direction identity to batch resume
feat: reject off-direction candidate batches
docs: document research direction hard constraint
```
