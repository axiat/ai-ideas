"""Deterministic capacity and budget planning for history audit v2."""

import copy
import datetime
import hashlib
import json
import math
import pathlib

try:
    from lib import history_contract_v2
except ImportError:
    import history_contract_v2


_POOL_FIELDS = ("comparator", "map", "detail", "reduce")
_CAPABILITY_BINDINGS = {
    "profile_hash": "capability_profile_hash",
    "model_identity": "model_identity",
    "reasoning_identity": "reasoning_identity",
    "cli_revision": "cli_revision",
    "serializer_revision": "capability_serializer_revision",
    "immutable_capacity_identity": "immutable_capacity_identity",
}
_RESOURCE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "provider_usage_units",
    "currency_micros",
)

RUNTIME_PLAN_SCHEMA = "history-audit-plan-v2"
_HOST_POLICY_ROOT = pathlib.Path(__file__).resolve().parents[1] / "history"
_TEST_RUNTIME_AUTHORITIES = {}
_TEST_RUNTIME_AUTHORITY_IDS = {}


def _reject_duplicate_json_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise AuditPlanError("invalid_host_policy", "duplicate_json_key")
        value[key] = item
    return value


def _load_host_policy(filename):
    """Load one repository-owned policy without accepting duplicate keys."""
    try:
        raw = (_HOST_POLICY_ROOT / filename).read_text(encoding="utf-8")
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(
                AuditPlanError("invalid_host_policy", filename)
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditPlanError("invalid_host_policy", filename) from exc


def _semantic_policy_canonical_bytes(policy):
    """Encode policy decimals exactly as the semantic evaluator identifies them."""
    def decimal_identity(value):
        if isinstance(value, float):
            if not math.isfinite(value):
                raise AuditPlanError("invalid_host_policy")
            return format(value, ".17g")
        if isinstance(value, list):
            return [decimal_identity(item) for item in value]
        if isinstance(value, dict):
            return {key: decimal_identity(item) for key, item in value.items()}
        return value

    return history_contract_v2.canonical_bytes(decimal_identity(policy))


def _host_runtime_authority():
    capacity_registry = _load_host_policy("capacity-profiles-v1.json")
    budget_policy = _load_host_policy("l2-budget-v1.json")
    risk_policy = _load_host_policy("risk-policy-v1.json")
    settlement_policy = _load_host_policy("settlement-policy-v1.json")
    semantic_policy = _load_host_policy("semantic-release-policy-v1.json")
    if (
        not isinstance(capacity_registry, dict)
        or set(capacity_registry) != {"schema_version", "profiles"}
        or capacity_registry.get("schema_version") != "capacity-profiles-v1"
        or not isinstance(capacity_registry.get("profiles"), dict)
        or not isinstance(risk_policy, dict)
        or set(risk_policy) != {"schema_version", "risk_policy_version", "rules"}
        or risk_policy.get("schema_version") != "history-risk-policy-v1"
        or not isinstance(risk_policy.get("risk_policy_version"), str)
        or not risk_policy["risk_policy_version"]
        or not isinstance(risk_policy.get("rules"), list)
        or not isinstance(settlement_policy, dict)
        or set(settlement_policy) != {
            "schema_version", "settlement_policy_id", "valid_result_policy",
            "retry_policy",
        }
        or settlement_policy.get("schema_version")
        != "history-settlement-policy-v1"
        or not isinstance(semantic_policy, dict)
        or semantic_policy.get("schema_version") != "semantic-release-policy-v1"
        or not isinstance(semantic_policy.get("semantic_policy_profile_id"), str)
        or not semantic_policy["semantic_policy_profile_id"]
    ):
        raise AuditPlanError("invalid_host_policy")
    risk_rule_ids = []
    for rule in risk_policy["rules"]:
        if (
            not isinstance(rule, dict)
            or set(rule) != {
                "rule_id", "fact", "equals", "required_route", "pre_l1"
            }
            or not isinstance(rule["rule_id"], str)
            or not rule["rule_id"]
            or rule["rule_id"] in risk_rule_ids
        ):
            raise AuditPlanError("invalid_host_policy", "risk-policy-v1.json")
        risk_rule_ids.append(rule["rule_id"])
    try:
        risk_policy_sha = _canonical_sha("history-risk-policy-v1", risk_policy)
        risk_rule_table_sha = _canonical_sha(
            "history-risk-rule-table-v1", risk_policy["rules"]
        )
        settlement_policy_sha = _canonical_sha(
            "history-settlement-policy-v1", settlement_policy
        )
        semantic_policy_bytes = _semantic_policy_canonical_bytes(semantic_policy)
        semantic_policy_sha = history_contract_v2.framed_sha256(
            "semantic-release-policy-v1", semantic_policy_bytes
        )
    except history_contract_v2.ContractV2Error as exc:
        raise AuditPlanError("invalid_host_policy") from exc
    if (
        not isinstance(budget_policy, dict)
        or budget_policy.get("schema_version") != "l2-budget-v1"
        or budget_policy.get("risk_policy_sha") != risk_policy_sha
        or budget_policy.get("settlement_policy_sha") != settlement_policy_sha
    ):
        raise AuditPlanError("invalid_host_policy", "l2-budget-v1.json")
    for profile in capacity_registry["profiles"].values():
        status = profile.get("status") if isinstance(profile, dict) else None
        if status in {"hard-complete", "hard-complete-test-only"}:
            _validate_authoritative_capacity_profile(
                profile, error_code="invalid_host_policy"
            )
        elif status != "unbudgetable":
            raise AuditPlanError(
                "invalid_host_policy", "capacity-profiles-v1.json"
            )
    return {
        "capacity_profiles": capacity_registry["profiles"],
        "budget_policy": budget_policy,
        "risk_policy": risk_policy,
        "risk_policy_sha": risk_policy_sha,
        "risk_rule_table_sha": risk_rule_table_sha,
        "receipt_risk_policy_version": "%s@%s" % (
            risk_policy["risk_policy_version"], risk_rule_table_sha
        ),
        "risk_rule_ids": risk_rule_ids,
        "settlement_policy": settlement_policy,
        "settlement_policy_sha": settlement_policy_sha,
        "semantic_policy_profile_id": semantic_policy[
            "semantic_policy_profile_id"
        ],
        "semantic_policy_canonical_bytes": semantic_policy_bytes,
        "semantic_policy_sha": semantic_policy_sha,
    }


def _canonical_sha(domain, value):
    return history_contract_v2.framed_sha256(
        domain, history_contract_v2.canonical_bytes(value)
    )


def _canonical_equal(left, right):
    try:
        return (
            history_contract_v2.canonical_bytes(left)
            == history_contract_v2.canonical_bytes(right)
        )
    except history_contract_v2.ContractV2Error:
        return False


def _host_authority_for_capacity(capacity_profile_id):
    authority = _host_runtime_authority()
    profile = authority["capacity_profiles"].get(capacity_profile_id)
    if profile is None:
        raise AuditPlanError("unauthorized_capacity_profile")
    scope = _capacity_authority_scope(profile)
    authority = copy.deepcopy(authority)
    authority["authority_scope"] = scope
    authority["private_test_authority"] = False
    authority["authority_id"] = _canonical_sha(
        "history-runtime-host-authority-v1",
        {
            "authority_scope": scope,
            "capacity_profile": profile,
            "budget_policy_sha": runtime_budget_policy_sha(
                authority["budget_policy"]
            ),
            "risk_policy_sha": authority["risk_policy_sha"],
            "receipt_risk_policy_version": authority[
                "receipt_risk_policy_version"
            ],
            "settlement_policy_sha": authority["settlement_policy_sha"],
            "semantic_policy_profile_id": authority[
                "semantic_policy_profile_id"
            ],
            "semantic_policy_sha": authority["semantic_policy_sha"],
        },
    )
    return authority


def _test_budget_limits_from_material(material):
    """Recover only the narrowing shape emitted by the private fake issuer."""
    host = _host_runtime_authority()
    try:
        intent = material["intent"]
        policy = material["budget_policy"]
        base_policy = host["budget_policy"]
        base_intent = base_policy["intents"][intent]
        candidate = policy["intents"][intent]["candidate"]
        round_policy = policy["intents"][intent]["round"]
    except (KeyError, TypeError) as exc:
        raise AuditPlanError("unauthorized_runtime_authority") from exc
    resource_fields = {
        "started_attempts", "input_tokens", "output_tokens",
        "provider_usage_units",
    }
    if (
        not isinstance(policy, dict)
        or set(policy) != {
            "schema_version", "settlement_policy_sha", "risk_policy_sha",
            "intents",
        }
        or policy["schema_version"] != base_policy["schema_version"]
        or policy["settlement_policy_sha"] != host["settlement_policy_sha"]
        or policy["risk_policy_sha"] != host["risk_policy_sha"]
        or not isinstance(policy["intents"], dict)
        or set(policy["intents"]) != {intent}
        or not isinstance(round_policy, dict)
        or set(round_policy) != resource_fields | {"candidates"}
        or not isinstance(candidate, dict)
        or set(candidate) != resource_fields
        or round_policy["candidates"] != base_intent["round"]["candidates"]
    ):
        raise AuditPlanError("unauthorized_runtime_authority")
    limits = {}
    for field in sorted(resource_fields):
        pair = (round_policy[field], candidate[field])
        base_pair = (
            base_intent["round"][field], base_intent["candidate"][field]
        )
        if pair == base_pair:
            continue
        if (
            type(pair[0]) is not int
            or type(pair[1]) is not int
            or pair[0] != pair[1]
            or pair[0] < 0
            or pair[0] > min(base_pair)
        ):
            raise AuditPlanError("unauthorized_runtime_authority")
        limits[field] = pair[0]
    return limits


def _reconstruct_test_runtime_authority(value):
    """Rebuild a deterministic fake-only authority in a fresh process."""
    if (
        not isinstance(value, dict)
        or value.get("authority_scope") != "test-only-shadow"
        or not _is_sha(value.get("authority_id"))
    ):
        raise AuditPlanError("unauthorized_runtime_authority")
    try:
        capacity = value["capacity_profile"]
        issued = _issue_test_runtime_authority(
            provider_pools_ordered=value["provider_pools_ordered"],
            provider_capabilities=value["provider_capabilities"],
            intent=value["intent"],
            budget_limits=_test_budget_limits_from_material(value),
            semantic_policy_profile_id=value["semantic_policy_profile_id"],
            matched_router_rule_ids=value["matched_router_rule_ids"],
            max_output_tokens=capacity["max_output_tokens"],
        )
    except (KeyError, TypeError, AuditPlanError) as exc:
        raise AuditPlanError("unauthorized_runtime_authority") from exc
    exact_fields = {
        "authority_id", "authority_scope", "capacity_profile_id",
        "base_capacity_profile_id", "capacity_profile", "budget_policy",
        "semantic_policy_profile_id", "risk_policy_version",
        "matched_router_rule_ids", "risk_policy_sha",
        "settlement_policy_sha",
    }
    if any(
        field not in value
        or not _canonical_equal(value[field], issued[field])
        for field in exact_fields
    ):
        raise AuditPlanError("unauthorized_runtime_authority")
    authority = _TEST_RUNTIME_AUTHORITIES.get(issued["authority_id"])
    if authority is None:
        raise AuditPlanError("unauthorized_runtime_authority")
    return copy.deepcopy(authority)


def _resolve_plan_authority(plan):
    requested = plan.get("authority_id")
    if requested in _TEST_RUNTIME_AUTHORITIES:
        return copy.deepcopy(_TEST_RUNTIME_AUTHORITIES[requested])
    if (
        requested is not None
        and plan.get("authority_scope") == "test-only-shadow"
        and isinstance(plan.get("capacity_profile_id"), str)
        and plan["capacity_profile_id"].startswith("fake-runtime-")
    ):
        return _reconstruct_test_runtime_authority(plan)
    authority = _host_authority_for_capacity(plan.get("capacity_profile_id"))
    if requested is not None and requested != authority["authority_id"]:
        raise AuditPlanError("unauthorized_runtime_authority")
    return authority


def _resolve_material_authority(material):
    authority_id = material["authority_id"]
    if authority_id in _TEST_RUNTIME_AUTHORITIES:
        return copy.deepcopy(_TEST_RUNTIME_AUTHORITIES[authority_id])
    if (
        material.get("authority_scope") == "test-only-shadow"
        and isinstance(material.get("capacity_profile_id"), str)
        and material["capacity_profile_id"].startswith("fake-runtime-")
    ):
        return _reconstruct_test_runtime_authority(material)
    authority = _host_authority_for_capacity(material["capacity_profile_id"])
    if authority_id != authority["authority_id"]:
        raise AuditPlanError("unauthorized_runtime_authority")
    return authority


def _issue_test_runtime_authority(
    *,
    provider_pools_ordered,
    provider_capabilities,
    intent,
    started_attempt_limit=None,
    budget_limits=None,
    semantic_policy_profile_id="semantic-test-v1",
    matched_router_rule_ids=(),
    max_output_tokens=64,
):
    """Issue one process-local, fake-only shadow authority for offline tests."""
    _validate_pools(provider_pools_ordered)
    host = _host_runtime_authority()
    providers = {
        provider
        for pool in provider_pools_ordered.values()
        for provider in pool
    }
    capability_fields = {
        "provider", "capability_profile_hash", "model_identity",
        "reasoning_identity", "model_default", "reasoning_default",
        "executable", "cli_revision",
    }
    if (
        not isinstance(provider_capabilities, dict)
        or set(provider_capabilities) != providers
        or not isinstance(semantic_policy_profile_id, str)
        or not semantic_policy_profile_id
        or not isinstance(matched_router_rule_ids, (list, tuple))
        or any(
            not isinstance(rule_id, str) or not rule_id
            for rule_id in matched_router_rule_ids
        )
        or len(set(matched_router_rule_ids)) != len(matched_router_rule_ids)
        or type(max_output_tokens) is not int
        or max_output_tokens <= 0
    ):
        raise AuditPlanError("invalid_test_authority")
    for provider in sorted(providers):
        capability = provider_capabilities[provider]
        if (
            not isinstance(capability, dict)
            or set(capability) != capability_fields
            or capability["provider"] != provider
            or not _is_sha(capability["capability_profile_hash"])
            or not isinstance(capability["model_identity"], str)
            or not capability["model_identity"].startswith("fake-")
            or not isinstance(capability["reasoning_identity"], str)
            or not capability["reasoning_identity"]
            or not isinstance(capability["cli_revision"], str)
            or not capability["cli_revision"].startswith("fake-")
            or type(capability["model_default"]) is not bool
            or type(capability["reasoning_default"]) is not bool
        ):
            raise AuditPlanError("invalid_test_authority", provider)
    base_capacity = host["capacity_profiles"].get("fake-safe-24k-v1")
    if (
        not isinstance(base_capacity, dict)
        or max_output_tokens > base_capacity.get("max_output_tokens", 0)
    ):
        raise AuditPlanError("invalid_test_authority")
    try:
        base_intent = copy.deepcopy(host["budget_policy"]["intents"][intent])
    except (KeyError, TypeError) as exc:
        raise AuditPlanError("invalid_test_authority", "intent") from exc
    narrowed = copy.deepcopy(budget_limits) if budget_limits is not None else {}
    if not isinstance(narrowed, dict):
        raise AuditPlanError("invalid_test_authority", "budget_limits")
    if started_attempt_limit is not None:
        if "started_attempts" in narrowed:
            raise AuditPlanError("invalid_test_authority", "budget_limits")
        narrowed["started_attempts"] = started_attempt_limit
    allowed_limits = {
        "started_attempts", "input_tokens", "output_tokens",
        "provider_usage_units",
    }
    if set(narrowed).difference(allowed_limits):
        raise AuditPlanError("invalid_test_authority", "budget_limits")
    for field, limit in narrowed.items():
        if (
            type(limit) is not int
            or limit < 0
            or any(limit > base_intent[scope][field] for scope in ("round", "candidate"))
        ):
            raise AuditPlanError("invalid_test_authority", field)
        for scope in ("round", "candidate"):
            base_intent[scope][field] = limit
    budget_policy = {
        "schema_version": host["budget_policy"]["schema_version"],
        "settlement_policy_sha": host["settlement_policy_sha"],
        "risk_policy_sha": host["risk_policy_sha"],
        "intents": {intent: base_intent},
    }
    identity_material = {
        "provider_pools_ordered": provider_pools_ordered,
        "provider_capabilities": provider_capabilities,
        "max_output_tokens": max_output_tokens,
    }
    profile_identity = _canonical_sha(
        "history-runtime-test-capacity-v1", identity_material
    )
    profile_id = f"fake-runtime-{profile_identity[:24]}-v1"
    bindings = {}
    for provider in sorted(providers):
        capability = provider_capabilities[provider]
        bindings[provider] = {
            "state": "hard-complete",
            "capability_profile_hash": capability["capability_profile_hash"],
            "model_identity": capability["model_identity"],
            "reasoning_identity": capability["reasoning_identity"],
            "model_default": capability["model_default"],
            "reasoning_default": capability["reasoning_default"],
            "executable": capability["executable"],
            "cli_revision": capability["cli_revision"],
            "capability_serializer_revision": "test-runtime-capability-v1",
            "request_serializer_revision": base_capacity[
                "serializer_revision"
            ],
            "immutable_capacity_identity": "test-only-" + profile_identity,
            "prompt_sha256": base_capacity["prompt"]["sha256"],
            "schema_sha256": base_capacity["schema"]["sha256"],
            "evidence_limit_tokens": base_capacity["evidence_limit_tokens"],
        }
    capacity_profile = {
        "profile_id": profile_id,
        "base_profile_id": base_capacity["base_profile_id"],
        "status": "hard-complete-test-only",
        "counter": copy.deepcopy(base_capacity["counter"]),
        "context_tokens": base_capacity["context_tokens"],
        "max_input_tokens": base_capacity["max_input_tokens"],
        "evidence_limit_tokens": base_capacity["evidence_limit_tokens"],
        "evidence_max_bytes": base_capacity["evidence_max_bytes"],
        "max_output_tokens": max_output_tokens,
        "item_cap": base_capacity["item_cap"],
        "utilization_ppm": base_capacity["utilization_ppm"],
        "prompt": copy.deepcopy(base_capacity["prompt"]),
        "schema": copy.deepcopy(base_capacity["schema"]),
        "serializer_revision": base_capacity["serializer_revision"],
        "usage_source": "fake-runtime-usage-v1",
        "expires_at": base_capacity["expires_at"],
        "provider_bindings": bindings,
    }
    authority_material = {
        "authority_scope": "test-only-shadow",
        "capacity_profile": capacity_profile,
        "budget_policy": budget_policy,
        "semantic_policy_profile_id": semantic_policy_profile_id,
        "semantic_policy_sha": host["semantic_policy_sha"],
        "matched_router_rule_ids": list(matched_router_rule_ids),
    }
    fingerprint = _canonical_sha(
        "history-runtime-test-authority-fingerprint-v1", authority_material
    )
    authority_id = _canonical_sha(
        "history-runtime-test-authority-v1", authority_material
    )
    prior_authority_id = _TEST_RUNTIME_AUTHORITY_IDS.get(fingerprint)
    if prior_authority_id not in (None, authority_id):
        raise AuditPlanError("invalid_test_authority")
    _TEST_RUNTIME_AUTHORITY_IDS[fingerprint] = authority_id
    authority = copy.deepcopy(host)
    authority.update(
        {
            "authority_id": authority_id,
            "authority_scope": "test-only-shadow",
            "capacity_profiles": {profile_id: capacity_profile},
            "budget_policy": budget_policy,
            "semantic_policy_profile_id": semantic_policy_profile_id,
            "matched_router_rule_ids": list(matched_router_rule_ids),
            "private_test_authority": True,
        }
    )
    _TEST_RUNTIME_AUTHORITIES[authority_id] = copy.deepcopy(authority)
    return {
        "authority_id": authority_id,
        "authority_scope": "test-only-shadow",
        "capacity_profile_id": profile_id,
        "base_capacity_profile_id": capacity_profile["base_profile_id"],
        "capacity_profile": copy.deepcopy(capacity_profile),
        "budget_policy": copy.deepcopy(budget_policy),
        "semantic_policy_profile_id": semantic_policy_profile_id,
        "risk_policy_version": host["receipt_risk_policy_version"],
        "matched_router_rule_ids": list(matched_router_rule_ids),
        "risk_policy_sha": host["risk_policy_sha"],
        "settlement_policy_sha": host["settlement_policy_sha"],
    }


def _is_sha(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def runtime_candidate_hash(candidate):
    required = {"candidate_id", "candidate_hash", "raw_artifact_sha", "source_order"}
    if not isinstance(candidate, dict) or set(candidate) != required:
        raise AuditPlanError("invalid_runtime_candidate")
    if (
        not isinstance(candidate["candidate_id"], str)
        or not candidate["candidate_id"]
        or not _is_sha(candidate["raw_artifact_sha"])
        or type(candidate["source_order"]) is not int
        or candidate["source_order"] < 0
    ):
        raise AuditPlanError("invalid_runtime_candidate")
    material = {
        "candidate_id": candidate["candidate_id"],
        "raw_artifact_sha": candidate["raw_artifact_sha"],
        "source_order": candidate["source_order"],
    }
    return _canonical_sha("history-runtime-candidate-v2", material)


def runtime_snapshot_records(records):
    if not isinstance(records, list) or not records:
        raise AuditPlanError("invalid_runtime_snapshot_records")
    normalized = []
    seen = set()
    for item in records:
        if not isinstance(item, dict) or set(item) != {
            "item_id", "artifact_sha", "content", "lineage_id"
        }:
            raise AuditPlanError("invalid_runtime_snapshot_records")
        if (
            not isinstance(item["item_id"], str)
            or not item["item_id"]
            or item["item_id"] in seen
            or not isinstance(item["content"], str)
            or hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
            != item["artifact_sha"]
            or not isinstance(item["lineage_id"], str)
            or not item["lineage_id"]
        ):
            raise AuditPlanError("invalid_runtime_snapshot_records")
        seen.add(item["item_id"])
        normalized.append(copy.deepcopy(item))
    normalized.sort(key=lambda item: item["item_id"])
    return normalized


def runtime_snapshot_records_sha(records):
    try:
        return _canonical_sha(
            "history-l2-snapshot-records-v2", runtime_snapshot_records(records)
        )
    except history_contract_v2.ContractV2Error as exc:
        raise AuditPlanError("invalid_runtime_snapshot_records") from exc


def runtime_shard_plan_sha(shards):
    if not isinstance(shards, list) or not shards:
        raise AuditPlanError("invalid_runtime_shards")
    normalized = []
    for shard in shards:
        if not isinstance(shard, dict) or set(shard) != {
            "shard_id", "item_ids", "request_sha256", "serialized_request",
            "final_request_tokens",
        }:
            raise AuditPlanError("invalid_runtime_shards")
        if (
            not isinstance(shard["shard_id"], str)
            or not shard["shard_id"]
            or not isinstance(shard["serialized_request"], str)
            or not _is_sha(shard["request_sha256"])
            or type(shard["final_request_tokens"]) is not int
            or shard["final_request_tokens"] <= 0
            or not isinstance(shard["item_ids"], list)
            or not shard["item_ids"]
            or shard["item_ids"] != sorted(shard["item_ids"])
            or len(set(shard["item_ids"])) != len(shard["item_ids"])
            or any(not isinstance(item_id, str) or not item_id for item_id in shard["item_ids"])
        ):
            raise AuditPlanError("invalid_runtime_shards")
        request_sha = hashlib.sha256(
            shard["serialized_request"].encode("utf-8")
        ).hexdigest()
        if request_sha != shard["request_sha256"]:
            raise AuditPlanError("invalid_runtime_shards")
        normalized.append(copy.deepcopy(shard))
    if len({shard["shard_id"] for shard in normalized}) != len(normalized):
        raise AuditPlanError("invalid_runtime_shards")
    normalized.sort(key=lambda shard: shard["shard_id"])
    return _canonical_sha("history-shard-plan-v2", normalized)


def _validate_runtime_serialized_requests(material, frozen_records=None):
    """Rebuild executable requests from frozen identity material."""
    strict = (
        material.get("test_execution_binding") is not None
        or (
            material.get("authority_scope") == "production"
            and material.get("capacity_profile", {}).get("status")
            == "hard-complete"
        )
    )
    if not strict:
        if frozen_records is not None:
            records = runtime_snapshot_records(frozen_records)
            if (
                runtime_snapshot_records_sha(records)
                != material["snapshot"]["records_sha"]
            ):
                raise AuditPlanError("invalid_runtime_snapshot")
        profile = material["capacity_profile"]
        for shard in material["shards"]:
            legacy_canonical = history_contract_v2.canonical_bytes(
                {
                    "candidate": material["candidate"],
                    "items": shard["item_ids"],
                    "output_schema": profile["schema"],
                    "prompt": profile["prompt"],
                }
            )[:-1]
            legacy_item_ids = json.dumps(
                {"item_ids": shard["item_ids"]}, sort_keys=True
            ).encode("utf-8")
            actual = shard["serialized_request"].encode("utf-8")
            if actual not in (legacy_canonical, legacy_item_ids):
                raise AuditPlanError("invalid_runtime_shards")
        return
    try:
        if frozen_records is None:
            snapshots = []
            for shard in material["shards"]:
                parsed = history_contract_v2.parse_json_bytes(
                    shard["serialized_request"].encode("utf-8")
                )
                if (
                    not isinstance(parsed, dict)
                    or set(parsed) != {
                        "schema_version", "serializer_revision", "snapshot",
                        "candidate", "prompt", "output_schema", "items",
                    }
                    or not isinstance(parsed["snapshot"], dict)
                    or "records" not in parsed["snapshot"]
                ):
                    raise AuditPlanError("invalid_runtime_shards")
                snapshots.append(parsed["snapshot"]["records"])
            if not snapshots or any(
                not _canonical_equal(records, snapshots[0])
                for records in snapshots[1:]
            ):
                raise AuditPlanError("invalid_runtime_shards")
            frozen_records = snapshots[0]
        records = runtime_snapshot_records(frozen_records)
        if runtime_snapshot_records_sha(records) != material["snapshot"]["records_sha"]:
            raise AuditPlanError("invalid_runtime_snapshot")
        record_by_id = {record["item_id"]: record for record in records}
        request_snapshot = copy.deepcopy(material["snapshot"])
        request_snapshot.pop("records_sha")
        request_snapshot["records"] = copy.deepcopy(records)
        profile = material["capacity_profile"]
        for shard in material["shards"]:
            try:
                selected = [
                    {
                        "item_id": item_id,
                        "wrapper": {
                            "item_id": item_id,
                            "record": copy.deepcopy(record_by_id[item_id]),
                        },
                    }
                    for item_id in shard["item_ids"]
                ]
            except KeyError as exc:
                raise AuditPlanError("invalid_runtime_shards") from exc
            expected = history_contract_v2.canonical_bytes(
                _request(request_snapshot, material["candidate"], profile, selected)
            )
            actual = shard["serialized_request"].encode("utf-8")
            if actual not in (expected, expected[:-1]):
                raise AuditPlanError("invalid_runtime_shards")
    except history_contract_v2.ContractV2Error as exc:
        raise AuditPlanError("invalid_runtime_shards") from exc


def runtime_budget_policy_sha(policy):
    if not isinstance(policy, dict):
        raise AuditPlanError("invalid_budget")
    return _canonical_sha("history-budget-policy-v1", policy)


def _capacity_authority_scope(profile):
    if not isinstance(profile, dict):
        raise AuditPlanError("unauthorized_capacity_profile")
    if profile.get("status") == "hard-complete-test-only":
        return "test-only-shadow"
    if profile.get("status") == "hard-complete":
        return "production"
    raise AuditPlanError("unbudgetable_provider")


def _validate_authoritative_capacity_profile(profile, *, error_code):
    required = {
        "profile_id", "base_profile_id", "status", "counter",
        "context_tokens", "max_input_tokens", "evidence_limit_tokens",
        "evidence_max_bytes", "max_output_tokens", "item_cap",
        "utilization_ppm", "prompt", "schema",
        "serializer_revision", "usage_source", "expires_at",
        "provider_bindings",
    }

    def invalid(detail="capacity-profiles-v1.json"):
        raise AuditPlanError(error_code, detail)

    if not isinstance(profile, dict) or set(profile) != required:
        invalid()
    if (
        profile["status"] not in {"hard-complete", "hard-complete-test-only"}
        or not isinstance(profile["profile_id"], str)
        or not profile["profile_id"]
        or not isinstance(profile["base_profile_id"], str)
        or not profile["base_profile_id"]
        or not isinstance(profile["serializer_revision"], str)
        or not profile["serializer_revision"]
        or not isinstance(profile["usage_source"], str)
        or not profile["usage_source"]
    ):
        invalid()
    counter = profile["counter"]
    if (
        not isinstance(counter, dict)
        or set(counter) != {"kind", "revision"}
        or counter not in (
            {"kind": "exact", "revision": "fake-utf8-byte-counter-v1"},
            {
                "kind": "validated_upper_bound",
                "revision": "fake-utf8-byte-bound-v1",
            },
        )
    ):
        invalid()
    for field in (
        "context_tokens", "max_input_tokens", "evidence_limit_tokens",
        "evidence_max_bytes", "max_output_tokens", "item_cap",
        "utilization_ppm",
    ):
        if type(profile[field]) is not int or profile[field] <= 0:
            invalid()
    if (
        profile["max_input_tokens"] + profile["max_output_tokens"]
        > profile["context_tokens"]
        or profile["evidence_limit_tokens"] > profile["max_input_tokens"]
        or profile["utilization_ppm"] > 1_000_000
    ):
        invalid()
    try:
        expires = datetime.datetime.fromisoformat(profile["expires_at"])
    except (TypeError, ValueError) as exc:
        raise AuditPlanError(error_code, "expires_at") from exc
    if (
        expires.tzinfo is None
        or expires <= datetime.datetime.now(datetime.timezone.utc)
    ):
        invalid("expires_at")
    for field in ("prompt", "schema"):
        artifact = profile[field]
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"id", "sha256", "text"}
            or not isinstance(artifact["id"], str)
            or not artifact["id"]
            or not isinstance(artifact["text"], str)
            or hashlib.sha256(artifact["text"].encode("utf-8")).hexdigest()
            != artifact["sha256"]
        ):
            invalid(field)
    bindings = profile["provider_bindings"]
    if not isinstance(bindings, dict) or not bindings:
        invalid("provider_bindings")
    hard_fields = {
        "state", "capability_profile_hash", "model_identity",
        "reasoning_identity", "model_default", "reasoning_default",
        "executable", "cli_revision", "capability_serializer_revision",
        "request_serializer_revision", "immutable_capacity_identity",
        "prompt_sha256", "schema_sha256", "evidence_limit_tokens",
    }
    price_fields = {
        "price_source", "input_currency_micros_per_token",
        "output_currency_micros_per_token",
    }
    for provider, binding in bindings.items():
        if not isinstance(provider, str) or not provider or not isinstance(binding, dict):
            invalid("provider_bindings")
        if binding.get("state") == "unbudgetable":
            if set(binding) != {"state"}:
                invalid(provider)
            continue
        if (
            binding.get("state") != "hard-complete"
            or not hard_fields.issubset(binding)
            or set(binding).difference(hard_fields | price_fields)
            or not _is_sha(binding["capability_profile_hash"])
            or any(
                not isinstance(binding[field], str) or not binding[field]
                for field in (
                    "model_identity", "reasoning_identity", "executable",
                    "cli_revision", "capability_serializer_revision",
                    "request_serializer_revision",
                    "immutable_capacity_identity",
                )
            )
            or type(binding["model_default"]) is not bool
            or type(binding["reasoning_default"]) is not bool
            or binding["request_serializer_revision"]
            != profile["serializer_revision"]
            or binding["prompt_sha256"] != profile["prompt"]["sha256"]
            or binding["schema_sha256"] != profile["schema"]["sha256"]
            or type(binding["evidence_limit_tokens"]) is not int
            or binding["evidence_limit_tokens"] <= 0
            or binding["evidence_limit_tokens"]
            > profile["evidence_limit_tokens"]
        ):
            invalid(provider)
        present_price = price_fields.intersection(binding)
        if present_price and present_price != price_fields:
            invalid(provider)
        if present_price:
            if (
                not isinstance(binding["price_source"], str)
                or not binding["price_source"]
                or any(
                    type(binding[field]) is not int or binding[field] < 0
                    for field in (
                        "input_currency_micros_per_token",
                        "output_currency_micros_per_token",
                    )
                )
            ):
                invalid(provider)
    return profile


def _validate_runtime_capacity_authority(material, authority):
    capacity_profile_id = material["capacity_profile_id"]
    registered = authority["capacity_profiles"].get(capacity_profile_id)
    if registered is None or not _canonical_equal(
        material["capacity_profile"], registered
    ):
        raise AuditPlanError("unauthorized_capacity_profile")
    if (
        material["capacity_profile_id"] != registered.get("profile_id")
        or material["base_capacity_profile_id"]
        != registered.get("base_profile_id")
        or material["authority_scope"] != authority["authority_scope"]
        or material["authority_scope"] != _capacity_authority_scope(registered)
    ):
        raise AuditPlanError("unauthorized_capacity_profile")
    _validate_authoritative_capacity_profile(
        registered,
        error_code=(
            "invalid_capacity_profile"
            if authority.get("private_test_authority")
            else "invalid_host_policy"
        ),
    )
    providers = {
        provider
        for pool in material["provider_pools_ordered"].values()
        for provider in pool
    }
    bindings = registered.get("provider_bindings")
    if not isinstance(bindings, dict):
        raise AuditPlanError("invalid_host_policy", "capacity-profiles-v1.json")
    capabilities = material["provider_capabilities"]
    for provider in sorted(providers):
        binding = bindings.get(provider)
        capability = capabilities.get(provider)
        if (
            not isinstance(binding, dict)
            or binding.get("state") != "hard-complete"
            or not isinstance(capability, dict)
            or binding.get("capability_profile_hash")
            != capability.get("capability_profile_hash")
            or binding.get("model_identity") != capability.get("model_identity")
            or binding.get("reasoning_identity")
            != capability.get("reasoning_identity")
            or binding.get("model_default") != capability.get("model_default")
            or binding.get("reasoning_default")
            != capability.get("reasoning_default")
            or binding.get("executable") != capability.get("executable")
            or binding.get("cli_revision") != capability.get("cli_revision")
            or binding.get("prompt_sha256") != registered["prompt"]["sha256"]
            or binding.get("schema_sha256") != registered["schema"]["sha256"]
            or binding.get("request_serializer_revision")
            != registered.get("serializer_revision")
        ):
            raise AuditPlanError("stale_capacity", provider)
    return registered


def _validate_runtime_policy_authority(material, authority):
    if (
        material["settlement_policy_sha"]
        != authority["settlement_policy_sha"]
        or material["budget_policy"].get("settlement_policy_sha")
        != authority["settlement_policy_sha"]
    ):
        raise AuditPlanError("unauthorized_settlement_policy")
    if (
        material["risk_policy_version"]
        != authority["receipt_risk_policy_version"]
        or material["risk_policy_sha"] != authority["risk_policy_sha"]
        or material["budget_policy"].get("risk_policy_sha")
        != authority["risk_policy_sha"]
    ):
        raise AuditPlanError("unauthorized_risk_policy")
    if not _canonical_equal(
        material["budget_policy"], authority["budget_policy"]
    ):
        raise AuditPlanError("unauthorized_budget_policy")
    if (
        runtime_budget_policy_sha(material["budget_policy"])
        != material["budget_policy_sha"]
    ):
        raise AuditPlanError("invalid_budget")
    if (
        material["semantic_policy_profile_id"]
        != authority["semantic_policy_profile_id"]
    ):
        raise AuditPlanError("unauthorized_semantic_policy")
    matched = material["matched_router_rule_ids"]
    if authority.get("private_test_authority"):
        if matched != authority.get("matched_router_rule_ids"):
            raise AuditPlanError("unauthorized_router_rules")
    else:
        rule_order = authority["risk_rule_ids"]
        if (
            not isinstance(matched, list)
            or any(not isinstance(rule_id, str) for rule_id in matched)
            or len(set(matched)) != len(matched)
            or any(rule_id not in rule_order for rule_id in matched)
            or matched != sorted(matched, key=rule_order.index)
        ):
            raise AuditPlanError("unauthorized_router_rules")


def _validate_runtime_shard_resources(material, intent_policy):
    capacity = material["capacity_profile"]
    counter = capacity["counter"]
    if counter not in (
        {"kind": "exact", "revision": "fake-utf8-byte-counter-v1"},
        {
            "kind": "validated_upper_bound",
            "revision": "fake-utf8-byte-bound-v1",
        },
    ):
        raise AuditPlanError("unbudgetable_provider")
    map_pool = material["provider_pools_ordered"]["map"]
    bindings = capacity["provider_bindings"]
    pool_input_limit = min(
        capacity["evidence_limit_tokens"],
        capacity["max_input_tokens"],
        *(bindings[provider]["evidence_limit_tokens"] for provider in map_pool),
    )
    total_input = 0
    for shard in material["shards"]:
        raw = shard["serialized_request"].encode("utf-8")
        measured_tokens = len(raw)
        if measured_tokens != shard["final_request_tokens"]:
            raise AuditPlanError("invalid_runtime_shards")
        if (
            len(raw) > capacity["evidence_max_bytes"]
            or measured_tokens > pool_input_limit
            or len(shard["item_ids"]) > capacity["item_cap"]
        ):
            raise AuditPlanError("runtime_capacity_exceeded")
        total_input += measured_tokens * len(map_pool)
    started_attempts = len(material["shards"]) * len(map_pool)
    total_output = started_attempts * capacity["max_output_tokens"]
    requested = {
        "started_attempts": started_attempts,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "provider_usage_units": total_input + total_output,
    }
    for field, amount in requested.items():
        if any(
            amount > intent_policy[scope][field]
            for scope in ("round", "candidate")
        ):
            raise AuditPlanError("runtime_budget_exceeded")
_RUNTIME_PLAN_FIELDS = frozenset(
    {
        "schema_version", "run_id", "batch_id", "candidate", "snapshot",
        "provider_pools_ordered", "provider_capability_profile_hashes",
        "provider_capabilities",
        "capacity_profile_id", "base_capacity_profile_id", "capacity_profile",
        "semantic_policy_profile_id", "risk_policy_version",
        "matched_router_rule_ids", "authority_id", "authority_scope", "budget_policy",
        "budget_policy_sha", "intent", "risk_policy_sha",
        "settlement_policy_sha", "shard_plan_sha", "shards",
    }
)
_TEST_EXECUTION_BINDING_FIELDS = frozenset(
    {"schema_version", "fake_executable_sha256", "protocol_revision"}
)


def build_runtime_plan_material(plan):
    if not isinstance(plan, dict):
        raise AuditPlanError("invalid_runtime_plan")
    if plan.get("schema_version") != RUNTIME_PLAN_SCHEMA:
        raise AuditPlanError("invalid_runtime_plan")
    try:
        authority = _resolve_plan_authority(plan)
        capacity_profile = copy.deepcopy(plan["capacity_profile"])
        capacity_profile_id = plan["capacity_profile_id"]
        registered_capacity = authority["capacity_profiles"].get(
            capacity_profile_id
        )
        if not _canonical_equal(registered_capacity, capacity_profile):
            raise AuditPlanError("unauthorized_capacity_profile")
        authority_scope = authority["authority_scope"]
        candidate = copy.deepcopy(plan["candidate"])
        snapshot = copy.deepcopy(plan["snapshot"])
        frozen_records = runtime_snapshot_records(plan["snapshot"]["records"])
        snapshot.pop("records")
        snapshot["records_sha"] = runtime_snapshot_records_sha(frozen_records)
        material = {
            "schema_version": RUNTIME_PLAN_SCHEMA,
            "run_id": plan["run_id"],
            "batch_id": plan["batch_id"],
            "candidate": candidate,
            "snapshot": snapshot,
            "provider_pools_ordered": copy.deepcopy(plan["provider_pools_ordered"]),
            "provider_capability_profile_hashes": copy.deepcopy(
                plan["provider_capability_profile_hashes"]
            ),
            "provider_capabilities": copy.deepcopy(plan["provider_capabilities"]),
            "capacity_profile_id": capacity_profile_id,
            "base_capacity_profile_id": plan["base_capacity_profile_id"],
            "capacity_profile": capacity_profile,
            "semantic_policy_profile_id": plan["semantic_policy_profile_id"],
            "risk_policy_version": plan["risk_policy_version"],
            "matched_router_rule_ids": copy.deepcopy(
                plan["matched_router_rule_ids"]
            ),
            "authority_id": authority["authority_id"],
            "authority_scope": authority_scope,
            "budget_policy": copy.deepcopy(plan["budget_policy"]),
            "budget_policy_sha": runtime_budget_policy_sha(plan["budget_policy"]),
            "intent": plan["intent"],
            "risk_policy_sha": plan["risk_policy_sha"],
            "settlement_policy_sha": plan["settlement_policy_sha"],
            "shard_plan_sha": runtime_shard_plan_sha(plan["shards"]),
            "shards": sorted(
                copy.deepcopy(plan["shards"]), key=lambda shard: shard["shard_id"]
            ),
        }
        if "test_execution_binding" in plan:
            material["test_execution_binding"] = copy.deepcopy(
                plan["test_execution_binding"]
            )
    except history_contract_v2.ContractV2Error as exc:
        raise AuditPlanError("invalid_runtime_snapshot") from exc
    except (KeyError, TypeError) as exc:
        raise AuditPlanError("invalid_runtime_plan") from exc
    validate_runtime_plan_material(material, _frozen_records=frozen_records)
    return material


def validate_runtime_plan_material(material, *, _frozen_records=None):
    fields = set(material) if isinstance(material, dict) else set()
    if not isinstance(material, dict) or fields not in (
        set(_RUNTIME_PLAN_FIELDS),
        set(_RUNTIME_PLAN_FIELDS) | {"test_execution_binding"},
    ):
        raise AuditPlanError("invalid_runtime_plan")
    if material["schema_version"] != RUNTIME_PLAN_SCHEMA:
        raise AuditPlanError("invalid_runtime_plan")
    if (
        not isinstance(material["run_id"], str)
        or not material["run_id"]
        or not isinstance(material["batch_id"], str)
        or not material["batch_id"]
    ):
        raise AuditPlanError("invalid_runtime_plan")
    if runtime_candidate_hash(material["candidate"]) != material["candidate"]["candidate_hash"]:
        raise AuditPlanError("invalid_runtime_candidate")
    snapshot = material["snapshot"]
    required_snapshot = {
        "snapshot_id", "snapshot_hash", "history_as_of_watermark",
        "current_batch_id_namespace", "current_batch_ids_hash",
        "current_batch_ids", "exclusion_policy_sha", "expected_asset_ids_hash",
        "expected_asset_ids", "records_sha",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != required_snapshot:
        raise AuditPlanError("invalid_runtime_snapshot")
    if not _is_sha(snapshot["records_sha"]):
        raise AuditPlanError("invalid_runtime_snapshot")
    if snapshot["current_batch_id_namespace"] != "history-v2-staging-v1":
        raise AuditPlanError("invalid_runtime_snapshot")
    try:
        if (
            history_contract_v2.ordered_set_sha256(
                "history-current-batch-ids-v2", snapshot["current_batch_ids"]
            )
            != snapshot["current_batch_ids_hash"]
            or history_contract_v2.ordered_set_sha256(
                "history-snapshot-assets-v2", snapshot["expected_asset_ids"]
            )
            != snapshot["expected_asset_ids_hash"]
        ):
            raise AuditPlanError("invalid_runtime_snapshot")
        snapshot_material = {
            "run_id": material["run_id"],
            "batch_id": material["batch_id"],
            "history_as_of_watermark": snapshot["history_as_of_watermark"],
            "current_batch_id_namespace": snapshot["current_batch_id_namespace"],
            "current_batch_ids_hash": snapshot["current_batch_ids_hash"],
            "exclusion_policy_sha": snapshot["exclusion_policy_sha"],
            "expected_asset_ids_hash": snapshot["expected_asset_ids_hash"],
        }
        expected_snapshot_hash = _canonical_sha(
            "history-snapshot-v2", snapshot_material
        )
        expected_snapshot_id = _canonical_sha(
            "history-snapshot-id-v2",
            {
                "run_id": material["run_id"],
                "batch_id": material["batch_id"],
                "snapshot_hash": expected_snapshot_hash,
            },
        )
    except history_contract_v2.ContractV2Error as exc:
        raise AuditPlanError("invalid_runtime_snapshot") from exc
    if (
        snapshot["snapshot_hash"] != expected_snapshot_hash
        or snapshot["snapshot_id"] != expected_snapshot_id
    ):
        raise AuditPlanError("invalid_runtime_snapshot")
    authority = _resolve_material_authority(material)
    execution_binding = material.get("test_execution_binding")
    if execution_binding is not None and (
        material["authority_scope"] != "test-only-shadow"
        or not isinstance(execution_binding, dict)
        or set(execution_binding) != _TEST_EXECUTION_BINDING_FIELDS
        or execution_binding.get("schema_version")
        != "history-test-execution-binding-v1"
        or not _is_sha(execution_binding.get("fake_executable_sha256"))
        or not isinstance(execution_binding.get("protocol_revision"), str)
        or not execution_binding["protocol_revision"]
    ):
        raise AuditPlanError("invalid_test_execution_binding")
    intent_policy = _intent_policy(material["budget_policy"], material["intent"])
    if not intent_policy:
        raise AuditPlanError("invalid_budget")
    _validate_runtime_policy_authority(material, authority)
    if runtime_shard_plan_sha(material["shards"]) != material["shard_plan_sha"]:
        raise AuditPlanError("invalid_runtime_shards")
    assigned_ids = [
        item_id for shard in material["shards"] for item_id in shard["item_ids"]
    ]
    if sorted(assigned_ids) != snapshot["expected_asset_ids"]:
        raise AuditPlanError("invalid_runtime_shards")
    _validate_pools(material["provider_pools_ordered"])
    capacity = material["capacity_profile"]
    if (
        not isinstance(capacity, dict)
        or (
            type(capacity.get("utilization_ppm")) is int
            and capacity["utilization_ppm"] > 1_000_000
        )
        or any(
            type(capacity.get(field)) is not int or capacity[field] <= 0
            for field in (
                "context_tokens", "max_input_tokens", "evidence_limit_tokens",
                "evidence_max_bytes", "max_output_tokens", "item_cap",
                "utilization_ppm",
            )
        )
    ):
        raise AuditPlanError("invalid_capacity_profile")
    profiles = material["provider_capability_profile_hashes"]
    capabilities = material["provider_capabilities"]
    providers = {
        provider
        for pool in material["provider_pools_ordered"].values()
        for provider in pool
    }
    if (
        not isinstance(profiles, dict)
        or set(profiles) != providers
        or not isinstance(capabilities, dict)
        or set(capabilities) != providers
    ):
        raise AuditPlanError("invalid_provider_capabilities")
    capability_fields = {
        "provider", "capability_profile_hash", "model_identity",
        "reasoning_identity", "model_default", "reasoning_default",
        "executable", "cli_revision",
    }
    for provider in sorted(providers):
        capability = capabilities[provider]
        if (
            not isinstance(capability, dict)
            or set(capability) != capability_fields
            or capability["provider"] != provider
            or not _is_sha(capability["capability_profile_hash"])
            or profiles[provider] != capability["capability_profile_hash"]
            or any(
                not isinstance(capability[field], str) or not capability[field]
                for field in (
                    "model_identity", "reasoning_identity", "executable",
                    "cli_revision",
                )
            )
            or type(capability["model_default"]) is not bool
            or type(capability["reasoning_default"]) is not bool
        ):
            raise AuditPlanError("invalid_provider_capabilities")
    _validate_runtime_capacity_authority(material, authority)
    _validate_runtime_shard_resources(material, intent_policy)
    _validate_runtime_serialized_requests(material, _frozen_records)
    return copy.deepcopy(material)


def runtime_plan_sha_from_material(material):
    normalized = validate_runtime_plan_material(material)
    return _canonical_sha("history-audit-plan-v2", normalized)


def runtime_plan_sha(plan):
    return runtime_plan_sha_from_material(build_runtime_plan_material(plan))


class AuditPlanError(RuntimeError):
    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail
        super().__init__(code)


def _sha(domain, value):
    return history_contract_v2.framed_sha256(
        domain, history_contract_v2.canonical_bytes(value)
    )


def _require_nonnegative(value, name):
    if type(value) is not int or value < 0:
        raise AuditPlanError("invalid_budget", name)
    return value


def _intent_policy(policy, intent):
    if (
        not isinstance(policy, dict)
        or set(policy) != {
            "schema_version", "settlement_policy_sha", "risk_policy_sha", "intents"
        }
        or policy.get("schema_version") != "l2-budget-v1"
    ):
        raise AuditPlanError("invalid_budget")
    for field in ("settlement_policy_sha", "risk_policy_sha"):
        value = policy[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise AuditPlanError("invalid_budget")
    try:
        value = policy["intents"][intent]
    except (KeyError, TypeError) as exc:
        raise AuditPlanError("unknown_intent") from exc
    if set(value) != {"round", "candidate"}:
        raise AuditPlanError("invalid_budget")
    for scope in ("round", "candidate"):
        limits = value[scope]
        if not isinstance(limits, dict):
            raise AuditPlanError("invalid_budget")
        required = {"started_attempts", "input_tokens", "output_tokens", "provider_usage_units"}
        if scope == "round":
            required.add("candidates")
        if not required.issubset(limits) or set(limits).difference(required | {"currency_micros"}):
            raise AuditPlanError("invalid_budget")
        for name, limit in limits.items():
            _require_nonnegative(limit, name)
    return value


def _event(events, material):
    if not isinstance(events, list):
        raise AuditPlanError("invalid_budget_ledger")
    value = copy.deepcopy(material)
    value["sequence"] = len(events)
    value["event_id"] = _sha("history-budget-event-v1", value)
    events.append(value)
    return value


def reserve_candidate_set(policy, intent, candidate_ids, events):
    """Atomically reserve a candidate set or append one rejection event."""
    intent_policy = _intent_policy(policy, intent)
    if (
        not isinstance(candidate_ids, (list, tuple))
        or not candidate_ids
        or any(not isinstance(item, str) or not item for item in candidate_ids)
        or len(set(candidate_ids)) != len(candidate_ids)
    ):
        raise AuditPlanError("invalid_candidate_set")
    prior = sum(
        len(event["candidate_ids"])
        for event in events
        if event.get("event_type") == "candidate_reserved" and event.get("intent") == intent
    )
    material = {
        "event_type": "candidate_reserved",
        "intent": intent,
        "candidate_ids": list(candidate_ids),
    }
    if prior + len(candidate_ids) > intent_policy["round"]["candidates"]:
        material["event_type"] = "reservation_rejected"
        _event(events, material)
        raise AuditPlanError("candidate_budget_exceeded")
    return _event(events, material)


def _settlements(events):
    result = {}
    for event in events:
        if event.get("event_type") == "attempt_settled":
            result[event["reservation_id"]] = event
    return result


def budget_totals(events, intent, candidate_id=None):
    """Return effective reserved or verified-settled usage without inventing price."""
    settlements = _settlements(events)
    totals = {
        "started_attempts": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "provider_usage_units": 0,
    }
    currency_known = False
    for reservation in events:
        if reservation.get("event_type") != "attempt_reserved":
            continue
        if reservation.get("intent") != intent:
            continue
        if candidate_id is not None and reservation.get("candidate_id") != candidate_id:
            continue
        totals["started_attempts"] += 1
        settlement = settlements.get(reservation["event_id"])
        actual = settlement.get("actual") if settlement and settlement["usage_verified"] else None
        for field in _RESOURCE_FIELDS:
            if actual is not None and field in actual:
                value = actual[field]
            else:
                value = reservation["reserved"].get(field)
            if value is None:
                continue
            if field == "currency_micros":
                currency_known = True
                totals.setdefault(field, 0)
            totals[field] += value
    if not currency_known:
        totals.pop("currency_micros", None)
    return totals


def reserve_attempt(
    policy,
    intent,
    candidate_id,
    logical_task_key,
    attempt_kind,
    estimate,
    events,
    *,
    provider=None,
):
    """Append one per-candidate reservation shared by every attempt kind."""
    intent_policy = _intent_policy(policy, intent)
    for value, name in (
        (candidate_id, "candidate_id"),
        (logical_task_key, "logical_task_key"),
        (attempt_kind, "attempt_kind"),
    ):
        if not isinstance(value, str) or not value:
            raise AuditPlanError("invalid_reservation", name)
    if not isinstance(estimate, dict) or not {
        "input_tokens", "output_tokens", "provider_usage_units"
    }.issubset(estimate) or set(estimate).difference(_RESOURCE_FIELDS):
        raise AuditPlanError("invalid_reservation")
    reserved = {}
    for field, value in estimate.items():
        reserved[field] = _require_nonnegative(value, field)
    if "currency_micros" in reserved and any(
        "currency_micros" not in intent_policy[scope]
        for scope in ("round", "candidate")
    ):
        raise AuditPlanError("unknown_currency_budget")
    if "currency_micros" not in reserved and any(
        "currency_micros" in intent_policy[scope]
        for scope in ("round", "candidate")
    ):
        raise AuditPlanError("unknown_currency_budget")
    candidate_totals = budget_totals(events, intent, candidate_id)
    round_totals = budget_totals(events, intent)
    requested = {"started_attempts": 1, **reserved}
    exceeded = False
    for field, amount in requested.items():
        if (
            candidate_totals.get(field, 0) + amount > intent_policy["candidate"][field]
            or round_totals.get(field, 0) + amount > intent_policy["round"][field]
        ):
            exceeded = True
    material = {
        "event_type": "attempt_reserved",
        "intent": intent,
        "candidate_id": candidate_id,
        "logical_task_key": logical_task_key,
        "attempt_kind": attempt_kind,
        "reserved": reserved,
    }
    if provider is not None:
        if not isinstance(provider, str) or not provider:
            raise AuditPlanError("invalid_reservation", "provider")
        material["provider"] = provider
    if exceeded:
        material["event_type"] = "attempt_reservation_rejected"
        _event(events, material)
        raise AuditPlanError("attempt_budget_exceeded")
    return _event(events, material)


def settle_attempt(reservation_id, actual_usage, usage_verified, events):
    """Append one settlement; an unverified settlement retains reservation usage."""
    reservations = [
        event for event in events
        if event.get("event_type") == "attempt_reserved" and event.get("event_id") == reservation_id
    ]
    if len(reservations) != 1 or reservation_id in _settlements(events):
        raise AuditPlanError("invalid_settlement")
    if type(usage_verified) is not bool:
        raise AuditPlanError("invalid_settlement")
    if usage_verified:
        required_usage = {
            "input_tokens", "output_tokens", "provider_usage_units"
        }
        if (
            not isinstance(actual_usage, dict)
            or not required_usage.issubset(actual_usage)
            or set(actual_usage).difference(_RESOURCE_FIELDS)
        ):
            raise AuditPlanError("invalid_settlement")
        actual = {
            field: _require_nonnegative(value, field)
            for field, value in actual_usage.items()
        }
    else:
        if actual_usage is not None:
            raise AuditPlanError("invalid_settlement")
        actual = None
    reservation = reservations[0]
    return _event(
        events,
        {
            "event_type": "attempt_settled",
            "intent": reservation["intent"],
            "candidate_id": reservation["candidate_id"],
            "reservation_id": reservation_id,
            "usage_verified": usage_verified,
            "actual": actual,
        },
    )


_RUNTIME_ATTEMPT_KINDS = frozenset(
    {"initial", "retry", "failover", "split", "detail", "reduce"}
)


def append_runtime_budget_event(events, *, work_id, attempt_kind, usage):
    """Append one idempotent realized-work event without replaying plan reserves."""
    if not isinstance(events, list):
        raise AuditPlanError("invalid_budget_ledger")
    if not isinstance(work_id, str) or not work_id:
        raise AuditPlanError("invalid_runtime_budget", "work_id")
    if attempt_kind not in _RUNTIME_ATTEMPT_KINDS:
        raise AuditPlanError("invalid_runtime_budget", "attempt_kind")
    if (
        not isinstance(usage, dict)
        or not {"input_tokens", "output_tokens", "provider_usage_units"}.issubset(usage)
        or set(usage).difference(_RESOURCE_FIELDS)
    ):
        raise AuditPlanError("invalid_runtime_budget", "usage")
    normalized = {
        field: _require_nonnegative(value, field) for field, value in usage.items()
    }
    material = {
        "event_type": "runtime_attempt_settled",
        "work_id": work_id,
        "attempt_kind": attempt_kind,
        "usage": normalized,
    }
    prior = [
        event for event in events
        if event.get("event_type") == "runtime_attempt_settled"
        and event.get("work_id") == work_id
    ]
    if prior:
        comparable = {
            key: prior[0][key]
            for key in ("event_type", "work_id", "attempt_kind", "usage")
        }
        if comparable != material:
            raise AuditPlanError("conflicting_runtime_budget_event")
        return prior[0]
    return _event(events, material)


def realized_budget_totals(events):
    """Count realized Task 5 work only; planner reservations stay separate."""
    totals = {
        "started_attempts": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "provider_usage_units": 0,
    }
    currency_known = False
    seen = set()
    for event in events:
        if event.get("event_type") != "runtime_attempt_settled":
            continue
        work_id = event.get("work_id")
        if work_id in seen:
            continue
        seen.add(work_id)
        totals["started_attempts"] += 1
        for field, value in event["usage"].items():
            if field == "currency_micros":
                currency_known = True
                totals.setdefault(field, 0)
            totals[field] += value
    if not currency_known:
        totals.pop("currency_micros", None)
    return totals


def _validate_pools(provider_pools):
    if not isinstance(provider_pools, dict) or set(provider_pools) != set(_POOL_FIELDS):
        raise AuditPlanError("invalid_provider_pools")
    for stage in _POOL_FIELDS:
        pool = provider_pools[stage]
        if (
            not isinstance(pool, list)
            or not pool
            or any(not isinstance(item, str) or not item for item in pool)
            or len(set(pool)) != len(pool)
        ):
            raise AuditPlanError("invalid_provider_pools")


def _frozen_capability_material(provider, capability):
    get = (
        capability.get
        if isinstance(capability, dict)
        else lambda field, default=None: getattr(capability, field, default)
    )
    return {
        "provider": provider,
        "capability_profile_hash": get("profile_hash"),
        "model_identity": get("model_identity"),
        "reasoning_identity": get("reasoning_identity"),
        "model_default": get("model_override") is None,
        "reasoning_default": get("reasoning_override") is None,
        "executable": get("executable", provider),
        "cli_revision": get("cli_revision"),
    }


def _validate_profile(profile, provider_pools, capabilities):
    required = {
        "profile_id", "base_profile_id", "status", "counter", "context_tokens",
        "evidence_limit_tokens", "max_output_tokens", "item_cap", "utilization_ppm",
        "prompt", "schema", "serializer_revision", "usage_source", "expires_at",
        "provider_bindings",
    }
    if not isinstance(profile, dict) or set(profile) != required:
        raise AuditPlanError("invalid_capacity_profile")
    if profile["status"] != "hard-complete-test-only":
        raise AuditPlanError("unbudgetable_provider")
    if profile["counter"] not in (
        {"kind": "exact", "revision": "fake-utf8-byte-counter-v1"},
        {"kind": "validated_upper_bound", "revision": "fake-utf8-byte-bound-v1"},
    ):
        raise AuditPlanError("unbudgetable_provider")
    for name in (
        "context_tokens", "evidence_limit_tokens", "max_output_tokens", "item_cap",
        "utilization_ppm",
    ):
        if type(profile[name]) is not int or profile[name] <= 0:
            raise AuditPlanError("invalid_capacity_profile")
    if profile["utilization_ppm"] > 1_000_000:
        raise AuditPlanError("invalid_capacity_profile")
    if profile["context_tokens"] != 24576 or profile["max_output_tokens"] != 3072:
        raise AuditPlanError("invalid_capacity_profile")
    if profile["base_profile_id"] == "safe-24k-v1" and (
        profile["evidence_limit_tokens"] != 12288 or profile["item_cap"] != 12
    ):
        raise AuditPlanError("invalid_capacity_profile")
    for field in ("prompt", "schema"):
        artifact = profile[field]
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"id", "sha256", "text"}
            or not isinstance(artifact["id"], str)
            or not artifact["id"]
            or not isinstance(artifact["text"], str)
            or hashlib.sha256(artifact["text"].encode("utf-8")).hexdigest()
            != artifact["sha256"]
        ):
            raise AuditPlanError("stale_capacity")
    try:
        expires = datetime.datetime.fromisoformat(profile["expires_at"])
    except (TypeError, ValueError) as exc:
        raise AuditPlanError("invalid_capacity_profile") from exc
    if expires.tzinfo is None or expires <= datetime.datetime.now(datetime.timezone.utc):
        raise AuditPlanError("stale_capacity")
    pool_bounds = {}
    for stage, pool in provider_pools.items():
        bounds = []
        for provider in pool:
            capability = capabilities.get(provider) if isinstance(capabilities, dict) else None
            binding = profile["provider_bindings"].get(provider)
            capability_get = (
                capability.get
                if isinstance(capability, dict)
                else lambda field, default=None: getattr(capability, field, default)
            )
            if (
                capability is None
                or not capability_get("hard_complete_eligible")
                or not isinstance(binding, dict)
                or binding.get("state") != "hard-complete"
            ):
                raise AuditPlanError("unbudgetable_provider", provider)
            for capability_field, binding_field in _CAPABILITY_BINDINGS.items():
                if binding.get(binding_field) != capability_get(capability_field):
                    raise AuditPlanError("stale_capacity", provider)
            if (
                binding["prompt_sha256"] != profile["prompt"].get("sha256")
                or binding["schema_sha256"] != profile["schema"].get("sha256")
                or binding["request_serializer_revision"]
                != profile["serializer_revision"]
            ):
                raise AuditPlanError("stale_capacity", provider)
            bound = binding.get("evidence_limit_tokens")
            if type(bound) is not int or bound <= 0:
                raise AuditPlanError("unbudgetable_provider", provider)
            bounds.append(min(bound, profile["evidence_limit_tokens"]))
        pool_bounds[stage] = min(bounds)
    return pool_bounds


def _record_items(records):
    if not isinstance(records, list) or not records:
        raise AuditPlanError("invalid_records")
    result = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise AuditPlanError("invalid_records")
        item_id = record.get("item_id")
        if not isinstance(item_id, str) or not item_id or item_id in seen:
            raise AuditPlanError("invalid_records")
        seen.add(item_id)
        wrapper = {"item_id": item_id, "record": copy.deepcopy(record)}
        weight = len(history_contract_v2.canonical_bytes(wrapper))
        result.append({"item_id": item_id, "wrapper": wrapper, "weight": weight})
    return sorted(result, key=lambda item: (-item["weight"], item["item_id"]))


def _request(snapshot, candidate, profile, items):
    return {
        "schema_version": "history-audit-map-request-v1",
        "serializer_revision": profile["serializer_revision"],
        "snapshot": copy.deepcopy(snapshot),
        "candidate": copy.deepcopy(candidate),
        "prompt": copy.deepcopy(profile["prompt"]),
        "output_schema": copy.deepcopy(profile["schema"]),
        "items": [copy.deepcopy(item["wrapper"]) for item in sorted(items, key=lambda value: value["item_id"])],
    }


def _serialized_request(snapshot, candidate, profile, items):
    raw = history_contract_v2.canonical_bytes(
        _request(snapshot, candidate, profile, items)
    )
    return raw, len(raw)


def _candidate_is_reserved(events, intent, candidate_id):
    return any(
        event.get("event_type") == "candidate_reserved"
        and event.get("intent") == intent
        and candidate_id in event.get("candidate_ids", ())
        for event in events
    )


def _worst_case_estimate(profile, provider, input_tokens):
    output_tokens = profile["max_output_tokens"]
    estimate = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "provider_usage_units": input_tokens + output_tokens,
    }
    binding = profile["provider_bindings"][provider]
    price_fields = {
        "price_source",
        "input_currency_micros_per_token",
        "output_currency_micros_per_token",
    }
    present = price_fields.intersection(binding)
    if present:
        if present != price_fields or not isinstance(binding["price_source"], str) or not binding["price_source"]:
            raise AuditPlanError("invalid_price_evidence", provider)
        for field in (
            "input_currency_micros_per_token",
            "output_currency_micros_per_token",
        ):
            if type(binding[field]) is not int or binding[field] < 0:
                raise AuditPlanError("invalid_price_evidence", provider)
        estimate["currency_micros"] = (
            input_tokens * binding["input_currency_micros_per_token"]
            + output_tokens * binding["output_currency_micros_per_token"]
        )
    return estimate


def _reserve_planned_work(
    *,
    budget_policy,
    intent,
    candidate_id,
    provider_pool,
    capacity_profile,
    shards,
    logical_tasks,
    budget_events,
):
    if not isinstance(budget_events, list):
        raise AuditPlanError("budget_state_required")
    scratch = copy.deepcopy(budget_events)
    initial_length = len(scratch)
    if not _candidate_is_reserved(scratch, intent, candidate_id):
        reserve_candidate_set(budget_policy, intent, [candidate_id], scratch)
    for shard, logical_task in zip(shards, logical_tasks):
        for pool_index, provider in enumerate(provider_pool):
            reserve_attempt(
                budget_policy,
                intent,
                candidate_id,
                logical_task,
                "initial" if pool_index == 0 else "failover",
                _worst_case_estimate(
                    capacity_profile, provider, shard["final_request_tokens"]
                ),
                scratch,
                provider=provider,
            )
    appended = scratch[initial_length:]
    budget_events.extend(appended)
    return copy.deepcopy(appended)


def build_plan(
    snapshot,
    candidate,
    provider_pools,
    capabilities,
    capacity_profile,
    budget_policy,
    intent,
    records,
    *,
    budget_events,
):
    """Preflight budgets and return deterministic token/item shards."""
    _validate_pools(provider_pools)
    pool_bounds = _validate_profile(capacity_profile, provider_pools, capabilities)
    if not isinstance(snapshot, dict) or not isinstance(candidate, dict):
        raise AuditPlanError("invalid_identity")
    for value, fields in (
        (snapshot, ("snapshot_id", "snapshot_hash", "history_as_of_watermark")),
        (candidate, ("candidate_id", "candidate_hash")),
    ):
        if any(field not in value for field in fields):
            raise AuditPlanError("invalid_identity")
    items = _record_items(records)
    b_pool = pool_bounds["map"]
    b_target = (capacity_profile["utilization_ppm"] * b_pool) // 1000000
    if b_target <= 0:
        raise AuditPlanError("invalid_capacity_profile")
    shards = []
    for item in items:
        selected = None
        ordered = sorted(
            shards,
            key=lambda shard: (shard["weight_sum"], len(shard["items"]), shard["shard_id"]),
        )
        for shard in ordered:
            if len(shard["items"]) >= capacity_profile["item_cap"]:
                continue
            _, count = _serialized_request(
                snapshot, candidate, capacity_profile, shard["items"] + [item]
            )
            if count <= b_target:
                selected = shard
                break
        if selected is None:
            raw, count = _serialized_request(snapshot, candidate, capacity_profile, [item])
            if count > b_target:
                raise AuditPlanError("single_item_overflow", item["item_id"])
            selected = {
                "shard_id": f"map-{len(shards):04d}",
                "items": [],
                "weight_sum": 0,
            }
            shards.append(selected)
        selected["items"].append(item)
        selected["weight_sum"] += item["weight"]
    planned_shards = []
    for shard in shards:
        raw, count = _serialized_request(snapshot, candidate, capacity_profile, shard["items"])
        if count > b_target or len(shard["items"]) > capacity_profile["item_cap"]:
            raise AuditPlanError("final_request_overflow")
        planned_shards.append(
            {
                "shard_id": shard["shard_id"],
                "item_ids": sorted(item["item_id"] for item in shard["items"]),
                "final_request_tokens": count,
                "request_sha256": hashlib.sha256(raw).hexdigest(),
                "serialized_request": raw.decode("utf-8"),
            }
        )
    shard_plan_sha = _sha(
        "history-shard-plan-v1",
        [
            {
                "shard_id": shard["shard_id"],
                "item_ids": shard["item_ids"],
                "final_request_tokens": shard["final_request_tokens"],
                "request_sha256": shard["request_sha256"],
            }
            for shard in planned_shards
        ],
    )
    capability_hashes = {
        provider: (
            capabilities[provider]["profile_hash"]
            if isinstance(capabilities[provider], dict)
            else capabilities[provider].profile_hash
        )
        for stage in _POOL_FIELDS
        for provider in provider_pools[stage]
    }
    frozen_capabilities = {
        provider: _frozen_capability_material(provider, capabilities[provider])
        for provider in sorted(capability_hashes)
    }
    plan_material = {
        "schema_version": "history-audit-plan-v1",
        "authority_scope": _capacity_authority_scope(capacity_profile),
        "snapshot": copy.deepcopy(snapshot),
        "candidate": copy.deepcopy(candidate),
        "provider_pools_ordered": copy.deepcopy(provider_pools),
        "provider_capability_profile_hashes": capability_hashes,
        "provider_capabilities": frozen_capabilities,
        "capacity_profile": copy.deepcopy(capacity_profile),
        "intent": intent,
        "settlement_policy_sha": budget_policy["settlement_policy_sha"],
        "risk_policy_sha": budget_policy["risk_policy_sha"],
        "budget_policy_sha": _sha("history-budget-policy-v1", budget_policy),
        "pool_bounds": pool_bounds,
        "b_pool": b_pool,
        "b_target": b_target,
        "shard_plan_sha": shard_plan_sha,
    }
    plan_sha = _sha("history-audit-plan-v1", plan_material)
    logical_tasks = [
        history_contract_v2.logical_task_key(
            plan_sha,
            "map",
            candidate["candidate_id"],
            shard["request_sha256"],
        )
        for shard in planned_shards
    ]
    reserved_events = _reserve_planned_work(
        budget_policy=budget_policy,
        intent=intent,
        candidate_id=candidate["candidate_id"],
        provider_pool=provider_pools["map"],
        capacity_profile=capacity_profile,
        shards=planned_shards,
        logical_tasks=logical_tasks,
        budget_events=budget_events,
    )
    return {
        "status": "planned",
        "authority_scope": plan_material["authority_scope"],
        "plan_sha": plan_sha,
        "capacity_profile_id": capacity_profile["profile_id"],
        "base_capacity_profile_id": capacity_profile["base_profile_id"],
        "provider_pools_ordered": copy.deepcopy(provider_pools),
        "provider_capability_profile_hashes": capability_hashes,
        "provider_capabilities": frozen_capabilities,
        "pool_bounds": pool_bounds,
        "b_pool": b_pool,
        "b_target": b_target,
        "shard_plan_sha": shard_plan_sha,
        "shards": planned_shards,
        "logical_task_keys": logical_tasks,
        "budget_events": reserved_events,
    }


def build_test_only_runtime_plan(
    *,
    run_id,
    batch_id,
    snapshot,
    candidate,
    provider_pools_ordered,
    provider_capabilities,
    intent,
    matched_router_rule_ids,
    semantic_policy_profile_id,
    test_execution_binding,
    max_output_tokens=64,
):
    """Build one deterministic v2 plan under the private fake-only authority.

    This is deliberately narrower than ``build_plan``: it accepts only the
    process-local test authority issued below, uses its exact byte counter, and
    returns a plan whose public material can be reconstructed in another
    process.  Registered provider commands are neither resolved nor launched.
    """
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(batch_id, str)
        or not batch_id
        or not isinstance(snapshot, dict)
        or not isinstance(candidate, dict)
    ):
        raise AuditPlanError("invalid_runtime_plan")
    _validate_pools(provider_pools_ordered)
    if runtime_candidate_hash(candidate) != candidate.get("candidate_hash"):
        raise AuditPlanError("invalid_runtime_candidate")
    try:
        records = runtime_snapshot_records(snapshot["records"])
    except KeyError as exc:
        raise AuditPlanError("invalid_runtime_snapshot") from exc

    authority = _issue_test_runtime_authority(
        provider_pools_ordered=provider_pools_ordered,
        provider_capabilities=provider_capabilities,
        intent=intent,
        semantic_policy_profile_id=semantic_policy_profile_id,
        matched_router_rule_ids=matched_router_rule_ids,
        max_output_tokens=max_output_tokens,
    )
    if (
        not isinstance(test_execution_binding, dict)
        or set(test_execution_binding) != _TEST_EXECUTION_BINDING_FIELDS
        or test_execution_binding.get("schema_version")
        != "history-test-execution-binding-v1"
        or not _is_sha(test_execution_binding.get("fake_executable_sha256"))
        or not isinstance(test_execution_binding.get("protocol_revision"), str)
        or not test_execution_binding["protocol_revision"]
    ):
        raise AuditPlanError("invalid_test_execution_binding")
    capacity = authority["capacity_profile"]
    map_bounds = [
        capacity["max_input_tokens"],
        capacity["evidence_limit_tokens"],
        capacity["evidence_max_bytes"],
    ]
    for provider in provider_pools_ordered["map"]:
        binding = capacity["provider_bindings"].get(provider)
        if (
            not isinstance(binding, dict)
            or binding.get("state") != "hard-complete"
            or type(binding.get("evidence_limit_tokens")) is not int
            or binding["evidence_limit_tokens"] <= 0
        ):
            raise AuditPlanError("invalid_test_authority", provider)
        map_bounds.append(binding["evidence_limit_tokens"])
    target = (min(map_bounds) * capacity["utilization_ppm"]) // 1000000
    if target <= 0:
        raise AuditPlanError("invalid_capacity_profile")

    def render_request(selected_items):
        raw, _ = _serialized_request(
            snapshot, candidate, capacity, selected_items
        )
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        return raw, len(raw)

    items = _record_items(records)
    mutable_shards = []
    for item in items:
        selected = None
        for shard in sorted(
            mutable_shards,
            key=lambda value: (
                value["weight_sum"], len(value["items"]), value["shard_id"]
            ),
        ):
            if len(shard["items"]) >= capacity["item_cap"]:
                continue
            _, size = render_request(shard["items"] + [item])
            if size <= target:
                selected = shard
                break
        if selected is None:
            _, size = render_request([item])
            if size > target:
                raise AuditPlanError("single_item_overflow", item["item_id"])
            selected = {
                "shard_id": f"map-{len(mutable_shards):04d}",
                "items": [],
                "weight_sum": 0,
            }
            mutable_shards.append(selected)
        selected["items"].append(item)
        selected["weight_sum"] += item["weight"]

    shards = []
    for shard in mutable_shards:
        raw, size = render_request(shard["items"])
        if size > target or len(shard["items"]) > capacity["item_cap"]:
            raise AuditPlanError("final_request_overflow")
        shards.append(
            {
                "shard_id": shard["shard_id"],
                "item_ids": sorted(item["item_id"] for item in shard["items"]),
                "request_sha256": hashlib.sha256(raw).hexdigest(),
                "serialized_request": raw.decode("utf-8"),
                "final_request_tokens": size,
            }
        )

    plan = {
        "schema_version": RUNTIME_PLAN_SCHEMA,
        "run_id": run_id,
        "batch_id": batch_id,
        "candidate": copy.deepcopy(candidate),
        "snapshot": {**copy.deepcopy(snapshot), "records": records},
        "provider_pools_ordered": copy.deepcopy(provider_pools_ordered),
        "provider_capability_profile_hashes": {
            provider: provider_capabilities[provider][
                "capability_profile_hash"
            ]
            for provider in sorted(provider_capabilities)
        },
        "provider_capabilities": copy.deepcopy(provider_capabilities),
        "intent": intent,
        "shards": shards,
        "test_execution_binding": copy.deepcopy(test_execution_binding),
        **authority,
    }
    plan["shard_plan_sha"] = runtime_shard_plan_sha(shards)
    plan["plan_sha"] = runtime_plan_sha(plan)
    plan["logical_task_keys"] = [
        history_contract_v2.logical_task_key(
            plan["plan_sha"],
            "map",
            candidate["candidate_id"],
            shard["request_sha256"],
        )
        for shard in shards
    ]
    build_runtime_plan_material(plan)
    return plan


def attempt_manifest(plan, shard_index, ordinal, capability):
    """Bind actual provider provenance without changing logical task identity."""
    if (
        type(shard_index) is not int
        or shard_index < 0
        or type(ordinal) is not int
    ):
        raise AuditPlanError("invalid_attempt")
    try:
        logical_key = plan["logical_task_keys"][shard_index]
        shard = plan["shards"][shard_index]
        capability_get = (
            capability.get
            if isinstance(capability, dict)
            else lambda field: getattr(capability, field)
        )
        provenance = {
            "provider": capability_get("provider"),
            "capability_profile_hash": capability_get("profile_hash"),
            "model_identity": capability_get("model_identity"),
            "reasoning_identity": capability_get("reasoning_identity"),
            "cli_revision": capability_get("cli_revision"),
            "request_sha256": shard["request_sha256"],
        }
    except (IndexError, KeyError, TypeError) as exc:
        raise AuditPlanError("invalid_attempt") from exc
    return {
        "logical_task_key": logical_key,
        "attempt_id": history_contract_v2.attempt_id(logical_key, ordinal, provenance),
        "provenance": provenance,
    }
