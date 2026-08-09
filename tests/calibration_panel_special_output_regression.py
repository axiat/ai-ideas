#!/usr/bin/env python3
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile


MODE = os.environ.get("CALIB_SPECIAL_OUTPUT_MODE")
if MODE:
    artifact, kind = MODE.split("-", 1)
    out = pathlib.Path("tmp/out")
    out.mkdir(parents=True, exist_ok=True)
    verdict = out / "verdict.tsv"
    review = out / "review.md"
    if artifact == "review":
        verdict.write_text(
            "I1\taccept-w-rev\t1\tThe fixture retains one bounded major finding.\n",
            encoding="utf-8",
        )
        target = review
        payload = "## I1\nA complete fixture review.\n"
    else:
        target = verdict
        payload = "I1\treject\t0\tThe fixture is directly occupied.\n"

    backing = target.with_name(target.name + ".backing")
    if kind == "symlink":
        backing.write_text(payload, encoding="utf-8")
        target.symlink_to(backing.name)
    elif kind == "hardlink":
        backing.write_text(payload, encoding="utf-8")
        os.link(backing, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        raise SystemExit(f"unknown special-output mode: {MODE}")
    raise SystemExit(0)


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODES = (
    "verdict-symlink",
    "verdict-hardlink",
    "verdict-fifo",
    "review-symlink",
    "review-hardlink",
    "review-fifo",
)


def run_bounded(command, *, cwd, env, timeout):
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        raise AssertionError(
            f"panel hung while capturing {env['CALIB_SPECIAL_OUTPUT_MODE']}"
        )
    return process.returncode, stdout, stderr


with tempfile.TemporaryDirectory(prefix="calibration-special-output-") as temporary:
    repo = pathlib.Path(temporary) / "repo"
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(repo)],
        check=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "--detach", head],
        check=True,
    )
    shutil.copy2(ROOT / "calib/run_panel.sh", repo / "calib/run_panel.sh")
    shutil.copy2(pathlib.Path(__file__), repo / "tests" / pathlib.Path(__file__).name)
    backend = repo / "tests" / pathlib.Path(__file__).name
    backend.chmod(0o755)

    case = repo / "test-fixtures" / "special-output-case"
    case.mkdir(parents=True)
    (case / "ideas.md").write_text(
        "## I1\n"
        "One-Sentence Story: Special output capture fixture\n"
        "Theme: Evaluation and Diagnostics\n"
        "Form: new mechanism or new problem\n"
        "Summary: A fixture used only for offline contract validation.\n"
        "Minimal Falsification Experiment: Compare one held-out case using 1xH100.\n"
        "Why It May Be Novel: Independent research decides.\n",
        encoding="utf-8",
    )
    (case / "priorwork.md").write_text(
        "## I1\nOverlap: high - Fixture Paper occupies the headline.\n",
        encoding="utf-8",
    )

    failures = []
    for mode in MODES:
        env = os.environ.copy()
        env["CALIB_SPECIAL_OUTPUT_MODE"] = mode
        env["PANEL_CMD"] = f"tests/{backend.name}"
        try:
            returncode, stdout, stderr = run_bounded(
                ["./calib/run_panel.sh", str(case), "1"],
                cwd=repo,
                env=env,
                timeout=8,
            )
        except AssertionError as exc:
            failures.append(str(exc))
            continue
        combined = stdout + stderr
        artifact = mode.split("-", 1)[0] + (".tsv" if mode.startswith("verdict") else ".md")
        if returncode == 0:
            failures.append(f"accepted unsafe reviewer output: {mode}")
        elif f"unsafe reviewer output {artifact}" not in combined:
            failures.append(f"did not report unsafe capture for {mode}")

if failures:
    for failure in failures:
        print(f"not ok: {failure}", file=sys.stderr)
    raise SystemExit(1)
print("ok: calibration panel rejects special reviewer outputs without hanging")
