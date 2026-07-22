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
    "row_urls": "6015a625fa509040d974ba6bba4bf00dde25fca1763fc1ace1cdf57cded3f9c9",
    "row_technical_tokens": "5dfe40626264373a4a7e695c9a2c6dae837cf03d79d4be4762ea3526d17f1e42",
    "row_code_spans": "4d9fe189ad926f8263062722340592a64fefb3e408cbb8a13d891f01532f4ebb",
    "row_symbols": "f7ddcafc43335b78f33e8115abb9a44bebdf19fdb33399867084e98ba57d2268",
    "case_ids": "aed82be120ea6d26d1735050352867fafa4551e9681b8da3abce496915fae1c4",
    "assertions": "a85dfbcece8c4c223ab0dfca3eb6a2ef17f091d71b67cebccfc7e3348aaea3f0",
    "calibration_evidence": "1da4ef109b01fd5d7c7984993004009c2a888b3dc38c593569bea437b35f9fd0",
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
    "calib/run_all.sh": (
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
LEDGER_HEADER = ["date", "source", "theme", "idea", "verdict", "reason", "overlap"]
LEDGER_URL = re.compile(r"https?://[^\s\t()<>\[\]`，。；（）]+")
LEDGER_TECH_DIGITS = r"0-9⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉"
LEDGER_TECH_CHARS = rf"A-Za-z{LEDGER_TECH_DIGITS}_.%+:/×x~<>=\-–—"
LEDGER_TECH_TOKEN = re.compile(
    rf"(?<![{LEDGER_TECH_CHARS}])"
    rf"(?=[{LEDGER_TECH_CHARS}]*[{LEDGER_TECH_DIGITS}])"
    rf"[{LEDGER_TECH_CHARS}]+"
    rf"(?![{LEDGER_TECH_CHARS}])"
)
LEDGER_CODE_SPAN = re.compile(r"`([^`\n]+)`")
LEDGER_SYMBOL = re.compile(
    r"[≥≤<>≠≈±→↔⇒⇔↑×−≡∈∉∃∀∞∝∼∩∪⊂⊃⊆⊇⊥⟂∥∧∨∇∂√∑∏≫①-⑳+=|~^]"
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
    return " ".join(HAN.sub("", value).split())

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
    def close_unit_gap(match):
        unit = match.group(1)
        if unit.lower() in {"rollout", "rollouts"}:
            return "rollout"
        if unit.lower() in {"seed", "seeds"}:
            return "seed"
        return unit

    return re.sub(
        rf"(?<=[{LEDGER_TECH_DIGITS}])\s+(ms|s|Hz|rollouts?|seeds?)\b",
        close_unit_gap,
        value,
    )

def normalize_ledger_technical_token(token):
    token = re.sub(r"^[:;–—]+", "", token)
    token = re.sub(r"[.,:;–—]+$", "", token)
    if len(token) > 2 and token.startswith("/") and token.endswith("/"):
        token = token[1:-1]
    token = re.sub(
        r"(?<=\d)(rollouts?|seeds?)$",
        lambda match: "rollout" if match.group(1).startswith("rollout") else "seed",
        token,
    )
    token = re.sub(r"^(\d{4}\.\d{5}):[A-Za-z].*$", r"\1", token)
    return token

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
    if digest("\n".join(evidence)) != EXPECTED["calibration_evidence"]:
        raise AssertionError("calibration evidence tokens changed")

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
