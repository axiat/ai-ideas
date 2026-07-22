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
    "stable_projection": "4350ee8caad0fb81b1b8d962236c7a1be3ba345f65ed4c41725c06628b1ccb9a",
    "urls": "6c26006c40788e96d0d5e91662867644f7720287b787d4d43f5257fc85bea23a",
    "numbers": "1f8236a7f082296dc1e754189e7a921ff625fb51e61fe6e8dc77f53ba6741e1a",
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

def verify_runtime():
    assert_english(runtime_paths())
    for name in ("hunt.sh", "calib/run_panel.sh", "calib/run_all.sh", "calib/run_e2e.sh"):
        text = (ROOT / name).read_text()
        if re.search(r":-claude(?:\s|$)", text):
            raise AssertionError(f"automatic Claude fallback remains in {name}")
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

def verify_ledger():
    rows = ledger_rows()
    data = rows[1:]
    if len(data) != 531:
        raise AssertionError(f"ledger row count changed: {len(data)}")
    nf7 = sum(len(row) == 7 for row in data)
    nf8 = sum(len(row) == 8 for row in data)
    if (nf7, nf8) != (216, 315):
        raise AssertionError(f"ledger shape changed: nf7={nf7}, nf8={nf8}")
    projection = "\n".join("\t".join([row[0], row[1], row[4], row[6], row[7] if len(row) == 8 else ""]) for row in data)
    if digest(projection) != EXPECTED["stable_projection"]:
        raise AssertionError("stable ledger columns changed")
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
    assert_english(tracked_text_paths())
    verify_runtime()
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
