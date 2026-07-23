#!/usr/bin/env python3
"""Pinned adapter between a preflighted prompt and one registered backend."""

import hashlib
import json
import os
import pathlib
import stat
import sys


_MODEL_ARTIFACTS = {
    "generate": (
        ("generation-ideas-markdown", "output/ideas.md", 65536),
        ("generation-ideas-tsv", "output/ideas.tsv", 65536),
    ),
    "history-compare": (
        (
            "history-comparison-json",
            "output/history-comparison.json",
            65536,
        ),
    ),
    "review": (
        ("review-markdown", "output/review.md", 65536),
        ("review-verdict-tsv", "output/verdict.tsv", 16384),
    ),
    "meta": (
        (
            "failure-distillation-json",
            "output/failure-distillation.json",
            65536,
        ),
    ),
}
_MODEL_OUTPUT_MAX_BYTES = 128 * 1024


def _canonical_bytes(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def model_artifacts(stage):
    try:
        values = _MODEL_ARTIFACTS[stage]
    except KeyError as exc:
        raise ValueError("unsupported stage") from exc
    return tuple((path, maximum) for _, path, maximum in values)


def stage_response_schema(stage):
    """Return the strict one-message schema used by the canonicalizer."""
    try:
        artifacts = _MODEL_ARTIFACTS[stage]
    except KeyError as exc:
        raise ValueError("unsupported stage") from exc
    return {
        "additionalProperties": False,
        "properties": {
            "artifacts": {
                "items": {
                    "additionalProperties": False,
                    "properties": {
                        "artifact_kind": {
                            "enum": [item[0] for item in artifacts],
                            "type": "string",
                        },
                        "content": {
                            "maxLength": max(
                                item[2] for item in artifacts
                            ),
                            "type": "string",
                        },
                    },
                    "required": ["artifact_kind", "content"],
                    "type": "object",
                },
                "maxItems": len(artifacts),
                "minItems": len(artifacts),
                "type": "array",
            },
            "schema_version": {
                "enum": [1],
                "type": "integer",
            },
            "stage": {
                "enum": [stage],
                "type": "string",
            },
        },
        "required": ["schema_version", "stage", "artifacts"],
        "type": "object",
    }


def parse_model_output(stage, raw):
    """Validate one structured final message and return artifact bytes."""
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > _MODEL_OUTPUT_MAX_BYTES
    ):
        raise ValueError("model output byte bound is invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("model output is not UTF-8 JSON") from exc
    expected = _MODEL_ARTIFACTS.get(stage)
    if (
        expected is None
        or not isinstance(value, dict)
        or set(value) != {"schema_version", "stage", "artifacts"}
        or value.get("schema_version") != 1
        or value.get("stage") != stage
        or not isinstance(value.get("artifacts"), list)
        or len(value["artifacts"]) != len(expected)
    ):
        raise ValueError("model output envelope is invalid")
    rendered = {}
    for item, (kind, path, maximum) in zip(
        value["artifacts"],
        expected,
    ):
        if (
            not isinstance(item, dict)
            or set(item) != {"artifact_kind", "content"}
            or item.get("artifact_kind") != kind
            or not isinstance(item.get("content"), str)
        ):
            raise ValueError("model artifact envelope is invalid")
        content = item["content"].encode("utf-8")
        if not content or len(content) > maximum:
            raise ValueError("model artifact content is invalid")
        rendered[path] = content
    return rendered


def _write_exclusive(path, raw, mode=0o444):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validated_existing_attestation(
    path,
    stage,
    seat_id,
    prompt_sha256,
):
    value = path.lstat()
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or value.st_size > 4096
    ):
        raise ValueError("prompt attestation path is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        raw = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    expected = _canonical_bytes(
        {
            "schema_version": 1,
            "stage": stage,
            "seat_id": seat_id,
            "prompt_sha256": prompt_sha256,
        }
    )
    if raw != expected:
        raise ValueError("prompt attestation does not match")


def materialize_model_output(
    mirror,
    stage,
    seat_id,
    prompt_sha256,
    raw,
):
    """Render a validated model message into the existing stage ABI."""
    if (
        not isinstance(seat_id, str)
        or not seat_id
        or not isinstance(prompt_sha256, str)
        or len(prompt_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in prompt_sha256
        )
    ):
        raise ValueError("materialization identity is invalid")
    mirror = pathlib.Path(mirror)
    output = mirror / "output"
    output_stat = output.lstat()
    if not stat.S_ISDIR(output_stat.st_mode):
        raise ValueError("stage output root is invalid")
    rendered = parse_model_output(stage, raw)
    result = {}
    for relative, content in rendered.items():
        target = mirror.joinpath(
            *pathlib.PurePosixPath(relative).parts
        )
        if target.parent != output:
            raise ValueError("model artifact path is invalid")
        _write_exclusive(target, content)
        result[relative] = hashlib.sha256(content).hexdigest()
    attestation_path = output / "prompt-attestation.json"
    if attestation_path.exists():
        _validated_existing_attestation(
            attestation_path,
            stage,
            seat_id,
            prompt_sha256,
        )
    else:
        _write_exclusive(
            attestation_path,
            _canonical_bytes(
                {
                    "schema_version": 1,
                    "stage": stage,
                    "seat_id": seat_id,
                    "prompt_sha256": prompt_sha256,
                }
            ),
        )
    directory = os.open(output, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return result


def _write_attestation(path, raw):
    target = pathlib.Path(path)
    if target != pathlib.Path("output/prompt-attestation.json"):
        raise ValueError("attestation path is outside the adapter contract")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o444)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 5:
        return 64
    stage, seat_id, attestation_path, command_json, prompt = arguments
    if (
        stage not in {"generate", "history-compare", "review", "meta"}
        or not seat_id
        or any(character in seat_id for character in "\r\n\x00")
    ):
        return 64
    try:
        command = json.loads(command_json)
    except (TypeError, ValueError):
        return 64
    if (
        not isinstance(command, list)
        or not command
        or any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in command
        )
        or not pathlib.Path(command[0]).is_absolute()
    ):
        return 64
    attestation = {
        "schema_version": 1,
        "stage": stage,
        "seat_id": seat_id,
        "prompt_sha256": hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest(),
    }
    try:
        _write_attestation(
            attestation_path,
            _canonical_bytes(attestation),
        )
    except (OSError, TypeError, ValueError):
        return 74
    try:
        os.execve(
            command[0],
            [*command, prompt],
            dict(os.environ),
        )
    except OSError:
        return 71


if __name__ == "__main__":
    raise SystemExit(main())
