#!/usr/bin/env python3
import csv
import hashlib
import pathlib
import re
import shlex
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
EXPECTED = {
    "stable_projection": "810adad8122a7761ba394e6a67cdfa12d8c4f869fc888a5c2a8e8cb61c3a29cb",
    "theme_projection": "5dd438abbc8fd9e71f42256fd453afa9a538d13201dd19ae59fdb4400cb6d435",
    "row_urls": "6015a625fa509040d974ba6bba4bf00dde25fca1763fc1ace1cdf57cded3f9c9",
    "row_technical_tokens": "2c54301a75872d2f8198634652ee3e2c4706e71c0d7cc716d3a337b5933bfef6",
    "row_count_units": "1aebc3b0cbc0a5c74b74b0e93a6cba509f28f8579cb4a7d26e84d9415ab16d91",
    "row_labeled_quantities": "d098ed9408958d094b29ccb8cbf4c7eb2c8f1044f67d9bfcdba7698b9373d08d",
    "row_numeric_operators": "b9e49642028714c4d04c2627bc485e8995a093a5ba2557e2526d5d43b1ded4bb",
    "row_code_spans": "4d9fe189ad926f8263062722340592a64fefb3e408cbb8a13d891f01532f4ebb",
    "row_symbols": "3167603d417007adeec710fb58c67a243568d417fcdeeffbc1d6190c002e6de3",
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
    "lib/resolve_cmd.sh", "PROGRAM.md", "hunt.md", "trigger.md",
    "research_context.md", "brainstorming_policy.md", "rubric.md",
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
    except UnicodeDecodeError:
        return None

def assert_text_contract(paths):
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
            if re.match(r"^\s*model=", line)
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

def verify_runtime():
    assert_backend_defaults()
    assert_no_claude_invocations()
    verify_awr_state_aliases()
    assert_text_contract(runtime_paths())
    required = {
        "brainstorming_policy.md": ["## Divergence Lenses", "## Theme Vocabulary"],
        "hunt.sh": ["Papers Read", "Minimal Falsification Experiment", "Overlap"],
        "awr-side.sh": ["Revised Idea", "Strongest Counterexample", "Reviewer Feedback"],
        "calib/run_panel.sh": ["suspected published counterpart:"],
        "calib/run_e2e.sh": ["Overlap:"],
    }
    for name, needles in required.items():
        text = (ROOT / name).read_text()
        for needle in needles:
            if needle not in text:
                raise AssertionError(f"missing {needle!r} in {name}")

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
    if len(data) != 531:
        raise AssertionError(f"ledger row count changed: {len(data)}")
    nf7 = sum(len(row) == 7 for row in data)
    nf8 = sum(len(row) == 8 for row in data)
    if (nf7, nf8) != (216, 315):
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
