#!/usr/bin/env python3
"""Canonical identity and closed receipt validation for history runtime v2."""

import hashlib
import json
import struct
import unicodedata


CANONICAL_CODEC_VERSION = "history-canonical-json-v2"
MANIFEST_SCHEMA_VERSION = "history-audit-manifest-v2"
STAGING_CANDIDATE_NAMESPACE = "history-v2-staging-v1"
_SIGNED_INT_MIN = -(2**63)
_SIGNED_INT_MAX = 2**63 - 1
_SHA_FIELDS = frozenset(
    {
        "plan_hash",
        "candidate_hash",
        "snapshot_hash",
        "current_batch_ids_hash",
        "exclusion_policy_sha",
        "expected_asset_ids_hash",
        "observed_asset_ids_hash",
        "settlement_policy_sha",
        "shard_plan_sha",
        "minimum_receipt_sha",
    }
)
_SHA_LIST_FIELDS = frozenset(
    {
        "provider_capability_profile_hashes",
        "logical_task_hashes",
        "attempt_manifest_hashes",
        "raw_request_output_cas_hashes",
    }
)
_STRING_LIST_FIELDS = frozenset(
    {
        "missing_ids",
        "duplicate_ids",
        "extra_ids",
        "matched_router_rule_ids",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "manifest_schema_version",
        "canonical_codec_version",
        "run_id",
        "plan_hash",
        "candidate_hash",
        "snapshot_id",
        "snapshot_hash",
        "history_as_of_watermark",
        "current_batch_id_namespace",
        "current_batch_ids_hash",
        "exclusion_policy_sha",
        "expected_asset_ids_hash",
        "observed_asset_ids_hash",
        "missing_ids",
        "duplicate_ids",
        "extra_ids",
        "invalid_schema",
        "invalid_anchor",
        "truncated",
        "provider_pools_ordered",
        "provider_capability_profile_hashes",
        "capacity_profile_id",
        "semantic_policy_profile_id",
        "risk_policy_version",
        "matched_router_rule_ids",
        "settlement_policy_sha",
        "shard_plan_sha",
        "logical_task_hashes",
        "attempt_manifest_hashes",
        "raw_request_output_cas_hashes",
        "minimum_receipt_sha",
        "coverage_complete",
        "adjudication_complete",
        "semantic_policy_qualified",
        "no_match_basis",
        "final_status",
        "stage_reason_code",
        "evidence_anchors",
    }
)
_POOL_FIELDS = frozenset({"comparator", "map", "detail", "reduce"})
_FINAL_STATUSES = frozenset(
    {"overlap_found", "complete_no_match", "uncertain", "partial", "invalid"}
)
_NO_MATCH_BASES = frozenset({"l1_calibrated", "l2_exhaustive"})


class ContractV2Error(ValueError):
    pass


def _normalize_text(value):
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ContractV2Error("control characters are not canonical")
    return unicodedata.normalize("NFC", value)


def _normalize(value, *, require_nfc=False):
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not _SIGNED_INT_MIN <= value <= _SIGNED_INT_MAX:
            raise ContractV2Error("integer is outside signed 64-bit range")
        return value
    if isinstance(value, str):
        normalized = _normalize_text(value)
        if require_nfc and normalized != value:
            raise ContractV2Error("text is not NFC-normalized")
        return normalized
    if isinstance(value, list):
        return [_normalize(item, require_nfc=require_nfc) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractV2Error("object keys must be strings")
            normalized_key = _normalize_text(key)
            if require_nfc and normalized_key != key:
                raise ContractV2Error("object key is not NFC-normalized")
            if normalized_key in normalized:
                raise ContractV2Error("normalized object keys collide")
            normalized[normalized_key] = _normalize(
                item, require_nfc=require_nfc
            )
        return normalized
    raise ContractV2Error("value contains a non-canonical JSON type")


def canonical_bytes(value):
    """Return NFC-normalized, sorted, compact UTF-8 JSON plus one LF."""
    normalized = _normalize(value)
    return (
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _pairs_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ContractV2Error("duplicate JSON object key")
        value[key] = item
    return value


def parse_json_bytes(raw, *, allowed_fields=None):
    """Reject invalid UTF-8, duplicate keys, non-NFC text, and unknown fields."""
    if not isinstance(raw, bytes):
        raise ContractV2Error("JSON input must be bytes")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_float=lambda _: (_ for _ in ()).throw(
                ContractV2Error("floats are not canonical")
            ),
            parse_constant=lambda _: (_ for _ in ()).throw(
                ContractV2Error("non-finite numbers are not canonical")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractV2Error("invalid UTF-8 JSON") from exc
    normalized = _normalize(value, require_nfc=True)
    if allowed_fields is not None:
        if not isinstance(normalized, dict):
            raise ContractV2Error("closed fields require a JSON object")
        allowed = set(allowed_fields)
        unknown = set(normalized).difference(allowed)
        if unknown:
            raise ContractV2Error("unknown JSON fields: " + ",".join(sorted(unknown)))
    return normalized


def _text(value, name):
    if not isinstance(value, str) or not value:
        raise ContractV2Error(f"{name} must be a non-empty string")
    return _normalize_text(value)


def _sha(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractV2Error(f"{name} must be a lowercase SHA-256")
    return value


def framed_sha256(domain, *parts):
    """Hash a domain and uint64-be length-prefixed byte parts."""
    domain_bytes = _text(domain, "domain").encode("utf-8")
    digest = hashlib.sha256()
    for part in (domain_bytes,) + parts:
        if not isinstance(part, bytes):
            raise ContractV2Error("framed hash parts must be bytes")
        digest.update(struct.pack(">Q", len(part)))
        digest.update(part)
    return digest.hexdigest()


def ordered_set_sha256(domain, values):
    """Validate unique strings, sort them, and hash canonical set bytes."""
    if not isinstance(values, (list, tuple)):
        raise ContractV2Error("ordered set input must be a sequence")
    normalized = [_text(value, "set value") for value in values]
    if len(set(normalized)) != len(normalized):
        raise ContractV2Error("set values must be unique")
    return framed_sha256(domain, canonical_bytes(sorted(normalized)))


def plan_sha256(manifest):
    """Bind the complete ordered v2 plan manifest."""
    if not isinstance(manifest, dict):
        raise ContractV2Error("plan manifest must be an object")
    return framed_sha256("history-plan-v2", canonical_bytes(manifest))


def logical_task_key(plan_sha, stage, candidate_id, input_id):
    """Return a domain-separated stable logical task identity."""
    _sha(plan_sha, "plan_sha")
    return framed_sha256(
        "history-logical-task-v2",
        bytes.fromhex(plan_sha),
        _text(stage, "stage").encode("utf-8"),
        _text(candidate_id, "candidate_id").encode("utf-8"),
        _text(input_id, "input_id").encode("utf-8"),
    )


def attempt_id(task_key, ordinal, provenance):
    """Bind one attempt ordinal and actual execution provenance."""
    _sha(task_key, "task_key")
    if type(ordinal) is not int or not 0 <= ordinal <= 2**64 - 1:
        raise ContractV2Error("attempt ordinal must be uint64")
    if not isinstance(provenance, dict):
        raise ContractV2Error("attempt provenance must be an object")
    return framed_sha256(
        "history-attempt-v2",
        bytes.fromhex(task_key),
        ordinal.to_bytes(8, "big"),
        canonical_bytes(provenance),
    )


def _validate_unique_string_list(value, name, *, sha_values=False):
    if not isinstance(value, list):
        raise ContractV2Error(f"{name} must be an array")
    normalized = []
    for item in value:
        normalized.append(_sha(item, name) if sha_values else _text(item, name))
    if len(set(normalized)) != len(normalized):
        raise ContractV2Error(f"{name} values must be unique")
    return normalized


def validate_receipt(value):
    """Validate one closed history-audit-receipt-v2 object and aliases."""
    if not isinstance(value, dict):
        raise ContractV2Error("receipt must be an object")
    fields = set(value)
    if fields != _RECEIPT_FIELDS:
        missing = sorted(_RECEIPT_FIELDS.difference(fields))
        unknown = sorted(fields.difference(_RECEIPT_FIELDS))
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unknown:
            detail.append("unknown=" + ",".join(unknown))
        raise ContractV2Error("receipt fields are invalid: " + " ".join(detail))

    normalized = _normalize(value)
    if normalized["manifest_schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ContractV2Error("manifest schema version is invalid")
    if normalized["canonical_codec_version"] != CANONICAL_CODEC_VERSION:
        raise ContractV2Error("canonical codec version is invalid")
    for name in ("run_id", "snapshot_id", "capacity_profile_id",
                 "semantic_policy_profile_id", "risk_policy_version",
                 "stage_reason_code"):
        _text(normalized[name], name)
    for name in _SHA_FIELDS:
        _sha(normalized[name], name)
    watermark = normalized["history_as_of_watermark"]
    if type(watermark) is not int or watermark < 0:
        raise ContractV2Error("history watermark must be a non-negative integer")
    if normalized["current_batch_id_namespace"] != STAGING_CANDIDATE_NAMESPACE:
        raise ContractV2Error("staging candidate namespace is invalid")

    for name in _STRING_LIST_FIELDS:
        _validate_unique_string_list(normalized[name], name)
    for name in _SHA_LIST_FIELDS:
        _validate_unique_string_list(normalized[name], name, sha_values=True)

    pools = normalized["provider_pools_ordered"]
    if not isinstance(pools, dict) or set(pools) != _POOL_FIELDS:
        raise ContractV2Error("provider_pools_ordered is invalid")
    for role in sorted(_POOL_FIELDS):
        _validate_unique_string_list(pools[role], f"provider pool {role}")

    for name in (
        "invalid_schema",
        "invalid_anchor",
        "truncated",
        "coverage_complete",
        "adjudication_complete",
        "semantic_policy_qualified",
    ):
        if type(normalized[name]) is not bool:
            raise ContractV2Error(f"{name} must be boolean")
    if not isinstance(normalized["evidence_anchors"], list):
        raise ContractV2Error("evidence_anchors must be an array")

    status = normalized["final_status"]
    if status not in _FINAL_STATUSES:
        raise ContractV2Error("final_status is invalid")
    basis = normalized["no_match_basis"]
    if status == "complete_no_match":
        if basis not in _NO_MATCH_BASES:
            raise ContractV2Error("complete_no_match requires a closed basis")
    elif basis is not None:
        raise ContractV2Error("no_match_basis requires complete_no_match")
    return normalized
