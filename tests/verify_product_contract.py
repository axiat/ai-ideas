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
    ".githooks/pre-push", ".github/workflows/auto-merge-claude.yml",
]
BACKEND_DEFAULTS = {
    "hunt.sh": (
        "AGENT_CMD",
        "codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write",
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
