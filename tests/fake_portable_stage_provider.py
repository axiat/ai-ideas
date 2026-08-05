#!/usr/bin/env python3
"""Offline fixture for portable Hunt/AwR stage tests."""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time


EXPECTED_TRANSPORT_INSTRUCTIONS = {
    "schema_version": "portable-stage-transport-instructions-v1",
    "precedence": (
        "These transport instructions override any output-location or "
        "file-writing instruction in role.md."
    ),
    "role": "Treat role.md as artifact-content instructions only.",
    "mirror": "Do not create, modify, or delete any file in the mirror.",
    "stdout": (
        "Emit exactly one UTF-8 NFC canonical JSON object to stdout, with "
        "lexicographically sorted object keys, compact separators, and "
        "exactly one trailing LF. The object must match response_schema. "
        "Do not emit Markdown fences, narration, or any other bytes."
    ),
    "request_attestation": (
        "Copy request_binding.provider_request_binding_sha256 and "
        "request_binding.serialized_prompt_sha256 exactly into "
        "request_attestation."
    ),
}

GROK_EXPECTED_TRANSPORT_INSTRUCTIONS = {
    **EXPECTED_TRANSPORT_INSTRUCTIONS,
    "stdout": (
        "Make the FINAL ASSISTANT RESPONSE exactly one UTF-8 NFC canonical "
        "JSON object inside exactly one Markdown fence. The opening fence "
        "must be the exact lowercase bytes ```json followed by LF. The JSON "
        "object must use lexicographically sorted object keys, compact "
        "separators, and exactly one trailing LF; that LF must be followed "
        "immediately by the terminal closing bytes ```. The object must "
        "match response_schema. Do not emit any bytes before the opening "
        "fence or after the closing fence in the FINAL ASSISTANT RESPONSE, "
        "and do not emit triple-backtick bytes in any earlier assistant "
        "response."
    ),
}


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


def _overwrite_same_size(path):
    raw = bytearray(path.read_bytes())
    if not raw:
        raise RuntimeError("fixture requires a nonempty declared input")
    raw[0] ^= 1
    path.write_bytes(raw)


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
        "legacy_file_wording_seen": (
            "write only the requested output file"
            in pathlib.Path(request.get("role_path", "role.md"))
            .read_text(encoding="utf-8")
            .lower()
        ),
        "transport_instructions_valid": (
            request.get("transport_instructions")
            == (
                GROK_EXPECTED_TRANSPORT_INSTRUCTIONS
                if _grok_json_requested(sys.argv[1:])
                else EXPECTED_TRANSPORT_INSTRUCTIONS
            )
        ),
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


def _grok_json_requested(arguments):
    return (
        "--output-format" in arguments
        and arguments[arguments.index("--output-format") + 1] == "json"
    )


def _grok_transport(inner_raw, mode):
    if mode == "malformed-outer-json":
        return b'{"text":'
    if mode == "invalid-outer-utf8":
        return b'{"text":"\xff","stopReason":"end_turn"}'
    if mode == "outer-array":
        return b"[]"
    if mode == "surrogate-text":
        return b'{"text":"\\ud800","stopReason":"end_turn"}'
    if mode == "duplicate-key":
        inner_text = inner_raw.decode("utf-8")
    else:
        try:
            inner_value = json.loads(inner_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            inner_text = inner_raw.decode("utf-8")
        else:
            reordered = {}
            for key in (
                "stage",
                "artifacts",
                "schema_version",
                "request_attestation",
            ):
                if key in inner_value:
                    reordered[key] = inner_value[key]
            inner_text = json.dumps(reordered, ensure_ascii=False, indent=2)
    fenced = "```json\n" + inner_text + "\n```"
    if mode == "narrated-terminal-bare":
        inner_text = (
            "Provider completed the request.\n"
            + inner_raw.decode("utf-8")
        )
    elif mode == "narrated-terminal-fence":
        inner_text = "Provider completed the request.\n" + fenced
    elif mode == "fence-duplicate-delimiter":
        inner_text = fenced + "\n" + fenced
    elif mode == "fence-four-backtick-prefix":
        inner_text = "`" + fenced
    elif mode == "fence-five-backtick-prefix":
        inner_text = "``" + fenced
    elif mode == "fence-non-line-start":
        inner_text = "Provider completed the request." + fenced
    elif mode == "fence-crlf":
        inner_text = "```json\r\n" + inner_text + "\r\n```"
    elif mode == "fence-cr-before-close":
        inner_text = "```json\n" + inner_text + "\r\n```"
    elif mode == "fence-prefix-inline-delimiter":
        inner_text = "Provider mentioned ``` in this line.\n" + fenced
    elif mode == "fence-prefix-indented-delimiter":
        inner_text = "Provider note:\n  ```\n" + fenced
    elif mode == "fence-prefix-inline-wrong-language":
        inner_text = "Provider mentioned ```javascript\n" + fenced
    elif mode == "fence-wrong-language":
        inner_text = "```javascript\n" + inner_text + "\n```"
    elif mode == "fence-wrong-case":
        inner_text = "```JSON\n" + inner_text + "\n```"
    elif mode == "fence-missing-close":
        inner_text = "```json\n" + inner_text
    elif mode == "fence-trailing-newline":
        inner_text = fenced + "\n"
    elif mode == "fence-trailing-space":
        inner_text = fenced + " "
    elif mode == "fence-trailing-text":
        inner_text = fenced + "provider progress"
    elif mode == "fence-trailing-nul":
        inner_text = fenced + "\x00"
    elif mode != "bare-inner-success":
        inner_text = fenced
    outer = {
        "text": inner_text,
        "stopReason": "max_tokens" if mode == "max-tokens" else "end_turn",
        "sessionId": "fixture-session",
        "requestId": "fixture-request",
        "num_turns": 1,
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 5,
            "reasoning_tokens": 2,
            "total_tokens": 15,
        },
        "modelUsage": {},
        "total_cost_usd": 0.001,
        "total_cost_usd_ticks": 10000000,
    }
    if mode == "missing-text":
        outer.pop("text")
    if mode == "nonstring-text":
        outer["text"] = 1
    if mode == "duplicate-outer-text":
        encoded = [
            '"text":' + json.dumps(inner_text, ensure_ascii=False),
            '"text":' + json.dumps(inner_text, ensure_ascii=False),
        ]
        encoded.extend(
            json.dumps(key, ensure_ascii=False)
            + ":"
            + json.dumps(value, ensure_ascii=False)
            for key, value in outer.items()
            if key != "text"
        )
        return ("{" + ",".join(encoded) + "}").encode("utf-8")
    return json.dumps(outer, ensure_ascii=False, indent=2).encode("utf-8")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--delayed-child":
        time.sleep(0.8)
        _write(pathlib.Path(sys.argv[2]), b"child survived\n")
        return 0

    if sys.argv[1:] == ["--pure", "debug", "config"]:
        sys.stdout.write('{"model":"openai/fixture-model"}\n')
        return 0

    if sys.argv[1:] == ["models", "--pure"]:
        sys.stdout.write("openai/fixture-model\n")
        return 0

    if sys.argv[1:] == ["models"]:
        sys.stdout.write("gemini/fixture-model\n")
        return 0

    arguments = sys.argv[1:]
    request = json.loads(_prompt(arguments))
    _record(request)
    mode = os.environ.get("FAKE_PORTABLE_STAGE_MODE", "success")
    grok_json = _grok_json_requested(arguments)
    output = pathlib.Path("output/result.json")
    if mode in {"mirror-audit", "agy-portable-audit"}:
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
    if mode == "agy-portable-audit":
        if pathlib.Path(sys.argv[0]).name != "agy":
            return 36
        role_text = pathlib.Path(
            request.get("role_path", "role.md")
        ).read_text(encoding="utf-8")
        if "write only the requested output file" not in role_text.lower():
            return 37
        if (
            request.get("transport_instructions")
            != EXPECTED_TRANSPORT_INSTRUCTIONS
        ):
            return 38
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
        raw = b"{bad json\n"
        if grok_json:
            sys.stdout.buffer.write(_grok_transport(raw, mode))
            return 0
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "oversize":
        raw = b"x" * (256 * 1024)
        if grok_json:
            sys.stdout.buffer.write(_grok_transport(raw, mode))
            return 0
        sys.stdout.buffer.write(raw)
        return 0

    kind, content = _artifact(request["stage"], _inner_request(request))
    binding = request.get("request_binding", {})
    raw = (
        json.dumps(
            {
                "schema_version": 1,
                "stage": request["stage"],
                "request_attestation": {
                    "schema_version": (
                        "portable-stage-response-attestation-v1"
                    ),
                    "provider_request_binding_sha256": binding.get(
                        "provider_request_binding_sha256", "0" * 64
                    ),
                    "serialized_prompt_sha256": binding.get(
                        "serialized_prompt_sha256", "0" * 64
                    ),
                },
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
    if mode == "missing-request-attestation":
        value = json.loads(raw)
        value.pop("request_attestation")
        raw = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    if mode in {"wrong-request-attestation", "wrong-prompt-attestation"}:
        value = json.loads(raw)
        field = (
            "provider_request_binding_sha256"
            if mode == "wrong-request-attestation"
            else "serialized_prompt_sha256"
        )
        value["request_attestation"][field] = "f" * 64
        raw = (
            json.dumps(
                value,
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
    if mode == "float-inner-value":
        value = json.loads(raw)
        value["schema_version"] = 1.0
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
    if mode == "noncanonical-raw":
        raw = json.dumps(
            json.loads(raw),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
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
    if mode == "agy-mirror-write":
        _write(pathlib.Path("provider-created.md"), b"provider artifact\n")
        sys.stdout.buffer.write(raw)
        return 0
    if mode in {
        "overwrite-role",
        "overwrite-declared-input",
        "chmod-declared-input",
    }:
        declared_input = pathlib.Path("input") / request["declared_inputs"][0]
        if mode == "overwrite-role":
            _overwrite_same_size(
                pathlib.Path(request.get("role_path", "role.md"))
            )
        elif mode == "overwrite-declared-input":
            _overwrite_same_size(declared_input)
        else:
            os.chmod(declared_input, 0o400)
        sys.stdout.buffer.write(raw)
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
    if grok_json:
        raw = _grok_transport(raw, mode)
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
