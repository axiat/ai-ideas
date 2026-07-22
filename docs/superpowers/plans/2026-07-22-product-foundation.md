# Product Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a coherent English-language `ai-ideas` product with English runtime artifact contracts, a generated hero image, a fully rewritten ledger and history, safe backend defaults, and evidence-backed independent review.

**Architecture:** Preserve the existing shell-orchestrated pipeline and file layout. Align every producer and parser through one English artifact glossary, preserve stable ledger and calibration facts with hash-based tests, and separate the concise product entry from detailed operational documentation.

**Tech Stack:** Bash 3.2-compatible shell, Python 3 standard library, Markdown, TSV, Git worktrees, GitHub Actions YAML, and a generated PNG asset.

## Global Constraints

- Work only in `/Users/qinningxu/code/ai-ideas/.worktrees/product-foundation` on `feat/product-foundation`.
- Preserve all 531 ledger data rows, including the 111 rows copied from the dirty `main` checkout.
- Preserve the ledger field-count distribution: 216 seven-field rows and 315 eight-field rows.
- Preserve dates, sources, verdicts, overlap semantics and row positions, categories, URLs, identifiers, model names, commands, counts, thresholds, and table values. Map the 29 legacy unknown-overlap labels one-to-one to `unknown`.
- Keep these machine tokens byte-stable throughout the product rollout: `strong-accept`, `accept-w-rev`, `reject`, `low`, `medium`, `high`, `unknown`, `novelty-dead`, `evidence-incomplete`, `design-fixable`, `ceiling-limited`, `hunt`, and `weekly`.
- Use English in every tracked human-readable file and in `s1_report_20260720.md`; ignored `tmp/` state is outside the product-content boundary.
- Codex is the default trusted backend. Claude is explicit opt-in only and must never appear in a shell fallback value.
- Do not invent a license, claim topic independence, implement roadmap features, push, open a pull request, or merge.
- Keep prose minimal, bounded, and dense as defined by `/Users/qinningxu/AI_SHARED_MEMORY.md`.

## Target Artifact Glossary

Use these exact labels across producers, parsers, fixtures, and tests:

| Purpose | Exact English label or token |
| --- | --- |
| Policy lens heading | `## Divergence Lenses` |
| Policy theme heading | `## Theme Vocabulary` |
| Paper count | `Papers Read:` |
| Falsification field | `Minimal Falsification Experiment:` |
| Prescreen decision | `Decision: keep` or `Decision: kill` |
| Prior-work overlap | `Overlap: low`, `Overlap: medium`, or `Overlap: high` |
| Ledger unknown overlap | `unknown` |
| Assumption-removal form | `Form: remove-load-bearing-assumption` |
| Assumption attempt marker | `Assumption-Removal Attempt:` |
| Assumption fields | `Assumption to Remove:`, `Why It Can Be Removed Now:`, `Forcing Constraint:` |
| Crack evidence | `Crack Evidence:` and `Crack Evidence Verification` |
| Crack outcomes | `supports`, `partial`, `contradicts`, `unreachable` |
| Candidate lineage | `Recheck:` and `Evolved from:` |
| AwR draft heading | `## Revised Idea` |
| Reproducible query | `- Query:` |
| Strongest negative evidence | `Strongest Counterexample:` |
| AwR decision | `Decision: SA-possible` or `Decision: not-ready` |
| AwR defect | `- Defect:` |
| Feedback heading | `## Reviewer Feedback` |
| Final status | `Status: ready` or `Status: not-ready` |
| Calibration leak marker | `suspected published counterpart` |

The English theme vocabulary, in policy order, is:

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

---

### Task 1: Add Product Content Invariant Tests

**Files:**
- Create: `tests/verify_product_contract.py`

**Interfaces:**
- Consumes: the untouched working-tree ledger, calibration fixtures, runtime sources, and Git tracked-file list.
- Produces: `runtime`, `fixtures`, `ledger`, and `all` verification scopes used by every later task.

- [ ] **Step 1: Create the verifier with frozen baseline hashes**

```python
#!/usr/bin/env python3
import csv
import hashlib
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
EXPECTED = {
    "stable_projection": "810adad8122a7761ba394e6a67cdfa12d8c4f869fc888a5c2a8e8cb61c3a29cb",
    "theme_projection": "5dd438abbc8fd9e71f42256fd453afa9a538d13201dd19ae59fdb4400cb6d435",
    "urls": "6c26006c40788e96d0d5e91662867644f7720287b787d4d43f5257fc85bea23a",
    "numbers": "1f8236a7f082296dc1e754189e7a921ff625fb51e61fe6e8dc77f53ba6741e1a",
    "row_urls": "6894b19bbc53362874f64c17dec1d593e9d54dabac4c7b0242036d7df10ba707",
    "row_numbers": "6d026efe735888bd0a83639537f7630975133b9a30599fc6d5b68c7445849e35",
    "row_code_spans": "42985fa08be8aa8ff358d322a13120470bdb579d7468d9126b2cd84852edc2d1",
    "case_ids": "aed82be120ea6d26d1735050352867fafa4551e9681b8da3abce496915fae1c4",
    "assertions": "a85dfbcece8c4c223ab0dfca3eb6a2ef17f091d71b67cebccfc7e3348aaea3f0",
}
THEMES = {
    "World Models - Architecture",
    "World Models - Training Objectives",
    "VLA - Architecture",
    "VLA - Training Paradigms",
    "Action Representation",
    "Data Engines",
    "Evaluation and Diagnostics",
    "Efficiency and Systems",
    "Safety and Robustness",
    "Cross-Domain Transfer",
    "Human-Robot Interaction and Deployment",
}
RUNTIME_FILES = [
    "hunt.sh", "awr-side.sh", "agy-worker.sh", "grok-worker.sh",
    "litwatch.sh", "litwatch_test.sh", "publish.sh", "settle.sh",
    "lib/litwatch.py", "lib/md_ids.sh", "lib/mirror_pre.sh",
    "lib/resolve_cmd.sh", "PROGRAM.md", "hunt.md", "trigger.md",
    "research_context.md", "brainstorming_policy.md", "rubric.md",
    "calib/run_panel.sh", "calib/run_all.sh", "calib/run_e2e.sh",
    ".githooks/pre-push", ".github/workflows/auto-merge-routine.yml",
]
BACKEND_DEFAULTS = {
    "hunt.sh": (
        "AGENT_CMD",
        "codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write",
    ),
    "awr-side.sh": (
        "SIDE_CMD",
        "codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral",
    ),
    "litwatch.sh": (
        "LITWATCH_CMD",
        "codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral",
    ),
    "calib/run_panel.sh": (
        "PANEL_CMD",
        "codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral",
    ),
    "calib/run_e2e.sh": (
        "E2E_CMD",
        "codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral",
    ),
}
SHELL_ASSIGNMENT = re.compile(
    r"^\s*(?:(?:export|readonly|local)\s+)*([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)
FALLBACK_EXPANSION = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?[-=])([^}\n]*)\}"
)
VARIABLE_REFERENCE = re.compile(r"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)")

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def read_text(path):
    try:
        return path.read_text()
    except UnicodeDecodeError:
        return None

def assert_english(paths):
    failures = []
    for path in paths:
        text = read_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if HAN.search(line):
                failures.append(f"{path.relative_to(ROOT)}:{number}")
    if failures:
        raise AssertionError("Han characters remain in " + ", ".join(failures[:40]))

def runtime_paths():
    paths = [ROOT / name for name in RUNTIME_FILES]
    paths.extend(sorted((ROOT / "roles").glob("*.md")))
    paths.extend(sorted((ROOT / "calib/cases").glob("**/*")))
    return [path for path in paths if path.is_file()]

def tracked_shell_paths():
    raw = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "*.sh", ".githooks/*"],
        cwd=ROOT,
    )
    return [ROOT / item.decode() for item in raw.split(b"\0") if item]

def assert_backend_defaults():
    for name, (variable, command) in BACKEND_DEFAULTS.items():
        expected = f"{variable}=${{{variable}:-{command}}}"
        assignments = [
            line
            for line in (ROOT / name).read_text().splitlines()
            if re.match(rf"^\s*{re.escape(variable)}=", line)
        ]
        if assignments != [expected]:
            raise AssertionError(
                f"default backend mismatch in {name}: expected {expected!r}, found {assignments!r}"
            )

def shell_code_lines(text):
    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        code = line.split("#", 1)[0]
        if code.strip():
            lines.append((number, code))
    return lines

def claude_tainted_variables(lines):
    values = {}
    for _, code in lines:
        match = SHELL_ASSIGNMENT.match(code)
        if match:
            values.setdefault(match.group(1), []).append(match.group(2))
    tainted = set()
    while True:
        expanded = {
            variable
            for variable, assignments in values.items()
            if any(
                "claude" in value.lower()
                or any(reference in tainted for reference in VARIABLE_REFERENCE.findall(value))
                for value in assignments
            )
        }
        if expanded == tainted:
            return tainted
        tainted = expanded

def claude_fallback_lines(text):
    lines = shell_code_lines(text)
    tainted = claude_tainted_variables(lines)
    failures = []
    for number, code in lines:
        for match in FALLBACK_EXPANSION.finditer(code):
            fallback = match.group(1)
            references = VARIABLE_REFERENCE.findall(fallback)
            if "claude" in fallback.lower() or any(item in tainted for item in references):
                failures.append(number)
                break
    return failures

def assert_no_claude_fallbacks():
    failures = []
    for path in tracked_shell_paths():
        text = read_text(path)
        if text is None:
            continue
        failures.extend(
            f"{path.relative_to(ROOT)}:{number}"
            for number in claude_fallback_lines(text)
        )
    if failures:
        raise AssertionError("automatic Claude fallback remains in " + ", ".join(failures))

def verify_runtime():
    assert_backend_defaults()
    assert_no_claude_fallbacks()
    assert_english(runtime_paths())
    required = {
        "brainstorming_policy.md": ["## Divergence Lenses", "## Theme Vocabulary"],
        "hunt.sh": ["Papers Read", "Minimal Falsification Experiment", "Overlap"],
        "awr-side.sh": ["Revised Idea", "Strongest Counterexample", "Reviewer Feedback"],
        "calib/run_panel.sh": ["suspected published counterpart"],
    }
    for name, needles in required.items():
        text = (ROOT / name).read_text()
        for needle in needles:
            if needle not in text:
                raise AssertionError(f"missing {needle!r} in {name}")

def ledger_rows():
    with (ROOT / "ledger.tsv").open(newline="") as handle:
        return list(csv.reader(handle, delimiter="\t"))

def row_token_projection(data, pattern, group=0, normalize=None):
    normalize = normalize or (lambda token: token)
    lines = []
    for i, row in enumerate(data, 1):
        tokens = sorted(
            normalize(match.group(group))
            for field in (row[3], row[5])
            for match in pattern.finditer(field)
        )
        lines.append(f"{i}:{'|'.join(tokens)}")
    return "\n".join(lines)

def verify_ledger():
    rows = ledger_rows()
    data = rows[1:]
    if len(data) != 531:
        raise AssertionError(f"ledger row count changed: {len(data)}")
    nf7 = sum(len(row) == 7 for row in data)
    nf8 = sum(len(row) == 8 for row in data)
    if (nf7, nf8) != (216, 315):
        raise AssertionError(f"ledger shape changed: nf7={nf7}, nf8={nf8}")
    overlap_values = sorted({row[6] for row in data})
    if overlap_values != ["high", "low", "medium", "unknown"]:
        raise AssertionError(f"unmigrated or unknown overlap values: {overlap_values}")
    unknown_overlap = sum(row[6] == "unknown" for row in data)
    if unknown_overlap != 29:
        raise AssertionError(f"legacy unknown-overlap count changed: {unknown_overlap}")
    projection = "\n".join("\t".join([row[0], row[1], row[4], row[6], row[7] if len(row) == 8 else ""]) for row in data)
    if digest(projection) != EXPECTED["stable_projection"]:
        raise AssertionError("stable ledger columns or overlap row association changed")
    unknown = sorted({row[2] for row in data} - THEMES)
    if unknown:
        raise AssertionError(f"unmigrated or unknown themes: {unknown}")
    url_re = re.compile(r"https?://[^\s\t)>\]]+")
    urls = sorted(token.rstrip(".,;:") for row in data for field in (row[3], row[5]) for token in url_re.findall(field))
    if digest("\n".join(urls)) != EXPECTED["urls"]:
        raise AssertionError("ledger URL set changed")
    number_re = re.compile(r"(?<![A-Za-z])(?:\d+(?:\.\d+)?%?|\d+[x×]\d+)(?![A-Za-z])")
    numbers = sorted(token for row in data for field in (row[3], row[5]) for token in number_re.findall(field))
    if digest("\n".join(numbers)) != EXPECTED["numbers"]:
        raise AssertionError("ledger numeric token set changed")
    row_urls = row_token_projection(data, url_re, normalize=lambda token: token.rstrip(".,;:"))
    if digest(row_urls) != EXPECTED["row_urls"]:
        raise AssertionError("ledger row-bound URL tokens changed")
    row_numbers = row_token_projection(data, number_re)
    if digest(row_numbers) != EXPECTED["row_numbers"]:
        raise AssertionError("ledger row-bound numeric tokens changed")
    code_span_re = re.compile(r"`([^`\n]+)`")
    row_code_spans = row_token_projection(data, code_span_re, group=1)
    if digest(row_code_spans) != EXPECTED["row_code_spans"]:
        raise AssertionError("ledger row-bound code spans changed")
    theme_projection = "\n".join(row[2] for row in data)
    if digest(theme_projection) != EXPECTED["theme_projection"]:
        raise AssertionError("ledger theme sequence changed")
    assert_english([ROOT / "ledger.tsv"])

def verify_fixtures():
    case_ids = []
    assertions = []
    for case in sorted((ROOT / "calib/cases").iterdir()):
        if not case.is_dir():
            continue
        ids = re.findall(r"^## (I\d+)\b", (case / "ideas.md").read_text(), re.M)
        case_ids.append(f"{case.name}:{','.join(ids)}")
        for name in ("expect", "e2e.expect"):
            path = case / name
            if not path.exists():
                continue
            values = []
            for line in path.read_text().splitlines():
                value = line.split("#", 1)[0].strip()
                if value:
                    values.append(value)
            assertions.append(f"{case.name}/{name}:{'|'.join(values)}")
    if digest("\n".join(case_ids)) != EXPECTED["case_ids"]:
        raise AssertionError("calibration idea IDs changed")
    if digest("\n".join(assertions)) != EXPECTED["assertions"]:
        raise AssertionError("calibration assertions changed")

def tracked_text_paths():
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    paths = [ROOT / item.decode() for item in raw.split(b"\0") if item]
    report = ROOT / "s1_report_20260720.md"
    if report.exists() and report not in paths:
        paths.append(report)
    return paths

def verify_all():
    verify_runtime()
    assert_english(tracked_text_paths())
    verify_fixtures()
    verify_ledger()

SCOPES = {
    "runtime": verify_runtime,
    "fixtures": verify_fixtures,
    "ledger": verify_ledger,
    "all": verify_all,
}

if __name__ == "__main__":
    scope = sys.argv[1] if len(sys.argv) == 2 else "all"
    if scope not in SCOPES:
        raise SystemExit(f"usage: {sys.argv[0]} [{'|'.join(SCOPES)}]")
    SCOPES[scope]()
    print(f"ok: {scope}")
```

- [ ] **Step 2: Run the verifier and confirm the expected red state**

Run: `python3 tests/verify_product_contract.py all`

Expected: non-zero exit reporting remaining Han characters or unmigrated themes. A passing result before implementation means the test is incomplete.

- [ ] **Step 3: Commit the failing contract test**

```bash
git add tests/verify_product_contract.py
git commit -m "test: lock product content invariants"
```

### Task 2: Align the Core Hunt Runtime and Prompts

**Files:**
- Modify: `hunt.sh`
- Modify: `PROGRAM.md`
- Modify: `hunt.md`
- Modify: `trigger.md`
- Modify: `brainstorming_policy.md`
- Modify: `research_context.md`
- Modify: `roles/generate.md`
- Modify: `roles/select.md`
- Modify: `roles/prescreen.md`
- Modify: `roles/research.md`
- Modify: `roles/review.md`
- Modify: `roles/report.md`
- Modify: `roles/meta.md`
- Create: `tests/fake_agent.sh`
- Create: `tests/runtime_abi_smoke.sh`

**Interfaces:**
- Consumes: the Target Artifact Glossary and the existing stage/output paths.
- Produces: an English generation-to-report ABI and a Codex default backend.

- [ ] **Step 1: Add a failing fake-agent smoke test**

`tests/fake_agent.sh` must inspect the final prompt argument and emit valid English artifacts for meta, generation, selection, prescreen, prior-work, review, and report stages. `tests/runtime_abi_smoke.sh` must copy the repository into a temporary Git worktree, replace only publication with a local no-op, run one complete Strong Accept round with `AGENT_CMD=tests/fake_agent.sh`, and assert:

```text
tmp/round/ideas.tsv exists
tmp/round/priorwork.md contains "Papers Read: 5"
tmp/round/rev/1/verdict.tsv contains "I1<TAB>strong-accept"
ledger.tsv gains exactly one eight-field row
the new row uses "World Models - Architecture"
the generated report exists and contains no Han characters
```

Run: `bash tests/runtime_abi_smoke.sh`

Expected: FAIL because the current parsers still require legacy labels.

- [ ] **Step 2: Apply the product content contract to core policy and prompts**

Preserve every gate, default value, stage boundary, file path, and output token. Replace producer templates and parser literals with the exact Target Artifact Glossary in the same commit. Replace explicit requirements for non-English output with English output requirements.

- [ ] **Step 3: Change the trusted default backend to Codex**

Use this exact `AGENT_CMD` fallback in `hunt.sh`:

```bash
AGENT_CMD=${AGENT_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write}
```

Keep Claude examples only as explicitly assigned environment variables. Do not execute them.

- [ ] **Step 4: Run focused tests**

```bash
bash -n hunt.sh tests/fake_agent.sh tests/runtime_abi_smoke.sh
bash tests/runtime_abi_smoke.sh
! rg -n --pcre2 '\p{Script=Han}' hunt.sh PROGRAM.md hunt.md trigger.md brainstorming_policy.md research_context.md roles/generate.md roles/select.md roles/prescreen.md roles/research.md roles/review.md roles/report.md roles/meta.md
```

Expected: all commands succeed.

- [ ] **Step 5: Commit the core runtime contract**

```bash
git add hunt.sh PROGRAM.md hunt.md trigger.md brainstorming_policy.md research_context.md roles tests/fake_agent.sh tests/runtime_abi_smoke.sh
git commit -m "feat: align hunt runtime contracts"
```

### Task 3: Align Supporting Workflows and Automation

**Files:**
- Modify: `awr-side.sh`
- Modify: `agy-worker.sh`
- Modify: `grok-worker.sh`
- Modify: `litwatch.sh`
- Modify: `litwatch_test.sh`
- Modify: `lib/litwatch.py`
- Modify: `lib/md_ids.sh`
- Modify: `lib/mirror_pre.sh`
- Modify: `lib/resolve_cmd.sh`
- Modify: `roles/awr.md`
- Modify: `roles/awr-priorwork.md`
- Modify: `roles/awr-judge.md`
- Modify: `roles/litwatch.md`
- Modify: `publish.sh`
- Modify: `settle.sh`
- Modify: `.githooks/pre-push`
- Rename: `.github/workflows/auto-merge-claude.yml` to `.github/workflows/auto-merge-routine.yml`
- Modify: `tests/fake_agent.sh`
- Modify: `tests/runtime_abi_smoke.sh`
- Modify: `tests/verify_product_contract.py`

**Interfaces:**
- Consumes: English ledger content and the Target Artifact Glossary.
- Produces: consistent AwR, litwatch, wrapper, publishing, and automation behavior.

- [ ] **Step 1: Extend the fake-agent smoke test to AwR**

Add two AwR cases that copy one `accept-w-rev` row into an isolated temporary ledger. Both cases must set every backend command to the fake agent and disable all polling, launch-gap, and cooldown delays:

```bash
SIDE_CMD=tests/fake_agent.sh
SIDE_RESEARCH_CMD=tests/fake_agent.sh
SIDE_PRIORWORK_CMD=tests/fake_agent.sh
SIDE_JUDGE_CMD=tests/fake_agent.sh
SIDE_POLL_SEC=0
SIDE_MAX_ROUNDS=1
SIDE_MAX_BAD=1
SIDE_GAP_SEC=0
SIDE_GAP_MIN_SEC=0
SIDE_GAP_MAX_SEC=0
SIDE_COOLDOWN_SEC=0
```

Clear `tmp/ledger.good` and the AwR scratch directory before each case. The ready case asserts a final artifact with:

```text
## Revised Idea
Strongest Counterexample:
Decision: SA-possible
Status: ready
AGY-DONE
```

The not-ready case must exercise `- Defect:`, `## Reviewer Feedback`, `Round: 1`, `Decision: not-ready`, and `Status: not-ready`. In both cases, `AGY-DONE` remains the last nonempty line.

Expected before implementation: FAIL on legacy parser labels.

- [ ] **Step 2: Apply the supporting workflow contract**

Use the exact AwR labels from the glossary. Preserve path restrictions, mirror boundaries, retry counts, bad-artifact handling, launch throttling, polling, and stable `AGY-DONE` sentinel behavior. Use these exact trusted defaults:

```bash
SIDE_CMD=${SIDE_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral}
LITWATCH_CMD=${LITWATCH_CMD:-codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral}
```

An explicit `SIDE_CMD=agy` must continue to select the existing mirror-local agy adapter. `litwatch.sh` must keep `LITWATCH_AGY_CMD` as a compatibility override when `LITWATCH_CMD` is not explicitly set. Provider-specific `agy-worker.sh` and `grok-worker.sh` remain explicit adapters and never become fallbacks.

- [ ] **Step 3: Run syntax and deterministic tests**

```bash
bash -n awr-side.sh agy-worker.sh grok-worker.sh litwatch.sh litwatch_test.sh publish.sh settle.sh lib/md_ids.sh lib/mirror_pre.sh lib/resolve_cmd.sh .githooks/pre-push
python3 -c 'import ast, pathlib; ast.parse(pathlib.Path("lib/litwatch.py").read_text())'
./litwatch_test.sh
bash tests/runtime_abi_smoke.sh
```

Expected: shell and Python checks pass; litwatch reports 14 passed, 0 failed, with the live network smoke allowed to skip; the hunt smoke and both AwR fake-agent cases pass.

- [ ] **Step 4: Commit the supporting workflow contract**

```bash
git add awr-side.sh agy-worker.sh grok-worker.sh litwatch.sh litwatch_test.sh lib roles/awr.md roles/awr-priorwork.md roles/awr-judge.md roles/litwatch.md publish.sh settle.sh .githooks/pre-push .github/workflows/auto-merge-routine.yml tests/fake_agent.sh tests/runtime_abi_smoke.sh tests/verify_product_contract.py
git commit -m "feat: align supporting workflow contracts"
```

### Task 4: Establish the Calibration Product Suite

**Files:**
- Modify: `calib/README.md`
- Modify: `calib/run_panel.sh`
- Modify: `calib/run_all.sh`
- Modify: `calib/run_e2e.sh`
- Modify: `calib/results-2026-07-05.md`
- Modify: `calib/results-2026-07-06.md`
- Modify: `calib/results-2026-07-12.md`
- Modify: `calib/results-2026-07-19.md`
- Modify: every `ideas.md`, `priorwork.md`, `expect`, and `e2e.expect` under `calib/cases/`
- Create: `tests/calibration_abi_smoke.sh`
- Modify: `tests/verify_product_contract.py`

**Interfaces:**
- Consumes: English review and prior-work contracts.
- Produces: product-ready gold cases with unchanged IDs and assertion DSL.

- [ ] **Step 1: Add the offline calibration contract tests**

Extend the fixture verifier to preserve all case IDs, non-comment assertions, URLs, paper identifiers, numeric tokens, and other stable evidence tokens. Add `tests/calibration_abi_smoke.sh` with an isolated fake backend that covers:

- a valid four-column verdict and min-vote aggregation;
- rejection of malformed verdict rows;
- `run_all.sh` assertion grading and recorded `PANEL_CMD`;
- exact `Overlap:` enum parsing;
- rejection of commentary-only overlap words;
- one-time aggregation of the `suspected published counterpart:` marker.

Expected before implementation: the exact `Overlap:` case and trusted-default assertions fail on the current calibration sources.

- [ ] **Step 2: Align calibration scripts and fixtures atomically**

Use `suspected published counterpart:` for the leak marker and the Target Artifact Glossary for fixture content. `run_e2e.sh` must accept only `high`, `medium`, or `low` immediately after the anchored `Overlap:` label; an enum mentioned later in commentary is not a verdict. Preserve every `I<n>` heading, URL, paper identifier, score, vote, numeric token, and non-comment assertion line.

Keep the frozen panel ABI exact:

```text
verdict.tsv: id<TAB>strong-accept|accept-w-rev|reject<TAB>MAJOR-count<TAB>reason
aggregate.tsv: id<TAB>comma-separated-votes<TAB>min-vote
```

Every verdict row must have exactly four columns, a numeric MAJOR count, and a nonempty reason. An `accept-w-rev` or `strong-accept` vote must have the corresponding `## I<n>` review block. Reject-only output may omit `review.md`.

- [ ] **Step 3: Set trusted backend defaults**

Use this frozen-panel fallback:

```bash
PANEL_CMD=${PANEL_CMD:-codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral}
```

Use this retrieval fallback:

```bash
E2E_CMD=${E2E_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral}
```

`run_all.sh` must pass through and record the same `PANEL_CMD`; it may not carry a separate provider fallback. Update the `calib/README.md` example that points to the nonexistent `pos-meanflow` case.

- [ ] **Step 4: Run fixture and runtime verification**

```bash
bash -n calib/run_panel.sh calib/run_all.sh calib/run_e2e.sh
python3 tests/verify_product_contract.py fixtures
python3 tests/verify_product_contract.py runtime
bash tests/calibration_abi_smoke.sh
! rg -n --pcre2 '\p{Script=Han}' calib
```

Expected: both Python scopes print `ok`; shell syntax, offline calibration smoke, and the product-content gate pass. Do not run a live model panel or live retrieval during this task.

- [ ] **Step 5: Commit the calibration suite**

```bash
git add calib tests/calibration_abi_smoke.sh tests/verify_product_contract.py
git commit -m "feat: establish calibration suite"
```

### Task 5: Curate Historical Documents and Reports

**Files:**
- Modify: `AWR-REBUILD-DRAFT.md`
- Modify: `LITWATCH-DRAFT.md`
- Modify: `DEVELOPMENT.md`
- Modify: `CHANGELOG.md`
- Modify: `rubric.md`
- Modify: `ideas/2026-07-12_weekly_ideas.md`
- Modify: `ideas/2026-07-17_hunt.md`
- Modify: `ideas/2026-07-19_weekly_ideas.md`
- Modify: `s1_report_20260720.md`

**Interfaces:**
- Consumes: the existing facts, caveats, tables, links, and frozen-versus-corrected distinctions.
- Produces: fluent standalone historical records.

- [ ] **Step 1: Curate the design, development, and changelog records**

Preserve chronology, exact commands, commit references, identifiers, measured values, known limitations, and completed-versus-open status. Remove no historical claim and add no new roadmap item.

- [ ] **Step 2: Curate idea reports and the S1 report**

Preserve report IDs, verdicts, vote counts, paper links, correction notes, experimental tables, preregistered outcomes, and the separation between supported, falsified, and boundary claims.

- [ ] **Step 3: Run the document content gate**

```bash
! rg -n --pcre2 '\p{Script=Han}' AWR-REBUILD-DRAFT.md LITWATCH-DRAFT.md DEVELOPMENT.md CHANGELOG.md rubric.md ideas s1_report_20260720.md
git diff --check
```

Expected: no matches and no whitespace errors.

- [ ] **Step 4: Commit historical records**

```bash
git add AWR-REBUILD-DRAFT.md LITWATCH-DRAFT.md DEVELOPMENT.md CHANGELOG.md rubric.md ideas s1_report_20260720.md
git commit -m "docs: curate project history"
```

### Task 6: Build the Product Documentation Surface

**Files:**
- Modify: `README.md`
- Create: `docs/getting-started.md`
- Create: `docs/architecture.md`
- Create: `docs/backends.md`
- Create: `docs/trust-boundaries.md`
- Create: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: current commands, paths, backend defaults, runtime boundaries, and the design spec.
- Produces: a concise product entry and focused operator documentation.

- [ ] **Step 1: Build README as the product entry**

Use the information order in the design spec. Include the exact hero reference `![ai-ideas pipeline](assets/ai-ideas-hero.png)`, a minimal Codex quick start, one compact artifact example, backend opt-in examples, current limitations, and links to the focused documents.

- [ ] **Step 2: Create focused operator documents**

`getting-started.md` owns prerequisites, first run, result locations, and recovery. `architecture.md` owns stage boundaries and artifact flow. `backends.md` owns command examples and defaults. `trust-boundaries.md` owns filesystem, network, process, publishing, and CI guarantees. `CONTRIBUTING.md` owns local validation and branch/commit expectations.

- [ ] **Step 3: Verify references against live files**

```bash
python3 - <<'PY'
import pathlib, re
root = pathlib.Path('.')
for page in [root/'README.md', *sorted((root/'docs').glob('*.md'))]:
    text = page.read_text()
    for target in re.findall(r'\[[^]]+\]\(([^)#]+)', text):
        if target == 'assets/ai-ideas-hero.png':
            continue
        if '://' not in target and not (page.parent/target).resolve().exists():
            raise SystemExit(f'{page}: missing {target}')
print('ok: markdown links')
PY
! rg -n --pcre2 '\p{Script=Han}' README.md docs CONTRIBUTING.md
```

Expected: `ok: markdown links` and no Han matches. The hero link may be checked after Task 10 if the asset is not yet present.

- [ ] **Step 4: Commit the product documentation**

```bash
git add README.md docs/getting-started.md docs/architecture.md docs/backends.md docs/trust-boundaries.md CONTRIBUTING.md
git commit -m "docs: build product documentation"
```

### Task 7: Curate Ledger Archive Rows 2-178

**Files:**
- Modify: `ledger.tsv:2-178`

**Interfaces:**
- Consumes: the first 177 historical data rows.
- Produces: fluent theme, idea, and reason fields with unchanged row order, plus the target `unknown` overlap token in legacy unknown rows.

- [ ] **Step 1: Curate columns 3, 4, and 6 for rows 2-178**

Use the exact theme vocabulary. Preserve tabs, URLs, identifiers, digits, percentages, comparison symbols, and epistemic strength. In column 7 only, replace the 29 legacy unknown-overlap labels in this tranche with `unknown`; preserve every other machine field.

- [ ] **Step 2: Verify the chunk and global shape**

```bash
! sed -n '2,178p' ledger.tsv | rg --pcre2 '\p{Script=Han}'
python3 - <<'PY'
import csv
rows=list(csv.reader(open('ledger.tsv'), delimiter='\t'))[1:]
assert len(rows)==531
assert sum(len(r)==7 for r in rows)==216
assert sum(len(r)==8 for r in rows)==315
print('ok: ledger shape')
PY
```

Expected: no Han matches in the chunk and `ok: ledger shape`.

- [ ] **Step 3: Commit the first ledger tranche**

```bash
git add ledger.tsv
git commit -m "data: curate ledger archive part one"
```

### Task 8: Curate Ledger Archive Rows 179-355

**Files:**
- Modify: `ledger.tsv:179-355`

**Interfaces:**
- Consumes: the middle 177 historical data rows and the committed first tranche.
- Produces: curated product ledger rows with the same schema and facts.

- [ ] **Step 1: Curate columns 3, 4, and 6 for rows 179-355**

Apply the same preservation rules as Task 7.

- [ ] **Step 2: Verify the second chunk and global shape**

```bash
! sed -n '179,355p' ledger.tsv | rg --pcre2 '\p{Script=Han}'
python3 tests/verify_product_contract.py fixtures
git diff --check
```

Expected: no Han matches, fixture verification passes, and no whitespace errors.

- [ ] **Step 3: Commit the second ledger tranche**

```bash
git add ledger.tsv
git commit -m "data: curate ledger archive part two"
```

### Task 9: Curate Ledger Archive Rows 356-532 and Prove Integrity

**Files:**
- Modify: `ledger.tsv:356-532`

**Interfaces:**
- Consumes: the final 177 rows, including the 111 copied working-tree rows.
- Produces: a complete product ledger with preserved stable projections and target overlap tokens.

- [ ] **Step 1: Curate columns 3, 4, and 6 for rows 356-532**

Apply the same preservation rules as Tasks 7 and 8. Preserve the deliberate frozen-report and later-correction distinctions.

- [ ] **Step 2: Run the complete ledger verifier**

Run: `python3 tests/verify_product_contract.py ledger`

Expected: `ok: ledger`.

- [ ] **Step 3: Commit the final ledger tranche**

```bash
git add ledger.tsv
git commit -m "data: complete ledger archive curation"
```

### Task 10: Generate and Integrate the Hero Image

**Files:**
- Create: `assets/ai-ideas-hero.png`
- Modify: `README.md` only if the final asset path or alt text differs from Task 6.

**Interfaces:**
- Consumes: the product positioning and pipeline described in the design spec.
- Produces: a text-free wide README hero image.

- [ ] **Step 1: Generate one project-bound hero with the image generation skill**

Use this prompt:

```text
Use case: stylized-concept
Asset type: wide open-source project README hero
Primary request: visualize research ideas flowing through three clearly separated lanes for generation, prior-work search, and independent review, converging at a strict evidence gate, then emerging as an auditable report and ledger
Scene/backdrop: abstract technical workspace with a restrained embodied-robotics motif
Style/medium: precise modern editorial illustration, clean geometric systems art, high production polish
Composition/framing: wide landscape composition, strong left-to-right flow, readable at repository README width, balanced negative space
Lighting/mood: focused, rigorous, calm
Color palette: deep navy, graphite, cool cyan, restrained amber accents
Constraints: no text, no letters, no logos, no badges, no watermark, no decorative UI chrome, no humanoid mascot
```

- [ ] **Step 2: Save and inspect the project asset**

Save the selected output at `assets/ai-ideas-hero.png`. Inspect it at full resolution and as a README-width preview. Reject any result with text-like glyphs, a misleading single-agent funnel, poor lane separation, or weak legibility.

- [ ] **Step 3: Verify README integration and commit**

```bash
test -s assets/ai-ideas-hero.png
rg -nF '![ai-ideas pipeline](assets/ai-ideas-hero.png)' README.md
git add assets/ai-ideas-hero.png README.md
git commit -m "docs: add ai-ideas hero artwork"
```

### Task 11: Complete Verification and Independent Review

**Files:**
- Modify only files required to fix verified findings.

**Interfaces:**
- Consumes: the complete branch, design spec, implementation plan, and shared writing rules.
- Produces: final evidence that every objective is satisfied.

- [ ] **Step 1: Run the full local gate**

```bash
python3 tests/verify_product_contract.py all
git diff --check
bash -n hunt.sh awr-side.sh agy-worker.sh grok-worker.sh litwatch.sh litwatch_test.sh publish.sh settle.sh calib/run_panel.sh calib/run_all.sh calib/run_e2e.sh lib/md_ids.sh lib/mirror_pre.sh lib/resolve_cmd.sh .githooks/pre-push tests/fake_agent.sh tests/runtime_abi_smoke.sh
python3 -c 'import ast, pathlib; ast.parse(pathlib.Path("lib/litwatch.py").read_text())'
./litwatch_test.sh
bash tests/runtime_abi_smoke.sh
git status --short --branch
```

Expected: all English, invariant, syntax, deterministic, and fake-agent tests pass; only intentional branch changes remain.

- [ ] **Step 2: Dispatch three independent read-only Codex reviews**

Reviewer A checks product usability, README hierarchy, onboarding, navigation, and whether the repository looks complete without claiming nonexistent features.

Reviewer B checks every changed prose artifact for fluency, consistency, dense engineer-facing style, duplication, invented content, addressee language, meta framing, and compliance with `/Users/qinningxu/AI_SHARED_MEMORY.md`.

Reviewer C checks runtime producer/parser alignment, backend defaults, ledger and fixture invariants, tests, and whether any field-label alignment changes behavior.

Each reviewer returns findings ordered by severity with exact paths and lines. None may edit files or invoke Claude.

- [ ] **Step 3: Fix all severe findings and rerun affected gates**

Apply each substantiated finding in its owning file. Rerun the narrow affected test first, then repeat the full command block from Step 1.

- [ ] **Step 4: Run final completion audit and commit fixes**

Confirm every design-spec verification bullet with current output, inspect the hero again, and confirm the original `main` checkout still contains its original dirty ledger and S1 report hashes.

```bash
git add -A
if ! git diff --cached --quiet; then git commit -m "fix: close product review findings"; fi
git status --short --branch
git log --oneline --decorate -12
```

Expected: clean feature worktree, intact original checkout, no push or merge, and review evidence with no unresolved severe finding.
