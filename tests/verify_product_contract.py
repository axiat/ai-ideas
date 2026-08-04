#!/usr/bin/env python3
import csv
import hashlib
import json
import pathlib
import re
import shlex
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
EXPECTED = {
    "stable_projection": "e83e63d21c98793a9237b71c41307079e3a14074dac207c105b4e9b1ce635e7f",
    "theme_projection": "0fe4dcc0fd467ab4f6e18a42e49c3ad15890d545ccad66a4829b20dad731532a",
    "row_urls": "ab4dc50b551a4bb9fa28e2cbdc025a9653401db7eb4377e132c5e941d7b95f4d",
    "row_technical_tokens": "348129d3629c4974f03231b2c20ee221582419c58281457b777739cadffec141",
    "row_count_units": "29c6bfa20d487989590bdfb20b684f9079fd697eba3daf8e75afb22fc8ef479c",
    "row_labeled_quantities": "c6d836cbddb0a5633899ef182869fb10a0e287f8970fb42503303ae5651c477a",
    "row_numeric_operators": "76beb5849c86bb78478bf613a75846bf862c0e4b49bba920da1398ec5bcd2fc2",
    "row_code_spans": "7be72d7825ae0d5e8234219066ea638478cf084bd5fe3397e67505eb6c64e8c1",
    "row_symbols": "fe650735fb3b6cc9ee45de391c5f7804bbb3e381ab87c0a9872cbc1496edf92d",
    "case_ids": "f60b9cad357cf1bbf3a8e591e17251ef388f0ed6fbac01fa3fda9477419a14b6",
    "assertions": "5f12400d936aa208097077d680eefa74babb0ef6f0090984cc264a42031c7da0",
    "calibration_evidence": "ed86ecc2dcd80b2d248a931e87d47357c15586d4250b240b494cf2ccc3a4495e",
    "awr_state_aliases": "0eae2f95882e5ce730933cfaa206a048a97bf5347a0d5e7330885febc138690b",
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
    "lib/resolve_cmd.sh", "lib/history_archive.py", "lib/history_budget.py",
    "lib/history_cli.py", "lib/history_eval.py", "lib/history_projection.py",
    "lib/history_retrieval.py", "lib/history_runtime.py",
    "lib/provider_adapters.py", "lib/portable_agent.py",
    "lib/history_audit_plan.py",
    "lib/history_stage.py", "lib/history_store.py", "lib/history_witness.py",
    "README.md", "PROGRAM.md", "hunt.md", "trigger.md",
    "docs/getting-started.md", "docs/architecture.md",
    "research_context.md", "brainstorming_policy.md", "rubric.md",
    "history/retrieval-policy-v1.json",
    "history/provider-adapters-v1.json",
    "history/capacity-profiles-v1.json",
    "history/l2-budget-v1.json",
    "history/settlement-policy-v1.json",
    "history/production-evidence-roots-v1.json",
    "history/review-contract-v1.md",
    "ledger.instance-id",
    "roles/generate.md", "roles/meta.md", "roles/review.md",
    "roles/history-compare.md", "roles/research.md",
    "calib/run_panel.sh", "calib/run_all.sh", "calib/run_e2e.sh",
    ".githooks/pre-push", ".github/workflows/auto-merge-routine.yml",
    "awr-state-aliases.tsv",
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
    "calib/run_all.sh": (
        "PANEL_CMD",
        "codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral",
    ),
    "calib/run_e2e.sh": (
        "E2E_CMD",
        "codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral",
    ),
}
AGY_MODEL_DEFAULT = "gemini-3.6-flash-high"
SHELL_ASSIGNMENT = re.compile(
    r"^\s*(?:(?:export|readonly|local)\s+)*([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)
FALLBACK_EXPANSION = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?[-=])([^}\n]*)\}"
)
VARIABLE_REFERENCE = re.compile(r"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)")
SHELL_COMMAND_PREFIX = r"(?:(?:if|then|elif|while|until|do|else|time|sudo|command|exec|nohup|!)\s+)*"
DIRECT_CLAUDE_COMMAND = re.compile(
    r"(?:^|[;&|()]\s*)"
    + r"(?:[A-Za-z_][A-Za-z0-9_]*=[^;&|()\s]+\s+)*"
    + SHELL_COMMAND_PREFIX
    + r"(?:env\s+(?:[A-Za-z_][A-Za-z0-9_]*=[^;&|()\s]+\s+)*)?"
    + r"(?:[^;&|()\s]*/)?claude(?:\s|$)",
    re.I,
)
VARIABLE_COMMAND = re.compile(
    r"(?:^|[;&|()]\s*)"
    + SHELL_COMMAND_PREFIX
    + r"[\"']?\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)"
)
EVAL_COMMAND = re.compile(r"(?:^|[;&|()]\s*)" + SHELL_COMMAND_PREFIX + r"eval(?:\s|$)")
SHELL_C_COMMAND = re.compile(r"(?:^|[;&|()]\s*)" + SHELL_COMMAND_PREFIX + r"(?:ba|z)?sh\s+-c(?:\s|$)")
LEDGER_HEADER = ["date", "source", "theme", "idea", "verdict", "reason", "overlap", "category"]
LEDGER_URL = re.compile(r"https?://[^\s\t()<>\[\]`,;，。；（）]+")
LEDGER_TECH_DIGITS = r"0-9⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉"
LEDGER_TECH_ALPHA = r"A-Za-zΑ-Ωα-ω"
LEDGER_TECH_CHARS = rf"{LEDGER_TECH_ALPHA}{LEDGER_TECH_DIGITS}_.%~\-–—"
LEDGER_TECH_TOKEN = re.compile(
    rf"(?<![{LEDGER_TECH_ALPHA}{LEDGER_TECH_DIGITS}])(?:"
    rf"[0-9]{{4}}\.[0-9]{{5}}"
    rf"|(?=[{LEDGER_TECH_CHARS}]*[{LEDGER_TECH_DIGITS}])"
    rf"(?=[{LEDGER_TECH_CHARS}]*[{LEDGER_TECH_ALPHA}])"
    rf"[{LEDGER_TECH_CHARS}]+"
    rf"|[0-9]+(?:\.[0-9]+)?(?:[-–~][0-9]+(?:\.[0-9]+)?)?%?"
    rf")(?![{LEDGER_TECH_ALPHA}{LEDGER_TECH_DIGITS}])"
)
LEDGER_COUNT_UNIT = re.compile(r"(?<![A-Za-z0-9])([0-9]+(?:\.[0-9]+)?)\s*(rollouts?|seeds?)\b", re.I)
LEDGER_LABELED_QUANTITY = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9]+(?:\.[0-9]+)?\s*(?:kg|MAJOR)\b|[0-9]+-state\b)",
    re.I,
)
LEDGER_NUMERIC_OPERATOR = re.compile(
    r"(?<![A-Za-z0-9])(?:\+[0-9]+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?\+|[0-9]+/[0-9]+)(?![A-Za-z0-9])"
)
LEDGER_CODE_SPAN = re.compile(r"`([^`\n]+)`")
LEDGER_SYMBOL = re.compile(
    r"[≥≤<>≠≈±↔⇒⇔↑×−≡∈∉∃∀∞∝∼∩∪⊂⊃⊆⊇⊥⟂∥∧∨∇∂√∑∏≫①-⑳$|~^]"
    r"|[Α-Ωα-ω]+"
)
CALIB_URL = re.compile(r"https?://[^\s\t|<>\[\]()`，。；;]+")
CALIB_ARXIV_ID = re.compile(r"(?<!\d)\d{4}\.\d{5}(?!\d)")
CALIB_DATE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?(?!\d)")
CALIB_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])(?:v\d+(?:\.\d+)*|\d+[x×]H\d+|\d+/\d+|"
    r"\d+(?:\.\d+)?(?:[-~]\d+(?:\.\d+)?)?(?:%|[A-Za-z]+)?)(?![A-Za-z0-9])"
)
CALIB_VERDICT = re.compile(
    r"(?<![A-Za-z0-9-])(?:strong-accept|accept-w-rev|reject|AwR|SA)(?![A-Za-z0-9-])"
)
CALIB_MODEL = re.compile(r"(?:Fable 5|Opus 4\.8)")
CALIB_PAPER_TITLE = re.compile(r"^-\s+([^|\n]+?)\s*\|\s*https?://", re.M)

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def stable_calibration_title(value):
    value = " ".join(HAN.sub("", value).split())
    value = re.sub(r"\s+\(", "(", value)
    return re.sub(r"\s+\)", ")", value)

def read_text(path):
    try:
        return path.read_text()
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError, OSError):
        return None

def assert_text_contract(paths):
    failures = []
    for path in paths:
        if not path.is_file():
            continue
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

def executable_shell_paths():
    raw = subprocess.check_output(
        [
            "git", "ls-files", "-z", "--", "*.sh", ".githooks/*",
            ".github/workflows/*.yml", ".github/workflows/*.yaml",
        ],
        cwd=ROOT,
    )
    return [ROOT / item.decode() for item in raw.split(b"\0") if item]

def workflow_shell_text(text):
    lines = text.splitlines()
    commands = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)env:[ \t]*$", line)
        if not match:
            continue
        base_indent = len(match.group(1))
        for nested in lines[index + 1:]:
            if not nested.strip():
                continue
            indent = len(nested) - len(nested.lstrip())
            if indent <= base_indent:
                break
            assignment = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.+)$", nested)
            if assignment:
                commands.append(f"{assignment.group(1)}={assignment.group(2).strip()}")
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r"^(\s*)(?:-[ \t]+)?run:[ \t]*(.*)$", line)
        if not match:
            i += 1
            continue
        value = match.group(2).strip()
        if value not in {"|", ">", "|-", ">-", "|+", ">+"}:
            if value:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                commands.append(value)
            i += 1
            continue
        base_indent = len(match.group(1))
        i += 1
        while i < len(lines):
            nested = lines[i]
            if not nested.strip():
                commands.append("")
                i += 1
                continue
            indent = len(nested) - len(nested.lstrip())
            if indent <= base_indent:
                break
            commands.append(nested.lstrip())
            i += 1
    return "\n".join(commands)

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
    expected = f"model=${{AGY_MODEL:-{AGY_MODEL_DEFAULT}}}"
    for name in ("agy-worker.sh", "awr-side.sh"):
        assignments = [
            line
            for line in (ROOT / name).read_text().splitlines()
            if line == expected
        ]
        if assignments != [expected]:
            raise AssertionError(
                f"agy model default mismatch in {name}: expected {expected!r}, found {assignments!r}"
            )

def shell_code_lines(text):
    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()!")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens = list(lexer)
        except ValueError:
            tokens = [] if line.lstrip().startswith("#") else [line]
        if not tokens:
            continue
        lines.append((number, " ".join(tokens)))
    return lines

def shell_command_segments(code):
    lexer = shlex.shlex(code, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    segments = []
    current = []
    for token in tokens:
        if token and all(character in ";&|()" for character in token):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments

def optioned_wrapper_invokes_claude(code, tainted=None):
    tainted = tainted or set()
    controls = {"!", "if", "then", "elif", "while", "until", "do", "else"}
    assignments = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
    sudo_arg_options = {
        "-C", "-D", "-g", "-h", "-p", "-r", "-R", "-t", "-T", "-u", "-U",
        "--chdir", "--close-from", "--group", "--host", "--prompt", "--role",
        "--type", "--other-user", "--command-timeout",
    }
    env_arg_options = {"-C", "-S", "-u", "--chdir", "--split-string", "--unset"}
    time_arg_options = {"-f", "-o", "--format", "--output"}
    exec_arg_options = {"-a"}

    def skip_options(tokens, index, options_with_arguments):
        while index < len(tokens):
            token = tokens[index]
            if token == "--":
                return index + 1
            if not token.startswith("-") or token == "-":
                return index
            name = token.split("=", 1)[0]
            index += 1
            if name in options_with_arguments and "=" not in token:
                if len(name) == 2 and len(token) > 2:
                    continue
                index += 1
        return index

    for tokens in shell_command_segments(code):
        index = 0
        while index < len(tokens):
            while index < len(tokens) and (
                tokens[index].lower() in controls or assignments.match(tokens[index])
            ):
                index += 1
            if index >= len(tokens):
                break
            command_token = tokens[index]
            command_variable = re.fullmatch(
                r"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)(?:\})?",
                command_token,
            )
            if command_variable and command_variable.group(1) in tainted:
                return True
            command = command_token.rsplit("/", 1)[-1].lower()
            if command == "claude":
                return True
            index += 1
            if command == "sudo":
                index = skip_options(tokens, index, sudo_arg_options)
                continue
            if command == "env":
                index = skip_options(tokens, index, env_arg_options)
                continue
            if command == "command":
                if index < len(tokens) and tokens[index] in {"-v", "-V"}:
                    break
                index = skip_options(tokens, index, set())
                continue
            if command == "time":
                index = skip_options(tokens, index, time_arg_options)
                continue
            if command == "exec":
                index = skip_options(tokens, index, exec_arg_options)
                continue
            if command == "nohup":
                index = skip_options(tokens, index, set())
                continue
            break
    return False

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

def claude_invocation_lines(text):
    lines = shell_code_lines(text)
    tainted = claude_tainted_variables(lines)
    failures = []
    for number, code in lines:
        code = code.lstrip()
        unsafe = bool(DIRECT_CLAUDE_COMMAND.search(code)) or optioned_wrapper_invokes_claude(
            code,
            tainted,
        )
        for match in FALLBACK_EXPANSION.finditer(code):
            fallback = match.group(1)
            references = VARIABLE_REFERENCE.findall(fallback)
            if "claude" in fallback.lower() or any(item in tainted for item in references):
                unsafe = True
                break
        if any(match.group(1) in tainted for match in VARIABLE_COMMAND.finditer(code)):
            unsafe = True
        if EVAL_COMMAND.search(code) and any(
            reference in tainted for reference in VARIABLE_REFERENCE.findall(code)
        ):
            unsafe = True
        if SHELL_C_COMMAND.search(code) and (
            "claude" in code.lower()
            or any(reference in tainted for reference in VARIABLE_REFERENCE.findall(code))
        ):
            unsafe = True
        if unsafe:
            failures.append(number)
    return failures

def assert_no_claude_invocations():
    failures = []
    for path in executable_shell_paths():
        text = read_text(path)
        if text is None:
            continue
        if path.suffix in {".yml", ".yaml"}:
            text = workflow_shell_text(text)
        failures.extend(
            f"{path.relative_to(ROOT)}:{number}"
            for number in claude_invocation_lines(text)
        )
    if failures:
        raise AssertionError("automatic Claude invocation remains in " + ", ".join(failures))

def provider_registry_forbidden_paths(value, path="$"):
    forbidden = "cl" + "aude"
    failures = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if forbidden in str(key).lower():
                failures.append(child)
            failures.extend(provider_registry_forbidden_paths(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            failures.extend(provider_registry_forbidden_paths(item, f"{path}[{index}]"))
    elif isinstance(value, str) and forbidden in value.lower():
        failures.append(path)
    return failures

def verify_provider_registry():
    registry = json.loads((ROOT / "history/provider-adapters-v1.json").read_text())
    expected_providers = ["codex", "kimi", "grok", "opencode", "agy"]
    if list(registry) != ["schema_version", "registry_revision", "providers", "surfaces"]:
        raise AssertionError("provider registry fields changed")
    if list(registry["providers"]) != expected_providers:
        raise AssertionError("provider registry set or order changed")
    if registry["surfaces"] != {
        "hunt": expected_providers[:3],
        "awr": expected_providers,
    }:
        raise AssertionError("provider surface eligibility changed")
    failures = provider_registry_forbidden_paths(registry)
    if failures:
        raise AssertionError("forbidden provider registry path: " + ", ".join(failures))

    forbidden = "cl" + "aude"
    scanner_vectors = (
        {"providers": {forbidden: {}}},
        {"aliases": {"portable": forbidden}},
        {"default_provider": forbidden},
        {"wrapper": f"/tmp/{forbidden}"},
    )
    if any(not provider_registry_forbidden_paths(vector) for vector in scanner_vectors):
        raise AssertionError("provider registry forbidden-path scanner regressed")
    shell_vector = f"BACKEND=/tmp/{forbidden}\nsudo env $BACKEND --print prompt\n"
    if not claude_invocation_lines(shell_vector):
        raise AssertionError("tainted wrapper command scanner regressed")


def verify_production_evidence_roots():
    roots = json.loads(
        (ROOT / "history/production-evidence-roots-v1.json").read_text()
    )
    if list(roots) != [
        "schema_version", "registry_revision",
        "fault_reports", "replay_reports", "semantic_evaluation_reports",
    ]:
        raise AssertionError("production evidence root registry is not closed")
    if (
        roots["schema_version"] != "history-production-evidence-roots-v1"
        or not isinstance(roots["registry_revision"], str)
        or not roots["registry_revision"]
        or roots["fault_reports"] != []
        or roots["replay_reports"] != []
        or roots["semantic_evaluation_reports"] != []
    ):
        raise AssertionError(
            "shipped production evidence roots must remain empty"
        )

def verify_runtime():
    assert_backend_defaults()
    assert_no_claude_invocations()
    verify_provider_registry()
    verify_production_evidence_roots()
    verify_awr_state_aliases()
    assert_text_contract(runtime_paths())
    required = {
        "brainstorming_policy.md": ["## Divergence Lenses", "## Theme Vocabulary"],
        "hunt.sh": [
            "Papers Read",
            "Overlap",
            "CONTAINED_AGENT_CMD_JSON",
            "history_sync()",
            "history_reconcile_ledger()",
            "history_build_brief()",
            "run_contained_stage()",
            "history_observe_round()",
            "history_compare_shortlist()",
            "history_compare_targets()",
            "history_seal_resume_attempt()",
            "history_materialize_research()",
            "history_receipts_ok()",
            "history_append_rows()",
            "history_materialize_ledger()",
            "prepare_external_mirror()",
            "copy_external_output()",
            "lib/history_runtime.py",
            "lib/history_archive.py",
        ],
        "roles/generate.md": [
            "generation_brief.json",
            "Minimal Falsification Experiment",
        ],
        "roles/review.md": [
            "history_summary.json",
        ],
        "PROGRAM.md": [
            ".ai-ideas/history.sqlite3",
            "immutable candidate batch",
            "history_abstain",
            "replayable TSV projection",
            "shadow",
            "enforcement",
        ],
        "roles/research.md": [
            "history-summaries",
            "not evidence",
            "academic novelty",
            "complete_no_match",
        ],
        "ledger.instance-id": [],
        "lib/history_eval.py": ["synthetic_contract_only"],
        "lib/provider_adapters.py": ["def load_registry", "def resolve_provider", "def render_command"],
        "lib/portable_agent.py": ["def run_portable_attempt"],
        "lib/history_audit_plan.py": ["def build_plan", "def reserve_attempt", "def settle_attempt"],
        "history/provider-adapters-v1.json": ["provider-adapters-v1"],
        "history/capacity-profiles-v1.json": ["safe-24k-v1", "unbudgetable"],
        "history/l2-budget-v1.json": ["l2-budget-v1"],
        "history/settlement-policy-v1.json": [
            "history-settlement-policy-v1",
            "deterministic-equality-v1",
        ],
        "history/production-evidence-roots-v1.json": [
            "history-production-evidence-roots-v1",
            "fault_reports",
            "replay_reports",
            "semantic_evaluation_reports",
        ],
        "lib/history_store.py": [
            "search_projection_outbox",
            "ledger_projection_outbox",
            "near_sa_observations",
        ],
        "awr-side.sh": ["Revised Idea", "Strongest Counterexample", "Reviewer Feedback"],
        "calib/run_panel.sh": ["suspected published counterpart:"],
        "calib/run_e2e.sh": ["Overlap:"],
    }
    for name, needles in required.items():
        path = ROOT / name
        if not path.is_file():
            raise AssertionError(f"missing required runtime path: {name}")
        text = path.read_text()
        for needle in needles:
            if needle not in text:
                raise AssertionError(f"missing {needle!r} in {name}")
    hunt = (ROOT / "hunt.sh").read_text()
    forbidden_hunt = {
        "routine meta invocation": r"Read roles/meta\.md and follow it",
        "legacy meta scheduler": r"\bMETA_(?:EVERY|MIN_REJECTS)\b",
        "production test escape": r"HISTORY_RUNTIME_TEST_MODE",
        "direct ledger append": r">>[ \t]*[\"']?ledger\.tsv",
        "projection copy ownership": (
            r"\bcp[ \t]+(?:[\"']?ledger\.tsv|"
            r"[\"']?\$(?:HISTORY_)?LEDGER_GOOD)"
        ),
        "unbounded generate ledger read": r"Read roles/generate\.md and follow it",
    }
    for label, pattern in forbidden_hunt.items():
        if re.search(pattern, hunt):
            raise AssertionError(f"{label} remains in hunt.sh")
    for name in (
        "roles/bounded-generate.md",
        "roles/bounded-meta.md",
        "roles/bounded-review.md",
    ):
        if (ROOT / name).exists():
            raise AssertionError(f"temporary contained role remains: {name}")
    store = (ROOT / "lib/history_store.py").read_text()
    for deferred in (
        "reentry_grants",
        "reentry_requests",
        "round_slots",
        "materialization_outbox",
    ):
        if re.search(
            rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{deferred}\b",
            store,
            flags=re.IGNORECASE,
        ):
            raise AssertionError(
                f"deferred AWR bridge table created in history store: {deferred}"
            )
    for role_name in (
        "roles/generate.md",
        "roles/meta.md",
        "roles/review.md",
        "roles/history-compare.md",
    ):
        role_text = (ROOT / role_name).read_text()
        if "ledger.tsv" in role_text:
            raise AssertionError(
                f"bounded role still references full ledger: {role_name}"
            )
    policy = (ROOT / "history/retrieval-policy-v1.json").read_text()
    if '"mode": "shadow"' not in policy and '"mode":"shadow"' not in policy:
        raise AssertionError("shipped retrieval policy must remain shadow")
    assert_research_direction_product_contract()


def assert_research_direction_product_contract():
    """Bind the directed-run operator contract across its public surfaces."""
    required = {
        "README.md": [
            "RESEARCH_DIRECTION_FILE='directions/dynamic-spatial-memory-vla-v1.json'",
            "caffeinate -is ./hunt.sh",
            "canonicalized before any agent invocation",
            "Direction Axis",
            "Target Failure",
            "Direction Evidence",
            "independent selector classification",
            "rejects the whole batch before history retrieval and research",
            "same canonical direction identity",
            "broad generation",
            "not a proof of natural-language meaning",
        ],
        "docs/getting-started.md": [
            "RESEARCH_DIRECTION_FILE='directions/dynamic-spatial-memory-vla-v1.json'",
            "caffeinate -is ./hunt.sh",
            "canonicalized before any agent invocation",
            "independent selector classification",
            "rejects the whole batch before history retrieval and research",
            "same canonical direction identity",
            "broad generation",
            "not a proof of natural-language meaning",
        ],
        "PROGRAM.md": [
            "direction_constraint.json",
            "direction-constraint.json",
            "Direction Axis",
            "Target Failure",
            "Direction Evidence",
            "direction.tsv",
            "in-scope",
            "out-of-scope",
            "schema-v2",
            "expected-direction",
            "rejected:direction",
            "short no-hit retry",
            "theme_min_low=0",
            "Form, falsification, evidence, novelty, review, and",
        ],
        "docs/architecture.md": [
            "direction_constraint.json",
            "direction-constraint.json",
            "Direction Axis",
            "Target Failure",
            "Direction Evidence",
            "direction.tsv",
            "in-scope",
            "out-of-scope",
            "schema-v2",
            "expected-direction",
            "rejected:direction",
            "short no-hit retry",
            "theme_min_low=0",
            "while all other quality gates remain active",
        ],
        "hunt.sh": [
            "RESEARCH_DIRECTION_FILE",
            "direction_constraint.json",
            "direction.tsv",
            "validate_direction_verdicts",
            "rejected:direction",
            "random_no_hit_sleep_min",
            "theme_min_low=0",
            "--expected-direction",
        ],
        "roles/generate.md": [
            "direction_constraint.json",
            "Direction Axis",
            "Target Failure",
            "Direction Evidence",
        ],
        "roles/select.md": [
            "direction-constraint.json",
            "direction.tsv",
            "in-scope",
            "out-of-scope",
            "rejects the whole batch",
        ],
    }
    direction = ROOT / "directions/dynamic-spatial-memory-vla-v1.json"
    if not direction.is_file():
        raise AssertionError("missing initial research-direction contract")
    direction_text = direction.read_text()
    if '"direction_id": "dynamic-spatial-memory-vla-v1"' not in direction_text:
        raise AssertionError("initial research-direction contract identity changed")
    for name, needles in required.items():
        text = (ROOT / name).read_text()
        for needle in needles:
            if needle not in text:
                raise AssertionError(
                    f"missing directed-run contract {needle!r} in {name}"
                )

def ledger_rows():
    with (ROOT / "ledger.tsv").open(newline="") as handle:
        return list(csv.reader(handle, delimiter="\t"))

def verify_awr_state_aliases():
    path = ROOT / "awr-state-aliases.tsv"
    with path.open(newline="") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    if not rows or rows[0] != ["ledger_row", "legacy_key"]:
        raise AssertionError("AwR state-alias header changed")
    entries = []
    for row in rows[1:]:
        if len(row) != 2 or not row[0].isdigit() or not re.fullmatch(r"[0-9a-f]{12}", row[1]):
            raise AssertionError(f"invalid AwR state alias: {row}")
        entries.append((int(row[0]), row[1]))
    if len(entries) != 371:
        raise AssertionError(f"AwR state-alias count changed: {len(entries)}")
    ledger = ledger_rows()
    eligible_rows = {
        number
        for number, row in enumerate(ledger[1:], 2)
        if row[1] == "hunt" and row[4] == "accept-w-rev"
    }
    alias_rows = [number for number, _ in entries]
    alias_keys = [key for _, key in entries]
    if set(alias_rows) != eligible_rows or len(alias_rows) != len(set(alias_rows)):
        raise AssertionError("AwR state aliases no longer cover each eligible physical row exactly once")
    # Ten duplicate source ideas share their historical state key. The frozen
    # projection below protects that intentional many-to-one compatibility map.
    if len(set(alias_keys)) != 361:
        raise AssertionError("AwR state-alias collision set changed")
    projection = "\n".join(f"{number}\t{key}" for number, key in entries)
    if digest(projection) != EXPECTED["awr_state_aliases"]:
        raise AssertionError("AwR state aliases changed")

def ordered_row_token_projection(
    data,
    pattern,
    group=0,
    normalize=None,
    preprocess=None,
    strip_urls=False,
):
    normalize = normalize or (lambda token: token)
    lines = []
    for i, row in enumerate(data, 1):
        for field_index in (3, 5):
            value = row[field_index]
            if strip_urls:
                value = LEDGER_URL.sub(
                    lambda match: " " * len(match.group(0)),
                    value,
                )
            if preprocess:
                value = preprocess(value)
            tokens = [
                normalize(match.group(group))
                for match in pattern.finditer(value)
            ]
            lines.append(f"{i}:{field_index}:{'|'.join(tokens)}")
    return "\n".join(lines)

def normalize_ledger_technical_text(value):
    return re.sub(
        rf"(?<=[{LEDGER_TECH_DIGITS}])\s+(ms|s|Hz|rollouts?|seeds?|kg|MAJOR)\b",
        lambda match: match.group(1),
        value,
        flags=re.I,
    )

def normalize_ledger_technical_token(token):
    token = re.sub(r"^[/+:;–—]+", "", token)
    token = re.sub(r"[/.,:;–—]+$", "", token)
    arxiv = re.match(r"^([0-9]{4}\.[0-9]{5})(?::.*)?$", token)
    if arxiv:
        return arxiv.group(1)
    token = re.sub(
        r"^([0-9]+(?:\.[0-9]+)?)(?:rollouts?|seeds?|kg|MAJOR)$",
        r"\1",
        token,
        flags=re.I,
    )
    token = re.sub(r"^([0-9]+)-state$", r"\1", token, flags=re.I)
    return token

def normalize_ledger_count_unit(token):
    match = LEDGER_COUNT_UNIT.fullmatch(token)
    if not match:
        return token
    unit = "rollout" if match.group(2).lower().startswith("rollout") else "seed"
    return f"{match.group(1)}:{unit}"

def normalize_ledger_labeled_quantity(token):
    state = re.fullmatch(r"([0-9]+)-state", token, re.I)
    if state:
        return f"{state.group(1)}:state"
    labeled = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(kg|MAJOR)", token, re.I)
    if not labeled:
        return token
    return f"{labeled.group(1)}:{labeled.group(2).lower()}"

def ledger_evidence(data):
    return {
        "row_urls": digest(ordered_row_token_projection(
            data,
            LEDGER_URL,
            normalize=lambda token: token.rstrip(".,;:"),
        )),
        "row_technical_tokens": digest(ordered_row_token_projection(
            data,
            LEDGER_TECH_TOKEN,
            normalize=normalize_ledger_technical_token,
            preprocess=normalize_ledger_technical_text,
            strip_urls=True,
        )),
        # Product-artifact projections guard quantitative semantics introduced
        # or made explicit by the curated prose. The URL, technical-token,
        # code-span, and mathematical-symbol projections remain source-frozen.
        "row_count_units": digest(ordered_row_token_projection(
            data,
            LEDGER_COUNT_UNIT,
            normalize=normalize_ledger_count_unit,
            strip_urls=True,
        )),
        "row_labeled_quantities": digest(ordered_row_token_projection(
            data,
            LEDGER_LABELED_QUANTITY,
            normalize=normalize_ledger_labeled_quantity,
            strip_urls=True,
        )),
        "row_numeric_operators": digest(ordered_row_token_projection(
            data,
            LEDGER_NUMERIC_OPERATOR,
            strip_urls=True,
        )),
        "row_code_spans": digest(ordered_row_token_projection(
            data,
            LEDGER_CODE_SPAN,
            group=1,
        )),
        "row_symbols": digest(ordered_row_token_projection(
            data,
            LEDGER_SYMBOL,
            strip_urls=True,
        )),
    }

def verify_ledger_evidence(data=None, header=None):
    if data is None:
        rows = ledger_rows()
        header = rows[0]
        data = rows[1:]
    if header is not None and header != LEDGER_HEADER:
        raise AssertionError(f"ledger header changed: {header}")
    if len(data) != 538:
        raise AssertionError(f"ledger row count changed: {len(data)}")
    nf7 = sum(len(row) == 7 for row in data)
    nf8 = sum(len(row) == 8 for row in data)
    if (nf7, nf8) != (216, 322):
        raise AssertionError(f"ledger shape changed: nf7={nf7}, nf8={nf8}")
    actual = ledger_evidence(data)
    for key, value in actual.items():
        if value != EXPECTED[key]:
            raise AssertionError(f"ledger {key} changed")

def verify_ledger():
    rows = ledger_rows()
    data = rows[1:]
    verify_ledger_evidence(data, rows[0])
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
    theme_projection = "\n".join(row[2] for row in data)
    if digest(theme_projection) != EXPECTED["theme_projection"]:
        raise AssertionError("ledger theme sequence changed")
    assert_text_contract([ROOT / "ledger.tsv"])

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
    evidence_paths = [ROOT / "calib/README.md"]
    evidence_paths.extend(sorted((ROOT / "calib/cases").glob("**/*")))
    evidence_paths.extend(sorted((ROOT / "calib").glob("results-*.md")))
    evidence = []
    for path in evidence_paths:
        if not path.is_file():
            continue
        text = path.read_text()
        tokens = []
        tokens.extend(
            "url:" + match.group(0).rstrip(".,;:")
            for match in CALIB_URL.finditer(text)
        )
        tokens.extend("arxiv:" + match.group(0) for match in CALIB_ARXIV_ID.finditer(text))
        tokens.extend("date:" + match.group(0) for match in CALIB_DATE.finditer(text))
        tokens.extend("number:" + match.group(0) for match in CALIB_NUMBER.finditer(text))
        tokens.extend("verdict:" + match.group(0) for match in CALIB_VERDICT.finditer(text))
        tokens.extend("model:" + match.group(0) for match in CALIB_MODEL.finditer(text))
        tokens.extend(
            "title:" + stable_calibration_title(match.group(1))
            for match in CALIB_PAPER_TITLE.finditer(text)
        )
        evidence.append(f"{path.relative_to(ROOT)}:{'|'.join(sorted(tokens))}")
    actual = digest("\n".join(evidence))
    if actual != EXPECTED["calibration_evidence"]:
        raise AssertionError(f"calibration evidence tokens changed: {actual}")

def tracked_text_paths():
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    paths = [ROOT / item.decode() for item in raw.split(b"\0") if item]
    report = ROOT / "s1_report_20260720.md"
    if report.exists() and report not in paths:
        paths.append(report)
    return paths

def verify_all():
    verify_runtime()
    assert_text_contract(tracked_text_paths())
    verify_fixtures()
    verify_ledger()

SCOPES = {
    "runtime": verify_runtime,
    "fixtures": verify_fixtures,
    "ledger-evidence": verify_ledger_evidence,
    "ledger": verify_ledger,
    "all": verify_all,
}

if __name__ == "__main__":
    scope = sys.argv[1] if len(sys.argv) == 2 else "all"
    if scope not in SCOPES:
        raise SystemExit(f"usage: {sys.argv[0]} [{'|'.join(SCOPES)}]")
    SCOPES[scope]()
    print(f"ok: {scope}")
