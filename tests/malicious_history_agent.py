#!/usr/bin/python3
import json
import os
import pathlib
import sys
import time


prompt = sys.argv[1]
payload = json.loads(prompt)
stage = payload["stage"]
seat_id = os.environ["HISTORY_STAGE_SEAT_ID"]
mode = os.environ.get("HISTORY_STAGE_ATTACK_MODE", "none")
sentinels = json.loads(os.environ.get("HISTORY_STAGE_SENTINELS_JSON", "[]"))


def readable(path):
    candidate = pathlib.Path(path)
    try:
        if candidate.is_dir():
            os.listdir(candidate)
        else:
            with candidate.open("rb") as handle:
                handle.read(1)
        return True
    except (OSError, PermissionError):
        return False


for sentinel in sentinels:
    if readable(sentinel):
        raise SystemExit(70)

sibling = os.environ.get("HISTORY_STAGE_SIBLING")
if sibling and readable(sibling):
    raise SystemExit(71)

input_path = os.environ.get("HISTORY_STAGE_INPUT_PATH")
if input_path:
    try:
        pathlib.Path(input_path).write_bytes(b"mutated\n")
    except OSError:
        pass
    else:
        raise SystemExit(72)

outside_write = os.environ.get("HISTORY_STAGE_OUTSIDE_WRITE")
if outside_write:
    try:
        pathlib.Path(outside_write).write_bytes(b"escaped\n")
    except OSError:
        pass
    else:
        raise SystemExit(73)

if outside_write and mode == "delayed-child":
    child = os.fork()
    if child == 0:
        time.sleep(0.25)
        try:
            pathlib.Path(outside_write).write_bytes(b"escaped-late\n")
        except OSError:
            pass
        os._exit(0)

detached_pid = None
fork_status = None
if mode == "fork-probe":
    try:
        child = os.fork()
    except OSError:
        fork_status = "fork-probe: denied before child creation"
    else:
        if child == 0:
            os._exit(0)
        os.waitpid(child, 0)
        fork_status = "fork-probe: unexpectedly allowed"

if outside_write and mode == "detached-child":
    detached_pid = os.fork()
    if detached_pid == 0:
        os.setsid()
        for descriptor in (0, 1, 2):
            try:
                os.close(descriptor)
            except OSError:
                pass
        time.sleep(0.5)
        try:
            pathlib.Path(outside_write).write_bytes(
                b"escaped-detached\n"
            )
        except OSError:
            pass
        time.sleep(10)
        os._exit(0)
    time.sleep(0.1)
elif outside_write and mode == "rapid-double-fork":
    try:
        first_child = os.fork()
    except OSError:
        fork_status = "rapid-double-fork: denied before child creation"
    else:
        fork_status = "rapid-double-fork: unexpectedly allowed"
        if first_child == 0:
            os.setsid()
            second_child = os.fork()
            if second_child > 0:
                os._exit(0)
            for descriptor in (0, 1, 2):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            time.sleep(0.25)
            try:
                pathlib.Path(outside_write).write_bytes(
                    b"escaped-double-fork\n"
                )
            except OSError:
                pass
            time.sleep(10)
            os._exit(0)

if mode == "timeout":
    time.sleep(2)
elif mode in {"stdout-overflow", "stderr-overflow"}:
    descriptor = 1 if mode == "stdout-overflow" else 2
    remaining = 1024 * 1024 + 1
    chunk = b"x" * 65536
    while remaining:
        written = os.write(
            descriptor,
            chunk[:min(len(chunk), remaining)],
        )
        remaining -= written
    time.sleep(2)

output = pathlib.Path("output")
output.mkdir(exist_ok=True)

if stage == "generate":
    tsv = "I1\tBounded Test Idea\tWorld Models - Architecture\n"
    heading = "I1"
    why = "Why It May Be Novel: Downstream research must test occupation.\n"
    if mode == "generation-duplicate-id":
        tsv += (
            "I1\tSecond Bounded Test Idea\t"
            "World Models - Architecture\n"
        )
    elif mode == "generation-id-mismatch":
        heading = "I2"
    elif mode == "generation-missing-field":
        why = ""
    summary = (
        fork_status
        or (
            f"Detached child pid {detached_pid} exercises cleanup."
            if detached_pid is not None
            else "Exercise the bounded stage contract."
        )
    )
    markdown = (
        "Assumption-Removal Attempt: incomplete — fixture; "
        "blocked by: evidence\n\n"
        f"## {heading}\n"
        "One-Sentence Story: Bounded Test Idea\n"
        "Theme: World Models - Architecture\n"
        "Form: new mechanism or new problem\n"
        f"Summary: {summary}\n"
        "Minimal Falsification Experiment: Compare against the "
        "strongest fixture baseline on 128 episodes using one H100; "
        "kill the idea if the expected bounded signal is absent.\n"
        f"{why}"
    )
    (output / "ideas.tsv").write_text(tsv, encoding="utf-8")
    (output / "ideas.md").write_text(markdown, encoding="utf-8")
elif stage == "history-compare":
    pack = payload["retrieval_payload"]
    relations = []
    for lineage in pack["lineages"]:
        match = lineage["matches"][0]
        relations.append(
            {
                "relation": "distinct",
                "candidate_id": match["candidate_id"],
                "lineage_id": match["lineage_id"],
                "facet": match["facet"],
                "evidence_id": match["evidence_id"],
                "material_difference": "The bounded evidence differs.",
                "confidence": 0.8,
            }
        )
    comparison = {
        "status": "complete_no_match",
        "comparator_version": "history-comparator-v1",
        "relations": relations,
        "expansion_request": None,
    }
    (output / "history-comparison.json").write_text(
        json.dumps(
            comparison,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
elif stage == "review":
    candidate_id = payload["candidate"]["candidate_id"]
    verdict = "accept-w-rev"
    major = "1"
    markdown_verdict = verdict
    markdown_major = major
    history_line = "History: unavailable\n"
    if mode == "review-missing-field":
        history_line = ""
    elif mode == "review-vote-mismatch":
        markdown_verdict = "strong-accept"
    elif mode == "review-major-mismatch":
        markdown_major = "2"
    elif mode == "review-gate-violation":
        verdict = "strong-accept"
        major = "2"
        markdown_verdict = verdict
        markdown_major = major
    reason = "Bounded evidence needs one revision."
    (output / "review.md").write_text(
        f"# {candidate_id}\n"
        f"Verdict: {markdown_verdict}\n"
        "CRITICAL: 0\n"
        f"MAJOR: {markdown_major}\n"
        "Headline: The bounded candidate remains plausible.\n"
        "Occupation: The supplied prior work leaves one gap.\n"
        "Experiment: The falsification is bounded but incomplete.\n"
        "Estimand: The supplied estimand is aligned.\n"
        "Payoff: One attributable payoff remains possible.\n"
        "Feasibility: One researcher and one H100 suffice.\n"
        f"{history_line}"
        f"Reason: {reason}\n",
        encoding="utf-8",
    )
    (output / "verdict.tsv").write_text(
        f"{candidate_id}\t{verdict}\t{major}\t{reason}\n",
        encoding="utf-8",
    )
elif stage == "meta":
    mounted = {
        item["path"]: item["text"]
        for item in payload["mounted_inputs"]
    }
    batch = json.loads(mounted["failure_batch.json"])
    failure_code = next(
        (
            value
            for value in batch["failure_codes"]
            if value != "unmapped"
        ),
        "unmapped",
    )
    theme = next(
        (
            value
            for value in batch["themes"]
            if value != "unmapped"
        ),
        "unmapped",
    )
    mappings = [
        {
            "source_id": item["source_id"],
            "failure_code": failure_code,
            "theme": theme,
        }
        for item in batch["items"]
    ]
    if mode == "meta-missing" and mappings:
        mappings.pop()
    elif mode == "meta-duplicate" and mappings:
        mappings[-1] = dict(mappings[0])
    elif mode == "meta-extra":
        mappings.append(
            {
                "source_id": "unregistered-source",
                "failure_code": failure_code,
                "theme": theme,
            }
        )
    elif mode == "meta-wrong-vocabulary" and mappings:
        mappings[0]["failure_code"] = "unregistered-code"
    elif mode == "meta-wrong-order":
        mappings.reverse()
    result = {"schema_version": 1, "mappings": mappings}
    (output / "failure-distillation.json").write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
else:
    raise SystemExit(65)

if mode == "extra-output":
    (output / "unlisted.txt").write_text("extra\n", encoding="utf-8")
elif mode == "wrong-attestation":
    attestation = {
        "schema_version": 1,
        "stage": stage,
        "seat_id": seat_id,
        "prompt_sha256": "0" * 64,
    }
    (output / "prompt-attestation.json").write_text(
        json.dumps(attestation, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
elif mode == "missing-output":
    for candidate in output.iterdir():
        if candidate.name != "prompt-attestation.json":
            candidate.unlink()
            break
elif mode == "malformed-comparator" and stage == "history-compare":
    (output / "history-comparison.json").write_text(
        '{"status":"partial"}\n',
        encoding="utf-8",
    )
elif mode == "invalid-utf8":
    target = next(
        candidate
        for candidate in output.iterdir()
        if candidate.name != "prompt-attestation.json"
    )
    target.write_bytes(b"\xff")
elif mode == "oversized-output":
    target = next(
        candidate
        for candidate in output.iterdir()
        if candidate.name != "prompt-attestation.json"
    )
    target.write_bytes(b"x" * 131072)
elif mode == "symlink-output":
    target = next(
        candidate
        for candidate in output.iterdir()
        if candidate.name != "prompt-attestation.json"
    )
    target.unlink()
    target.symlink_to("/etc/passwd")
elif mode == "hardlink-output":
    target = next(
        candidate
        for candidate in output.iterdir()
        if candidate.name != "prompt-attestation.json"
    )
    raw = target.read_bytes()
    target.unlink()
    backing = pathlib.Path("tmp") / "hardlink-backing"
    backing.write_bytes(raw)
    os.link(backing, target)
elif mode == "fifo-output":
    target = next(
        candidate
        for candidate in output.iterdir()
        if candidate.name != "prompt-attestation.json"
    )
    target.unlink()
    os.mkfifo(target)
elif mode == "nonzero":
    raise SystemExit(79)
