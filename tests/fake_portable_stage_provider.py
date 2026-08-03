#!/usr/bin/env python3
"""Offline fixture for portable Hunt/AwR stage tests."""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time


def _prompt(arguments):
    for flag in ("-p", "--print"):
        if flag in arguments:
            return arguments[arguments.index(flag) + 1]
    return arguments[-1]


def _write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_bytes(raw)
    os.chmod(path, 0o600)


def _inner_request(request):
    prompt = request.get("serialized_prompt", request.get("prompt", "{}"))
    if isinstance(prompt, dict):
        return prompt
    try:
        value = json.loads(prompt)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _generation_markdown():
    directed = (pathlib.Path("input") / "direction_constraint.json").is_file()
    direction = ""
    story = "Constraint-Driven Sparse World Models"
    summary = "Gate latent updates with model confidence."
    experiment = (
        "Compare dense and gated updates on 128 held-out episodes using one "
        "H100; kill the idea if success drops by more than two points or "
        "latency improves by less than thirty percent."
    )
    if directed:
        story = "Repair-Aware 3D Memory Updates for Dynamic VLA Scenes"
        summary = "Repair persistent 3D memory after dynamic scene changes."
        experiment = (
            "Compare stale and repair-aware 3D memory on dynamic scene "
            "changes; kill the idea if repaired VLA decisions do not recover."
        )
        direction = (
            "Direction Axis: memory-representation-update\n"
            "Target Failure: dynamic-scene-change\n"
            "Direction Evidence: The repair arm attributes recovery to "
            "corrected 3D memory.\n"
        )
    return (
        "Assumption-Removal Attempt: complete I1\n\n"
        "## I1\n"
        f"One-Sentence Story: {story}\n"
        "Theme: World Models - Architecture\n"
        f"{direction}"
        "Form: remove-load-bearing-assumption\n"
        f"Summary: {summary}\n"
        f"Minimal Falsification Experiment: {experiment}\n"
        "Why It May Be Novel: Independent research must test occupation.\n"
        "Assumption to Remove: Dense latent updates are required.\n"
        "Why It Can Be Removed Now: Confidence is calibrated online.\n"
        "Forcing Constraint: Deployment latency limits dense updates.\n"
        "Crack Evidence: https://example.com/one | Stable skipped updates.\n"
        "Crack Evidence: https://example.com/two | Bounded latent drift.\n"
    )


def _comparison(inner):
    pack = inner.get("retrieval_payload", {})
    relations = []
    for lineage in pack.get("lineages", []):
        matches = lineage.get("matches", [])
        if not matches:
            continue
        match = matches[0]
        relations.append(
            {
                "relation": "distinct",
                "candidate_id": match["candidate_id"],
                "lineage_id": match["lineage_id"],
                "facet": match["facet"],
                "evidence_id": match["evidence_id"],
                "material_difference": "The supplied propositions differ.",
                "confidence": 0.8,
            }
        )
    return json.dumps(
        {
            "status": "complete_no_match",
            "comparator_version": "history-comparator-v1",
            "relations": relations,
            "expansion_request": None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _review(inner):
    candidate = inner.get("candidate", {})
    candidate_id = candidate.get("candidate_id", "I1")
    return (
        f"# {candidate_id}\n"
        "Verdict: strong-accept\n"
        "CRITICAL: 0\n"
        "MAJOR: 0\n"
        "Headline: The bounded candidate clears the review gates.\n"
        "Occupation: The supplied prior work leaves the claim open.\n"
        "Experiment: The bounded falsification is decisive.\n"
        "Estimand: The supplied estimand is aligned.\n"
        "Payoff: One attributable payoff remains possible.\n"
        "Feasibility: One researcher and one H100 suffice.\n"
        "History: unavailable\n"
        "Reason: The bounded fixture supports strong acceptance.\n"
    )


def _awr_draft():
    return (
        "## Revised Idea\n"
        "Confidence-gated latent updates skip redundant world-model inference "
        "while preserving closed-loop control.\n"
        "Minimal Falsification Experiment: Compare dense and confidence-gated "
        "updates on 128 held-out episodes using one H100; reject the claim if "
        "success drops by more than two points or latency improves by less than "
        "thirty percent.\n"
        "## Search Record\n"
        "- Neighbor One | https://example.com/awr-paper-one | Fixed-rate updates.\n"
        "- Neighbor Two | https://example.com/awr-paper-two | Observation gating.\n"
        "- Neighbor Three | https://example.com/awr-paper-three | Compression.\n"
        "## Response\n"
        "The revision isolates confidence gating and a dense baseline.\n"
        "AGY-DONE\n"
    )


def _awr_priorwork():
    return (
        "## Independent Prior Work\n"
        "Search Terms: confidence-gated latent updates; event-triggered world models\n"
        "- Query: https://api.semanticscholar.org/graph/v1/paper/search?query=confidence-gated-latent-updates\n"
        "Nearest Work:\n"
        "- Neighbor One | https://example.com/awr-paper-one | Fixed-rate updates.\n"
        "- Neighbor Two | https://example.com/awr-paper-two | Observation gating.\n"
        "- Neighbor Three | https://example.com/awr-paper-three | Compression.\n"
        "- Neighbor Four | https://example.com/awr-paper-four | Adaptive compute.\n"
        "- Neighbor Five | https://example.com/awr-paper-five | Event triggers.\n"
        "Strongest Counterexample: Neighbor Four omits closed-loop control.\n"
        "Overlap: low — The five works do not occupy the bounded claim.\n"
        "Papers Read: 5\n"
        "arXiv ID Check: yes\n"
        "## Crack Evidence Verification\n"
        "- https://example.com/crack-one | Verification: supports — Stable control.\n"
        "- https://example.com/crack-two | Verification: supports — Bounded drift.\n"
        "AGY-DONE\n"
    )


def _artifact(stage, inner):
    if stage == "generate":
        return "generation-ideas-markdown", _generation_markdown()
    if stage == "history-compare":
        return "history-comparison-json", _comparison(inner)
    if stage == "review":
        return "review-markdown", _review(inner)
    if stage == "awr-research":
        return "awr-draft-markdown", _awr_draft()
    if stage == "awr-priorwork":
        return "awr-priorwork-markdown", _awr_priorwork()
    if stage == "awr-judge":
        return "awr-judge-markdown", "Decision: SA-possible\nAGY-DONE\n"
    raise SystemExit(64)


def _record(request):
    path = os.environ.get("FAKE_PORTABLE_STAGE_LOG")
    if not path:
        return
    prompt = request.get("serialized_prompt", request.get("prompt", ""))
    record = {
        "provider": pathlib.Path(sys.argv[0]).name,
        "stage": request.get("stage"),
        "seat_id": request.get("seat_id"),
        "prompt_sha256": hashlib.sha256(
            str(prompt).encode("utf-8")
        ).hexdigest(),
    }
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--delayed-child":
        time.sleep(0.8)
        _write(pathlib.Path(sys.argv[2]), b"child survived\n")
        return 0

    request = json.loads(_prompt(sys.argv[1:]))
    _record(request)
    mode = os.environ.get("FAKE_PORTABLE_STAGE_MODE", "success")
    output = pathlib.Path("output/result.json")
    if mode == "mirror-audit":
        observed = {
            path.as_posix()
            for path in pathlib.Path(".").rglob("*")
            if path.is_symlink() or path.is_file()
        }
        declared = {
            "input/" + name for name in request.get("declared_inputs", [])
        }
        declared.add(request.get("role_path", "role.md"))
        if observed != declared:
            return 31
        forbidden_names = {".git", ".claude", "ledger.tsv"}
        if any(
            part in forbidden_names
            or part.endswith((".db", ".sqlite", ".sqlite3"))
            for path in pathlib.Path(".").rglob("*")
            for part in path.parts
        ):
            return 32
        for name in (
            "PWD",
            "OLDPWD",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "HISTORY_RUNTIME_ABI",
            "HISTORY_DB",
            "RESEARCH_DIRECTION_FILE",
            "AGENT_CMD",
            "FRONT_CMD",
            "BACK_CMD",
            "CONTAINED_AGENT_CMD_JSON",
            "SIDE_CMD",
        ):
            if name in os.environ:
                return 33
        if os.environ.get("HOME") != os.environ.get("EXPECTED_PROVIDER_HOME"):
            return 34
        if os.environ.get("CODEX_HOME") != os.environ.get(
            "EXPECTED_PROVIDER_CODEX_HOME"
        ):
            return 35
    if mode == "nonzero":
        return 23
    if mode == "timeout":
        delayed = os.environ["FAKE_PORTABLE_DELAYED_PATH"]
        subprocess.Popen(
            [sys.executable, __file__, "--delayed-child", delayed]
        )
        time.sleep(60)
        return 0
    if mode == "malformed":
        sys.stdout.buffer.write(b"{bad json\n")
        return 0
    if mode == "oversize":
        sys.stdout.buffer.write(b"x" * (256 * 1024))
        return 0

    kind, content = _artifact(request["stage"], _inner_request(request))
    raw = (
        json.dumps(
            {
                "schema_version": 1,
                "stage": request["stage"],
                "artifacts": [
                    {"artifact_kind": kind, "content": content}
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if mode == "boolean-schema-version":
        value = json.loads(raw)
        value["schema_version"] = True
        raw = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    if mode == "non-nfc-envelope":
        value = json.loads(raw)
        value["artifacts"][0]["content"] += "Cafe\u0301\n"
        raw = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    if mode == "duplicate-key":
        raw = raw.replace(
            b'{"artifacts":',
            b'{"schema_version":1,"artifacts":',
            1,
        )
    if mode == "extra-envelope":
        value = json.loads(raw)
        value["unexpected"] = True
        raw = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    if mode == "stdout-prefix":
        sys.stdout.buffer.write(b"provider progress\n" + raw)
        return 0
    if mode == "second-envelope":
        sys.stdout.buffer.write(raw + raw)
        return 0
    if mode == "extra":
        _write(pathlib.Path("output/extra.txt"), b"extra\n")
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "symlink":
        target = pathlib.Path("output/real.json")
        _write(target, raw)
        output.symlink_to("real.json")
        sys.stdout.buffer.write(raw)
        return 0
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
