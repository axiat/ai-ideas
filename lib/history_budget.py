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
        return []
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


def preflight_stage_invocation(serialized, policy, tokenizer=None):
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
        serialized_adapter = invocation["adapter"]["version"]
        if invocation["adapter"]["fixed_wrapper"] != "history-stage-prompt-v1":
            raise ValueError("unexpected wrapper")
        if serialized_adapter != adapter_version:
            _raise("adapter_policy_mismatch", serialized, policy)
        for mounted in invocation["mounted_inputs"]:
            text = mounted["text"].encode("utf-8")
            if hashlib.sha256(text).hexdigest() != mounted["sha256"]:
                _raise("mounted_input_hash_mismatch", serialized, policy)
    except PreflightError:
        raise
    except (KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        _raise("serialization_invalid", serialized, policy)
    required = ("model_context_limit", "max_output_tokens", "safety_margin")
    if any(type(policy.get(key)) is not int or policy[key] < 0 for key in required):
        _raise("invalid_budget_policy", serialized, policy)
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
        "total_upper_bound": total,
    }
    if not receipt["fits"]:
        raise PreflightError("budget_exceeded", receipt)
    return receipt
