#!/usr/bin/env python3
"""Portable provider stages with host-owned projection and receipts."""

import argparse
import copy
import hashlib
import json
import math
import os
import pathlib
import shutil
import stat
import struct
import sys
import tempfile
import unicodedata
import weakref

try:
    from lib import direction_contract
    from lib import history_contract_v2
    from lib import history_retrieval
    from lib import history_stage
    from lib import history_stage_adapter
    from lib import portable_agent
    from lib import provider_adapters
except ImportError:
    import direction_contract
    import history_contract_v2
    import history_retrieval
    import history_stage
    import history_stage_adapter
    import portable_agent
    import provider_adapters


ROOT = pathlib.Path(__file__).resolve().parents[1]
BOUNDARY = "portable-mirror-v1"
HOST_INPUT_MAX_BYTES = 256 * 1024
MODEL_OUTPUT_MAX_BYTES = 128 * 1024
DECLARED_INPUT_MAX_BYTES = 128 * 1024
PREFLIGHT_MAX_BYTES = 64 * 1024
_EXEC_SINGLE_STRING_MAX_BYTES = 128 * 1024
_EXEC_AGGREGATE_CEILING_BYTES = 256 * 1024
_EXEC_RESERVE_BYTES = 32 * 1024
_EXEC_DYNAMIC_PATH_RESERVE_CHARS = 64
_REQUEST_OVERHEAD_BYTES = 4096
_REGISTRY = ROOT / "history" / "provider-adapters-v1.json"
_ROLES = {
    "generate": ROOT / "roles" / "generate.md",
    "history-compare": ROOT / "roles" / "history-compare.md",
    "review": ROOT / "roles" / "review.md",
    "meta": ROOT / "roles" / "meta.md",
    "awr-research": ROOT / "roles" / "awr.md",
    "awr-priorwork": ROOT / "roles" / "awr-priorwork.md",
    "awr-judge": ROOT / "roles" / "awr-judge.md",
}
_AWR_ARTIFACTS = {
    "awr-research": ("awr-draft-markdown", "draft.md"),
    "awr-priorwork": ("awr-priorwork-markdown", "priorwork.md"),
    "awr-judge": ("awr-judge-markdown", "judge.md"),
}
_OUTPUT_PROFILES = {
    "generate": {
        "ideas.md": ("generation-ideas-markdown", 65536),
        "ideas.tsv": ("generation-ideas-tsv", 65536),
        "prompt-attestation.json": ("prompt-attestation-json", 4096),
    },
    "history-compare": {
        "history-comparison.json": ("history-comparison-json", 65536),
        "prompt-attestation.json": ("prompt-attestation-json", 4096),
    },
    "review": {
        "review.md": ("review-markdown", 65536),
        "verdict.tsv": ("review-verdict-tsv", 16384),
        "prompt-attestation.json": ("prompt-attestation-json", 4096),
    },
    "meta": {
        "failure-distillation.json": ("failure-distillation-json", 65536),
        "prompt-attestation.json": ("prompt-attestation-json", 4096),
    },
    "awr-research": {"draft.md": ("awr-draft-markdown", 65536)},
    "awr-priorwork": {
        "priorwork.md": ("awr-priorwork-markdown", 65536)
    },
    "awr-judge": {"judge.md": ("awr-judge-markdown", 65536)},
}
_PREFLIGHT_FIELDS = {
    "schema_version",
    "execution_boundary",
    "stage",
    "seat_id",
    "provider",
    "provider_validation",
    "authority",
    "execution_request_profile_hash",
    "max_output_tokens",
    "output_token_cap_binding",
    "output_token_cap_semantics",
    "serialized_prompt_sha256",
    "role_sha256",
    "input_sha256s",
    "provider_command",
    "provider_request_sha256",
    "provider_request_binding_sha256",
    "response_schema_sha256",
    "output_contract",
    "output_names",
    "environment_policy",
    "scrubbed_environment",
    "preserved_provider_config_environment",
    "byte_budget",
    "exec_budget",
}
_COMPLETION_FIELDS = {
    "schema_version",
    "execution_boundary",
    "stage",
    "seat_id",
    "provider",
    "provider_validation",
    "authority",
    "execution_request_profile_hash",
    "max_output_tokens",
    "output_token_cap_binding",
    "output_token_cap_semantics",
    "preflight_sha256",
    "model_envelope_sha256",
    "outputs",
    "completion_id",
}


# Host-side validation failures: short cooldown. Provider/runtime failures keep
# the long operational sleep.
_CONTRACT_ERROR_CODES = frozenset(
    {
        # Host projection / stage DSL
        "invalid_generation_output",
        "invalid_model_envelope",
        "invalid_model_artifact",
        "invalid_history_comparison",
        "noncanonical_history_comparison",
        "invalid_retrieval_pack",
        "noncanonical_retrieval_pack",
        "invalid_review_candidate",
        "noncanonical_review_candidate",
        "invalid_failure_distillation",
        "noncanonical_failure_distillation",
        "invalid_failure_batch",
        "noncanonical_failure_batch",
        "invalid_review_output",
        "invalid_direction_contract",
        "noncanonical_direction_contract",
        "schema_mismatch",
        "provider_request_attestation_mismatch",
        # Provider transport envelope shape (not backend outage)
        "malformed_output",
        "noncanonical_output",
        "final_output_missing",
        "final_output_unreadable",
        "final_output_oversize",
        # Request assembly / binding
        "invalid_contract_text",
        "contract_text_hash_mismatch",
        "request_too_large",
        "invalid_serialized_prompt",
        "unsupported_stage",
        "invalid_inputs",
        "duplicate_input",
        "invalid_seat_id",
        "invalid_input_name",
        "invalid_input_argument",
        "invalid_provider_request",
        "invalid_prepared_stage",
        "noncanonical_runtime_value",
        "provider_surface_mismatch",
        "provider_request_changed",
        "provider_default_changed",
        "provider_executable_changed",
        "response_schema_changed",
        "unsafe_input",
        "unsafe_role",
        "unsafe_preflight",
        "unsafe_state_root",
        "unsafe_output_root",
        "overlapping_state_roots",
        "output_root_exists",
        "state_root_exists",
        "completion_exists",
        "preflight_changed",
        "input_changed",
        "role_changed",
        "projected_output_bound",
        "output_projection_mismatch",
        "preflight_too_large",
        "exec_argument_too_large",
        "exec_environment_too_large",
        "exec_aggregate_too_large",
        "exec_contract_changed",
        "invalid_response_schema",
        "invalid_output_contract",
        "invalid_capability",
        "invalid_prompt",
        "invalid_timeout",
        "invalid_manifest",
        "invalid_provenance",
        "reserved_input",
        "source_boundary_violation",
        "input_sha_mismatch",
        "unexpected_artifact",
        "provider_model_authority_changed",
        "output_token_cap_unsupported",
        "output_token_cap_changed",
        "import_conflict",
    }
)


class PortableStageError(RuntimeError):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")

    @property
    def error_class(self):
        return "contract" if self.code in _CONTRACT_ERROR_CODES else "execution"


class _FrozenDict(dict):
    @staticmethod
    def _immutable(*args, **kwargs):
        raise TypeError("portable stage values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class _FrozenList(list):
    @staticmethod
    def _immutable(*args, **kwargs):
        raise TypeError("portable stage values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


class PreparedStage(_FrozenDict):
    """Opaque in-process authority for one preflighted provider launch."""

    __hash__ = object.__hash__
    __eq__ = object.__eq__
    __ne__ = object.__ne__

    def __new__(cls, *args, **kwargs):
        raise TypeError("prepared stages are issued by prepare_stage")


_PREPARED_STAGES = weakref.WeakKeyDictionary()


def _freeze_value(value):
    if isinstance(value, dict):
        frozen = dict.__new__(_FrozenDict)
        dict.__init__(
            frozen,
            {key: _freeze_value(item) for key, item in value.items()},
        )
        return frozen
    if isinstance(value, list):
        frozen = list.__new__(_FrozenList)
        list.__init__(frozen, (_freeze_value(item) for item in value))
        return frozen
    return value


def _thaw_value(value):
    if isinstance(value, dict):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_thaw_value(item) for item in value]
    return value


def _issue_prepared(
    public,
    *,
    launch_intent,
    preflight_raw,
    response_schema_raw: bytes,
    executable_identity,
):
    prepared = dict.__new__(PreparedStage)
    dict.__init__(prepared, _freeze_value(public))
    _PREPARED_STAGES[prepared] = {
        "public": copy.deepcopy(public),
        "public_snapshot": _exact_canonical_bytes(public),
        "launch_intent": launch_intent,
        "preflight_raw": preflight_raw,
        "response_schema_raw": response_schema_raw,
        "executable_identity": executable_identity,
    }
    return prepared


def _private_prepared(prepared):
    if type(prepared) is not PreparedStage:
        raise PortableStageError("invalid_prepared_stage")
    private = _PREPARED_STAGES.get(prepared)
    if private is None:
        raise PortableStageError("invalid_prepared_stage")
    try:
        observed_snapshot = _exact_canonical_bytes(_thaw_value(prepared))
    except PortableStageError as exc:
        raise PortableStageError("invalid_prepared_stage") from exc
    if observed_snapshot != private["public_snapshot"]:
        raise PortableStageError("invalid_prepared_stage")
    return copy.deepcopy(private["public"]), private


def _frozen_response_schema(prepared, private):
    raw = private["response_schema_raw"]
    expected_sha = prepared["output_contract"]["response_schema_sha256"]
    current = _canonical_bytes(_response_schema(prepared["stage"]))
    if _sha(raw) != expected_sha or current != raw:
        raise PortableStageError("response_schema_changed")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableStageError("invalid_prepared_stage") from exc
    return value


def _canonical_bytes(value):
    return history_contract_v2.canonical_bytes(value)


def _normalize_json_value(value, *, require_nfc=False, allow_float=True):
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not allow_float or not math.isfinite(value):
            raise PortableStageError("noncanonical_runtime_value")
        return value
    if type(value) is str:
        normalized = unicodedata.normalize("NFC", value)
        if require_nfc and normalized != value:
            raise PortableStageError("noncanonical_runtime_value")
        return normalized
    if type(value) is list:
        return [
            _normalize_json_value(
                item,
                require_nfc=require_nfc,
                allow_float=allow_float,
            )
            for item in value
        ]
    if type(value) is dict:
        normalized = {}
        for key, item in value.items():
            if type(key) is not str:
                raise PortableStageError("noncanonical_runtime_value")
            normalized_key = unicodedata.normalize("NFC", key)
            if require_nfc and normalized_key != key:
                raise PortableStageError("noncanonical_runtime_value")
            if normalized_key in normalized:
                raise PortableStageError("noncanonical_runtime_value")
            normalized[normalized_key] = _normalize_json_value(
                item,
                require_nfc=require_nfc,
                allow_float=allow_float,
            )
        return normalized
    raise PortableStageError("noncanonical_runtime_value")


def _exact_canonical_bytes(value):
    normalized = _normalize_json_value(
        value,
        require_nfc=True,
        allow_float=False,
    )
    try:
        return (
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PortableStageError("noncanonical_runtime_value") from exc


def _canonical_json_bytes(value):
    normalized = _normalize_json_value(value)
    try:
        return (
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PortableStageError("noncanonical_runtime_value") from exc


def _decode_json_bytes(raw, code):
    def reject_constant(_):
        raise ValueError("non-finite JSON number")

    def closed_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=closed_pairs,
            parse_constant=reject_constant,
        )
        _normalize_json_value(value, require_nfc=True)
        return value
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        PortableStageError,
        ValueError,
    ) as exc:
        raise PortableStageError(code) from exc


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _valid_text(value):
    return (
        type(value) is str
        and bool(value)
        and not any(character in value for character in "\x00\r\n")
    )


def _safe_input_name(name):
    if not _valid_text(name):
        raise PortableStageError("invalid_input_name")
    relative = pathlib.PurePosixPath(name)
    if (
        relative.is_absolute()
        or len(relative.parts) != 1
        or relative.name in {"role.md", ".", ".."}
        or portable_agent._reserved(relative)
    ):
        raise PortableStageError("invalid_input_name")
    return relative.name


def _capture_regular(path, maximum, code):
    try:
        return portable_agent._open_read_stable(
            pathlib.Path(path),
            maximum,
            code,
        )
    except portable_agent.PortableAgentError as exc:
        raise PortableStageError(exc.code, exc.detail) from exc


def _request_attestation_schema():
    return {
        "additionalProperties": False,
        "properties": {
            "provider_request_binding_sha256": {"type": "string"},
            "schema_version": {
                "enum": ["portable-stage-response-attestation-v1"],
                "type": "string",
            },
            "serialized_prompt_sha256": {"type": "string"},
        },
        "required": [
            "schema_version",
            "provider_request_binding_sha256",
            "serialized_prompt_sha256",
        ],
        "type": "object",
    }


def _response_schema(stage):
    if stage in _AWR_ARTIFACTS:
        kind, _ = _AWR_ARTIFACTS[stage]
        return {
            "additionalProperties": False,
            "properties": {
                "artifacts": {
                    "items": {
                        "additionalProperties": False,
                        "properties": {
                            "artifact_kind": {"enum": [kind], "type": "string"},
                            "content": {"maxLength": 65536, "type": "string"},
                        },
                        "required": ["artifact_kind", "content"],
                        "type": "object",
                    },
                    "maxItems": 1,
                    "minItems": 1,
                    "type": "array",
                },
                "request_attestation": _request_attestation_schema(),
                "schema_version": {
                    "maximum": 1,
                    "minimum": 1,
                    "type": "integer",
                },
                "stage": {"enum": [stage], "type": "string"},
            },
            "required": [
                "schema_version",
                "stage",
                "request_attestation",
                "artifacts",
            ],
            "type": "object",
        }
    try:
        schema = copy.deepcopy(
            history_stage_adapter.stage_response_schema(stage)
        )
    except ValueError as exc:
        raise PortableStageError("unsupported_stage") from exc
    schema["properties"]["schema_version"] = {
        "maximum": 1,
        "minimum": 1,
        "type": "integer",
    }
    schema["properties"]["request_attestation"] = (
        _request_attestation_schema()
    )
    schema["required"] = [
        "schema_version",
        "stage",
        "request_attestation",
        "artifacts",
    ]
    return schema


def _decode_contract_text(raw, label):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PortableStageError("invalid_contract_text", label) from exc
    if "\x00" in text or unicodedata.normalize("NFC", text) != text:
        raise PortableStageError("invalid_contract_text", label)
    return text


def _host_output_contract(stage):
    """Machine-facing output shape the host will parse after import."""
    if stage == "generate":
        return {
            "schema_version": "portable-stage-host-output-contract-v1",
            "artifact_kind": "generation-ideas-markdown",
            "required_prefix": (
                "Assumption-Removal Attempt: complete I#   OR   "
                "Assumption-Removal Attempt: incomplete — <candidate>; "
                "blocked by: <field>"
            ),
            "candidate_heading": "## I<n>",
            "required_fields": [
                "One-Sentence Story",
                "Theme",
                "Form",
                "Summary",
                "Minimal Falsification Experiment",
                "Why It May Be Novel",
            ],
            "direction_fields_when_present": [
                "Direction Axis",
                "Target Failure",
                "Direction Evidence",
            ],
            "field_line_format": "<Field Name>: <single-line value>",
            "forbidden_headings": [
                "# Generation Ideas",
                "## Idea ",
                "### Claim",
                "### Rationale",
                "### Evaluation plan",
            ],
            "example": (
                "Assumption-Removal Attempt: incomplete — I1; "
                "blocked by: Crack Evidence\n"
                "\n"
                "## I1\n"
                "One-Sentence Story: ...\n"
                "Theme: ...\n"
                "Direction Axis: <allowed_axes id when direction mounted>\n"
                "Target Failure: <target_failures id when direction mounted>\n"
                "Direction Evidence: ...\n"
                "Form: ...\n"
                "Summary: ...\n"
                "Minimal Falsification Experiment: ...\n"
                "Why It May Be Novel: ...\n"
            ),
        }
    return {
        "schema_version": "portable-stage-host-output-contract-v1",
        "stage": stage,
        "note": (
            "Follow role_text exactly. Host parses the artifact content "
            "with stage-specific validators."
        ),
    }


def _provider_request(
    provider,
    stage,
    seat_id,
    serialized_prompt,
    input_sha256s,
    role_sha256,
    schema,
    max_output_tokens,
    output_token_cap_semantics,
    *,
    role_text,
    declared_input_texts,
):
    stdout_instruction = (
        "Emit exactly one UTF-8 NFC canonical JSON object to stdout, "
        "with lexicographically sorted object keys, compact "
        "separators, and exactly one trailing LF. The object must "
        "match response_schema. Do not emit Markdown fences, "
        "narration, or any other bytes."
    )
    if provider == "codex":
        stdout_instruction = (
            "Return exactly one JSON object matching response_schema as the "
            "structured final result. The Codex CLI writes that final result "
            "through its host-configured output-last-message path; stdout is "
            "diagnostic transport and is never an output fallback. The harness "
            "parses and canonicalizes the final JSON. Do not put Markdown fences "
            "or narration inside the structured value."
        )
    elif provider == "grok":
        stdout_instruction = (
            "Make the FINAL ASSISTANT RESPONSE exactly one UTF-8 NFC "
            "canonical JSON object inside exactly one Markdown fence. "
            "The opening fence must be the exact lowercase bytes "
            "```json followed by LF. The JSON object must use "
            "lexicographically sorted object keys, compact separators, "
            "and exactly one trailing LF; that LF must be followed "
            "immediately by the terminal closing bytes ```. The object "
            "must match response_schema. Do not emit any bytes before "
            "the opening fence or after the closing fence in the FINAL "
            "ASSISTANT RESPONSE, and do not emit triple-backtick bytes "
            "in any earlier assistant response."
        )
    elif provider == "agy":
        stdout_instruction = (
            "Return exactly one JSON object matching response_schema as the "
            "structured final result. The Agy CLI owns the outer stdout JSON; "
            "only a status=SUCCESS structured_output member is eligible for "
            "import. Do not put Markdown fences or narration inside the "
            "structured value."
        )
    elif provider == "claude":
        stdout_instruction = (
            "Return exactly one JSON object matching response_schema as the "
            "structured final result. The Claude CLI owns the outer stdout JSON; "
            "only subtype=success structured_output is eligible for import. "
            "Do not put Markdown fences or narration inside the structured value."
        )
    if type(role_text) is not str or not role_text:
        raise PortableStageError("invalid_contract_text", "role_text")
    if type(declared_input_texts) is not dict:
        raise PortableStageError("invalid_contract_text", "declared_input_texts")
    if set(declared_input_texts) != set(input_sha256s):
        raise PortableStageError(
            "invalid_contract_text",
            "declared_input_texts keys must equal input_sha256s",
        )
    ordered_inputs = {
        name: declared_input_texts[name]
        for name in sorted(declared_input_texts)
    }
    for name, text in ordered_inputs.items():
        if type(text) is not str:
            raise PortableStageError("invalid_contract_text", name)
        if _sha(text.encode("utf-8")) != input_sha256s[name]:
            raise PortableStageError("contract_text_hash_mismatch", name)
    if _sha(role_text.encode("utf-8")) != role_sha256:
        raise PortableStageError("contract_text_hash_mismatch", "role_text")
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise PortableStageError("output_token_cap_unsupported")
    if output_token_cap_semantics != "reasoning-and-visible-output":
        raise PortableStageError("output_token_cap_unsupported")
    base = {
        "schema_version": "portable-stage-request-v1",
        "stage": stage,
        "seat_id": seat_id,
        "max_output_tokens": max_output_tokens,
        "output_token_cap_semantics": output_token_cap_semantics,
        "serialized_prompt": serialized_prompt,
        "role_path": "role.md",
        "role_text": role_text,
        "declared_inputs": sorted(input_sha256s),
        "declared_input_sha256s": dict(sorted(input_sha256s.items())),
        "declared_input_texts": ordered_inputs,
        "role_sha256": role_sha256,
        "response_schema": schema,
        "host_output_contract": _host_output_contract(stage),
        "transport_instructions": {
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
            "mirror": (
                "Do not create, modify, or delete any file in the mirror."
            ),
            "stdout": stdout_instruction,
            "request_attestation": (
                "Copy request_binding.provider_request_binding_sha256 and "
                "request_binding.serialized_prompt_sha256 exactly into "
                "request_attestation."
            ),
        },
    }
    binding_sha256 = history_contract_v2.framed_sha256(
        "portable-stage-request-base-v1",
        _canonical_json_bytes(base),
    )
    request = dict(base)
    request["request_binding"] = {
        "schema_version": "portable-stage-request-binding-v1",
        "provider_request_binding_sha256": binding_sha256,
        "serialized_prompt_sha256": _sha(serialized_prompt.encode("utf-8")),
    }
    return _canonical_json_bytes(request).decode("utf-8"), binding_sha256


def _launch_intent(request):
    if provider_adapters.command_intent_is_issued(request):
        return request
    raise PortableStageError("invalid_provider_request")


def _executable_identity(intent):
    path = pathlib.Path(intent.executable_path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise PortableStageError("provider_executable_changed") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise PortableStageError("provider_executable_changed")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _make_input_manifest(prepared):
    role = _ROLES[prepared["stage"]]
    role_relative = role.relative_to(ROOT).as_posix()
    manifest = [
        {
            "source_root": str(ROOT),
            "source_path": role_relative,
            "provenance": "declared-input-v1",
            "path": "role.md",
            "sha256": prepared["role_sha256"],
            "max_bytes": prepared["role_bytes"],
        }
    ]
    for name, source in sorted(prepared["input_paths"].items()):
        path = pathlib.Path(source)
        manifest.append(
            {
                "source_root": str(path.parent),
                "source_path": path.name,
                "provenance": "declared-input-v1",
                "path": "input/" + name,
                "sha256": prepared["input_sha256s"][name],
                "max_bytes": prepared["declared_input_bytes"][name],
            }
        )
    return manifest


def _write_owner_file(path, raw, mode=0o600):
    path = pathlib.Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    identity = None
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)
    return identity


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _path_identity(path):
    info = pathlib.Path(path).lstat()
    return (info.st_dev, info.st_ino)


def _remove_owned_tree(path, identity):
    path = pathlib.Path(path)
    try:
        if _path_identity(path) == identity:
            shutil.rmtree(path)
    except OSError:
        return


def _unlink_owned_file(path, identity):
    path = pathlib.Path(path)
    for _ in range(2):
        try:
            if _path_identity(path) == identity:
                path.unlink()
            return
        except OSError:
            continue


def _absolute_root(path):
    absolute = pathlib.Path(os.path.abspath(os.fspath(path)))
    temporary = pathlib.Path(os.path.abspath(tempfile.gettempdir()))
    try:
        relative = absolute.relative_to(temporary)
    except ValueError:
        return absolute
    try:
        resolved_temporary = temporary.resolve(strict=True)
    except (OSError, RuntimeError):
        return absolute
    return resolved_temporary / relative


def _validate_new_root(path, *, exists_code, unsafe_code):
    path = pathlib.Path(path)
    if not path.is_absolute():
        raise PortableStageError(unsafe_code)
    current = pathlib.Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PortableStageError(unsafe_code) from exc
        if stat.S_ISLNK(info.st_mode):
            raise PortableStageError(unsafe_code)
        if current == path:
            raise PortableStageError(exists_code)
        if not stat.S_ISDIR(info.st_mode):
            raise PortableStageError(unsafe_code)


def _system_arg_max():
    try:
        value = os.sysconf("SC_ARG_MAX")
    except (AttributeError, OSError, ValueError):
        value = _EXEC_AGGREGATE_CEILING_BYTES
    if type(value) is not int or value <= _EXEC_RESERVE_BYTES:
        value = _EXEC_AGGREGATE_CEILING_BYTES
    return value


def _rendered_exec_budget(intent, provider_request, response_schema, state_root):
    projected_mirror = (
        pathlib.Path(state_root)
        / (".portable-attempt-" + "x" * _EXEC_DYNAMIC_PATH_RESERVE_CHARS)
        / "mirror"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="portable-exec-preflight-") as directory:
            mirror = pathlib.Path(directory) / "mirror"
            mirror.mkdir(mode=0o700)
            mirror = mirror.resolve(strict=True)
            schema_path = None
            final_path = None
            if intent.provider == "codex":
                temporary = mirror / ".tmp"
                temporary.mkdir(mode=0o700)
                schema_path = temporary / "response-schema.json"
                _write_owner_file(schema_path, _canonical_json_bytes(response_schema))
                final_path = temporary / "model-final.json"
            argv, environment_delta = provider_adapters.render_command(
                intent,
                mirror,
                provider_request,
                schema_path,
                response_schema=response_schema,
                output_last_message_path=final_path,
            )
            rendered_prefix = str(mirror)
    except provider_adapters.ProviderResolutionError as exc:
        raise PortableStageError("invalid_provider_request", str(exc)) from exc
    except OSError as exc:
        raise PortableStageError("exec_preflight_failed") from exc

    if type(argv) is not list or type(environment_delta) is not dict:
        raise PortableStageError("invalid_provider_request")
    projected_prefix = str(projected_mirror)
    projected_argv = []
    for argument in argv:
        if type(argument) is not str or "\x00" in argument:
            raise PortableStageError("invalid_provider_request")
        projected_argv.append(
            argument.replace(rendered_prefix, projected_prefix)
        )
    projected_delta = {}
    for name, value in environment_delta.items():
        if (
            type(name) is not str
            or not name
            or "=" in name
            or "\x00" in name
            or type(value) is not str
            or "\x00" in value
        ):
            raise PortableStageError("invalid_provider_request")
        projected_delta[name] = value.replace(
            rendered_prefix, projected_prefix
        )

    environment = {
        name: value
        for name, value in os.environ.items()
        if not portable_agent._environment_is_scrubbed(name)
    }
    environment.update(projected_delta)
    environment["TMPDIR"] = str(projected_mirror / ".tmp")

    argv_sizes = [len(item.encode("utf-8")) + 1 for item in projected_argv]
    environment_sizes = [
        len(f"{name}={value}".encode("utf-8")) + 1
        for name, value in environment.items()
    ]
    if any(size > _EXEC_SINGLE_STRING_MAX_BYTES for size in argv_sizes):
        raise PortableStageError("exec_argument_too_large")
    if any(size > _EXEC_SINGLE_STRING_MAX_BYTES for size in environment_sizes):
        raise PortableStageError("exec_environment_too_large")
    pointer_bytes = (
        len(projected_argv) + len(environment) + 2
    ) * struct.calcsize("P")
    system_arg_max = _system_arg_max()
    aggregate_cap = min(system_arg_max, _EXEC_AGGREGATE_CEILING_BYTES)
    conservative_total = (
        sum(argv_sizes)
        + sum(environment_sizes)
        + pointer_bytes
        + _EXEC_RESERVE_BYTES
    )
    if conservative_total > aggregate_cap:
        raise PortableStageError(
            "exec_aggregate_too_large",
            f"conservative_total_bytes={conservative_total}",
        )
    return {
        "schema_version": "portable-stage-exec-budget-v1",
        "single_string_cap_bytes": _EXEC_SINGLE_STRING_MAX_BYTES,
        "system_arg_max_bytes": system_arg_max,
        "aggregate_cap_bytes": aggregate_cap,
        "reserve_bytes": _EXEC_RESERVE_BYTES,
        "argv_bytes": sum(argv_sizes),
        "environment_bytes": sum(environment_sizes),
        "pointer_bytes": pointer_bytes,
        "conservative_total_bytes": conservative_total,
        "rendered_argv_sha256": _sha(_exact_canonical_bytes(projected_argv)),
        "environment_delta_sha256": _sha(
            _exact_canonical_bytes(projected_delta)
        ),
    }


def prepare_stage(
    intent_or_capability,
    *,
    stage,
    seat_id,
    serialized_prompt,
    input_paths,
    output_root,
    state_root,
):
    """Preflight a portable stage without launching the provider."""
    if type(stage) is not str or stage not in _ROLES:
        raise PortableStageError("unsupported_stage")
    if not _valid_text(seat_id):
        raise PortableStageError("invalid_seat_id")
    if type(serialized_prompt) is not str or not serialized_prompt:
        raise PortableStageError("invalid_serialized_prompt")
    launch_intent = _launch_intent(intent_or_capability)
    expected_surface = "awr" if stage.startswith("awr-") else "hunt"
    if launch_intent.surface != expected_surface:
        raise PortableStageError("provider_surface_mismatch")
    try:
        max_output_tokens = provider_adapters.require_native_output_token_cap(
            launch_intent
        )
    except provider_adapters.ProviderResolutionError as exc:
        raise PortableStageError("output_token_cap_unsupported") from exc
    output_token_cap_binding = launch_intent.output_token_cap_binding
    output_token_cap_semantics = launch_intent.output_token_cap_semantics
    if not isinstance(input_paths, dict):
        raise PortableStageError("invalid_inputs")

    names = {}
    input_raws = {}
    for raw_name, raw_path in input_paths.items():
        name = _safe_input_name(raw_name)
        if name in names:
            raise PortableStageError("duplicate_input")
        path = pathlib.Path(raw_path)
        raw = _capture_regular(path, DECLARED_INPUT_MAX_BYTES, "unsafe_input")
        names[name] = str(path.resolve())
        input_raws[name] = raw

    role_raw = _capture_regular(_ROLES[stage], 128 * 1024, "unsafe_role")
    schema = _response_schema(stage)
    input_sha256s = {
        name: _sha(raw) for name, raw in sorted(input_raws.items())
    }
    role_sha256 = _sha(role_raw)
    role_text = _decode_contract_text(role_raw, "role.md")
    declared_input_texts = {
        name: _decode_contract_text(raw, name)
        for name, raw in sorted(input_raws.items())
    }
    provider_request, provider_request_binding_sha256 = _provider_request(
        launch_intent.provider,
        stage,
        seat_id,
        serialized_prompt,
        input_sha256s,
        role_sha256,
        schema,
        max_output_tokens,
        output_token_cap_semantics,
        role_text=role_text,
        declared_input_texts=declared_input_texts,
    )
    prompt_bytes = serialized_prompt.encode("utf-8")
    provider_request_raw = provider_request.encode("utf-8")
    declared_sizes = {name: len(raw) for name, raw in input_raws.items()}
    # role_text and declared_input_texts are embedded once inside
    # provider_request; do not double-count the disk copies.
    conservative_total = (
        len(provider_request_raw)
        + _REQUEST_OVERHEAD_BYTES
    )
    if conservative_total > HOST_INPUT_MAX_BYTES:
        raise PortableStageError(
            "request_too_large",
            f"conservative_total_bytes={conservative_total}",
        )

    output = _absolute_root(output_root)
    state = _absolute_root(state_root)
    _validate_new_root(
        output,
        exists_code="output_root_exists",
        unsafe_code="unsafe_output_root",
    )
    _validate_new_root(
        state,
        exists_code="state_root_exists",
        unsafe_code="unsafe_state_root",
    )
    if output == state or output in state.parents or state in output.parents:
        raise PortableStageError("overlapping_state_roots")

    provider_validation = launch_intent.provider_validation
    authority = launch_intent.authority
    profile_hash = launch_intent.profile_hash
    response_schema_raw = _canonical_bytes(schema)
    output_contract = {
        "capture": "stdout",
        "max_bytes": MODEL_OUTPUT_MAX_BYTES,
        "sha256": None,
        "response_schema_sha256": _sha(response_schema_raw),
    }
    provider_command = provider_adapters.command_intent_record(
        launch_intent
    )
    exec_budget = _rendered_exec_budget(
        launch_intent,
        provider_request,
        schema,
        state,
    )
    preflight = {
        "schema_version": "portable-stage-preflight-v1",
        "execution_boundary": BOUNDARY,
        "stage": stage,
        "seat_id": seat_id,
        "provider": launch_intent.provider,
        "provider_validation": provider_validation,
        "authority": authority,
        "execution_request_profile_hash": profile_hash,
        "max_output_tokens": max_output_tokens,
        "output_token_cap_binding": output_token_cap_binding,
        "output_token_cap_semantics": output_token_cap_semantics,
        "serialized_prompt_sha256": _sha(prompt_bytes),
        "role_sha256": role_sha256,
        "input_sha256s": input_sha256s,
        "provider_command": provider_command,
        "provider_request_sha256": _sha(provider_request_raw),
        "provider_request_binding_sha256": provider_request_binding_sha256,
        "response_schema_sha256": _sha(response_schema_raw),
        "output_contract": output_contract,
        "output_names": sorted(_OUTPUT_PROFILES[stage]),
        "environment_policy": portable_agent.ENVIRONMENT_POLICY,
        "scrubbed_environment": list(portable_agent.SCRUBBED_ENVIRONMENT),
        "preserved_provider_config_environment": list(
            portable_agent.PRESERVED_PROVIDER_CONFIG_ENVIRONMENT
        ),
        "byte_budget": {
            "host_cap_bytes": HOST_INPUT_MAX_BYTES,
            "role_bytes": len(role_raw),
            "declared_input_bytes": declared_sizes,
            "serialized_prompt_bytes": len(prompt_bytes),
            "provider_request_bytes": len(provider_request_raw),
            "conservative_total_bytes": conservative_total,
        },
        "exec_budget": exec_budget,
    }
    preflight_raw = _canonical_bytes(preflight)
    if len(preflight_raw) > PREFLIGHT_MAX_BYTES:
        raise PortableStageError(
            "preflight_too_large",
            f"preflight_bytes={len(preflight_raw)}",
        )
    state_identity = None
    try:
        state.mkdir(parents=True, mode=0o700)
        _validate_new_root(
            state / ".preflight-sentinel",
            exists_code="unsafe_state_root",
            unsafe_code="unsafe_state_root",
        )
        state_info = state.lstat()
        if not stat.S_ISDIR(state_info.st_mode) or state_info.st_mode & 0o077:
            raise PortableStageError("unsafe_state_root")
        state_identity = (state_info.st_dev, state_info.st_ino)
        preflight_path = state / "preflight.json"
        _write_owner_file(preflight_path, preflight_raw)
        _fsync_directory(state)
    except (OSError, PortableStageError) as exc:
        if state_identity is not None:
            try:
                if _path_identity(state) == state_identity and not any(
                    state.iterdir()
                ):
                    state.rmdir()
            except OSError:
                pass
        if isinstance(exc, PortableStageError):
            raise
        raise PortableStageError("preflight_publish_failed") from exc

    output_paths = {
        name: str(output / name) for name in _OUTPUT_PROFILES[stage]
    }
    public = {
        "schema_version": "portable-stage-prepared-v1",
        "execution_boundary": BOUNDARY,
        "stage": stage,
        "seat_id": seat_id,
        "surface": launch_intent.surface,
        "provider": launch_intent.provider,
        "provider_validation": provider_validation,
        "authority": authority,
        "hard_complete_eligible": launch_intent.hard_complete_eligible,
        "execution_request_profile_hash": profile_hash,
        "max_output_tokens": max_output_tokens,
        "output_token_cap_binding": output_token_cap_binding,
        "output_token_cap_semantics": output_token_cap_semantics,
        "provider_command": provider_command,
        "executable_path": launch_intent.executable_path,
        "serialized_prompt": serialized_prompt,
        "serialized_prompt_sha256": _sha(prompt_bytes),
        "provider_request": provider_request,
        "provider_request_sha256": _sha(provider_request_raw),
        "provider_request_binding_sha256": provider_request_binding_sha256,
        "role_sha256": role_sha256,
        "role_bytes": len(role_raw),
        "input_paths": names,
        "input_sha256s": preflight["input_sha256s"],
        "declared_input_bytes": declared_sizes,
        "output_root": str(output),
        "output_paths": output_paths,
        "state_root": str(state),
        "preflight_path": str(preflight_path),
        "preflight_sha256": _sha(preflight_raw),
        "completion_path": str(state / "completion.json"),
        "output_contract": output_contract,
        "exec_budget": exec_budget,
    }
    return _issue_prepared(
        public,
        launch_intent=launch_intent,
        preflight_raw=preflight_raw,
        response_schema_raw=response_schema_raw,
        executable_identity=_executable_identity(launch_intent),
    )


def _load_launch_intent(prepared, private):
    intent = private["launch_intent"]
    try:
        record = provider_adapters.command_intent_record(intent)
    except provider_adapters.ProviderResolutionError as exc:
        raise PortableStageError("provider_request_changed") from exc
    if (
        _exact_canonical_bytes(record)
        != _exact_canonical_bytes(prepared["provider_command"])
        or intent.provider != prepared["provider"]
        or intent.surface != prepared["surface"]
        or intent.profile_hash
        != prepared["execution_request_profile_hash"]
        or intent.max_output_tokens != prepared["max_output_tokens"]
        or intent.output_token_cap_binding
        != prepared["output_token_cap_binding"]
        or intent.output_token_cap_semantics
        != prepared["output_token_cap_semantics"]
        or _executable_identity(intent) != private["executable_identity"]
    ):
        raise PortableStageError("provider_request_changed")
    try:
        provider_adapters.revalidate_command_intent_for_launch(intent)
        provider_adapters.require_native_output_token_cap(
            intent, prepared["max_output_tokens"]
        )
    except provider_adapters.ProviderResolutionError as exc:
        raise PortableStageError("provider_default_changed") from exc
    return intent


def _load_prepared_inputs(prepared):
    result = {}
    for name, path in prepared["input_paths"].items():
        maximum = prepared["declared_input_bytes"][name]
        raw = _capture_regular(path, maximum, "unsafe_input")
        if len(raw) != maximum or _sha(raw) != prepared["input_sha256s"][name]:
            raise PortableStageError("input_changed")
        result[name] = raw
    role_raw = _capture_regular(_ROLES[prepared["stage"]], 128 * 1024, "unsafe_role")
    if (
        len(role_raw) != prepared["role_bytes"]
        or _sha(role_raw) != prepared["role_sha256"]
    ):
        raise PortableStageError("role_changed")
    return result


def _projected_prompt_attestation(prepared, response_attestation):
    return _canonical_bytes(
        {
            "schema_version": 1,
            "stage": prepared["stage"],
            "seat_id": prepared["seat_id"],
            "prompt_sha256": response_attestation[
                "serialized_prompt_sha256"
            ],
        }
    )


def _response_envelope(prepared, raw):
    value = _decode_json_bytes(raw, "invalid_model_envelope")
    if type(value) is not dict:
        raise PortableStageError("invalid_model_envelope")
    attestation = value.pop("request_attestation", None)
    expected = {
        "schema_version": "portable-stage-response-attestation-v1",
        "provider_request_binding_sha256": prepared[
            "provider_request_binding_sha256"
        ],
        "serialized_prompt_sha256": prepared[
            "serialized_prompt_sha256"
        ],
    }
    if (
        type(attestation) is not dict
        or _exact_canonical_bytes(attestation)
        != _exact_canonical_bytes(expected)
    ):
        raise PortableStageError("provider_request_attestation_mismatch")
    return _canonical_json_bytes(value), attestation


def _parse_json_artifact(raw, label):
    value = _decode_json_bytes(raw, "invalid_" + label)
    try:
        canonical = _canonical_json_bytes(value)
    except PortableStageError as exc:
        raise PortableStageError("invalid_" + label) from exc
    if raw != canonical:
        raise PortableStageError("noncanonical_" + label)
    return value


def _parse_awr_output(stage, raw):
    value = _decode_json_bytes(raw, "invalid_model_envelope")
    expected_kind, output_name = _AWR_ARTIFACTS[stage]
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "stage", "artifacts"}
        or value.get("schema_version") != 1
        or value.get("stage") != stage
        or not isinstance(value.get("artifacts"), list)
        or len(value["artifacts"]) != 1
        or not isinstance(value["artifacts"][0], dict)
        or set(value["artifacts"][0]) != {"artifact_kind", "content"}
        or value["artifacts"][0].get("artifact_kind") != expected_kind
        or not isinstance(value["artifacts"][0].get("content"), str)
    ):
        raise PortableStageError("invalid_model_envelope")
    content = value["artifacts"][0]["content"].encode("utf-8")
    if not content or len(content) > _OUTPUT_PROFILES[stage][output_name][1]:
        raise PortableStageError("invalid_model_artifact")
    return {output_name: content}


def _project_outputs(prepared, envelope_raw, input_raws):
    stage = prepared["stage"]
    envelope_raw, response_attestation = _response_envelope(
        prepared, envelope_raw
    )
    if stage in _AWR_ARTIFACTS:
        return _parse_awr_output(stage, envelope_raw)
    try:
        artifacts = history_stage_adapter.parse_model_output(stage, envelope_raw)
    except ValueError as exc:
        raise PortableStageError("invalid_model_envelope") from exc
    attestation = _projected_prompt_attestation(
        prepared, response_attestation
    )
    if stage == "generate":
        markdown = artifacts["output/ideas.md"]
        try:
            text = markdown.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PortableStageError(
                "invalid_generation_output",
                "generation markdown is not UTF-8",
            ) from exc
        contract = None
        if "direction_constraint.json" in input_raws:
            try:
                contract, canonical, _ = direction_contract.parse_contract_bytes(
                    input_raws["direction_constraint.json"]
                )
            except direction_contract.DirectionContractError as exc:
                raise PortableStageError(
                    "invalid_direction_contract",
                    str(exc),
                ) from exc
            if canonical != input_raws["direction_constraint.json"]:
                raise PortableStageError("noncanonical_direction_contract")
        try:
            tsv = history_stage._build_generation_tsv_from_markdown(
                text,
                direction_contract=contract,
            ).encode("utf-8")
        except history_stage.StageError as exc:
            raise PortableStageError(
                "invalid_generation_output",
                str(exc),
            ) from exc
        return {
            "ideas.md": markdown,
            "ideas.tsv": tsv,
            "prompt-attestation.json": attestation,
        }
    if stage == "history-compare":
        comparison_raw = artifacts["output/history-comparison.json"]
        comparison = _parse_json_artifact(
            comparison_raw,
            "history_comparison",
        )
        pack = _parse_json_artifact(
            input_raws.get("retrieval_pack.json", b""),
            "retrieval_pack",
        )
        try:
            history_retrieval._validate_response(pack, comparison)
        except history_retrieval.ComparisonValidationError as exc:
            raise PortableStageError("invalid_history_comparison") from exc
        return {
            "history-comparison.json": comparison_raw,
            "prompt-attestation.json": attestation,
        }
    if stage == "review":
        review = artifacts["output/review.md"]
        candidate = _parse_json_artifact(
            input_raws.get("candidate.json", b""),
            "review_candidate",
        )
        candidate_id = candidate.get("candidate_id") if isinstance(candidate, dict) else None
        if not _valid_text(candidate_id):
            raise PortableStageError("invalid_review_candidate")
        try:
            verdict = history_stage._build_review_verdict_from_markdown(
                review.decode("utf-8"),
                candidate_id,
            ).encode("utf-8")
        except (UnicodeDecodeError, history_stage.StageError) as exc:
            raise PortableStageError("invalid_review_output") from exc
        return {
            "review.md": review,
            "verdict.tsv": verdict,
            "prompt-attestation.json": attestation,
        }
    if stage == "meta":
        result_raw = artifacts["output/failure-distillation.json"]
        result = _parse_json_artifact(result_raw, "failure_distillation")
        batch = _parse_json_artifact(
            input_raws.get("failure_batch.json", b""),
            "failure_batch",
        )
        try:
            history_stage._validate_failure_distillation(result, batch)
        except history_stage.StageError as exc:
            raise PortableStageError("invalid_failure_distillation") from exc
        return {
            "failure-distillation.json": result_raw,
            "prompt-attestation.json": attestation,
        }
    raise PortableStageError("unsupported_stage")


def _publish_outputs(prepared, outputs):
    profile = _OUTPUT_PROFILES[prepared["stage"]]
    if set(outputs) != set(profile):
        raise PortableStageError("output_projection_mismatch")
    output_root = pathlib.Path(prepared["output_root"])
    _validate_new_root(
        output_root,
        exists_code="output_root_exists",
        unsafe_code="unsafe_output_root",
    )
    parent = output_root.parent
    _validate_new_root(
        parent / ".portable-parent-sentinel",
        exists_code="unsafe_output_root",
        unsafe_code="unsafe_output_root",
    )
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _validate_new_root(
        parent / ".portable-parent-sentinel",
        exists_code="unsafe_output_root",
        unsafe_code="unsafe_output_root",
    )
    staging = pathlib.Path(
        tempfile.mkdtemp(prefix=".portable-publish-", dir=parent)
    )
    os.chmod(staging, 0o700)
    staging_identity = _path_identity(staging)
    descriptors = {}
    renamed = False
    try:
        for name in sorted(outputs):
            raw = outputs[name]
            kind, maximum = profile[name]
            if not isinstance(raw, bytes) or not raw or len(raw) > maximum:
                raise PortableStageError("projected_output_bound")
            path = staging / name
            _write_owner_file(path, raw, mode=0o444)
            descriptors[name] = {
                "artifact_kind": kind,
                "sha256": _sha(raw),
                "byte_count": len(raw),
            }
        _fsync_directory(staging)
        os.rename(staging, output_root)
        renamed = True
        _fsync_directory(parent)
    except Exception:
        _remove_owned_tree(
            output_root if renamed else staging,
            staging_identity,
        )
        raise
    return descriptors, staging_identity


def _completion_id(material):
    return history_contract_v2.framed_sha256(
        "portable-stage-completion-id-v1",
        _canonical_bytes(material),
    )


def run_stage(prepared, timeout_seconds=600):
    """Launch one portable provider and publish validated host projections."""
    prepared, private = _private_prepared(prepared)
    if prepared.get("execution_boundary") != BOUNDARY:
        raise PortableStageError("invalid_prepared_stage")
    if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 3600:
        raise PortableStageError("invalid_timeout")
    completion_path = pathlib.Path(prepared.get("completion_path", ""))
    if completion_path.exists() or completion_path.is_symlink():
        raise PortableStageError("completion_exists")
    input_raws = _load_prepared_inputs(prepared)
    preflight_raw = _capture_regular(
        prepared["preflight_path"],
        PREFLIGHT_MAX_BYTES,
        "unsafe_preflight",
    )
    if (
        preflight_raw != private["preflight_raw"]
        or _sha(preflight_raw) != prepared["preflight_sha256"]
    ):
        raise PortableStageError("preflight_changed")
    response_schema = _frozen_response_schema(prepared, private)
    intent = _load_launch_intent(prepared, private)
    current_exec_budget = _rendered_exec_budget(
        intent,
        prepared["provider_request"],
        response_schema,
        prepared["state_root"],
    )
    for name in (
        "schema_version",
        "single_string_cap_bytes",
        "aggregate_cap_bytes",
        "reserve_bytes",
        "rendered_argv_sha256",
        "environment_delta_sha256",
    ):
        if current_exec_budget.get(name) != prepared["exec_budget"].get(name):
            raise PortableStageError("exec_contract_changed")
    try:
        attempt = portable_agent.run_portable_stdout_attempt(
            intent,
            inputs=_make_input_manifest(prepared),
            prompt=prepared["provider_request"],
            state_root=prepared["state_root"],
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=prepared["output_contract"]["max_bytes"],
            max_output_tokens=prepared["max_output_tokens"],
            response_schema=response_schema,
            expected_response_attestation={
                "schema_version": "portable-stage-response-attestation-v1",
                "provider_request_binding_sha256": prepared[
                    "provider_request_binding_sha256"
                ],
                "serialized_prompt_sha256": prepared[
                    "serialized_prompt_sha256"
                ],
            },
        )
    except portable_agent.PortableAgentError as exc:
        raise PortableStageError(exc.code, exc.detail) from exc
    if (
        attempt.get("provider") != prepared["provider"]
        or attempt.get("execution_request_profile_hash")
        != prepared["execution_request_profile_hash"]
        or attempt.get("max_output_tokens") != prepared["max_output_tokens"]
        or attempt.get("output_token_cap_binding")
        != prepared["output_token_cap_binding"]
        or attempt.get("output_token_cap_semantics")
        != prepared["output_token_cap_semantics"]
    ):
        raise PortableStageError("provider_request_changed")
    outputs = _project_outputs(prepared, attempt["raw"], input_raws)
    published = False
    output_identity = None
    completion_identity = None
    try:
        descriptors, output_identity = _publish_outputs(prepared, outputs)
        published = True
        material = {
            "schema_version": "portable-stage-completion-v1",
            "execution_boundary": BOUNDARY,
            "stage": prepared["stage"],
            "seat_id": prepared["seat_id"],
            "provider": prepared["provider"],
            "provider_validation": prepared["provider_validation"],
            "authority": prepared["authority"],
            "execution_request_profile_hash": prepared[
                "execution_request_profile_hash"
            ],
            "max_output_tokens": prepared["max_output_tokens"],
            "output_token_cap_binding": prepared[
                "output_token_cap_binding"
            ],
            "output_token_cap_semantics": prepared[
                "output_token_cap_semantics"
            ],
            "preflight_sha256": _sha(preflight_raw),
            "model_envelope_sha256": attempt["model_envelope_sha256"],
            "outputs": descriptors,
        }
        completion = dict(material)
        completion["completion_id"] = _completion_id(material)
        completion_raw = _canonical_bytes(completion)
        completion_identity = _write_owner_file(
            completion_path, completion_raw
        )
        _fsync_directory(completion_path.parent)
        return completion
    except Exception:
        if published and output_identity is not None:
            _remove_owned_tree(prepared["output_root"], output_identity)
        if completion_identity is not None:
            _unlink_owned_file(completion_path, completion_identity)
        raise


def _load_canonical_file(path, maximum, code):
    raw = _capture_regular(path, maximum, code)
    value = _decode_json_bytes(raw, code)
    try:
        canonical = _canonical_bytes(value)
    except (history_contract_v2.ContractV2Error, PortableStageError) as exc:
        raise PortableStageError(code) from exc
    if raw != canonical:
        raise PortableStageError(code)
    return raw, value


def verify_completion(prepared):
    """Verify a portable completion receipt and all published artifacts."""
    prepared, private = _private_prepared(prepared)
    if prepared.get("execution_boundary") != BOUNDARY:
        raise PortableStageError("invalid_prepared_stage")
    preflight_raw, preflight = _load_canonical_file(
        prepared["preflight_path"],
        PREFLIGHT_MAX_BYTES,
        "invalid_preflight",
    )
    _, completion = _load_canonical_file(
        prepared["completion_path"],
        64 * 1024,
        "invalid_completion",
    )
    _validate_public_preflight(preflight)
    _validate_public_completion(completion)
    if (
        preflight_raw != private["preflight_raw"]
        or _sha(preflight_raw) != prepared["preflight_sha256"]
        or preflight.get("execution_boundary") != BOUNDARY
        or preflight.get("execution_request_profile_hash")
        != prepared["execution_request_profile_hash"]
    ):
        raise PortableStageError("preflight_changed")
    if not isinstance(completion, dict) or set(completion) != _COMPLETION_FIELDS:
        raise PortableStageError("invalid_completion")
    material = dict(completion)
    completion_id = material.pop("completion_id")
    if (
        completion.get("schema_version") != "portable-stage-completion-v1"
        or completion.get("execution_boundary") != BOUNDARY
        or completion.get("stage") != prepared["stage"]
        or completion.get("seat_id") != prepared["seat_id"]
        or completion.get("provider") != prepared["provider"]
        or completion.get("provider_validation") != prepared["provider_validation"]
        or completion.get("authority") != prepared["authority"]
        or completion.get("execution_request_profile_hash")
        != prepared["execution_request_profile_hash"]
        or completion.get("max_output_tokens")
        != prepared["max_output_tokens"]
        or completion.get("output_token_cap_binding")
        != prepared["output_token_cap_binding"]
        or completion.get("output_token_cap_semantics")
        != prepared["output_token_cap_semantics"]
        or completion.get("preflight_sha256") != _sha(preflight_raw)
        or completion_id != _completion_id(material)
    ):
        raise PortableStageError("completion_changed")
    envelope_sha = completion.get("model_envelope_sha256")
    if (
        type(envelope_sha) is not str
        or len(envelope_sha) != 64
        or any(character not in "0123456789abcdef" for character in envelope_sha)
    ):
        raise PortableStageError("invalid_completion")
    envelope = pathlib.Path(prepared["state_root"]) / "imports" / (envelope_sha + ".json")
    raw = _capture_regular(
        envelope,
        prepared["output_contract"]["max_bytes"],
        "invalid_model_import",
    )
    if _sha(raw) != envelope_sha:
        raise PortableStageError("model_import_changed")

    descriptors = completion.get("outputs")
    profile = _OUTPUT_PROFILES[prepared["stage"]]
    if not isinstance(descriptors, dict) or set(descriptors) != set(profile):
        raise PortableStageError("invalid_completion")
    output_root = pathlib.Path(prepared["output_root"])
    try:
        root_info = output_root.lstat()
    except OSError as exc:
        raise PortableStageError("published_outputs_unavailable") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise PortableStageError("published_outputs_unavailable")
    observed = set()
    for path in output_root.iterdir():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PortableStageError("published_output_changed")
        observed.add(path.name)
    if observed != set(profile):
        raise PortableStageError("published_output_changed")
    for name, (kind, maximum) in profile.items():
        descriptor = descriptors.get(name)
        raw = _capture_regular(
            prepared["output_paths"][name],
            maximum,
            "published_output_changed",
        )
        if _exact_canonical_bytes(descriptor) != _exact_canonical_bytes({
            "artifact_kind": kind,
            "sha256": _sha(raw),
            "byte_count": len(raw),
        }):
            raise PortableStageError("published_output_changed")
    return True


def _require_sha256(value, code="invalid_public_descriptor"):
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PortableStageError(code)
    return value


def _validate_public_preflight(value):
    if (
        type(value) is not dict
        or set(value) != _PREFLIGHT_FIELDS
        or value.get("schema_version") != "portable-stage-preflight-v1"
        or value.get("execution_boundary") != BOUNDARY
        or value.get("stage") not in _OUTPUT_PROFILES
        or not _valid_text(value.get("seat_id"))
        or not _valid_text(value.get("provider"))
        or value.get("provider_validation") != "unverified"
        or value.get("authority") != "shadow-only"
        or type(value.get("max_output_tokens")) is not int
        or value["max_output_tokens"] <= 0
        or value.get("output_token_cap_binding") not in {
            "provider-native-exact",
            "test-provider-native-exact",
        }
        or value.get("output_token_cap_semantics")
        != "reasoning-and-visible-output"
    ):
        raise PortableStageError("invalid_preflight")
    for name in (
        "execution_request_profile_hash",
        "serialized_prompt_sha256",
        "role_sha256",
        "provider_request_sha256",
        "provider_request_binding_sha256",
        "response_schema_sha256",
    ):
        _require_sha256(value.get(name), "invalid_preflight")
    input_sha256s = value.get("input_sha256s")
    if type(input_sha256s) is not dict:
        raise PortableStageError("invalid_preflight")
    for name, digest in input_sha256s.items():
        _safe_input_name(name)
        _require_sha256(digest, "invalid_preflight")
    command = value.get("provider_command")
    try:
        registry = provider_adapters.load_registry(_REGISTRY)
        provider_adapters.validate_command_intent_record(registry, command)
    except provider_adapters.ProviderResolutionError as exc:
        raise PortableStageError("invalid_preflight") from exc
    if (
        command.get("provider") != value["provider"]
        or command.get("surface")
        != ("awr" if value["stage"].startswith("awr-") else "hunt")
        or command.get("execution_request_profile_hash")
        != value["execution_request_profile_hash"]
        or command.get("max_output_tokens") != value["max_output_tokens"]
        or command.get("output_token_cap_binding")
        != value["output_token_cap_binding"]
        or command.get("output_token_cap_semantics")
        != value["output_token_cap_semantics"]
    ):
        raise PortableStageError("invalid_preflight")
    output_contract = value.get("output_contract")
    if output_contract != {
        "capture": "stdout",
        "max_bytes": MODEL_OUTPUT_MAX_BYTES,
        "sha256": None,
        "response_schema_sha256": value["response_schema_sha256"],
    } or value.get("output_names") != sorted(
        _OUTPUT_PROFILES[value["stage"]]
    ):
        raise PortableStageError("invalid_preflight")
    if (
        value.get("environment_policy") != portable_agent.ENVIRONMENT_POLICY
        or value.get("scrubbed_environment")
        != list(portable_agent.SCRUBBED_ENVIRONMENT)
        or value.get("preserved_provider_config_environment")
        != list(portable_agent.PRESERVED_PROVIDER_CONFIG_ENVIRONMENT)
    ):
        raise PortableStageError("invalid_preflight")
    budget = value.get("byte_budget")
    budget_fields = {
        "host_cap_bytes",
        "role_bytes",
        "declared_input_bytes",
        "serialized_prompt_bytes",
        "provider_request_bytes",
        "conservative_total_bytes",
    }
    if (
        type(budget) is not dict
        or set(budget) != budget_fields
        or budget.get("host_cap_bytes") != HOST_INPUT_MAX_BYTES
        or type(budget.get("declared_input_bytes")) is not dict
        or set(budget["declared_input_bytes"]) != set(input_sha256s)
        or any(
            type(budget.get(name)) is not int or budget[name] <= 0
            for name in (
                "role_bytes",
                "serialized_prompt_bytes",
                "provider_request_bytes",
                "conservative_total_bytes",
            )
        )
        or any(
            type(size) is not int or size < 0
            for size in budget["declared_input_bytes"].values()
        )
        or budget["conservative_total_bytes"]
        != (
            budget["provider_request_bytes"]
            + _REQUEST_OVERHEAD_BYTES
        )
        or budget["conservative_total_bytes"] > HOST_INPUT_MAX_BYTES
    ):
        raise PortableStageError("invalid_preflight")

    exec_budget = value.get("exec_budget")
    exec_fields = {
        "schema_version",
        "single_string_cap_bytes",
        "system_arg_max_bytes",
        "aggregate_cap_bytes",
        "reserve_bytes",
        "argv_bytes",
        "environment_bytes",
        "pointer_bytes",
        "conservative_total_bytes",
        "rendered_argv_sha256",
        "environment_delta_sha256",
    }
    if (
        type(exec_budget) is not dict
        or set(exec_budget) != exec_fields
        or exec_budget.get("schema_version")
        != "portable-stage-exec-budget-v1"
        or exec_budget.get("single_string_cap_bytes")
        != _EXEC_SINGLE_STRING_MAX_BYTES
        or type(exec_budget.get("system_arg_max_bytes")) is not int
        or type(exec_budget.get("aggregate_cap_bytes")) is not int
        or exec_budget.get("aggregate_cap_bytes")
        != min(
            exec_budget["system_arg_max_bytes"],
            _EXEC_AGGREGATE_CEILING_BYTES,
        )
        or exec_budget.get("reserve_bytes") != _EXEC_RESERVE_BYTES
        or any(
            type(exec_budget.get(name)) is not int
            or exec_budget[name] <= 0
            for name in (
                "system_arg_max_bytes",
                "aggregate_cap_bytes",
                "argv_bytes",
                "environment_bytes",
                "pointer_bytes",
                "conservative_total_bytes",
            )
        )
        or exec_budget["conservative_total_bytes"]
        != (
            exec_budget["argv_bytes"]
            + exec_budget["environment_bytes"]
            + exec_budget["pointer_bytes"]
            + exec_budget["reserve_bytes"]
        )
        or exec_budget["conservative_total_bytes"]
        > exec_budget["aggregate_cap_bytes"]
    ):
        raise PortableStageError("invalid_preflight")
    _require_sha256(
        exec_budget.get("rendered_argv_sha256"), "invalid_preflight"
    )
    _require_sha256(
        exec_budget.get("environment_delta_sha256"), "invalid_preflight"
    )


def _validate_public_completion(value):
    if (
        type(value) is not dict
        or set(value) != _COMPLETION_FIELDS
        or value.get("schema_version") != "portable-stage-completion-v1"
        or value.get("execution_boundary") != BOUNDARY
        or value.get("stage") not in _OUTPUT_PROFILES
        or not _valid_text(value.get("seat_id"))
        or not _valid_text(value.get("provider"))
        or value.get("provider_validation") != "unverified"
        or value.get("authority") != "shadow-only"
        or type(value.get("max_output_tokens")) is not int
        or value["max_output_tokens"] <= 0
        or value.get("output_token_cap_binding") not in {
            "provider-native-exact",
            "test-provider-native-exact",
        }
        or value.get("output_token_cap_semantics")
        != "reasoning-and-visible-output"
        or type(value.get("outputs")) is not dict
    ):
        raise PortableStageError("invalid_completion")
    for name in (
        "execution_request_profile_hash",
        "preflight_sha256",
        "model_envelope_sha256",
        "completion_id",
    ):
        _require_sha256(value.get(name), "invalid_completion")


def _reference_root(path):
    try:
        root = pathlib.Path(path).resolve(strict=True)
        info = root.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PortableStageError("invalid_reference_root") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PortableStageError("invalid_reference_root")
    return root


def _relative_artifact_path(path, reference_root):
    root = _reference_root(reference_root)
    try:
        target = pathlib.Path(path).resolve(strict=True)
        relative = target.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PortableStageError("artifact_outside_reference_root") from exc
    if not relative.parts:
        raise PortableStageError("artifact_outside_reference_root")
    return pathlib.PurePosixPath(*relative.parts).as_posix()


def _resolve_artifact_path(reference_root, value):
    if type(value) is not str:
        raise PortableStageError("invalid_public_descriptor")
    relative = pathlib.PurePosixPath(value)
    if (
        relative.is_absolute()
        or str(relative) in {"", "."}
        or ".." in relative.parts
    ):
        raise PortableStageError("invalid_public_descriptor")
    root = _reference_root(reference_root)
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise PortableStageError("public_artifact_unavailable") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PortableStageError("public_artifact_unavailable")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise PortableStageError("public_artifact_unavailable")
    try:
        current.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PortableStageError("public_artifact_unavailable") from exc
    return current


def public_descriptor(prepared, reference_root):
    """Return the closed path-free record safe for canonical run indexes."""
    verify_completion(prepared)
    runtime, _ = _private_prepared(prepared)
    preflight_raw, preflight = _load_canonical_file(
        runtime["preflight_path"], PREFLIGHT_MAX_BYTES, "invalid_preflight"
    )
    completion_raw, completion = _load_canonical_file(
        runtime["completion_path"], 64 * 1024, "invalid_completion"
    )
    envelope_path = (
        pathlib.Path(runtime["state_root"])
        / "imports"
        / (completion["model_envelope_sha256"] + ".json")
    )
    return {
        "schema_version": "portable-stage-public-v1",
        "execution_boundary": BOUNDARY,
        "stage": runtime["stage"],
        "seat_id": runtime["seat_id"],
        "provider": runtime["provider"],
        "provider_validation": runtime["provider_validation"],
        "authority": runtime["authority"],
        "execution_request_profile_hash": runtime[
            "execution_request_profile_hash"
        ],
        "max_output_tokens": runtime["max_output_tokens"],
        "output_token_cap_binding": runtime["output_token_cap_binding"],
        "output_token_cap_semantics": runtime["output_token_cap_semantics"],
        "serialized_prompt_sha256": runtime["serialized_prompt_sha256"],
        "role_sha256": runtime["role_sha256"],
        "input_sha256s": dict(runtime["input_sha256s"]),
        "provider_request_sha256": runtime["provider_request_sha256"],
        "provider_request_binding_sha256": runtime[
            "provider_request_binding_sha256"
        ],
        "response_schema_sha256": runtime["output_contract"][
            "response_schema_sha256"
        ],
        "preflight": {
            "path": _relative_artifact_path(
                runtime["preflight_path"], reference_root
            ),
            "sha256": _sha(preflight_raw),
        },
        "completion": {
            "path": _relative_artifact_path(
                runtime["completion_path"], reference_root
            ),
            "sha256": _sha(completion_raw),
            "completion_id": completion["completion_id"],
            "model_envelope_sha256": completion[
                "model_envelope_sha256"
            ],
            "model_envelope_path": _relative_artifact_path(
                envelope_path, reference_root
            ),
        },
        "outputs": {
            name: {
                **dict(completion["outputs"][name]),
                "path": _relative_artifact_path(
                    runtime["output_paths"][name], reference_root
                ),
            }
            for name in sorted(completion["outputs"])
        },
    }


def verify_public_descriptor(descriptor, reference_root):
    """Verify one persisted public stage and return a read-only path view."""
    fields = {
        "schema_version",
        "execution_boundary",
        "stage",
        "seat_id",
        "provider",
        "provider_validation",
        "authority",
        "execution_request_profile_hash",
        "max_output_tokens",
        "output_token_cap_binding",
        "output_token_cap_semantics",
        "serialized_prompt_sha256",
        "role_sha256",
        "input_sha256s",
        "provider_request_sha256",
        "provider_request_binding_sha256",
        "response_schema_sha256",
        "preflight",
        "completion",
        "outputs",
    }
    if type(descriptor) is not dict:
        raise PortableStageError("invalid_public_descriptor")
    _exact_canonical_bytes(descriptor)
    if set(descriptor) != fields:
        raise PortableStageError("invalid_public_descriptor")
    stage = descriptor.get("stage")
    if (
        type(descriptor.get("schema_version")) is not str
        or descriptor["schema_version"] != "portable-stage-public-v1"
        or type(descriptor.get("execution_boundary")) is not str
        or descriptor["execution_boundary"] != BOUNDARY
        or not _valid_text(stage)
        or stage not in _OUTPUT_PROFILES
        or type(descriptor.get("max_output_tokens")) is not int
        or descriptor["max_output_tokens"] <= 0
        or descriptor.get("output_token_cap_binding") not in {
            "provider-native-exact",
            "test-provider-native-exact",
        }
        or descriptor.get("output_token_cap_semantics")
        != "reasoning-and-visible-output"
        or any(
            not _valid_text(descriptor.get(name))
            for name in (
                "seat_id",
                "provider",
                "provider_validation",
                "authority",
            )
        )
    ):
        raise PortableStageError("invalid_public_descriptor")
    for name in (
        "execution_request_profile_hash",
        "serialized_prompt_sha256",
        "role_sha256",
        "provider_request_sha256",
        "provider_request_binding_sha256",
        "response_schema_sha256",
    ):
        _require_sha256(descriptor.get(name))
    input_sha256s = descriptor.get("input_sha256s")
    if type(input_sha256s) is not dict:
        raise PortableStageError("invalid_public_descriptor")
    for name, digest in input_sha256s.items():
        _safe_input_name(name)
        _require_sha256(digest)

    preflight_ref = descriptor.get("preflight")
    completion_ref = descriptor.get("completion")
    if (
        type(preflight_ref) is not dict
        or set(preflight_ref) != {"path", "sha256"}
        or type(completion_ref) is not dict
        or set(completion_ref)
        != {
            "path",
            "sha256",
            "completion_id",
            "model_envelope_sha256",
            "model_envelope_path",
        }
    ):
        raise PortableStageError("invalid_public_descriptor")
    for value in (
        preflight_ref.get("sha256"),
        completion_ref.get("sha256"),
        completion_ref.get("completion_id"),
        completion_ref.get("model_envelope_sha256"),
    ):
        _require_sha256(value)
    preflight_path = _resolve_artifact_path(
        reference_root, preflight_ref.get("path")
    )
    completion_path = _resolve_artifact_path(
        reference_root, completion_ref.get("path")
    )
    envelope_path = _resolve_artifact_path(
        reference_root, completion_ref.get("model_envelope_path")
    )
    preflight_raw, preflight = _load_canonical_file(
        preflight_path, PREFLIGHT_MAX_BYTES, "invalid_preflight"
    )
    completion_raw, completion = _load_canonical_file(
        completion_path, 64 * 1024, "invalid_completion"
    )
    _validate_public_preflight(preflight)
    _validate_public_completion(completion)
    if (
        _sha(preflight_raw) != preflight_ref["sha256"]
        or _sha(completion_raw) != completion_ref["sha256"]
        or completion.get("preflight_sha256") != preflight_ref["sha256"]
        or completion.get("completion_id") != completion_ref["completion_id"]
        or completion.get("model_envelope_sha256")
        != completion_ref["model_envelope_sha256"]
    ):
        raise PortableStageError("public_receipt_changed")
    material = dict(completion)
    completion_id = material.pop("completion_id", None)
    if completion_id != _completion_id(material):
        raise PortableStageError("public_receipt_changed")
    shared_identity_fields = (
        "execution_boundary",
        "stage",
        "seat_id",
        "provider",
        "provider_validation",
        "authority",
        "execution_request_profile_hash",
        "max_output_tokens",
        "output_token_cap_binding",
        "output_token_cap_semantics",
    )
    for name in shared_identity_fields:
        if not descriptor[name] == preflight.get(name) == completion.get(name):
            raise PortableStageError("public_receipt_changed")
    for name in (
        "serialized_prompt_sha256",
        "role_sha256",
        "input_sha256s",
        "provider_request_sha256",
        "provider_request_binding_sha256",
        "response_schema_sha256",
    ):
        if descriptor[name] != preflight.get(name):
            raise PortableStageError("public_receipt_changed")
    envelope_raw = _capture_regular(
        envelope_path, MODEL_OUTPUT_MAX_BYTES, "invalid_model_import"
    )
    if _sha(envelope_raw) != completion_ref["model_envelope_sha256"]:
        raise PortableStageError("model_import_changed")

    outputs = descriptor.get("outputs")
    profile = _OUTPUT_PROFILES[stage]
    if type(outputs) is not dict or set(outputs) != set(profile):
        raise PortableStageError("invalid_public_descriptor")
    output_paths = {}
    receipt_outputs = {}
    for name, (kind, maximum) in profile.items():
        item = outputs.get(name)
        if (
            type(item) is not dict
            or set(item)
            != {"path", "artifact_kind", "sha256", "byte_count"}
            or type(item.get("artifact_kind")) is not str
            or item["artifact_kind"] != kind
            or type(item.get("byte_count")) is not int
            or item["byte_count"] <= 0
            or item["byte_count"] > maximum
        ):
            raise PortableStageError("invalid_public_descriptor")
        _require_sha256(item.get("sha256"))
        path = _resolve_artifact_path(reference_root, item.get("path"))
        raw = _capture_regular(path, maximum, "published_output_changed")
        receipt_item = {
            "artifact_kind": kind,
            "sha256": _sha(raw),
            "byte_count": len(raw),
        }
        if receipt_item != {
            key: item[key]
            for key in ("artifact_kind", "sha256", "byte_count")
        }:
            raise PortableStageError("published_output_changed")
        receipt_outputs[name] = receipt_item
        output_paths[name] = str(path)
    if completion.get("outputs") != receipt_outputs:
        raise PortableStageError("public_receipt_changed")
    return _freeze_value(
        {
            "execution_boundary": BOUNDARY,
            "stage": stage,
            "seat_id": descriptor["seat_id"],
            "provider": descriptor["provider"],
            "execution_request_profile_hash": descriptor[
                "execution_request_profile_hash"
            ],
            "max_output_tokens": descriptor["max_output_tokens"],
            "output_token_cap_binding": descriptor[
                "output_token_cap_binding"
            ],
            "output_token_cap_semantics": descriptor[
                "output_token_cap_semantics"
            ],
            "preflight_path": str(preflight_path),
            "completion_path": str(completion_path),
            "model_envelope_path": str(envelope_path),
            "output_paths": output_paths,
        }
    )


def _input_mapping(values):
    result = {}
    for value in values:
        if "=" not in value:
            raise PortableStageError("invalid_input_argument")
        name, path = value.split("=", 1)
        name = _safe_input_name(name)
        if not path or name in result:
            raise PortableStageError("invalid_input_argument")
        result[name] = pathlib.Path(path)
    return result


def _parser():
    parser = argparse.ArgumentParser(prog="portable_stage.py")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--provider-request-profile")
    run.add_argument("--surface", choices=("hunt", "awr"))
    run.add_argument("--provider")
    run.add_argument("--model")
    run.add_argument("--reasoning")
    run.add_argument("--max-output-tokens", type=int, required=True)
    run.add_argument("--registry", default=str(_REGISTRY))
    run.add_argument("--stage", choices=tuple(_ROLES), required=True)
    run.add_argument("--seat", required=True)
    run.add_argument("--serialized-prompt", required=True)
    run.add_argument("--input", action="append", default=[])
    run.add_argument("--output-root", required=True)
    run.add_argument("--state-root", required=True)
    run.add_argument("--timeout-seconds", type=float, default=600)
    return parser


def main(argv=None):
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command != "run":
        parser.error("unsupported command")
    try:
        registry = provider_adapters.load_registry(arguments.registry)
        if arguments.provider_request_profile:
            if any(
                value is not None
                for value in (
                    arguments.surface,
                    arguments.provider,
                    arguments.model,
                    arguments.reasoning,
                )
            ):
                raise PortableStageError("mixed_provider_request")
            intent = provider_adapters.load_command_intent(
                arguments.provider_request_profile,
                registry,
            )
            try:
                provider_adapters.require_native_output_token_cap(
                    intent, arguments.max_output_tokens
                )
            except provider_adapters.ProviderResolutionError as exc:
                raise PortableStageError("output_token_cap_changed") from exc
        else:
            if arguments.surface is None or arguments.provider is None:
                raise PortableStageError("missing_provider_request")
            intent = provider_adapters.resolve_command_intent(
                registry,
                arguments.surface,
                arguments.provider,
                model=arguments.model,
                reasoning=arguments.reasoning,
                max_output_tokens=arguments.max_output_tokens,
            )
        prompt_raw = _capture_regular(
            arguments.serialized_prompt,
            HOST_INPUT_MAX_BYTES,
            "unsafe_serialized_prompt",
        )
        try:
            serialized_prompt = prompt_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PortableStageError("invalid_serialized_prompt") from exc
        prepared = prepare_stage(
            intent,
            stage=arguments.stage,
            seat_id=arguments.seat,
            serialized_prompt=serialized_prompt,
            input_paths=_input_mapping(arguments.input),
            output_root=arguments.output_root,
            state_root=arguments.state_root,
        )
        completion = run_stage(
            prepared,
            timeout_seconds=arguments.timeout_seconds,
        )
        sys.stdout.buffer.write(_canonical_bytes(completion))
        return 0
    except (
        PortableStageError,
        provider_adapters.ProviderResolutionError,
    ) as exc:
        code = getattr(exc, "code", None)
        if code is None:
            code = str(exc)
        detail = getattr(exc, "detail", None)
        error_class = getattr(exc, "error_class", None)
        if error_class is None:
            error_class = (
                "contract"
                if isinstance(exc, provider_adapters.ProviderResolutionError)
                else "execution"
            )
        if detail:
            print(
                f"portable-stage: {code} [{error_class}] ({detail})",
                file=sys.stderr,
            )
        else:
            print(
                f"portable-stage: {code} [{error_class}]",
                file=sys.stderr,
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
