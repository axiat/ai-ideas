#!/usr/bin/env python3
"""Canonical stage-prompt serialization and fail-closed context preflight."""

import hashlib
import json
import pathlib


class PreflightError(RuntimeError):
    def __init__(self, code, receipt):
        self.code = code
        self.receipt = receipt
        super().__init__(code)


def _canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _mounted_inputs(mounted_inputs):
    if mounted_inputs is None:
        raise ValueError("mounted inputs must be supplied")
    values = []
    for path, content in dict(mounted_inputs).items():
        relative = pathlib.PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or str(relative) in ("", "."):
            raise ValueError("mounted input path must be a nonempty relative path")
        if isinstance(content, str):
            raw = content.encode("utf-8")
        elif isinstance(content, bytes):
            raw = content
        else:
            raise TypeError("mounted input must be UTF-8 text bytes or text")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("mounted input is not UTF-8 text") from exc
        values.append(
            {
                "path": str(relative),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "text": text,
            }
        )
    paths = [item["path"] for item in values]
    if len(paths) != len(set(paths)):
        raise ValueError("mounted input paths must be unique")
    return sorted(values, key=lambda item: item["path"])


def serialize_stage_invocation(
    *,
    stage,
    adapter_version,
    fixed_instructions,
    mounted_inputs,
    candidate,
    retrieval_payload,
    receipts,
    tool_schemas,
    messages,
    output_schema_instructions=None,
):
    """Return the sole UTF-8 prompt argument passed to a stage adapter."""
    if not isinstance(stage, str) or not stage:
        raise ValueError("stage is required")
    if not isinstance(adapter_version, str) or not adapter_version:
        raise ValueError("adapter version is required")
    if not isinstance(fixed_instructions, str):
        raise TypeError("fixed instructions must be text")
    if not isinstance(messages, list):
        raise TypeError("messages must be an ordered list")
    payload = {
        "adapter": {"fixed_wrapper": "history-stage-prompt-v1", "version": adapter_version},
        "candidate": candidate,
        "fixed_instructions": fixed_instructions,
        "messages": messages,
        "mounted_inputs": _mounted_inputs(mounted_inputs),
        "output_schema_instructions": output_schema_instructions,
        "receipts": list(receipts),
        "retrieval_payload": retrieval_payload,
        "schema_version": 1,
        "stage": stage,
        "tool_schemas": list(tool_schemas),
    }
    return (_canonical_json(payload) + "\n").encode("utf-8")


def _raise(code, serialized, policy, **extra):
    receipt = {
        "code": code,
        "fits": False,
        "serialized_sha256": hashlib.sha256(serialized).hexdigest(),
        **extra,
    }
    raise PreflightError(code, receipt)


def _token_count(tokenizer, serialized):
    if hasattr(tokenizer, "count"):
        count = tokenizer.count(serialized)
    elif callable(tokenizer):
        count = tokenizer(serialized)
    else:
        raise TypeError("tokenizer must be callable or expose count(bytes)")
    if type(count) is not int or count < 0:
        raise ValueError("tokenizer must return a nonnegative integer")
    return count


_INVOCATION_FIELDS = {
    "adapter", "candidate", "fixed_instructions", "messages", "mounted_inputs",
    "output_schema_instructions", "receipts", "retrieval_payload", "schema_version",
    "stage", "tool_schemas",
}
_STAGE_REQUIREMENTS = {
    "generate": {
        "required_mounts": {
            "generation_brief.json",
            "generation_policy.md",
        },
        "optional_mounts": {"research_context.md"},
        "candidate": False,
        "retrieval_payload": False,
    },
    "history-compare": {
        "required_mounts": {"retrieval_pack.json"},
        "optional_mounts": set(),
        "candidate": True,
        "retrieval_payload": True,
    },
    "review": {
        "required_mounts": {
            "candidate.json",
            "prior_work.md",
            "review_contract.md",
        },
        "optional_mounts": {"history_summary.json"},
        "candidate": True,
        "retrieval_payload": False,
    },
    "meta": {
        "required_mounts": {"failure_batch.json"},
        "optional_mounts": set(),
        "candidate": False,
        "retrieval_payload": False,
    },
}


def _expected_mount_hashes(expected_mounted_inputs):
    if not isinstance(expected_mounted_inputs, dict):
        raise TypeError("independent mount expectations must be a mapping")
    result = {}
    for path, value in expected_mounted_inputs.items():
        normalized = _mounted_inputs({path: value})[0]
        result[normalized["path"]] = normalized["sha256"]
    return result


def _validate_closed_invocation(invocation, expected_mounted_inputs):
    if set(invocation) != _INVOCATION_FIELDS or invocation.get("schema_version") != 1:
        raise ValueError("closed invocation schema mismatch")
    adapter = invocation.get("adapter")
    if not isinstance(adapter, dict) or set(adapter) != {"fixed_wrapper", "version"}:
        raise ValueError("adapter schema mismatch")
    stage = invocation.get("stage")
    requirements = _STAGE_REQUIREMENTS.get(stage)
    if requirements is None:
        raise ValueError("unsupported stage")
    if not isinstance(invocation["fixed_instructions"], str) or not invocation["fixed_instructions"]:
        raise ValueError("fixed instructions are required")
    if not isinstance(invocation["messages"], list) or not invocation["messages"]:
        raise ValueError("ordered messages are required")
    if not isinstance(invocation["receipts"], list) or not isinstance(invocation["tool_schemas"], list):
        raise ValueError("receipt and tool schema lists are required")
    if (invocation["candidate"] is not None) != requirements["candidate"]:
        raise ValueError("candidate does not match stage")
    if (invocation["retrieval_payload"] is not None) != requirements["retrieval_payload"]:
        raise ValueError("retrieval payload does not match stage")
    expected = _expected_mount_hashes(expected_mounted_inputs)
    actual = {}
    for mounted in invocation["mounted_inputs"]:
        if set(mounted) != {"path", "sha256", "text"} or not isinstance(mounted["text"], str):
            raise ValueError("mounted input schema mismatch")
        if mounted["path"] in actual:
            raise ValueError("duplicate mounted input path")
        actual[mounted["path"]] = mounted["sha256"]
        if hashlib.sha256(mounted["text"].encode("utf-8")).hexdigest() != mounted["sha256"]:
            raise ValueError("mounted input hash mismatch")
    required_mounts = requirements["required_mounts"]
    allowed_mounts = required_mounts | requirements["optional_mounts"]
    if (
        actual != expected
        or not required_mounts.issubset(actual)
        or not set(actual).issubset(allowed_mounts)
    ):
        raise ValueError("mounted input expectation mismatch")


def _validate_tokenizer(tokenizer, policy):
    if tokenizer is None:
        return
    identity = getattr(tokenizer, "identity", None)
    revision = getattr(tokenizer, "revision", None)
    if (
        identity != policy.get("tokenizer_identity")
        or revision != policy.get("tokenizer_revision")
    ):
        raise ValueError("tokenizer is not policy-bound")


def _declared_input_sha256s(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("sha256") and isinstance(item, str) and len(item) == 64:
                result.add(item)
            result.update(_declared_input_sha256s(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_declared_input_sha256s(item))
    return result


def preflight_stage_invocation(
    serialized, policy, tokenizer=None, expected_mounted_inputs=None
):
    """Return a receipt or raise before a backend process can launch."""
    if not isinstance(serialized, bytes):
        raise TypeError("serialized invocation must be bytes")
    policy = dict(policy)
    adapter_version = policy.get("adapter_version")
    allowances = policy.get("tested_adapter_allowances")
    allowance = policy.get("adapter_wrapper_allowance")
    if not isinstance(allowances, dict) or allowances.get(adapter_version) != allowance:
        _raise("unverified_adapter_allowance", serialized, policy)
    try:
        invocation = json.loads(serialized.decode("utf-8"))
        _validate_closed_invocation(invocation, expected_mounted_inputs)
        serialized_adapter = invocation["adapter"]["version"]
        if invocation["adapter"]["fixed_wrapper"] != "history-stage-prompt-v1":
            raise ValueError("unexpected wrapper")
        if serialized_adapter != adapter_version:
            _raise("adapter_policy_mismatch", serialized, policy)
    except PreflightError:
        raise
    except (KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        _raise("serialization_invalid", serialized, policy)
    required = ("model_context_limit", "max_output_tokens", "safety_margin")
    if any(type(policy.get(key)) is not int or policy[key] < 0 for key in required):
        _raise("invalid_budget_policy", serialized, policy)
    try:
        _validate_tokenizer(tokenizer, policy)
    except (TypeError, ValueError):
        _raise("unverified_tokenizer", serialized, policy)
    if tokenizer is None:
        input_upper_bound = len(serialized) + allowance
        count_method = "utf8_byte_upper_bound"
    else:
        input_upper_bound = _token_count(tokenizer, serialized)
        count_method = "exact_tokenizer"
    total = input_upper_bound + policy["max_output_tokens"] + policy["safety_margin"]
    receipt = {
        "adapter_version": adapter_version,
        "code": "ok" if total <= policy["model_context_limit"] else "budget_exceeded",
        "count_method": count_method,
        "fits": total <= policy["model_context_limit"],
        "input_upper_bound": input_upper_bound,
        "model_context_limit": policy["model_context_limit"],
        "output_tokens": policy["max_output_tokens"],
        "safety_margin": policy["safety_margin"],
        "serialized_byte_count": len(serialized),
        "serialized_sha256": hashlib.sha256(serialized).hexdigest(),
        "input_sha256s": sorted(_declared_input_sha256s(invocation)),
        "total_upper_bound": total,
    }
    if not receipt["fits"]:
        raise PreflightError("budget_exceeded", receipt)
    return receipt


def preflight_canonical_request(prompt, request, policy):
    """Budget the exact provider request authored by the canonicalizer."""
    if not isinstance(prompt, bytes) or not isinstance(request, bytes):
        raise TypeError("prompt and canonical request must be bytes")
    try:
        prompt_text = prompt.decode("utf-8")
        value = json.loads(request.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PreflightError(
            "canonical_request_invalid",
            {
                "code": "canonical_request_invalid",
                "fits": False,
                "canonical_request_sha256": hashlib.sha256(
                    request
                ).hexdigest(),
            },
        ) from exc
    required = (
        "model_context_limit",
        "max_output_tokens",
        "safety_margin",
    )
    text = value.get("text") if isinstance(value, dict) else None
    output_format = (
        text.get("format") if isinstance(text, dict) else None
    )
    reasoning = (
        value.get("reasoning") if isinstance(value, dict) else None
    )
    if (
        not isinstance(value, dict)
        or request != _canonical_json(value).encode("utf-8")
        or set(value)
        != {
            "include",
            "input",
            "instructions",
            "max_output_tokens",
            "model",
            "parallel_tool_calls",
            "reasoning",
            "store",
            "stream",
            "text",
            "tool_choice",
            "tools",
            "truncation",
        }
        or value.get("include") != []
        or value.get("input")
        != [
            {
                "content": [
                    {"text": prompt_text, "type": "input_text"}
                ],
                "role": "user",
                "type": "message",
            }
        ]
        or value.get("instructions") != ""
        or not isinstance(value.get("model"), str)
        or not value["model"]
        or len(value["model"].encode("utf-8")) > 256
        or value.get("parallel_tool_calls") is not False
        or not isinstance(reasoning, dict)
        or set(reasoning) != {"effort", "summary"}
        or reasoning.get("effort")
        not in {"low", "medium", "high", "xhigh"}
        or reasoning.get("summary") != "auto"
        or value.get("store") is not False
        or value.get("tools") != []
        or value.get("tool_choice") != "none"
        or value.get("truncation") != "disabled"
        or value.get("stream") is not True
        or not isinstance(text, dict)
        or set(text) != {"format", "verbosity"}
        or text.get("verbosity") != "low"
        or not isinstance(output_format, dict)
        or set(output_format)
        != {"name", "schema", "strict", "type"}
        or output_format.get("name") != "bounded_stage_output_v1"
        or not isinstance(output_format.get("schema"), dict)
        or output_format.get("strict") is not True
        or output_format.get("type") != "json_schema"
        or any(
            type(policy.get(key)) is not int or policy[key] < 0
            for key in required
        )
        or value.get("max_output_tokens")
        != policy.get("max_output_tokens")
    ):
        raise PreflightError(
            "canonical_request_invalid",
            {
                "code": "canonical_request_invalid",
                "fits": False,
                "canonical_request_sha256": hashlib.sha256(
                    request
                ).hexdigest(),
            },
        )
    envelope = json.loads(request.decode("utf-8"))
    envelope["input"][0]["content"][0]["text"] = ""
    envelope_bytes = (
        _canonical_json(envelope).encode("utf-8")
    )
    input_upper_bound = len(request)
    total = (
        input_upper_bound
        + policy["max_output_tokens"]
        + policy["safety_margin"]
    )
    receipt = {
        "code": (
            "ok"
            if total <= policy["model_context_limit"]
            else "budget_exceeded"
        ),
        "fits": total <= policy["model_context_limit"],
        "count_method": "canonical_request_utf8_byte_upper_bound",
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "canonical_request_sha256": hashlib.sha256(request).hexdigest(),
        "canonical_request_bytes": len(request),
        "fixed_envelope_bytes": len(envelope_bytes),
        "input_upper_bound": input_upper_bound,
        "model_context_limit": policy["model_context_limit"],
        "output_tokens": policy["max_output_tokens"],
        "safety_margin": policy["safety_margin"],
        "total_upper_bound": total,
    }
    if not receipt["fits"]:
        raise PreflightError("budget_exceeded", receipt)
    return receipt
