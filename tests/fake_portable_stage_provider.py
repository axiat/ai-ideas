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
        "role_text, declared_input_texts, host_output_contract, "
        "response_schema, and these transport instructions are the "
        "authoritative stage contract. They override any conflicting "
        "output-location or file-writing wording inside role_text."
    ),
    "role": (
        "Follow role_text and host_output_contract exactly when "
        "building artifact content. Disk paths role.md and input/* "
        "are byte-identical audit copies; do not require file tools."
    ),
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

CODEX_EXPECTED_TRANSPORT_INSTRUCTIONS = {
    **EXPECTED_TRANSPORT_INSTRUCTIONS,
    "stdout": (
        "Return exactly one JSON object matching response_schema as the "
        "structured final result. The Codex CLI writes that final result "
        "through its host-configured output-last-message path; stdout is "
        "diagnostic transport and is never an output fallback. The harness "
        "parses and canonicalizes the final JSON. Do not put Markdown fences "
        "or narration inside the structured value."
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

AGY_EXPECTED_TRANSPORT_INSTRUCTIONS = {
    **EXPECTED_TRANSPORT_INSTRUCTIONS,
    "stdout": (
        "Return exactly one JSON object matching response_schema as the "
        "structured final result. The Agy CLI owns the outer stdout JSON; "
        "only a status=SUCCESS structured_output member is eligible for "
        "import. Do not put Markdown fences or narration inside the "
        "structured value."
    ),
}

CLAUDE_EXPECTED_TRANSPORT_INSTRUCTIONS = {
    **EXPECTED_TRANSPORT_INSTRUCTIONS,
    "stdout": (
        "Return exactly one JSON object matching response_schema as the "
        "structured final result. The Claude CLI owns the outer stdout JSON; "
        "only subtype=success structured_output is eligible for import. "
        "Do not put Markdown fences or narration inside the structured value."
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
                else (
                    AGY_EXPECTED_TRANSPORT_INSTRUCTIONS
                    if _agy_json_requested(sys.argv[1:])
                    else (
                        CLAUDE_EXPECTED_TRANSPORT_INSTRUCTIONS
                        if _claude_json_requested(sys.argv[1:])
                        else (
                            CODEX_EXPECTED_TRANSPORT_INSTRUCTIONS
                            if "--output-schema" in sys.argv[1:]
                            else EXPECTED_TRANSPORT_INSTRUCTIONS
                        )
                    )
                )
            )
        ),
        "structured_transport_valid": (
            (
                _agy_json_requested(sys.argv[1:])
                or _claude_json_requested(sys.argv[1:])
            )
            and sys.argv[1:].count("--json-schema") == 1
            and json.loads(
                _flag_value(sys.argv[1:], "--json-schema")
            )
            == request.get("response_schema")
        ) if pathlib.Path(sys.argv[0]).name in {"agy", "claude"} else None,
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


def _flag_value(arguments, flag):
    index = arguments.index(flag)
    return arguments[index + 1]


def _grok_json_requested(arguments):
    return pathlib.Path(sys.argv[0]).name == "grok" and (
        _flag_value(arguments, "--output-format") == "json"
    )


def _agy_json_requested(arguments):
    return pathlib.Path(sys.argv[0]).name == "agy" and (
        _flag_value(arguments, "--output-format") == "json"
    )


def _claude_json_requested(arguments):
    return pathlib.Path(sys.argv[0]).name == "claude" and (
        _flag_value(arguments, "--output-format") == "json"
    )


def _claude_transport(inner_raw, arguments, mode):
    del arguments  # schema is host-bound; Claude does not echo it
    if mode == "malformed-outer-json":
        return b'{"subtype":'
    if mode == "invalid-outer-utf8":
        return b'{"subtype":"success","result":"\xff"}'
    if mode == "outer-array":
        return b"[]"
    if mode == "duplicate-outer-subtype":
        return (
            b'{"subtype":"success","subtype":"success",'
            b'"is_error":false,"structured_output":'
            + inner_raw.rstrip(b"\n")
            + b"}"
        )

    subtype = None if mode == "claude-missing-subtype" else (
        "error" if mode == "claude-error-subtype" else "success"
    )
    is_error = True if mode == "claude-is-error" else False
    if mode == "claude-missing-is-error":
        is_error_member = None
    else:
        is_error_member = is_error

    if mode == "claude-missing-payload":
        payload = None
    elif mode == "claude-null-payload":
        payload = b"null"
    elif mode == "claude-array-payload":
        payload = b"[]"
    elif mode == "claude-text-only":
        payload = None
    else:
        payload = inner_raw.rstrip(b"\n")

    members = [
        b'"type":"result"',
        b'"result":"{\\"ignored\\":true}"',
        b'"stop_reason":"end_turn"',
        b'"session_id":"fixture-session"',
        b'"total_cost_usd":0.001',
        b'"usage":{"input_tokens":10,"output_tokens":5}',
    ]
    if subtype is not None:
        members.append(b'"subtype":' + json.dumps(subtype).encode("utf-8"))
    if is_error_member is not None:
        members.append(
            b'"is_error":' + json.dumps(is_error_member).encode("utf-8")
        )
    if payload is not None:
        members.append(b'"structured_output":' + payload)
    return b"{" + b",".join(members) + b"}\n"


def _agy_transport(inner_raw, arguments, mode):
    schema_raw = _flag_value(arguments, "--json-schema").encode("utf-8")
    if mode == "malformed-outer-json":
        return b'{"status":'
    if mode == "invalid-outer-utf8":
        return b'{"status":"SUCCESS","response":"\xff"}'
    if mode == "outer-array":
        return b"[]"
    if mode == "duplicate-outer-status":
        return (
            b'{"status":"SUCCESS","status":"SUCCESS",'
            b'"json_schema":' + schema_raw
            + b',"structured_output":' + inner_raw.rstrip(b"\n") + b"}"
        )

    status = None if mode == "agy-missing-status" else (
        "FAILURE" if mode == "agy-failure-status" else "SUCCESS"
    )
    if mode == "agy-missing-schema":
        schema_member = None
    elif mode == "agy-mutated-schema":
        changed = json.loads(schema_raw.decode("utf-8"))
        changed["additionalProperties"] = True
        schema_member = json.dumps(
            changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    elif mode == "agy-schema-int-float":
        schema_member = schema_raw.replace(
            b'"maximum":1', b'"maximum":1.0', 1
        )
    elif mode == "agy-schema-int-bool":
        schema_member = schema_raw.replace(
            b'"minimum":1', b'"minimum":true', 1
        )
    else:
        schema_member = schema_raw

    if mode == "agy-missing-payload":
        payload = None
    elif mode == "agy-null-payload":
        payload = b"null"
    elif mode == "agy-array-payload":
        payload = b"[]"
    else:
        payload = inner_raw.rstrip(b"\n")

    members = [
        b'"duration_ms":1.25',
        b'"response":"provider narration ```json noisy-response ```"',
        b'"usage":{"input_tokens":10.0,"output_tokens":5}',
    ]
    if schema_member is not None:
        members.append(b'"json_schema":' + schema_member)
    if status is not None:
        members.append(b'"status":' + json.dumps(status).encode("utf-8"))
    if payload is not None:
        members.append(b'"structured_output":' + payload)
    return b"{" + b",".join(members) + b"}\n"


def _provider_transport(inner_raw, arguments, mode):
    if _agy_json_requested(arguments):
        return _agy_transport(inner_raw, arguments, mode)
    if _claude_json_requested(arguments):
        return _claude_transport(inner_raw, arguments, mode)
    if _grok_json_requested(arguments):
        return _grok_transport(inner_raw, mode)
    return inner_raw


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
    if len(sys.argv) == 6 and sys.argv[1] == "--wait-mutate":
        action = sys.argv[2]
        trigger = pathlib.Path(sys.argv[3])
        target = pathlib.Path(sys.argv[4])
        done = pathlib.Path(sys.argv[5])
        deadline = time.monotonic() + 2.0
        while not trigger.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not trigger.exists():
            return 0
        if action == "grow":
            target.write_bytes(b"x" * (1024 * 1024 + 1))
        elif action == "add":
            _write(target, b"late provider file\n")
        else:
            return 64
        _write(done, b"mutated\n")
        return 0

    if sys.argv[1:] == ["--version"]:
        sys.stdout.write("1.1.10\n")
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
    mode = os.environ.get("FAKE_PORTABLE_STAGE_MODE", "success")
    grok_json = _grok_json_requested(arguments)
    agy_json = _agy_json_requested(arguments)
    claude_json = _claude_json_requested(arguments)
    if mode == "agy-reject-schema-before-prompt" and agy_json:
        marker = os.environ.get("FAKE_PORTABLE_PREPROMPT_MARKER")
        if marker:
            _write(pathlib.Path(marker), b"rejected-before-prompt\n")
        return 67
    if mode == "agy-reject-numeric-enum" and agy_json:
        try:
            schema_raw = _flag_value(
                arguments, "--json-schema"
            ).encode("utf-8")
            version_schema = json.loads(schema_raw.decode("utf-8"))[
                "properties"
            ]["schema_version"]
        except (KeyError, TypeError, ValueError, IndexError):
            return 65
        numeric_enum = version_schema.get("enum", [])
        if any(type(value) in {int, float} for value in numeric_enum):
            return 66
        marker = os.environ.get("FAKE_PORTABLE_PREPROMPT_MARKER")
        if marker:
            _write(pathlib.Path(marker), schema_raw)
    request = json.loads(_prompt(arguments))
    _record(request)
    output = pathlib.Path("output/result.json")
    if mode == "grok-compatibility-audit":
        for name in (
            "GROK_CLAUDE_SKILLS_ENABLED",
            "GROK_CLAUDE_RULES_ENABLED",
            "GROK_CLAUDE_MCPS_ENABLED",
            "GROK_CLAUDE_HOOKS_ENABLED",
            "GROK_CLAUDE_SESSIONS_ENABLED",
        ):
            if os.environ.get(name) != "false":
                return 40
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
        if "--output-schema" in arguments:
            declared.add(
                pathlib.Path(
                    _flag_value(arguments, "--output-schema")
                ).resolve().relative_to(pathlib.Path.cwd().resolve()).as_posix()
            )
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
            != AGY_EXPECTED_TRANSPORT_INSTRUCTIONS
        ):
            return 38
        if not agy_json:
            return 39
        if arguments.count("--json-schema") != 1:
            return 40
        try:
            observed_schema = json.loads(
                _flag_value(arguments, "--json-schema")
            )
        except (ValueError, IndexError):
            return 41
        if observed_schema != request.get("response_schema"):
            return 42
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
        sys.stdout.buffer.write(_provider_transport(raw, arguments, mode))
        return 0
    if mode == "oversize":
        raw = b"x" * (256 * 1024)
        sys.stdout.buffer.write(_provider_transport(raw, arguments, mode))
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
    if mode == "surrogate-inner-value":
        raw = raw.replace(
            b'"content":"',
            b'"content":"\\ud800',
            1,
        )
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
    if agy_json:
        raw = _agy_transport(raw, arguments, mode)
    if claude_json:
        raw = _claude_transport(raw, arguments, mode)
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
    if mode == "agy-hidden-extra-mode-zero":
        hidden = pathlib.Path("hidden")
        _write(hidden / "undeclared.txt", b"hidden provider file\n")
        os.chmod(hidden, 0)
        sys.stdout.buffer.write(raw)
        return 0
    if mode in {
        "agy-input-symlink",
        "agy-input-hardlink",
        "agy-input-special",
    }:
        declared = pathlib.Path("input") / request["declared_inputs"][0]
        declared.unlink()
        if mode == "agy-input-symlink":
            declared.symlink_to(os.environ["FAKE_PORTABLE_LINK_TARGET"])
        elif mode == "agy-input-hardlink":
            os.link(os.environ["FAKE_PORTABLE_LINK_TARGET"], declared)
        else:
            os.mkfifo(declared)
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-schema":
        schema_bytes = 453 * 1024
        _write(
            pathlib.Path(
                ".tmp/agy/language-server/"
                "3unleash-repo-schema-v1-codeium-language-server.json"
            ),
            b"{" + b" " * (schema_bytes - 2) + b"}",
        )
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-exact-limits":
        for index in range(32):
            _write(
                pathlib.Path(".tmp") / f"directory-{index:02d}" / "cache",
                b"x" * 32768,
            )
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-aggregate-oversize":
        _write(pathlib.Path(".tmp/cache-a"), b"x" * (600 * 1024))
        _write(
            pathlib.Path(".tmp/cache-b"),
            b"y" * (424 * 1024 + 1),
        )
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-root-symlink":
        pathlib.Path(".tmp").rmdir()
        pathlib.Path(".tmp").symlink_to("input", target_is_directory=True)
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-missing":
        pathlib.Path(".tmp").rmdir()
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-file-symlink":
        _write(pathlib.Path(".tmp/target"), b"target\n")
        pathlib.Path(".tmp/link").symlink_to("target")
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-hardlink":
        original = pathlib.Path(".tmp/original")
        _write(original, b"linked\n")
        os.link(original, pathlib.Path(".tmp/alias"))
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-special":
        os.mkfifo(pathlib.Path(".tmp/pipe"))
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-oversize":
        _write(pathlib.Path(".tmp/oversize"), b"x" * (1024 * 1024 + 1))
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-too-many":
        for index in range(33):
            _write(pathlib.Path(".tmp") / f"entry-{index:02d}", b"")
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-too-many-directories":
        for index in range(65):
            pathlib.Path(".tmp", f"directory-{index:02d}").mkdir()
        sys.stdout.buffer.write(raw)
        return 0
    if mode == "agy-tmp-mode-zero":
        locked = pathlib.Path(".tmp/locked")
        _write(locked / "cache", b"locked\n")
        os.chmod(locked, 0)
        sys.stdout.buffer.write(raw)
        return 0
    if mode in {"agy-tmp-late-grow", "agy-late-root-file"}:
        action = "grow" if mode == "agy-tmp-late-grow" else "add"
        target = (
            pathlib.Path(".tmp/cache")
            if action == "grow"
            else pathlib.Path("late-provider-file")
        )
        if action == "grow":
            _write(target, b"x")
        subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--wait-mutate",
                action,
                os.environ["FAKE_PORTABLE_TRIGGER_PATH"],
                str(target.resolve()),
                os.environ["FAKE_PORTABLE_DONE_PATH"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
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
    if "--output-last-message" in arguments:
        if mode != "missing-final-output":
            _write(
                pathlib.Path(
                    _flag_value(arguments, "--output-last-message")
                ),
                raw,
            )
        sys.stdout.buffer.write(b"codex transport diagnostics\n")
        return 0
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
